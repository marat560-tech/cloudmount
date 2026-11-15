"""Box через официальный API. Box работает по идентификаторам — держим кэш путь → id."""
from __future__ import annotations

import base64
import hashlib
import json
import threading
import uuid
from datetime import datetime

from .base import AlreadyExists, Entry, NotFound, OAuthProvider, split

API = "https://api.box.com/2.0"
UPLOAD = "https://upload.box.com/api/2.0"
SIMPLE_LIMIT = 50 * 1024 * 1024


def _ts(s):
    try:
        return datetime.fromisoformat((s or "").replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


class MultipartReader:
    """multipart/form-data, который читает файл потоком (без загрузки в память)."""

    def __init__(self, attributes: dict, fileobj, size: int):
        self.boundary = "cm" + uuid.uuid4().hex
        b = self.boundary
        self.head = (f"--{b}\r\nContent-Disposition: form-data; name=\"attributes\"\r\n\r\n"
                     f"{json.dumps(attributes)}\r\n--{b}\r\nContent-Disposition: form-data; "
                     f"name=\"file\"; filename=\"file\"\r\nContent-Type: application/octet-stream"
                     f"\r\n\r\n").encode()
        self.tail = f"\r\n--{b}--\r\n".encode()
        self.f, self.size = fileobj, size
        self.seek(0)

    @property
    def content_type(self):
        return f"multipart/form-data; boundary={self.boundary}"

    def __len__(self):
        return len(self.head) + self.size + len(self.tail)

    def seek(self, pos, whence=0):
        self.stage, self.off = 0, 0
        self.f.seek(0)
        return 0

    def tell(self):
        return 0

    def read(self, n=-1):
        n = 1 << 20 if n is None or n < 0 else n
        out = b""
        while len(out) < n and self.stage < 3:
            if self.stage == 0:
                part = self.head[self.off:self.off + n - len(out)]
                self.off += len(part)
                if self.off >= len(self.head):
                    self.stage, self.off = 1, 0
            elif self.stage == 1:
                part = self.f.read(n - len(out))
                if not part:
                    self.stage = 2
            else:
                part = self.tail[self.off:self.off + n - len(out)]
                self.off += len(part)
                if self.off >= len(self.tail):
                    self.stage = 3
            out += part
        return out

    def __iter__(self):
        while True:
            b = self.read(1 << 20)
            if not b:
                return
            yield b


class BoxProvider(OAuthProvider):
    kind = "box"
    title = "Box"
    note = "официальный API"
    tint = "#3567B5"
    token_url = "https://api.box.com/oauth2/token"
    redirect_host = "localhost"
    fields = [
        ("client_id", "Client ID", False, ""),
        ("client_secret", "Client secret", True, ""),
    ]
    hint = ("Создайте приложение в Box Developer Console: Custom App →\n"
            "User Authentication (OAuth 2.0). Redirect URI: http://localhost:53682/,\n"
            "права: Read and write all files and folders.")
    links = [("Box Developer Console", "https://app.box.com/developers/console")]

    def __init__(self, data, save=None):
        super().__init__(data, save)
        self._ids: dict[str, str] = {"/": "0"}
        self._ids_lock = threading.Lock()

    @classmethod
    def login(cls, data, cancel_event=None):
        from ..oauth import browser_login
        return browser_login("https://account.box.com/api/oauth2/authorize", cls.token_url,
                             data["client_id"], data.get("client_secret"),
                             cancel_event=cancel_event, redirect_host=cls.redirect_host)

    def error_for(self, r):
        if r.status_code == 409:
            return AlreadyExists(r.text[:200])
        return super().error_for(r)

    @staticmethod
    def _key(path):
        return ("/" + path.strip("/")).lower()

    def _forget(self, path):
        k = self._key(path)
        with self._ids_lock:
            for p in [p for p in self._ids if p != "/" and (p == k or p.startswith(k + "/"))]:
                del self._ids[p]

    def _id(self, path):
        k = self._key(path)
        with self._ids_lock:
            if k in self._ids:
                return self._ids[k]
        parent, name = split(path)
        if not name:
            return "0"
        self.list_dir(parent)
        with self._ids_lock:
            if k in self._ids:
                return self._ids[k]
        raise NotFound(path)

    def _fid(self, path, entry):
        return entry.id if entry is not None and entry.id else self._id(path)

    def list_dir(self, path):
        fid = self._id(path)
        out, offset, seen = [], 0, {}
        while True:
            js = self.request("GET", f"{API}/folders/{fid}/items", params={
                "fields": "id,type,name,size,modified_at", "limit": 1000, "offset": offset}).json()
            items = js.get("entries", [])
            for it in items:
                if it.get("type") not in ("file", "folder") or it["name"].lower() in seen:
                    continue
                d = it["type"] == "folder"
                e = Entry(it["name"], d, 0 if d else int(it.get("size") or 0),
                          _ts(it.get("modified_at")), id=it["id"])
                seen[it["name"].lower()] = e
                out.append(e)
            offset += len(items)
            if not items or offset >= int(js.get("total_count") or 0):
                break
        base = self._key(path).rstrip("/")
        with self._ids_lock:
            for low, e in seen.items():
                self._ids[base + "/" + low] = e.id
        return out

    def fetch(self, path, entry, ctx, start=None, end=None):
        headers = {}
        if start is not None:
            headers["Range"] = f"bytes={start}-{'' if end is None else end}"
        return self.request("GET", f"{API}/files/{self._fid(path, entry)}/content",
                            headers=headers, stream=True)

    def upload(self, path, entry, fileobj, size):
        parent, name = split(path)
        fid = entry.id if entry is not None and entry.id else None
        if fid is None:
            try:
                fid = self._id(path)
            except NotFound:
                fid = None
        if size < SIMPLE_LIMIT:
            attrs = {"name": name} if fid else {"name": name, "parent": {"id": self._id(parent)}}
            body = MultipartReader(attrs, fileobj, size)
            url = f"{UPLOAD}/files/{fid}/content" if fid else f"{UPLOAD}/files/content"
            js = self.request("POST", url, data=body, rewind=body,
                              headers={"Content-Type": body.content_type,
                                       "Content-Length": str(len(body))}).json()
        else:
            js = self._chunked(fid, parent, name, fileobj, size)
        it = (js.get("entries") or [js])[0]
        with self._ids_lock:
            self._ids[self._key(path)] = it["id"]
        return Entry(it.get("name", name), False, int(it.get("size") or size),
                     _ts(it.get("modified_at")), id=it["id"])

    def _chunked(self, fid, parent, name, fileobj, size):
        if fid:
            sess = self.request("POST", f"{UPLOAD}/files/{fid}/upload_sessions",
                                json={"file_size": size}).json()
        else:
            sess = self.request("POST", f"{UPLOAD}/files/upload_sessions",
                                json={"folder_id": self._id(parent), "file_size": size,
                                      "file_name": name}).json()
        part_size = int(sess["part_size"])
        ends = sess["session_endpoints"]
        whole = hashlib.sha1()
        parts, pos = [], 0
        fileobj.seek(0)
        while pos < size:
            chunk = fileobj.read(part_size)
            if not chunk:
                break
            whole.update(chunk)
            digest = base64.b64encode(hashlib.sha1(chunk).digest()).decode()
            end = pos + len(chunk) - 1
            r = self.request("PUT", ends["upload_part"], data=chunk, headers={
                "Content-Type": "application/octet-stream", "Digest": f"sha={digest}",
                "Content-Range": f"bytes {pos}-{end}/{size}"})
            parts.append(r.json()["part"])
            pos = end + 1
        digest = base64.b64encode(whole.digest()).decode()
        return self.request("POST", ends["commit"], json={"parts": parts},
                            headers={"Digest": f"sha={digest}"}).json()

    def mkdir(self, path):
        parent, name = split(path)
        js = self.request("POST", f"{API}/folders",
                          json={"name": name, "parent": {"id": self._id(parent)}}).json()
        with self._ids_lock:
            self._ids[self._key(path)] = js["id"]

    def delete(self, path, entry):
        fid = self._fid(path, entry)
        if entry.is_dir:
            self.request("DELETE", f"{API}/folders/{fid}", params={"recursive": "true"})
        else:
            self.request("DELETE", f"{API}/files/{fid}")        # в корзину Box
        self._forget(path)

    def move(self, src, dst, entry, dst_entry):
        fid = self._fid(src, entry)
        if dst_entry is not None and src.lower() != dst.lower():
            self.delete(dst, dst_entry)
        dparent, dname = split(dst)
        kind = "folders" if entry.is_dir else "files"
        self.request("PUT", f"{API}/{kind}/{fid}",
                     json={"name": dname, "parent": {"id": self._id(dparent)}})
        self._forget(src)
        with self._ids_lock:
            self._ids[self._key(dst)] = fid

    def quota(self):
        js = self.request("GET", f"{API}/users/me",
                          params={"fields": "space_amount,space_used"}).json()
        return int(js.get("space_amount") or 0), int(js.get("space_used") or 0)
