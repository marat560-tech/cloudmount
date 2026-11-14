import io
import json
import os
import sys
import urllib.parse

import requests
from requests.adapters import BaseAdapter

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from cloudmount.providers import make_provider  # noqa: E402
from cloudmount.providers.base import NotFound  # noqa: E402
from cloudmount.providers.webdav import parse_propfind  # noqa: E402


class Router(BaseAdapter):
    def __init__(self, handler):
        super().__init__()
        self.handler = handler
        self.log = []

    def send(self, req, **kw):
        body = req.body
        if hasattr(body, "read"):
            body = body.read()
        self.log.append((req.method, req.url, dict(req.headers), body))
        status, payload, headers = self.handler(req.method, req.url, req.headers, body)
        r = requests.Response()
        r.status_code = status
        r.headers.update(headers or {})
        r._content = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        r.url = req.url
        r.request = req
        return r

    def close(self):
        pass


def attach(p, handler):
    router = Router(handler)
    p.session.mount("https://", router)
    return router


TOKEN = {"token": {"access_token": "T", "refresh_token": "R", "expires_at": 9e12},
         "client_id": "cid"}


def test_webdav_parse():
    xml = """<?xml version="1.0"?><d:multistatus xmlns:d="DAV:">
<d:response><d:href>/dav/%D0%94%D0%BE%D0%BA/</d:href><d:propstat><d:prop>
<d:resourcetype><d:collection/></d:resourcetype></d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>
<d:response><d:href>https://srv/dav/%D0%94%D0%BE%D0%BA/a%20b.txt</d:href><d:propstat><d:prop>
<d:resourcetype/><d:getcontentlength>42</d:getcontentlength>
<d:getlastmodified>Wed, 01 Jan 2025 10:00:00 GMT</d:getlastmodified></d:prop>
<d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>
<d:response><d:href>/dav/%D0%94%D0%BE%D0%BA/sub/</d:href><d:propstat><d:prop>
<d:resourcetype><d:collection/></d:resourcetype></d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>
</d:multistatus>""".encode()
    es = parse_propfind(xml, "/dav", "/Док")
    assert [(e.name, e.is_dir, e.size) for e in es] == [("a b.txt", False, 42), ("sub", True, 0)]
    assert es[0].mtime == 1735725600


def test_beeline_requests():
    p = make_provider({"type": "beeline", "data": {"username": "79001234567", "password": "pw"}})

    def h(m, url, hdr, body):
        if m == "PROPFIND":
            return 207, b'<d:multistatus xmlns:d="DAV:"/>', {}
        return 201, b"", {}
    r = attach(p, h)
    assert p.list_dir("/Фото") == []
    p.upload("/Фото/1.jpg", None, io.BytesIO(b"xyz"), 3)
    p.move("/a", "/b c", type("E", (), {"is_dir": False})(), None)
    m, url, hdr, _ = r.log[0]
    assert m == "PROPFIND" and url == "https://webdav.cloudbeeline.ru/%D0%A4%D0%BE%D1%82%D0%BE/"
    assert hdr["Depth"] == "1" and hdr["Authorization"].startswith("Basic ")
    assert r.log[1][0] == "PUT" and r.log[1][3] == b"xyz"
    assert r.log[2][2]["Destination"] == "https://webdav.cloudbeeline.ru/b%20c"


def test_yandex_paging_and_upload():
    p = make_provider({"type": "yandex", "data": dict(TOKEN)})

    def h(m, url, hdr, body):
        u = urllib.parse.urlparse(url)
        q = dict(urllib.parse.parse_qsl(u.query))
        if u.path == "/v1/disk/resources" and m == "GET":
            off = int(q["offset"])
            items = [{"name": f"f{i}", "type": "file", "size": i,
                      "modified": "2025-01-01T00:00:00+00:00"} for i in range(off, min(off + 1000, 1500))]
            return 200, {"_embedded": {"items": items, "total": 1500}}, {}
        if u.path == "/v1/disk/resources/upload":
            assert q["path"] == "disk:/a/б.txt" and q["overwrite"] == "true"
            return 200, {"href": "https://uploader.yandex.net/up/1"}, {}
        if u.netloc == "uploader.yandex.net":
            assert "Authorization" not in hdr
            return 201, b"", {}
        if u.path == "/v1/disk/resources" and m == "DELETE":
            return 404, {"error": "DiskNotFoundError", "description": "nope"}, {}
        raise AssertionError(url)
    r = attach(p, h)
    es = p.list_dir("/")
    assert len(es) == 1500 and es[5].size == 5
    assert r.log[0][2]["Authorization"] == "OAuth T"
    p.upload("/a/б.txt", None, io.BytesIO(b"data"), 4)
    try:
        p.delete("/x", None)
        raise AssertionError("ожидали NotFound")
    except NotFound:
        pass


