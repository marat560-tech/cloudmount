"""SFTP: свой сервер, VPS или NAS (Synology, QNAP…)."""
from __future__ import annotations

import base64
import errno
import posixpath
import stat
import threading

from .base import AlreadyExists, AuthError, Entry, NotFound, Provider, ProviderError


def _paramiko():
    try:
        import paramiko
    except ImportError as e:  # pragma: no cover
        raise ProviderError("Для SFTP нужна библиотека paramiko") from e
    return paramiko


def _map(e: Exception, path: str) -> Exception:
    if isinstance(e, FileNotFoundError) or getattr(e, "errno", None) == errno.ENOENT:
        return NotFound(path)
    if isinstance(e, PermissionError) or getattr(e, "errno", None) == errno.EACCES:
        return AuthError(f"Нет прав: {path}")
    if isinstance(e, ProviderError):
        return e
    return ProviderError(f"SFTP: {e}")


class SFTPProvider(Provider):
    kind = "sftp"
    title = "SFTP"
    note = "свой сервер или NAS"
    tint = "#555F6D"
    icon = "server"
    fields = [
        ("host", "Сервер", False, ""),
        ("port", "Порт", False, "22"),
        ("username", "Пользователь", False, ""),
        ("password", "Пароль (или пароль ключа)", True, ""),
        ("key_path", "Файл ключа (необязательно)", False, ""),
        ("root", "Папка на сервере (необязательно)", False, ""),
    ]
    optional = {"password", "key_path", "root"}
    hint = ("Подключение по SSH. Укажите пароль или путь к закрытому ключу\n"
            "(например C:\\Users\\Вы\\.ssh\\id_ed25519). При первом подключении\n"
            "запоминается ключ сервера; если он изменится, CloudMount откажется подключаться.")

    def __init__(self, data, save=None):
        super().__init__(data, save)
        self.root = "/" + (data.get("root") or "").strip("/")
        self._lock = threading.Lock()
        self._transport = None
        self._local = threading.local()

    # ---------- соединение ----------
    def _connect(self):
        pk = _paramiko()
        host, port = self.data.get("host", ""), int(self.data.get("port") or 22)
        try:
            t = pk.Transport((host, port))
            t.start_client(timeout=20)
        except Exception as e:  # noqa: BLE001
            raise ProviderError(f"Не удалось подключиться к {host}:{port}: {e}") from e
        key = t.get_remote_server_key()
        fp = f"{key.get_name()} {base64.b64encode(key.asbytes()).decode()}"
        known = self.data.get("host_key")
        if known and known != fp:
            t.close()
            raise AuthError("Ключ сервера изменился! Если вы переустанавливали сервер — "
                            "удалите и заново добавьте диск.")
        pkey = None
        if self.data.get("key_path"):
            try:
                pkey = pk.PKey.from_path(self.data["key_path"],
                                         passphrase=self.data.get("password") or None)
            except Exception as e:  # noqa: BLE001
                t.close()
                raise AuthError(f"Не удалось прочитать ключ: {e}") from e
        try:
            if pkey is not None:
                t.auth_publickey(self.data.get("username", ""), pkey)
            else:
                t.auth_password(self.data.get("username", ""), self.data.get("password", ""))
        except Exception as e:  # noqa: BLE001
            t.close()
            raise AuthError(f"Вход не выполнен: {e}") from e
        if not known:
            self.data["host_key"] = fp
            self.save()
        return t

    def _sftp(self):
        c = getattr(self._local, "c", None)
        if c is not None and self._transport is not None and self._transport.is_active():
            return c
        with self._lock:
            if self._transport is None or not self._transport.is_active():
                self._transport = self._connect()
            c = _paramiko().SFTPClient.from_transport(self._transport)
        self._local.c = c
        return c

    def _run(self, path, fn):
        for attempt in range(2):
            try:
                return fn(self._sftp())
            except (OSError, EOFError) as e:
                mapped = _map(e, path)
                if isinstance(mapped, (NotFound, AuthError)) or attempt:
                    raise mapped from e
                self._local.c = None            # соединение оборвалось — переподключимся
                with self._lock:
                    if self._transport is not None:
                        self._transport.close()
                    self._transport = None

    def _p(self, path: str) -> str:
        return posixpath.join(self.root, path.strip("/")) if path.strip("/") else self.root

    # ---------- операции ----------
    def list_dir(self, path):
        attrs = self._run(path, lambda s: s.listdir_attr(self._p(path)))
        return [Entry(a.filename, stat.S_ISDIR(a.st_mode or 0),
                      0 if stat.S_ISDIR(a.st_mode or 0) else int(a.st_size or 0),
                      float(a.st_mtime or 0)) for a in attrs if a.filename not in (".", "..")]

    def read_range(self, path, entry, offset, length, ctx):
        def go(s):
            with s.open(self._p(path), "rb") as f:
                f.seek(offset)
                return f.read(length)
        return self._run(path, go)

    def download(self, path, entry, fileobj, ctx):
        self._run(path, lambda s: s.getfo(self._p(path), fileobj))

    def upload(self, path, entry, fileobj, size):
        full = self._p(path)
        tmp = full + ".cloudmount-tmp"

        def go(s):
            fileobj.seek(0)
            s.putfo(fileobj, tmp, file_size=size, confirm=True)
            try:
                s.posix_rename(tmp, full)
            except OSError:
                try:
                    s.remove(full)
                except OSError:
                    pass
                s.rename(tmp, full)
        self._run(path, go)
        return None

    def mkdir(self, path):
        def go(s):
            try:
                s.stat(self._p(path))
            except FileNotFoundError:
                s.mkdir(self._p(path))
                return
            raise AlreadyExists(path)
        self._run(path, go)

    def _rmtree(self, s, full):
        for a in s.listdir_attr(full):
            p = posixpath.join(full, a.filename)
            if stat.S_ISDIR(a.st_mode or 0):
                self._rmtree(s, p)
            else:
                s.remove(p)
        s.rmdir(full)

    def delete(self, path, entry):
        full = self._p(path)
        self._run(path, lambda s: self._rmtree(s, full) if entry.is_dir else s.remove(full))

    def move(self, src, dst, entry, dst_entry):
        def go(s):
            if dst_entry is not None and dst_entry.is_dir and src.lower() != dst.lower():
                self._rmtree(s, self._p(dst))
            try:
                s.posix_rename(self._p(src), self._p(dst))
            except OSError:
                if dst_entry is not None and not dst_entry.is_dir:
                    s.remove(self._p(dst))
                s.rename(self._p(src), self._p(dst))
        self._run(src, go)
