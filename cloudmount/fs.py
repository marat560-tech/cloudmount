"""Виртуальная файловая система поверх провайдера облака (FUSE / WinFsp).

Как это работает:
* Метаданные папок кэшируются (TTL), отсутствующие имена отвечаются без сети.
* Чтение: маленькие файлы скачиваются целиком во временный файл, большие читаются
  блоками по HTTP Range (видео можно смотреть сразу, без полной закачки).
* Запись: файл материализуется во временный файл, на сервер отправляется при закрытии
  (flush) — Проводник показывает прогресс копирования до конца загрузки.
* Windows не различает регистр — имена сопоставляются без учёта регистра.
"""
from __future__ import annotations

import errno
import functools
import logging
import os
import json
import stat
import threading
import time
import uuid
from collections import OrderedDict

from .fusecompat import FuseOSError, Operations
from .providers.base import (AlreadyExists, AuthError, Entry, NotFound, join, split)

log = logging.getLogger(__name__)

BLOCK = 4 * 1024 * 1024          # размер блока для чтения по Range
MAX_BLOCKS = 16                  # блоков в памяти на один открытый файл
SMALL_FILE = 8 * 1024 * 1024     # файлы меньше — скачиваем целиком
DEFER_MIN = 64 * 1024 * 1024     # файлы больше — загружаем только при закрытии
BAD_CHARS = set('\\/:*?"<>|') | {chr(i) for i in range(32)}
ROOT = Entry("", True)


def _to_fuse_error(e: Exception) -> FuseOSError:
    if isinstance(e, FuseOSError):
        return e
    if isinstance(e, NotFound):
        return FuseOSError(errno.ENOENT)
    if isinstance(e, AlreadyExists):
        return FuseOSError(errno.EEXIST)
    if isinstance(e, AuthError):
        return FuseOSError(errno.EACCES)
    return FuseOSError(errno.EIO)


def op(fn):
    @functools.wraps(fn)
    def wrapper(self, *args):
        try:
            return fn(self, *args)
        except FuseOSError:
            raise
        except NotFound as e:
            log.debug("%s%s: не найдено (%s)", fn.__name__, args[:1], e)
            raise FuseOSError(errno.ENOENT) from e
        except Exception as e:  # noqa: BLE001
            log.error("%s%s: %s", fn.__name__, args[:1], e, exc_info=not isinstance(e, AuthError))
            raise _to_fuse_error(e) from e
    return wrapper


class ProgressReader:
    """Обёртка файла для загрузки: считает отправленные байты (для статуса)."""

    def __init__(self, f, size: int, cb):
        self.f, self.size, self.cb, self.pos = f, size, cb, 0

    def __len__(self):
        return self.size

    def read(self, n=-1):
        b = self.f.read(n)
        self.pos += len(b)
        self.cb(self.pos)
        return b

    def seek(self, off, whence=0):
        r = self.f.seek(off, whence)
        self.pos = self.f.tell()
        return r

    def tell(self):
        return self.f.tell()

    def __iter__(self):
        while True:
            b = self.read(1 << 20)
            if not b:
                return
            yield b