def test_dropbox_list_and_download_range():
    p = make_provider({"type": "dropbox", "data": dict(TOKEN)})

    def h(m, url, hdr, body):
        if url.endswith("/files/list_folder"):
            assert json.loads(body) == {"path": "", "limit": 2000}
            return 200, {"entries": [{".tag": "folder", "name": "Папка", "id": "id:1"}],
                         "has_more": True, "cursor": "c1"}, {}
        if url.endswith("/files/list_folder/continue"):
            return 200, {"entries": [{".tag": "file", "name": "a.txt", "size": 3,
                                      "server_modified": "2025-01-01T00:00:00Z"}],
                         "has_more": False}, {}
        if url.endswith("/files/download"):
            arg = hdr["Dropbox-API-Arg"]
            assert arg.isascii() and json.loads(arg) == {"path": "/Папка/a.txt"}
            assert hdr["Range"] == "bytes=1-2"
            return 206, b"bc", {}
        if url.endswith("/files/get_metadata"):
            return 409, {"error_summary": "path/not_found/.."}, {}
        raise AssertionError(url)
    attach(p, h)
    es = p.list_dir("/")
    assert [(e.name, e.is_dir) for e in es] == [("Папка", True), ("a.txt", False)]
    assert p.read_range("/Папка/a.txt", es[1], 1, 2, {}) == b"bc"


def test_dropbox_refresh_on_401():
    p = make_provider({"type": "dropbox", "data": dict(TOKEN, token=dict(TOKEN["token"]))})
    saved = []
    p._save = lambda: saved.append(1)
    calls = {"n": 0}

    def h(m, url, hdr, body):
        calls["n"] += 1
        if hdr.get("Authorization") == "Bearer T":
            return 401, {"error_summary": "expired_access_token/"}, {}
        return 200, {"entries": [], "has_more": False}, {}
    attach(p, h)
    orig_post = requests.post

    def fake_post(url, data=None, **kw):
        assert data["grant_type"] == "refresh_token" and data["refresh_token"] == "R"
        r = requests.Response()
        r.status_code = 200
        r._content = json.dumps({"access_token": "T2", "expires_in": 14400}).encode()
        return r
    requests.post = fake_post
    try:
        assert p.list_dir("/") == []
    finally:
        requests.post = orig_post
    assert p.data["token"]["access_token"] == "T2" and p.data["token"]["refresh_token"] == "R"
    assert saved


def test_gdrive_path_resolution_and_upload():
    p = make_provider({"type": "gdrive", "data": dict(TOKEN, client_secret="s")})
    tree = {"root": [("f1", "Работа", "application/vnd.google-apps.folder"),
                     ("d1", "Заметка", "application/vnd.google-apps.document")],
            "f1": [("x1", "отчёт.pdf", "application/pdf")]}

    def h(m, url, hdr, body):
        u = urllib.parse.urlparse(url)
        q = dict(urllib.parse.parse_qsl(u.query))
        if u.path == "/drive/v3/files" and m == "GET":
            pid = q["q"].split("'")[1]
            files = [{"id": i, "name": n, "mimeType": mt, "size": "10",
                      "modifiedTime": "2025-01-01T00:00:00.000Z"} for i, n, mt in tree[pid]]
            return 200, {"files": files}, {}
        if u.path == "/upload/drive/v3/files" and m == "POST":
            meta = json.loads(body)
            assert meta == {"name": "new.txt", "parents": ["f1"]}
            return 200, {}, {"Location": "https://www.googleapis.com/upload/session/1"}
        if u.path == "/upload/session/1":
            assert body == b"hi"
            return 200, {"id": "n1", "name": "new.txt", "size": "2"}, {}
        if u.path == "/drive/v3/files/x1" and q.get("alt") == "media":
            return 206, b"0123", {}
        raise AssertionError((m, url))
    r = attach(p, h)
    root = p.list_dir("/")
    assert [e.name for e in root] == ["Работа"]          # Google Docs скрыты
    assert p.read_range("/работа/ОТЧЁТ.pdf", None, 0, 4, {}) == b"0123"
    e = p.upload("/Работа/new.txt", None, io.BytesIO(b"hi"), 2)
    assert e.id == "n1" and p._id("/Работа/new.txt") == "n1"
    assert all("Authorization" in x[2] for x in r.log)
