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
import shutil
import stat
import tempfile
import threading
import time
from collections import OrderedDict

from .fusecompat import FuseOSError, Operations
from .providers.base import (AlreadyExists, AuthError, Entry, NotFound, join, split)

log = logging.getLogger(__name__)

BLOCK = 4 * 1024 * 1024          # размер блока для чтения по Range
MAX_BLOCKS = 16                  # блоков в памяти на один открытый файл
SMALL_FILE = 8 * 1024 * 1024     # файлы меньше — скачиваем целиком
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


class FileState:
    """Открытый файл (общий для всех дескрипторов одного пути)."""

    def __init__(self, fs: "CloudFS", path: str, entry: Entry | None, new: bool = False):
        self.fs = fs
        self.path = path
        self.entry = entry or Entry(split(path)[1], False, 0, time.time())
        self.size = 0 if new else self.entry.size
        self.mtime = time.time() if new else (self.entry.mtime or time.time())
        self.lock = threading.RLock()
        self.refs = 0
        self.tmp = None
        self.dirty = new
        self.is_new = new          # ещё нет на сервере
        self.deleted = False
        self.blocks: OrderedDict[int, bytes] = OrderedDict()
        self.ctx: dict = {}        # служебные данные провайдера (ссылки на скачивание)
        if new:
            self.tmp = self._new_tmp()

    @property
    def name(self) -> str:
        return split(self.path)[1]

    def view(self) -> Entry:
        return Entry(self.name, False, self.size, self.mtime, id=self.entry.id)

    def _new_tmp(self):
        return tempfile.TemporaryFile(dir=self.fs.cache_dir, prefix="cm_")

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
            tmp.close()
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
            self.dirty = True
            self.mtime = time.time()
            return len(data)

    def truncate(self, length: int):
        with self.lock:
            self._materialize(download=length > 0)
            self.tmp.truncate(length)
            self.size = length
            self.dirty = True
            self.mtime = time.time()

    def upload(self):
        with self.lock:
            if not self.dirty or self.deleted or self.tmp is None:
                return
            self.tmp.flush()
            self.tmp.seek(0)
            log.info("Загрузка %s (%d байт)", self.path, self.size)
            ent = self.fs.p.upload(self.path, None if self.is_new else self.entry,
                                   self.tmp, self.size)
            self.dirty = False
            self.is_new = False
            if ent is None:
                ent = Entry(self.name, False, self.size, time.time(), id=self.entry.id)
            ent.name = self.name
            self.entry = ent
            self.mtime = ent.mtime or time.time()
            self.ctx.clear()
            self.fs._cache_put(self.path, self.entry)

    def save_failed_copy(self) -> str | None:
        """Если загрузка не удалась — сохраняем копию, чтобы данные не пропали."""
        with self.lock:
            if self.tmp is None:
                return None
            d = os.path.join(self.fs.cache_dir, "..", "failed_uploads")
            os.makedirs(d, exist_ok=True)
            dst = os.path.join(d, time.strftime("%Y%m%d-%H%M%S_") + self.name)
            self.tmp.seek(0)
            with open(dst, "wb") as f:
                shutil.copyfileobj(self.tmp, f)
            return os.path.abspath(dst)

    def close(self):
        with self.lock:
            if self.tmp is not None:
                self.tmp.close()
                self.tmp = None
            self.blocks.clear()


class CloudFS(Operations):
    def __init__(self, provider, cache_dir: str, dir_ttl: float = 60.0):
        self.p = provider
        self.cache_dir = cache_dir
        os.makedirs(cache_dir, exist_ok=True)
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
            st.upload()
        except Exception as e:  # noqa: BLE001
            copy = None
            try:
                copy = st.save_failed_copy()
            except Exception:  # noqa: BLE001
                log.exception("Не удалось сохранить резервную копию")
            log.error("Не удалось загрузить %s: %s. Копия сохранена: %s", st.path, e, copy)
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
                if de is not None:
                    st.is_new = False      # цель уже есть на сервере — перезапишем
                self.files[dreal.lower()] = st
        self._cache_remove(sreal)
        self._drop_tree(dreal)
        if not (st is not None and st.is_new and de is None):
            moved = Entry(dname, se.is_dir, se.size, se.mtime or time.time(), id=se.id)
            self._cache_put(dreal, moved)
        return 0

    @op
    def statfs(self, path):
        t, (total, used) = self._quota
        if time.time() - t > 60:
            try:
                total, used = self.p.quota()
            except Exception as e:  # noqa: BLE001
                log.debug("quota: %s", e)
            self._quota = (time.time(), (total, used))
        bs = 4096
        if total <= 0:
            total = max(used, 0) + (1 << 40)
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
        with self.lock:
            states = list(self.files.values())
        for st in states:
            try:
                st.upload()
            except Exception:  # noqa: BLE001
                log.exception("Загрузка при отключении: %s", st.path)
            st.close()