class FileState:
    """Открытый файл (общий для всех дескрипторов одного пути).

    Локальная копия — обычный файл в кэше. Пока в ней есть незагруженные изменения,
    рядом лежит <id>.json с путём в облаке: если программа или Windows упадут,
    при следующем подключении изменения догрузятся автоматически.
    """

    def __init__(self, fs: "CloudFS", path: str, entry: Entry | None, new: bool = False):
        self.fs = fs
        self.path = path
        self.entry = entry or Entry(split(path)[1], False, 0, time.time())
        self.size = 0 if new else self.entry.size
        self.mtime = time.time() if new else (self.entry.mtime or time.time())
        self.lock = threading.RLock()
        self.refs = 0
        self.tmp = None
        self.tmp_id = None
        self.dirty = False
        self.is_new = new          # ещё нет на сервере
        self.deleted = False
        self.blocks: OrderedDict[int, bytes] = OrderedDict()
        self.ctx: dict = {}        # служебные данные провайдера (ссылки на скачивание)
        if new:
            self.tmp = self._new_tmp()
            self._set_dirty()

    @classmethod
    def from_pending(cls, fs: "CloudFS", tmp_id: str, path: str, entry: Entry | None):
        st = cls(fs, path, entry, new=entry is None)
        if st.tmp is not None:
            st._discard_tmp()
        st.tmp_id = tmp_id
        st.tmp = open(fs._tmp_path(tmp_id), "r+b")
        st.size = st.tmp.seek(0, os.SEEK_END)
        st.dirty = True
        st.mtime = time.time()
        return st

    @property
    def name(self) -> str:
        return split(self.path)[1]

    @property
    def deferred(self) -> bool:
        """Большие файлы (контейнеры VeraCrypt, образы) грузим только при закрытии."""
        return self.fs.defer_large and self.size >= DEFER_MIN

    def view(self) -> Entry:
        return Entry(self.name, False, self.size, self.mtime, id=self.entry.id)

    def _new_tmp(self):
        self.tmp_id = uuid.uuid4().hex
        return open(self.fs._tmp_path(self.tmp_id), "w+b")

    def _write_meta(self):
        meta = {"path": self.path, "time": time.time()}
        with open(self.fs._meta_path(self.tmp_id), "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False)

    def _set_dirty(self):
        if not self.dirty:
            self.dirty = True
            self._write_meta()

    def _discard_tmp(self):
        if self.tmp is not None:
            self.tmp.close()
            self.tmp = None
        if self.tmp_id:
            for p in (self.fs._tmp_path(self.tmp_id), self.fs._meta_path(self.tmp_id)):
                try:
                    os.remove(p)
                except OSError:
                    pass
            self.tmp_id = None

    def _materialize(self, download: bool = True):
        if self.tmp is not None:
            return
        tmp = self._new_tmp()
        try:
            if download and self.size > 0:
                self.fs.p.download(self.path, self.entry, tmp, self.ctx)
                got = tmp.seek(0, os.SEEK_END)
                if got != self.size:
                    log.warning("%s: ожидали %d байт, получили %d", self.path, self.size, got)
                    self.size = got
            elif not download:
                self.size = 0
        except BaseException:
            self.tmp = tmp
            self._discard_tmp()
            raise
        self.tmp = tmp
        self.blocks.clear()

    def _block(self, bi: int) -> bytes:
        b = self.blocks.get(bi)
        if b is not None:
            self.blocks.move_to_end(bi)
            return b
        start = bi * BLOCK
        b = self.fs.p.read_range(self.path, self.entry, start,
                                 min(BLOCK, self.size - start), self.ctx)
        self.blocks[bi] = b
        while len(self.blocks) > MAX_BLOCKS:
            self.blocks.popitem(last=False)
        return b

    def read(self, offset: int, size: int) -> bytes:
        with self.lock:
            if size <= 0 or offset >= self.size:
                return b""
            size = min(size, self.size - offset)
            if self.tmp is None and self.size <= SMALL_FILE:
                self._materialize()
            if self.tmp is not None:
                self.tmp.seek(offset)
                return self.tmp.read(size)
            out = bytearray()
            pos, end = offset, offset + size
            while pos < end:
                bi = pos // BLOCK
                blk = self._block(bi)
                bo = pos - bi * BLOCK
                chunk = blk[bo:bo + (end - pos)]
                if not chunk:
                    break
                out += chunk
                pos += len(chunk)
            return bytes(out)

    def write(self, offset: int, data: bytes) -> int:
        with self.lock:
            self._materialize()
            self.tmp.seek(offset)
            self.tmp.write(data)
            self.size = max(self.size, offset + len(data))
            self._set_dirty()
            self.mtime = time.time()
            return len(data)

    def truncate(self, length: int):
        with self.lock:
            self._materialize(download=length > 0)
            self.tmp.truncate(length)
            self.size = length
            self._set_dirty()
            self.mtime = time.time()

    def sync_local(self):
        """Сохранить изменения на локальный диск (без отправки в облако)."""
        with self.lock:
            if self.tmp is not None:
                self.tmp.flush()
                os.fsync(self.tmp.fileno())

    def upload(self):
        with self.lock:
            if not self.dirty or self.deleted or self.tmp is None:
                return
            self.tmp.flush()
            self.tmp.seek(0)
            log.info("Загрузка %s (%d байт)", self.path, self.size)
            self.fs._progress(self.name, 0, self.size)
            reader = ProgressReader(self.tmp, self.size,
                                    lambda n: self.fs._progress(self.name, n, self.size))
            try:
                ent = self.fs.p.upload(self.path, None if self.is_new else self.entry,
                                       reader, self.size)
            finally:
                self.fs._progress(None, 0, 0)
            self.dirty = False
            self.is_new = False
            try:
                os.remove(self.fs._meta_path(self.tmp_id))
            except OSError:
                pass
            if ent is None:
                ent = Entry(self.name, False, self.size, time.time(), id=self.entry.id)
            ent.name = self.name
            self.entry = ent
            self.mtime = ent.mtime or time.time()
            self.ctx.clear()
            self.fs._cache_put(self.path, self.entry)
            log.info("Загружено: %s", self.path)

    def close(self):
        """Закрыть локальную копию. Незагруженные изменения остаются для догрузки."""
        with self.lock:
            if self.dirty and not self.deleted:
                if self.tmp is not None:
                    self.tmp.close()
                    self.tmp = None
                log.warning("%s: изменения сохранены локально и будут загружены позже", self.path)
            else:
                self._discard_tmp()
            self.blocks.clear()


class CloudFS(Operations):
    def __init__(self, provider, cache_dir: str, dir_ttl: float = 60.0,
                 defer_large: bool = True, size_gb: float = 0):
        self.p = provider
        self.cache_dir = cache_dir
        os.makedirs(cache_dir, exist_ok=True)
        self.defer_large = defer_large
        self.size_override = int(size_gb * 2**30) if size_gb else 0
        self.upload_state = (None, 0, 0)          # (имя, отправлено, всего)
        self._clean_cache()
        self.ttl = dir_ttl
        self.lock = threading.RLock()
        self.dirs: dict[str, tuple[float, dict[str, Entry]]] = {}
        self.files: dict[str, FileState] = {}     # ключ — путь в нижнем регистре
        self.handles: dict[int, FileState] = {}
        self._fh = 0
        self._quota = (0.0, (0, 0))
        self.started = time.time()
        self.uid = os.getuid() if hasattr(os, "getuid") else 0
        self.gid = os.getgid() if hasattr(os, "getgid") else 0

    # ---------- локальные копии ----------
    def _tmp_path(self, tmp_id: str) -> str:
        return os.path.join(self.cache_dir, tmp_id + ".tmp")

    def _meta_path(self, tmp_id: str) -> str:
        return os.path.join(self.cache_dir, tmp_id + ".json")

    def _clean_cache(self):
        """Удаляет временные файлы без незагруженных изменений (остались после сбоя)."""
        for fn in os.listdir(self.cache_dir):
            if fn.endswith(".tmp") and not os.path.exists(self._meta_path(fn[:-4])):
                try:
                    os.remove(os.path.join(self.cache_dir, fn))
                except OSError:
                    pass

    def pending(self) -> list[tuple[str, str]]:
        """[(tmp_id, путь в облаке)] — изменения, не дошедшие до облака."""
        out = []
        for fn in sorted(os.listdir(self.cache_dir)):
            if not fn.endswith(".json"):
                continue
            tmp_id = fn[:-5]
            if not os.path.exists(self._tmp_path(tmp_id)):
                continue
            try:
                with open(self._meta_path(tmp_id), encoding="utf-8") as f:
                    out.append((tmp_id, json.load(f)["path"]))
            except (OSError, ValueError, KeyError):
                continue
        return out

    def recover_pending(self) -> int:
        """Догружает изменения, оставшиеся после сбоя. Возвращает число успешных."""
        ok = 0
        for tmp_id, path in self.pending():
            with self.lock:
                if path.lower() in self.files:
                    continue          # файл сейчас открыт — его допишет обычный путь
                if any(st.tmp_id == tmp_id for st in self.files.values()):
                    continue
            try:
                _, entry = self._resolve(path)
                if entry is not None and entry.is_dir:
                    log.error("Не догружено %s: на этом месте теперь папка", path)
                    continue
                st = FileState.from_pending(self, tmp_id, path, entry)
            except Exception as e:  # noqa: BLE001
                log.error("Не удалось подготовить догрузку %s: %s", path, e)
                continue
            with self.lock:
                st.refs = 1
                self.files[path.lower()] = st
            try:
                log.info("Догружаю изменения после сбоя: %s", path)
                st.upload()
                ok += 1
            except Exception as e:  # noqa: BLE001
                log.error("Догрузка %s не удалась: %s (повторю при следующем подключении)", path, e)
            finally:
                with self.lock:
                    st.refs -= 1
                    if st.refs <= 0 and self.files.get(path.lower()) is st:
                        del self.files[path.lower()]
                        st.close()
        return ok

    def _forget_pending(self, path: str, new_path: str | None = None):
        """При удалении/переименовании — чтобы догрузка не воскресила старый файл."""
        for tmp_id, p in self.pending():
            if p.lower() != path.lower():
                continue
            with self.lock:
                if any(st.tmp_id == tmp_id for st in self.files.values()):
                    continue
            if new_path is None:
                for fp in (self._meta_path(tmp_id), self._tmp_path(tmp_id)):
                    try:
                        os.remove(fp)
                    except OSError:
                        pass
            else:
                with open(self._meta_path(tmp_id), "w", encoding="utf-8") as f:
                    json.dump({"path": new_path, "time": time.time()}, f, ensure_ascii=False)

    def _progress(self, name, done, total):
        self.upload_state = (name, done, total)

    def status(self) -> dict:
        with self.lock:
            states = list(self.files.values())
        name, done, total = self.upload_state
        return {
            "uploading": name,
            "percent": int(done * 100 / total) if total else 0,
            "open": sorted({s.name for s in states if s.refs > 0}),
            "unsaved": sorted({s.name for s in states if s.dirty and not s.deleted}),
            "pending": len(self.pending()),
            "total": self.disk_size()[0] if (self.size_override or self._quota[1][0]) else 0,
            "used": self._quota[1][1],
        }

    def upload_all(self) -> int:
        """Отправить все изменения (перед отключением). Возвращает число ошибок."""
        with self.lock:
            states = list(self.files.values())
        failed = 0
        for st in states:
            try:
                st.upload()
            except Exception as e:  # noqa: BLE001
                failed += 1
                log.error("Не удалось загрузить %s: %s (сохранено для догрузки)", st.path, e)
        return failed

    # ---------- кэш ----------
    def _listing(self, path: str) -> dict[str, Entry]:
        k = path.lower()
        with self.lock:
            c = self.dirs.get(k)
            if c and time.time() - c[0] < self.ttl:
                return c[1]
        t = time.time()
        d = {}
        for e in self.p.list_dir(path):
            if not e.name or BAD_CHARS.intersection(e.name):
                continue
            d.setdefault(e.name.lower(), e)
        with self.lock:
            self.dirs[k] = (t, d)
        return d

    def _cache_put(self, path: str, e: Entry):
        parent, _ = split(path)
        with self.lock:
            c = self.dirs.get(parent.lower())
            if c:
                c[1][e.name.lower()] = e

    def _cache_remove(self, path: str):
        parent, name = split(path)
        with self.lock:
            c = self.dirs.get(parent.lower())
            if c:
                c[1].pop(name.lower(), None)
            self._drop_tree(path)

    def _drop_tree(self, path: str):
        k = path.lower().rstrip("/")
        with self.lock:
            for key in [x for x in self.dirs if x == k or x.startswith(k + "/")]:
                del self.dirs[key]

    def invalidate(self):
        with self.lock:
            self.dirs.clear()

    def _resolve(self, path: str) -> tuple[str, Entry | None]:
        """Возвращает (настоящий путь с регистром как на сервере, Entry или None)."""
        path = "/" + path.strip("/")
        if path == "/":
            return "/", ROOT
        parts = path.strip("/").split("/")
        cur, entry = "/", ROOT
        for i, part in enumerate(parts):
            if i == len(parts) - 1:
                with self.lock:
                    st = self.files.get(join(cur, part).lower())
                if st is not None and not st.deleted:
                    return st.path, st.view()
            if not entry.is_dir:
                return join(cur, "/".join(parts[i:])), None
            try:
                e = self._listing(cur).get(part.lower())
            except NotFound:
                e = None
            if e is None:
                return join(cur, "/".join(parts[i:])), None
            cur, entry = join(cur, e.name), e
        return cur, entry

    def _attrs(self, e: Entry) -> dict:
        t = e.mtime or self.started
        if e.is_dir:
            mode, nlink = stat.S_IFDIR | 0o777, 2
        else:
            mode, nlink = stat.S_IFREG | 0o666, 1
        return {"st_mode": mode, "st_nlink": nlink, "st_size": e.size,
                "st_mtime": t, "st_atime": t, "st_ctime": t,
                "st_uid": self.uid, "st_gid": self.gid}

    def _handle(self, fh) -> FileState:
        st = self.handles.get(fh)
        if st is None:
            raise FuseOSError(errno.EBADF)
        return st

    def _open_state(self, real: str, entry: Entry | None, new: bool) -> int:
        with self.lock:
            st = self.files.get(real.lower())
            if st is None or st.deleted:
                st = FileState(self, real, entry, new=new)
                self.files[real.lower()] = st
            st.refs += 1
            self._fh += 1
            self.handles[self._fh] = st
            return self._fh

    # ---------- операции FUSE ----------
    @op
    def getattr(self, path, fh=None, *_):
        if fh and fh in self.handles:
            return self._attrs(self.handles[fh].view())
        _, e = self._resolve(path)
        if e is None:
            raise FuseOSError(errno.ENOENT)
        return self._attrs(e)

    @op
    def readdir(self, path, fh, *_):
        real, e = self._resolve(path)
        if e is None:
            raise FuseOSError(errno.ENOENT)
        if not e.is_dir:
            raise FuseOSError(errno.ENOTDIR)
        lst = self._listing(real)
        with self.lock:
            entries = dict(lst)
            for st in self.files.values():
                if not st.deleted and split(st.path)[0].lower() == real.lower():
                    entries[st.name.lower()] = st.view()
        out = [".", ".."]
        out += [(x.name, self._attrs(x), 0) for x in entries.values()]
        return out

    @op
    def open(self, path, flags):
        real, e = self._resolve(path)
        if e is None:
            raise FuseOSError(errno.ENOENT)
        if e.is_dir:
            raise FuseOSError(errno.EISDIR)
        fh = self._open_state(real, e, new=False)
        if flags & getattr(os, "O_TRUNC", 0x200):
            self.handles[fh].truncate(0)
        return fh

    @op
    def create(self, path, mode, fi=None):
        parent, name = split(path)
        preal, pe = self._resolve(parent)
        if pe is None or not pe.is_dir:
            raise FuseOSError(errno.ENOENT)
        if BAD_CHARS.intersection(name):
            raise FuseOSError(errno.EINVAL)
        real, existing = self._resolve(join(preal, name))
        if existing is not None and existing.is_dir:
            raise FuseOSError(errno.EISDIR)
        if existing is not None:
            fh = self._open_state(real, existing, new=False)
            self.handles[fh].truncate(0)
        else:
            fh = self._open_state(real, None, new=True)
        return fh

    @op
    def read(self, path, size, offset, fh):
        return self._handle(fh).read(offset, size)

    @op
    def write(self, path, data, offset, fh):
        return self._handle(fh).write(offset, data)

    @op
    def truncate(self, path, length, fh=None):
        if fh and fh in self.handles:
            self.handles[fh].truncate(length)
            return 0
        real, e = self._resolve(path)
        if e is None:
            raise FuseOSError(errno.ENOENT)
        with self.lock:
            st = self.files.get(real.lower())
        if st is not None:
            st.truncate(length)
            return 0
        st = FileState(self, real, e)
        try:
            st.truncate(length)
            st.upload()
        finally:
            st.close()
        return 0

    @op
    def flush(self, path, fh):
        st = self.handles.get(fh)
        if st is not None:
            if st.deferred:
                st.sync_local()   # контейнер: в облако — только при закрытии
            else:
                st.upload()
        return 0

    @op
    def fsync(self, path, datasync, fh):
        return self.flush(path, fh)

    def release(self, path, fh):
        st = self.handles.pop(fh, None)
        if st is None:
            return 0
        try:
            with self.lock:
                last = st.refs <= 1
            if last or not st.deferred:
                st.upload()
        except Exception as e:  # noqa: BLE001
            log.error("Не удалось загрузить %s: %s. Изменения сохранены локально "
                      "и будут догружены при следующем подключении", st.path, e)
        finally:
            with self.lock:
                st.refs -= 1
                if st.refs <= 0:
                    if self.files.get(st.path.lower()) is st:
                        del self.files[st.path.lower()]
                    st.close()
        return 0

    @op
    def unlink(self, path):
        real, e = self._resolve(path)
        if e is None:
            raise FuseOSError(errno.ENOENT)
        if e.is_dir:
            raise FuseOSError(errno.EISDIR)
        with self.lock:
            st = self.files.get(real.lower())
        if st is not None and st.is_new:
            with st.lock:
                st.deleted, st.dirty = True, False
        else:
            self.p.delete(real, e)
            if st is not None:
                with st.lock:
                    st.deleted, st.dirty = True, False
        self._cache_remove(real)
        self._forget_pending(real)
        return 0

    @op
    def mkdir(self, path, mode):
        parent, name = split(path)
        preal, pe = self._resolve(parent)
        if pe is None or not pe.is_dir:
            raise FuseOSError(errno.ENOENT)
        if BAD_CHARS.intersection(name):
            raise FuseOSError(errno.EINVAL)
        real, existing = self._resolve(join(preal, name))
        if existing is not None:
            raise FuseOSError(errno.EEXIST)
        self.p.mkdir(real)
        self._cache_put(real, Entry(name, True, 0, time.time()))
        with self.lock:
            self.dirs[real.lower()] = (time.time(), {})
        return 0

    @op
    def rmdir(self, path):
        real, e = self._resolve(path)
        if e is None:
            raise FuseOSError(errno.ENOENT)
        if not e.is_dir:
            raise FuseOSError(errno.ENOTDIR)
        if real == "/":
            raise FuseOSError(errno.EBUSY)
        self._drop_tree(real)
        if self._listing(real):
            raise FuseOSError(errno.ENOTEMPTY)
        self.p.delete(real, e)
        self._cache_remove(real)
        return 0

    @op
    def rename(self, old, new, *_):
        sreal, se = self._resolve(old)
        if se is None:
            raise FuseOSError(errno.ENOENT)
        if sreal == "/":
            raise FuseOSError(errno.EBUSY)
        dparent, dname = split(new)
        dpreal, dpe = self._resolve(dparent)
        if dpe is None or not dpe.is_dir:
            raise FuseOSError(errno.ENOENT)
        if BAD_CHARS.intersection(dname):
            raise FuseOSError(errno.EINVAL)
        dreal = join(dpreal, dname)
        _, de = self._resolve(dreal)
        if dreal.lower() == sreal.lower():
            de = None                      # смена только регистра
        if de is not None:
            if de.is_dir != se.is_dir:
                raise FuseOSError(errno.EISDIR if de.is_dir else errno.ENOTDIR)
            if de.is_dir and self._listing(dreal):
                raise FuseOSError(errno.ENOTEMPTY)
        with self.lock:
            st = self.files.get(sreal.lower())
        if not (st is not None and st.is_new):
            self.p.move(sreal, dreal, se, de)
        if st is not None:
            with self.lock:
                self.files.pop(sreal.lower(), None)
                st.path = dreal
                if st.dirty and st.tmp_id:
                    st._write_meta()
                if de is not None:
                    st.is_new = False      # цель уже есть на сервере — перезапишем
                self.files[dreal.lower()] = st
        self._cache_remove(sreal)
        self._drop_tree(dreal)
        if not se.is_dir:
            self._forget_pending(dreal)
            self._forget_pending(sreal, dreal)
        if not (st is not None and st.is_new and de is None):
            moved = Entry(dname, se.is_dir, se.size, se.mtime or time.time(), id=se.id)
            self._cache_put(dreal, moved)
        return 0

    def _refresh_quota(self):
        try:
            total, used = self.p.quota()
            self._quota = (time.time(), (int(total or 0), int(used or 0)))
        except Exception as e:  # noqa: BLE001
            log.info("Размер облака не получен: %s", e)
            self._quota = (time.time(), self._quota[1])
        finally:
            self._quota_busy = False

    def prefetch_quota(self, timeout: float = 8.0):
        """Узнать размер до подключения диска, чтобы Проводник сразу показал полосу.
        Медленный сервер ждём не дольше timeout — дальше обновится в фоне."""
        self._quota_busy = True
        th = threading.Thread(target=self._refresh_quota, daemon=True, name="quota")
        th.start()
        th.join(timeout)

    def quota_cached(self) -> tuple[int, int]:
        """Размер из кэша; обновление — в фоне, Проводник никогда долго не ждёт сервер."""
        t, q = self._quota
        busy = getattr(self, "_quota_busy", False)
        if time.time() - t > 120 and not busy:
            self._quota_busy = busy = True
            threading.Thread(target=self._refresh_quota, daemon=True, name="quota").start()
        if t == 0 and busy:
            # ещё ни разу не получали — подождём совсем немного
            for _ in range(20):
                time.sleep(0.1)
                if self._quota[0]:
                    return self._quota[1]
        return self._quota[1]

    def disk_size(self) -> tuple[int, int]:
        total, used = self.quota_cached()
        if self.size_override:
            total = self.size_override          # размер задан вручную в настройках
        elif total <= 0:
            total = max(used, 0) + (1 << 40)    # облако не сообщает — условный 1 ТБ
        return total, used

    @op
    def statfs(self, path):
        total, used = self.disk_size()
        bs = 4096
        free = max(total - used, 0)
        return {"f_bsize": bs, "f_frsize": bs, "f_blocks": total // bs,
                "f_bfree": free // bs, "f_bavail": free // bs,
                "f_files": 1 << 20, "f_ffree": 1 << 20, "f_favail": 1 << 20,
                "f_namemax": 255}

    def chmod(self, path, mode, *_):
        return 0

    def chown(self, path, uid, gid, *_):
        return 0

    def utimens(self, path, times=None, *_):
        return 0

    def destroy(self, path):
        self.upload_all()
        with self.lock:
            states = list(self.files.values())
        for st in states:
            st.close()
