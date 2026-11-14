import errno
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from cloudmount import fs as fsmod  # noqa: E402
from cloudmount.fs import CloudFS  # noqa: E402
from cloudmount.fusecompat import FuseOSError  # noqa: E402
from cloudmount.providers.base import AlreadyExists, Entry, NotFound, Provider, split  # noqa: E402


class FakeResp:
    def __init__(self, data, status=200):
        self.content, self.status_code = data, status

    def iter_content(self, n):
        for i in range(0, len(self.content), n):
            yield self.content[i:i + n]

    def close(self):
        pass


class MemProvider(Provider):
    """Облако в памяти, регистрозависимое (как Яндекс/WebDAV)."""
    title = "mem"

    def __init__(self):
        super().__init__({})
        self.files = {}           # path -> bytes
        self.dirs = {"/"}
        self.calls = []

    def list_dir(self, path):
        self.calls.append(("list", path))
        if path not in self.dirs:
            raise NotFound(path)
        out = []
        for d in self.dirs:
            if d != "/" and split(d)[0] == path:
                out.append(Entry(split(d)[1], True))
        for f, b in self.files.items():
            if split(f)[0] == path:
                out.append(Entry(split(f)[1], False, len(b), time.time()))
        return out

    def fetch(self, path, entry, ctx, start=None, end=None):
        self.calls.append(("fetch", path, start, end))
        data = self.files[path]
        if start is None:
            return FakeResp(data)
        return FakeResp(data[start:end + 1], 206)

    def upload(self, path, entry, fileobj, size):
        assert split(path)[0] in self.dirs
        data = fileobj.read()
        assert len(data) == size
        self.files[path] = data
        self.calls.append(("upload", path, size))

    def mkdir(self, path):
        if path in self.dirs:
            raise AlreadyExists(path)
        self.dirs.add(path)

    def delete(self, path, entry):
        self.calls.append(("delete", path))
        if entry.is_dir:
            self.dirs.discard(path)
        else:
            del self.files[path]

    def move(self, src, dst, entry, dst_entry):
        self.calls.append(("move", src, dst))
        if entry.is_dir:
            self.dirs.discard(src)
            self.dirs.add(dst)
        else:
            self.files[dst] = self.files.pop(src)

    def quota(self):
        return 10 * 2**30, 2**30


@pytest.fixture
def env(tmp_path):
    p = MemProvider()
    p.dirs.add("/Docs")
    p.files["/Docs/Report.txt"] = b"hello world"
    return p, CloudFS(p, str(tmp_path / "cache"))


def write_file(f, path, data, chunk=3):
    fh = f.create(path, 0o666)
    for i in range(0, len(data), chunk):
        f.write(path, data[i:i + chunk], i, fh)
    f.flush(path, fh)
    f.release(path, fh)


def test_getattr_and_readdir(env):
    p, f = env
    assert f.getattr("/")["st_mode"] & 0o040000
    a = f.getattr("/Docs/Report.txt")
    assert a["st_size"] == 11
    names = [x if isinstance(x, str) else x[0] for x in f.readdir("/Docs", 0)]
    assert names == [".", "..", "Report.txt"]
    with pytest.raises(FuseOSError) as e:
        f.getattr("/Docs/desktop.ini")
    assert e.value.errno == errno.ENOENT


def test_case_insensitive(env):
    p, f = env
    fh = f.open("/docs/REPORT.TXT", os.O_RDONLY)
    assert f.read("/docs/REPORT.TXT", 100, 0, fh) == b"hello world"
    f.release("/docs/REPORT.TXT", fh)
    # к провайдеру ушёл правильный регистр
    assert ("fetch", "/Docs/Report.txt", None, None) in p.calls


def test_listing_is_cached(env):
    p, f = env
    for _ in range(5):
        f.getattr("/Docs/Report.txt")
        try:
            f.getattr("/Docs/Thumbs.db")
        except FuseOSError:
            pass
    assert sum(1 for c in p.calls if c[0] == "list") == 2   # "/" и "/Docs"


