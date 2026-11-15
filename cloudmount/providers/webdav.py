"""WebDAV: Облако Билайн и любые другие WebDAV-серверы."""
from __future__ import annotations

import email.utils
import logging
import urllib.parse
import xml.etree.ElementTree as ET

from .base import AlreadyExists, Entry, HttpProvider, NotFound, ProviderError

NS = "{DAV:}"
log = logging.getLogger(__name__)

PROPFIND_BODY = b"""<?xml version="1.0" encoding="utf-8"?>
<d:propfind xmlns:d="DAV:"><d:prop>
<d:resourcetype/><d:getcontentlength/><d:getlastmodified/>
</d:prop></d:propfind>"""

QUOTA_BODY = b"""<?xml version="1.0" encoding="utf-8"?>
<d:propfind xmlns:d="DAV:"><d:prop>
<d:quota-available-bytes/><d:quota-used-bytes/>
</d:prop></d:propfind>"""


ALLPROP_BODY = b"""<?xml version="1.0" encoding="utf-8"?>
<d:propfind xmlns:d="DAV:"><d:allprop/></d:propfind>"""


def parse_quota(xml_bytes: bytes) -> tuple[int, int]:
    """Ищет quota-available-bytes / quota-used-bytes в любом пространстве имён."""
    vals = {}
    for el in ET.fromstring(xml_bytes).iter():
        name = el.tag.rsplit("}", 1)[-1].lower()
        text = (el.text or "").strip()
        if name in ("quota-available-bytes", "quota-used-bytes", "quota-total-bytes") \
                and text.lstrip("-").isdigit() and name not in vals:
            vals[name] = int(text)
    used = max(vals.get("quota-used-bytes", 0), 0)
    if vals.get("quota-total-bytes", -1) > 0:
        return vals["quota-total-bytes"], used
    avail = vals.get("quota-available-bytes", -1)
    if avail >= 0 and (avail or used):
        return used + avail, used
    return 0, used


def _ts(s: str | None) -> float:
    if not s:
        return 0.0
    try:
        return email.utils.parsedate_to_datetime(s).timestamp()
    except (TypeError, ValueError):
        return 0.0


def parse_propfind(xml_bytes: bytes, base_path: str, req_path: str) -> list[Entry]:
    """Разбирает multistatus. base_path — путь корня WebDAV на сервере (например "/")."""
    root = ET.fromstring(xml_bytes)
    req = (base_path.rstrip("/") + "/" + req_path.strip("/")).rstrip("/")
    out = []
    for resp in root.iter(NS + "response"):
        href = resp.findtext(NS + "href") or ""
        hpath = urllib.parse.unquote(urllib.parse.urlparse(href).path).rstrip("/")
        if hpath == req or (not hpath and not req):
            continue  # сама папка
        name = hpath.rsplit("/", 1)[-1]
        if not name:
            continue
        is_dir, size, mtime = False, 0, 0.0
        for ps in resp.iter(NS + "propstat"):
            status = ps.findtext(NS + "status") or ""
            if " 200" not in status:
                continue
            prop = ps.find(NS + "prop")
            if prop is None:
                continue
            rt = prop.find(NS + "resourcetype")
            if rt is not None and rt.find(NS + "collection") is not None:
                is_dir = True
            cl = prop.findtext(NS + "getcontentlength")
            if cl and cl.strip().isdigit():
                size = int(cl)
            lm = prop.findtext(NS + "getlastmodified")
            if lm:
                mtime = _ts(lm)
        out.append(Entry(name, is_dir, 0 if is_dir else size, mtime))
    return out


