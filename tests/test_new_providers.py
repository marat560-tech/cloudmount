import io
import json
import os
import stat as st
import sys
import types
import urllib.parse

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
sys.path.insert(0, os.path.dirname(__file__))

from cloudmount.providers import PROVIDERS, make_provider  # noqa: E402
from cloudmount.providers.base import AlreadyExists, Entry, NotFound  # noqa: E402
from test_providers import attach  # noqa: E402

TOKEN = {"token": {"access_token": "T", "refresh_token": "R", "expires_at": 9e12},
         "client_id": "cid", "client_secret": "sec"}


def q(url):
    u = urllib.parse.urlparse(url)
    return u, dict(urllib.parse.parse_qsl(u.query, keep_blank_values=True))


def test_every_provider_has_ui_metadata():
    for kind, cls in PROVIDERS.items():
        assert cls.title and cls.fields and cls.tint.startswith("#"), kind
        assert cls.icon in ("cloud", "server", "bucket", "globe"), kind
        for key, label, secret, default in cls.fields:
            assert key and label
        for text, url in cls.links:
            assert url.startswith("https://"), (kind, url)


# ---------------- OneDrive ----------------
def test_onedrive_list_paging_and_paths():
    p = make_provider({"type": "onedrive", "data": dict(TOKEN)})

    def h(m, url, hdr, body):
        u, qs = q(url)
        if urllib.parse.unquote(u.path).endswith("/root:/Документы/Отчёт 1:/children"):
            if "skiptoken" in u.query:
                return 200, {"value": [{"name": "b", "folder": {}, "id": "2"}]}, {}
            return 200, {"value": [{"name": "a.txt", "size": 5, "file": {}, "id": "1",
                                    "lastModifiedDateTime": "2025-01-01T00:00:00Z"},
                                   {"name": "x", "package": {}}],
                         "@odata.nextLink": "https://graph.microsoft.com/v1.0/next?skiptoken=1"}, {}
        if "skiptoken" in u.query:
            return 200, {"value": [{"name": "b", "folder": {}, "id": "2"}]}, {}
        raise AssertionError(url)
    r = attach(p, h)
    es = p.list_dir("/Документы/Отчёт 1")
    assert [(e.name, e.is_dir, e.size) for e in es] == [("a.txt", False, 5), ("b", True, 0)]
    assert r.log[0][2]["Authorization"] == "Bearer T"


def test_onedrive_chunked_upload_and_move():
    p = make_provider({"type": "onedrive", "data": dict(TOKEN)})
    from cloudmount.providers import onedrive
    size = onedrive.SIMPLE_LIMIT + 10
    chunks = []

    def h(m, url, hdr, body):
        if url.endswith("/createUploadSession"):
            return 200, {"uploadUrl": "https://up.example.com/s1"}, {}
        if url.startswith("https://up.example.com"):
            assert "Authorization" not in hdr
            chunks.append(hdr["Content-Range"])
            done = hdr["Content-Range"].endswith(f"-{size - 1}/{size}")
            return (201, {"name": "big.bin", "id": "9", "size": size, "file": {}}, {}) if done \
                else (202, {}, {})
        if m == "PATCH":
            js = json.loads(body)
            assert js == {"name": "new.txt", "parentReference": {"path": "/drive/root:/Папка"}}
            return 200, {}, {}
        raise AssertionError((m, url))
    attach(p, h)
    e = p.upload("/big.bin", None, io.BytesIO(b"z" * size), size)
    assert e.id == "9" and chunks[0].startswith("bytes 0-") and len(chunks) == 1
    p.move("/old.txt", "/Папка/new.txt", Entry("old.txt", False), None)


# ---------------- S3 ----------------
def s3():
    return make_provider({"type": "s3", "data": {
        "endpoint": "https://storage.yandexcloud.net", "region": "ru-central1",
        "bucket": "my-bucket", "access_key": "AK", "secret_key": "SK", "prefix": "backup"}})


def test_s3_list_and_signature():
    p = s3()
    xml = """<?xml version="1.0"?><ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">
<IsTruncated>false</IsTruncated>
<Contents><Key>backup/Фото/</Key><Size>0</Size></Contents>
<Contents><Key>backup/Фото/1.jpg</Key><Size>42</Size><LastModified>2025-01-01T00:00:00.000Z</LastModified></Contents>
<CommonPrefixes><Prefix>backup/Фото/2024/</Prefix></CommonPrefixes>
</ListBucketResult>""".encode()

    def h(m, url, hdr, body):
        u, qs = q(url)
        assert u.path == "/my-bucket" and qs["prefix"] == "backup/Фото/" and qs["delimiter"] == "/"
        auth = hdr["authorization"]
        assert auth.startswith("AWS4-HMAC-SHA256 Credential=AK/") and "/ru-central1/s3/aws4_request" in auth
        assert "SignedHeaders=host;x-amz-content-sha256;x-amz-date" in auth
        return 200, xml, {}
    attach(p, h)
    es = p.list_dir("/Фото")
    assert [(e.name, e.is_dir, e.size) for e in es] == [("1.jpg", False, 42), ("2024", True, 0)]


