"""Google Drive API v3.

Google Drive работает по идентификаторам, а не путям, поэтому держим кэш путь -> id.
Документы Google (Docs/Sheets/Slides) не имеют бинарного содержимого и не показываются.
"""
from __future__ import annotations

import json
import threading
from datetime import datetime

from .base import AlreadyExists, Entry, NotFound, OAuthProvider, ProviderError, join, split

API = "https://www.googleapis.com/drive/v3"
UPLOAD = "https://www.googleapis.com/upload/drive/v3"
FOLDER = "application/vnd.google-apps.folder"
FIELDS = "id,name,mimeType,size,modifiedTime"


def _ts(s):
    if not s:
        return 0.0
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


class GDriveProvider(OAuthProvider):
    kind = "gdrive"
    title = "Google Drive"
    token_url = "https://oauth2.googleapis.com/token"
    fields = [
        ("client_id", "Client ID", False, ""),
        ("client_secret", "Client secret", True, ""),
    ]

    def __init__(self, data, save=None):
        super().__init__(data, save)
        self._ids: dict[str, str] = {"/": "root"}
        self._ids_lock = threading.Lock()

    @classmethod
    def login(cls, data, cancel_event=None):
        from ..oauth import browser_login
        return browser_login("https://accounts.google.com/o/oauth2/v2/auth", cls.token_url,
                             data["client_id"], data.get("client_secret"), pkce=True,
                             params={"scope": "https://www.googleapis.com/auth/drive",
                                     "access_type": "offline", "prompt": "consent"},
                             cancel_event=cancel_event)

    @staticmethod
    def _norm(path):
        return "/" + path.strip("/")

    def _key(self, path):
        return self._norm(path).lower()

    def _forget(self, path):
        k = self._key(path)
        with self._ids_lock:
            for p in [p for p in self._ids if p == k or p.startswith(k + "/")]:
                if p != "/":
                    del self._ids[p]

    def _id(self, path: str) -> str:
        k = self._key(path)
        with self._ids_lock:
            if k in self._ids:
                return self._ids[k]
        parent, name = split(path)
        if not name:
            return "root"
        self.list_dir(parent)       # заполнит кэш детей
        with self._ids_lock:
            if k in self._ids:
                return self._ids[k]
        raise NotFound(path)

    def list_dir(self, path):
        pid = self._id(path)
        out, token = [], None
        children = {}
        while True:
            params = {"q": f"'{pid}' in parents and trashed=false",
                      "fields": f"nextPageToken,files({FIELDS})", "pageSize": 1000,
                      "spaces": "drive"}
            if token:
                params["pageToken"] = token
            js = self.request("GET", API + "/files", params=params).json()
            for f in js.get("files", []):
                mt = f.get("mimeType", "")
                is_dir = mt == FOLDER
                if mt.startswith("application/vnd.google-apps.") and not is_dir:
                    continue
                if f["name"].lower() in children:   # одинаковые имена — берём первое
                    continue
                e = Entry(f["name"], is_dir, int(f.get("size") or 0),
                          _ts(f.get("modifiedTime")), id=f["id"])
                children[f["name"].lower()] = e
                out.append(e)
            token = js.get("nextPageToken")
            if not token:
                break
        base = self._key(path)
        with self._ids_lock:
            for low, e in children.items():
                self._ids[(base.rstrip("/") + "/" + low)] = e.id
        return out

    def _fid(self, path, entry):
        return entry.id if entry is not None and entry.id else self._id(path)

    def fetch(self, path, entry, ctx, start=None, end=None):
        headers = {}
        if start is not None:
            headers["Range"] = f"bytes={start}-{'' if end is None else end}"
        return self.request("GET", f"{API}/files/{self._fid(path, entry)}",
                            params={"alt": "media"}, headers=headers, stream=True)

    def upload(self, path, entry, fileobj, size):
        parent, name = split(path)
        fid = None
        if entry is not None and entry.id:
            fid = entry.id
        else:
            try:
                fid = self._id(path)
            except NotFound:
                fid = None
        jhead = {"Content-Type": "application/json; charset=UTF-8",
                 "X-Upload-Content-Length": str(size)}
        if fid:
            r = self.request("PATCH", f"{UPLOAD}/files/{fid}",
                             params={"uploadType": "resumable", "fields": FIELDS},
                             data=b"{}", headers=jhead)
        else:
            meta = {"name": name, "parents": [self._id(parent)]}
            r = self.request("POST", f"{UPLOAD}/files",
                             params={"uploadType": "resumable", "fields": FIELDS},
                             data=json.dumps(meta).encode(), headers=jhead)
        loc = r.headers["Location"]
        js = self.request("PUT", loc, data=fileobj, rewind=fileobj,
                          headers={"Content-Length": str(size)}).json()
        with self._ids_lock:
            self._ids[self._key(path)] = js["id"]
        return Entry(js["name"], False, int(js.get("size") or size),
                     _ts(js.get("modifiedTime")), id=js["id"])

    def mkdir(self, path):
        parent, name = split(path)
        pid = self._id(parent)
        for e in self.list_dir(parent):
            if e.name.lower() == name.lower():
                raise AlreadyExists(path)
        js = self.request("POST", API + "/files", params={"fields": FIELDS},
                          data=json.dumps({"name": name, "mimeType": FOLDER,
                                           "parents": [pid]}).encode(),
                          headers={"Content-Type": "application/json"}).json()
        with self._ids_lock:
            self._ids[self._key(path)] = js["id"]

    def delete(self, path, entry):
        fid = self._fid(path, entry)
        # в корзину Google Drive, а не навсегда
        self.request("PATCH", f"{API}/files/{fid}", data=b'{"trashed": true}',
                     headers={"Content-Type": "application/json"})
        self._forget(path)

    def move(self, src, dst, entry, dst_entry):
        fid = self._fid(src, entry)
        sp, _ = split(src)
        dp, dname = split(dst)
        if dst_entry is not None and src.lower() != dst.lower():
            self.delete(dst, dst_entry)
        params = {"fields": FIELDS}
        if self._key(sp) != self._key(dp):
            params["addParents"] = self._id(dp)
            params["removeParents"] = self._id(sp)
        self.request("PATCH", f"{API}/files/{fid}", params=params,
                     data=json.dumps({"name": dname}).encode(),
                     headers={"Content-Type": "application/json"})
        self._forget(src)
        with self._ids_lock:
            self._ids[self._key(dst)] = fid

    def quota(self):
        js = self.request("GET", API + "/about", params={"fields": "storageQuota"}).json()
        q = js.get("storageQuota") or {}
        return int(q.get("limit") or 0), int(q.get("usage") or 0)


__all__ = ["GDriveProvider", "join"]