class WebDAVProvider(HttpProvider):
    kind = "webdav"
    title = "WebDAV"
    note = "любой другой сервер"
    tint = "#6B5BB0"
    icon = "globe"
    default_url = ""
    hint = "Любой сервер с WebDAV: адрес, логин и пароль."
    fields = [
        ("url", "Адрес WebDAV", False, ""),
        ("username", "Логин", False, ""),
        ("password", "Пароль", True, ""),
    ]

    def __init__(self, data, save=None):
        super().__init__(data, save)
        self.base = (data.get("url") or self.default_url).rstrip("/")
        if not self.base:
            raise ProviderError("Не указан адрес WebDAV")
        self.base_path = urllib.parse.urlparse(self.base).path or "/"

    def setup_session(self, s):
        s.auth = (self.data.get("username", ""), self.data.get("password", ""))

    def url(self, path: str, slash=False) -> str:
        p = urllib.parse.quote("/" + path.strip("/"), safe="/")
        if slash and not p.endswith("/"):
            p += "/"
        return self.base + p

    def list_dir(self, path):
        r = self.request("PROPFIND", self.url(path, slash=True), data=PROPFIND_BODY,
                         headers={"Depth": "1", "Content-Type": "application/xml"})
        return parse_propfind(r.content, self.base_path, path)

    def fetch(self, path, entry, ctx, start=None, end=None):
        headers = {}
        if start is not None:
            headers["Range"] = f"bytes={start}-{'' if end is None else end}"
        return self.request("GET", self.url(path), headers=headers, stream=True)

    def upload(self, path, entry, fileobj, size):
        self.request("PUT", self.url(path), data=fileobj, rewind=fileobj,
                     headers={"Content-Length": str(size),
                              "Content-Type": "application/octet-stream"})
        return None

    def mkdir(self, path):
        try:
            self.request("MKCOL", self.url(path, slash=True))
        except ProviderError as e:
            if "405" in str(e):
                raise AlreadyExists(path) from e
            if "409" in str(e):
                raise NotFound(path) from e
            raise

    def delete(self, path, entry):
        self.request("DELETE", self.url(path, slash=entry.is_dir))

    def move(self, src, dst, entry, dst_entry):
        if dst_entry is not None and dst_entry.is_dir != entry.is_dir:
            raise AlreadyExists(dst)
        self.request("MOVE", self.url(src, slash=entry.is_dir),
                     headers={"Destination": self.url(dst, slash=entry.is_dir),
                              "Overwrite": "T"})

    def used_by_walk(self, max_entries: int = 50000, max_seconds: float = 300) -> int:
        """Если сервер не сообщает квоту — считаем занятое место сами, обходя папки."""
        import time as _t
        start, total, seen, stack = _t.time(), 0, 0, ["/"]
        while stack and seen < max_entries and _t.time() - start < max_seconds:
            path = stack.pop()
            try:
                entries = self.list_dir(path)
            except Exception as e:  # noqa: BLE001
                log.info("WebDAV: не удалось прочитать %s при подсчёте места: %s", path, e)
                continue
            for e in entries:
                seen += 1
                if e.is_dir:
                    stack.append(path.rstrip("/") + "/" + e.name)
                else:
                    total += e.size
        log.info("WebDAV: занято по подсчёту файлов %.2f ГБ (%d объектов%s)", total / 2**30, seen,
                 ", подсчёт неполный" if stack else "")
        return total

    def quota(self):
        used = 0
        for body in (QUOTA_BODY, ALLPROP_BODY):
            try:
                r = self.request("PROPFIND", self.url("/", slash=True), data=body,
                                 headers={"Depth": "0", "Content-Type": "application/xml"})
                total, used2 = parse_quota(r.content)
            except Exception as e:  # noqa: BLE001
                log.info("WebDAV: запрос квоты не удался: %s", e)
                continue
            used = used or used2
            if total:
                return total, used
            log.debug("WebDAV: сервер не сообщил размер. Ответ: %s", r.text[:600])
        if not used:
            used = self.used_by_walk()
        return 0, used


class BeelineProvider(WebDAVProvider):
    kind = "beeline"
    title = "Облако Билайн"
    note = "WebDAV"
    tint = "#B7791F"
    icon = "cloud"
    default_url = "https://webdav.cloudbeeline.ru"
    fields = [
        ("username", "Логин (номер телефона)", False, ""),
        ("password", "Пароль WebDAV", True, ""),
    ]
    hint = ("Включите WebDAV: «Профиль и настройки» → «Безопасность» →\n"
            "«Доступ через WebDAV». Там же логин и пароль.")
    links = [("Открыть Облако Билайн", "https://cloudbeeline.ru")]


class MailruProvider(WebDAVProvider):
    kind = "mailru"
    title = "Облако Mail.ru"
    note = "только платные тарифы"
    tint = "#3E7FB8"
    icon = "cloud"
    default_url = "https://webdav.cloud.mail.ru"
    fields = [
        ("username", "Почта (полностью, с @mail.ru)", False, ""),
        ("password", "Пароль для внешнего приложения", True, ""),
    ]
    hint = ("Mail.ru открывает WebDAV только на ПЛАТНЫХ тарифах Облака.\n"
            "Создайте пароль для внешнего приложения с доступом к Облаку\n"
            "(обычный пароль от почты не подойдёт).")
    links = [("Mail ID → Безопасность → Пароли для внешних приложений", "https://id.mail.ru/security"),
             ("Открыть Облако Mail.ru", "https://cloud.mail.ru/")]


class KoofrProvider(WebDAVProvider):
    kind = "koofr"
    title = "Koofr"
    note = "WebDAV, 10 ГБ бесплатно"
    tint = "#4C7A3A"
    icon = "cloud"
    default_url = "https://app.koofr.net/dav/Koofr"
    fields = [
        ("username", "Почта", False, ""),
        ("password", "Пароль приложения", True, ""),
    ]
    hint = ("Обычный пароль не подойдёт — создайте пароль приложения:\n"
            "Preferences → Password → App passwords → Generate.")
    links = [("Настройки Koofr", "https://app.koofr.net/app/admin/preferences/password")]


class NextcloudProvider(WebDAVProvider):
    kind = "nextcloud"
    title = "Nextcloud"
    note = "свой сервер, WebDAV"
    tint = "#2B6CA8"
    icon = "server"
    fields = [
        ("server", "Адрес сервера", False, "https://"),
        ("username", "Пользователь", False, ""),
        ("password", "Пароль приложения", True, ""),
    ]
    hint = ("Пароль приложения: Nextcloud → Настройки → Безопасность →\n"
            "«Создать новый пароль приложения». Подходит и для ownCloud.")

    def __init__(self, data, save=None):
        server = (data.get("server") or "").rstrip("/")
        if server and not server.startswith("http"):
            server = "https://" + server
        data["url"] = (f"{server}/remote.php/dav/files/"
                       f"{urllib.parse.quote(data.get('username', ''))}")
        super().__init__(data, save)