def test_create_write_upload(env):
    p, f = env
    data = os.urandom(10_000)
    fh = f.create("/Docs/new.bin", 0o666)
    f.write("/Docs/new.bin", data[:5000], 0, fh)
    # пока файл открыт, он виден в папке и имеет актуальный размер
    names = [x[0] for x in f.readdir("/Docs", 0)[2:]]
    assert "new.bin" in names
    assert f.getattr("/Docs/new.bin")["st_size"] == 5000
    f.write("/Docs/new.bin", data[5000:], 5000, fh)
    f.flush("/Docs/new.bin", fh)
    assert p.files["/Docs/new.bin"] == data
    f.release("/Docs/new.bin", fh)
    assert f.getattr("/Docs/new.bin")["st_size"] == 10_000
    assert sum(1 for c in p.calls if c[0] == "upload") == 1   # повторно не грузим


def test_large_file_range_reads(env, monkeypatch):
    p, f = env
    monkeypatch.setattr(fsmod, "SMALL_FILE", 100)
    monkeypatch.setattr(fsmod, "BLOCK", 64)
    big = bytes(range(256)) * 4
    p.files["/big.dat"] = big
    f.invalidate()
    fh = f.open("/big.dat", os.O_RDONLY)
    assert f.read("/big.dat", 100, 50, fh) == big[50:150]
    assert f.read("/big.dat", 5000, 1000, fh) == big[1000:]
    assert f.read("/big.dat", 10, 5000, fh) == b""
    fetches = [c for c in p.calls if c[0] == "fetch"]
    assert all(c[2] is not None for c in fetches)        # только Range-запросы
    f.release("/big.dat", fh)


def test_modify_existing(env):
    p, f = env
    fh = f.open("/Docs/Report.txt", os.O_RDWR)
    f.write("/Docs/Report.txt", b"HELLO", 0, fh)
    f.release("/Docs/Report.txt", fh)
    assert p.files["/Docs/Report.txt"] == b"HELLO world"


def test_overwrite_via_create_and_truncate(env):
    p, f = env
    write_file(f, "/Docs/Report.txt", b"new")
    assert p.files["/Docs/Report.txt"] == b"new"
    f.truncate("/Docs/Report.txt", 1)
    assert p.files["/Docs/Report.txt"] == b"n"


def test_rename_and_delete(env):
    p, f = env
    f.mkdir("/Archive", 0o777)
    assert "/Archive" in p.dirs
    f.rename("/Docs/Report.txt", "/Archive/Old.txt")
    assert "/Archive/Old.txt" in p.files and "/Docs/Report.txt" not in p.files
    assert f.getattr("/Archive/Old.txt")["st_size"] == 11
    with pytest.raises(FuseOSError):
        f.getattr("/Docs/Report.txt")
    with pytest.raises(FuseOSError) as e:
        f.rmdir("/Archive")
    assert e.value.errno == errno.ENOTEMPTY
    f.unlink("/Archive/Old.txt")
    f.rmdir("/Archive")
    assert "/Archive" not in p.dirs


def test_rename_dir_updates_children(env):
    p, f = env
    f.getattr("/Docs/Report.txt")
    f.rename("/Docs", "/Papers")
    assert "/Papers" in p.dirs
    p.files["/Papers/Report.txt"] = p.files.pop("/Docs/Report.txt")
    assert f.getattr("/Papers/Report.txt")["st_size"] == 11


def test_rename_unuploaded_new_file(env):
    """Проводник: создаёт «Новый текстовый документ.txt» и сразу переименовывает."""
    p, f = env
    fh = f.create("/tmp1.txt", 0o666)
    f.rename("/tmp1.txt", "/final.txt")
    f.write("/final.txt", b"abc", 0, fh)
    f.release("/final.txt", fh)
    assert p.files == {"/Docs/Report.txt": b"hello world", "/final.txt": b"abc"}
    assert not any(c[0] == "move" for c in p.calls)


def test_case_only_rename(env):
    p, f = env
    f.rename("/Docs/Report.txt", "/Docs/report.txt")
    assert "/Docs/report.txt" in p.files


def test_failed_upload_keeps_copy(env, tmp_path):
    p, f = env

    def boom(*a):
        raise RuntimeError("network down")
    p.upload = boom
    fh = f.create("/x.txt", 0o666)
    f.write("/x.txt", b"important", 0, fh)
    with pytest.raises(FuseOSError):
        f.flush("/x.txt", fh)
    f.release("/x.txt", fh)
    failed = tmp_path / "failed_uploads"
    saved = list(failed.iterdir())
    assert len(saved) == 1 and saved[0].read_bytes() == b"important"


def test_statfs(env):
    _, f = env
    s = f.statfs("/")
    assert s["f_blocks"] * s["f_bsize"] == 10 * 2**30
    assert s["f_bavail"] * s["f_bsize"] == 9 * 2**30
