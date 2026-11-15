"""Базовые классы провайдеров облачных хранилищ.

Все пути внутри программы — POSIX-вида: "/" — корень, "/папка/файл.txt".
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field

import requests

log = logging.getLogger(__name__)

USER_AGENT = "CloudMount/1.0"


class ProviderError(Exception):
    pass


class NotFound(ProviderError):
    pass


class AlreadyExists(ProviderError):
    pass


class AuthError(ProviderError):
    pass


@dataclass
class Entry:
    name: str
    is_dir: bool
    size: int = 0
    mtime: float = 0.0
    id: str | None = None          # идентификатор у провайдера (Google Drive)
    extra: dict = field(default_factory=dict)


def join(parent: str, name: str) -> str:
    return "/" + name if parent in ("", "/") else parent.rstrip("/") + "/" + name


def split(path: str) -> tuple[str, str]:
    path = "/" + path.strip("/")
    if path == "/":
        return "/", ""
    parent, _, name = path.rpartition("/")
    return parent or "/", name


class Provider:
    """Интерфейс провайдера. Методы вызываются из нескольких потоков."""

    kind = "base"
    title = "Base"
    oauth = False
    # (ключ, подпись, секретное поле, значение по умолчанию)
    fields: list[tuple[str, str, bool, str]] = []
    optional: set[str] = set()       # необязательные поля
    hint = ""                        # подсказка в окне настройки
    links: list[tuple[str, str]] = []   # кликабельные ссылки (текст, адрес)
    tint = "#3B6FD8"                 # цвет значка
    icon = "cloud"                   # cloud | server | bucket | globe
    note = ""                        # короткая подпись на плитке
    redirect_host = "127.0.0.1"      # адрес возврата для входа через браузер

    def __init__(self, data: dict, save=None):
        self.data = data            # настройки аккаунта (изменяемые: токены)
        self._save = save or (lambda: None)

    def save(self):
        try:
            self._save()
        except Exception:  # pragma: no cover
            log.exception("Не удалось сохранить настройки аккаунта")

    # --- обязательные операции ---
    def list_dir(self, path: str) -> list[Entry]:
        raise NotImplementedError

    def fetch(self, path: str, entry: Entry, ctx: dict,
              start: int | None = None, end: int | None = None) -> requests.Response:
        """GET содержимого файла (stream=True). start/end — включительно."""
        raise NotImplementedError

    def upload(self, path: str, entry: Entry | None, fileobj, size: int) -> Entry | None:
        raise NotImplementedError

    def mkdir(self, path: str) -> None:
        raise NotImplementedError

    def delete(self, path: str, entry: Entry) -> None:
        raise NotImplementedError

    def move(self, src: str, dst: str, entry: Entry, dst_entry: Entry | None) -> None:
        raise NotImplementedError

    def quota(self) -> tuple[int, int]:
        """(всего байт, занято байт); 0, 0 если неизвестно."""
        return 0, 0

    # --- общие реализации ---
    def read_range(self, path: str, entry: Entry, offset: int, length: int, ctx: dict) -> bytes:
        if length <= 0:
            return b""
        r = self.fetch(path, entry, ctx, offset, offset + length - 1)
        try:
            if r.status_code == 206:
                return r.content[:length]
            # Сервер проигнорировал Range — пропускаем лишнее
            out = bytearray()
            skipped = 0
            for chunk in r.iter_content(1 << 20):
                if skipped < offset:
                    need = offset - skipped
                    if len(chunk) <= need:
                        skipped += len(chunk)
                        continue
                    chunk = chunk[need:]
                    skipped = offset
                out += chunk
                if len(out) >= length:
                    break
            return bytes(out[:length])
        finally:
            r.close()

    def download(self, path: str, entry: Entry, fileobj, ctx: dict) -> None:
        r = self.fetch(path, entry, ctx)
        try:
            for chunk in r.iter_content(1 << 20):
                fileobj.write(chunk)
        finally:
            r.close()


class HttpProvider(Provider):
    timeout = (20, 300)

    def __init__(self, data: dict, save=None):
        super().__init__(data, save)
        self._local = threading.local()
        self._token_lock = threading.Lock()

    @property
    def session(self) -> requests.Session:
        s = getattr(self._local, "s", None)
        if s is None:
            s = requests.Session()
            s.headers["User-Agent"] = USER_AGENT
            self.setup_session(s)
            self._local.s = s
        return s

    def setup_session(self, s: requests.Session) -> None:
        pass

    def auth_headers(self) -> dict:
        return {}

    def refresh_auth(self) -> bool:
        return False

    def error_for(self, r: requests.Response) -> ProviderError:
        text = (r.text or "")[:300]
        if r.status_code == 404:
            return NotFound(text)
        if r.status_code in (401, 403):
            return AuthError(f"HTTP {r.status_code}: {text}")
        return ProviderError(f"HTTP {r.status_code}: {text}")

    def request(self, method: str, url: str, *, ok=(200, 201, 202, 204, 206, 207),
                auth: bool = True, rewind=None, **kw) -> requests.Response:
        headers = kw.pop("headers", None) or {}
        refreshed = False
        last_exc = None
        for attempt in range(5):
            if rewind is not None:
                rewind.seek(0)
            h = dict(headers)
            if auth:
                h.update(self.auth_headers())
            try:
                r = self.session.request(method, url, headers=h, timeout=self.timeout, **kw)
            except requests.RequestException as e:
                last_exc = e
                log.warning("%s %s: %s (попытка %d)", method, url, e, attempt + 1)
                time.sleep(min(2 ** attempt, 10))
                continue
            if r.status_code in ok:
                return r
            if r.status_code == 401 and auth and not refreshed and self.refresh_auth():
                refreshed = True
                r.close()
                continue
            if r.status_code in (429, 500, 502, 503, 504) and attempt < 4:
                delay = r.headers.get("Retry-After")
                try:
                    delay = float(delay) if delay else 2 ** attempt
                except ValueError:
                    delay = 2 ** attempt
                log.warning("%s %s -> %s, повтор через %.0f с", method, url, r.status_code, delay)
                r.close()
                time.sleep(min(delay, 30))
                continue
            err = self.error_for(r)
            r.close()
            raise err
        raise ProviderError(f"Сеть недоступна: {last_exc}")


class OAuthProvider(HttpProvider):
    """Провайдер с OAuth2-токеном в data['token'] (access/refresh/expires_at)."""

    oauth = True
    token_url = ""
    auth_scheme = "Bearer"

    def auth_headers(self) -> dict:
        tok = self.data.get("token") or {}
        if not tok.get("access_token"):
            raise AuthError("Нет токена — войдите в аккаунт в настройках")
        exp = tok.get("expires_at")
        if exp and tok.get("refresh_token") and exp - 120 < time.time():
            self.refresh_auth()
            tok = self.data["token"]
        return {"Authorization": f"{self.auth_scheme} {tok['access_token']}"}

    def refresh_auth(self) -> bool:
        old = (self.data.get("token") or {}).get("access_token")
        with self._token_lock:
            tok = self.data.get("token") or {}
            if tok.get("access_token") != old:   # уже обновил другой поток
                return True
            if not tok.get("refresh_token"):
                return False
            form = {"grant_type": "refresh_token", "refresh_token": tok["refresh_token"],
                    "client_id": self.data.get("client_id", "")}
            if self.data.get("client_secret"):
                form["client_secret"] = self.data["client_secret"]
            try:
                r = requests.post(self.token_url, data=form, timeout=30,
                                  headers={"User-Agent": USER_AGENT})
            except requests.RequestException as e:
                log.warning("Обновление токена: %s", e)
                return False
            if r.status_code != 200:
                log.error("Не удалось обновить токен: %s %s", r.status_code, r.text[:300])
                return False
            from ..oauth import token_from_response
            self.data["token"] = token_from_response(r.json(), old=tok)
            self.save()
            log.info("Токен обновлён")
            return True

    @classmethod
    def login(cls, data: dict) -> dict:
        """Запускает вход через браузер, возвращает token-словарь."""
        raise NotImplementedError