def test_s3_signature_is_deterministic():
    p = s3()
    from cloudmount.providers import s3 as s3mod

    class FixedDT:
        @staticmethod
        def now(tz=None):
            from datetime import datetime, timezone
            return datetime(2025, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    orig = s3mod.datetime
    s3mod.datetime = FixedDT
    try:
        u1, h1 = p._signed("GET", "a b/c.txt", {"x": "1"})
        u2, h2 = p._signed("GET", "a b/c.txt", {"x": "1"})
    finally:
        s3mod.datetime = orig
    assert u1 == u2 == "https://storage.yandexcloud.net/my-bucket/a%20b/c.txt?x=1"
    assert h1 == h2 and h1["x-amz-date"] == "20250101T120000Z"


def test_s3_multipart_upload_and_move():
    p = s3()
    from cloudmount.providers import s3 as s3mod
    s3mod.SIMPLE_LIMIT, s3mod.PART = 10, 4
    calls = []

    def h(m, url, hdr, body):
        u, qs = q(url)
        calls.append((m, u.path, tuple(sorted(qs))))
        if m == "POST" and "uploads" in qs:
            return 200, b"<InitiateMultipartUploadResult><UploadId>U1</UploadId></InitiateMultipartUploadResult>", {}
        if m == "PUT" and "partNumber" in qs:
            return 200, b"", {"ETag": f'"e{qs["partNumber"]}"'}
        if m == "POST" and "uploadId" in qs:
            assert b"<PartNumber>3</PartNumber>" in body
            return 200, b"<CompleteMultipartUploadResult/>", {}
        if m == "PUT" and "x-amz-copy-source" in hdr:
            assert hdr["x-amz-copy-source"] == "/my-bucket/backup/a.txt"
            return 200, b"<CopyObjectResult/>", {}
        if m == "DELETE":
            return 204, b"", {}
        raise AssertionError((m, url))
    try:
        attach(p, h)
        p.upload("/big", None, io.BytesIO(b"x" * 11), 11)
        assert sum(1 for c in calls if c[0] == "PUT") == 3
        p.move("/a.txt", "/b.txt", Entry("a.txt", False), None)
        assert calls[-1][:2] == ("DELETE", "/my-bucket/backup/a.txt")
    finally:
        s3mod.SIMPLE_LIMIT, s3mod.PART = 64 * 1024 * 1024, 16 * 1024 * 1024


# ---------------- pCloud ----------------
def test_pcloud_calls():
    p = make_provider({"type": "pcloud", "data": {"token": {"access_token": "PT", "hostname": "eapi.pcloud.com"}}})
    seen = []

    def h(m, url, hdr, body):
        u, qs = q(url)
        assert u.netloc in ("eapi.pcloud.com", "c1.pcloud.com")
        seen.append((m, u.path, qs))
        if u.path == "/listfolder":
            if qs["path"] == "/нет":
                return 200, {"result": 2005, "error": "Directory does not exist."}, {}
            return 200, {"result": 0, "metadata": {"contents": [
                {"name": "a.txt", "isfolder": False, "size": 3, "fileid": 1,
                 "modified": "Wed, 01 Jan 2025 10:00:00 +0000"},
                {"name": "dir", "isfolder": True, "folderid": 7}]}}, {}
        if u.path == "/getfilelink":
            return 200, {"result": 0, "hosts": ["c1.pcloud.com"], "path": "/dl/a.txt"}, {}
        if u.netloc == "c1.pcloud.com":
            return 206, b"bc", {}
        if u.path == "/upload_create":
            return 200, {"result": 0, "uploadid": 55}, {}
        if u.path in ("/upload_write", "/upload_save"):
            return 200, {"result": 0}, {}
        if u.path == "/createfolder":
            return 200, {"result": 2004, "error": "exists"}, {}
        raise AssertionError(url)
    attach(p, h)
    es = p.list_dir("/")
    assert [(e.name, e.is_dir) for e in es] == [("a.txt", False), ("dir", True)]
    assert seen[0][2]["access_token"] == "PT"
    with pytest.raises(NotFound):
        p.list_dir("/нет")
    assert p.read_range("/a.txt", es[0], 1, 2, {}) == b"bc"
    p.upload("/dir/b.bin", None, io.BytesIO(b"123"), 3)
    save = [s for s in seen if s[1] == "/upload_save"][0][2]
    assert save == {"access_token": "PT", "uploadid": "55", "path": "/dir", "name": "b.bin"}
    with pytest.raises(AlreadyExists):
        p.mkdir("/dir")


# ---------------- Box ----------------
def test_box_list_upload_multipart():
    p = make_provider({"type": "box", "data": dict(TOKEN)})
    up = {}

    def h(m, url, hdr, body):
        u, qs = q(url)
        if u.path == "/2.0/folders/0/items":
            return 200, {"total_count": 2, "entries": [
                {"type": "folder", "id": "11", "name": "Работа"},
                {"type": "web_link", "id": "12", "name": "link"}]}, {}
        if u.path == "/2.0/folders/11/items":
            return 200, {"total_count": 0, "entries": []}, {}
        if u.netloc == "upload.box.com" and u.path == "/api/2.0/files/content":
            up["ct"], up["body"] = hdr["Content-Type"], body
            return 201, {"entries": [{"id": "99", "name": "a.txt", "size": 3}]}, {}
        raise AssertionError(url)
    attach(p, h)
    assert [e.name for e in p.list_dir("/")] == ["Работа"]
    e = p.upload("/работа/a.txt", None, io.BytesIO(b"abc"), 3)
    assert e.id == "99" and p._id("/Работа/a.txt") == "99"
    boundary = up["ct"].split("boundary=")[1]
    assert up["body"].endswith(f"\r\n--{boundary}--\r\n".encode())
    assert b'"parent": {"id": "11"}' in up["body"] and b"\r\n\r\nabc\r\n" in up["body"]


def test_box_multipart_reader_rewind():
    from cloudmount.providers.box import MultipartReader
    r = MultipartReader({"name": "x"}, io.BytesIO(b"hello"), 5)
    a = b"".join(iter(lambda: r.read(7), b""))
    r.seek(0)
    b = b"".join(r)
    assert a == b and len(a) == len(r)


# ---------------- Nextcloud / Mail.ru ----------------
def test_presets_urls():
    nc = make_provider({"type": "nextcloud", "data": {"server": "cloud.example.com/", "username": "иван",
                                                        "password": "p"}})
    assert nc.base == "https://cloud.example.com/remote.php/dav/files/%D0%B8%D0%B2%D0%B0%D0%BD"
    assert make_provider({"type": "mailru", "data": {}}).base == "https://webdav.cloud.mail.ru"
    assert make_provider({"type": "koofr", "data": {}}).base == "https://app.koofr.net/dav/Koofr"


# ---------------- SFTP (с поддельным paramiko) ----------------
class FakeAttr:
    def __init__(self, name, d, size=0):
        self.filename, self.st_mode, self.st_size, self.st_mtime = \
            name, (st.S_IFDIR if d else st.S_IFREG) | 0o755, size, 1700000000


class FakeSFTP:
    def __init__(self, fs):
        self.fs = fs

    def listdir_attr(self, p):
        if p not in self.fs["dirs"]:
            raise FileNotFoundError(2, "no")
        out = [FakeAttr(d.rsplit("/", 1)[1], True) for d in self.fs["dirs"] if d.rsplit("/", 1)[0] == p and d != p]
        out += [FakeAttr(f.rsplit("/", 1)[1], False, len(b)) for f, b in self.fs["files"].items()
                if f.rsplit("/", 1)[0] == p]
        return out

    def open(self, p, mode):
        return io.BytesIO(self.fs["files"][p])

    def putfo(self, f, p, file_size, confirm):
        self.fs["files"][p] = f.read()

    def posix_rename(self, a, b):
        self.fs["files"][b] = self.fs["files"].pop(a)

    def stat(self, p):
        if p not in self.fs["dirs"] and p not in self.fs["files"]:
            raise FileNotFoundError(2, "no")

    def mkdir(self, p):
        self.fs["dirs"].add(p)


def test_sftp_with_fake_paramiko(monkeypatch):
    fs = {"dirs": {"/srv", "/srv/docs"}, "files": {"/srv/a.txt": b"hello"}}

    class Key:
        def get_name(self):
            return "ssh-ed25519"

        def asbytes(self):
            return b"KEY1"

    class Transport:
        def __init__(self, addr):
            self.addr = addr

        def start_client(self, timeout=None):
            pass

        def get_remote_server_key(self):
            return Key()

        def auth_password(self, u, pw):
            assert (u, pw) == ("root", "pw")

        def is_active(self):
            return True

        def close(self):
            pass

    fake = types.SimpleNamespace(Transport=Transport,
                                 SFTPClient=types.SimpleNamespace(from_transport=lambda t: FakeSFTP(fs)))
    monkeypatch.setitem(sys.modules, "paramiko", fake)
    saved = []
    p = make_provider({"type": "sftp", "data": {"host": "nas", "port": "22", "username": "root",
                                                  "password": "pw", "root": "/srv"}}, lambda: saved.append(1))
    es = sorted((e.name, e.is_dir) for e in p.list_dir("/"))
    assert es == [("a.txt", False), ("docs", True)]
    assert p.data["host_key"] == "ssh-ed25519 " + __import__("base64").b64encode(b"KEY1").decode() and saved
    assert p.read_range("/a.txt", None, 1, 3, {}) == b"ell"
    p.upload("/docs/b.txt", None, io.BytesIO(b"xyz"), 3)
    assert fs["files"]["/srv/docs/b.txt"] == b"xyz"
    with pytest.raises(AlreadyExists):
        p.mkdir("/docs")
    with pytest.raises(NotFound):
        p.list_dir("/nope")
    # ключ сервера изменился — подключение запрещено
    p2 = make_provider({"type": "sftp", "data": dict(p.data, host_key="ssh-ed25519 OTHER")})
    from cloudmount.providers.base import AuthError
    with pytest.raises(AuthError):
        p2.list_dir("/")
