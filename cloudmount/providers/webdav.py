"""WebDAV: Облако Билайн и любые другие WebDAV-серверы."""
from __future__ import annotations

import email.utils
import urllib.parse
import xml.etree.ElementTree as ET

from .base import AlreadyExists, Entry, HttpProvider, NotFound, ProviderError

NS = "{DAV:}"

PROPFIND_BODY = b"""<?xml version="1.0" encoding="utf-8"?>
<d:propfind xmlns:d="DAV:"><d:prop>
<d:resourcetype/><d:getcontentlength/><d:getlastmodified/>
</d:prop></d:propfind>"""

QUOTA_BODY = b"""<?xml version="1.0" encoding="utf-8"?>
<d:propfind xmlns:d="DAV:"><d:prop>
<d:quota-available-bytes/><d:quota-used-bytes/>
</d:prop></d:propfind>"""


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
    title = "WebDAV (другой сервер)"
    default_url = ""
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

    def quota(self):
        try:
            r = self.request("PROPFIND", self.url("/", slash=True), data=QUOTA_BODY,
                             headers={"Depth": "0", "Content-Type": "application/xml"})
            root = ET.fromstring(r.content)
            avail = root.findtext(f".//{NS}quota-available-bytes")
            used = root.findtext(f".//{NS}quota-used-bytes")
            if avail and used:
                used_i = int(used)
                return used_i + int(avail), used_i
        except Exception:
            pass
        return 0, 0


class BeelineProvider(WebDAVProvider):
    kind = "beeline"
    title = "Облако Билайн"
    default_url = "https://webdav.cloudbeeline.ru"
    fields = [
        ("username", "Логин (номер телефона)", False, ""),
        ("password", "Пароль WebDAV", True, ""),
    ]
