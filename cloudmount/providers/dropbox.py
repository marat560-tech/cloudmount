"""Dropbox API v2."""
from __future__ import annotations

import json
from datetime import datetime

from .base import AlreadyExists, Entry, NotFound, OAuthProvider, ProviderError

API = "https://api.dropboxapi.com/2"
CONTENT = "https://content.dropboxapi.com/2"
SIMPLE_LIMIT = 140 * 1024 * 1024
CHUNK = 32 * 1024 * 1024


def _ts(s):
    if not s:
        return 0.0
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def _arg(obj) -> str:
    # Dropbox-API-Arg должен быть ASCII: json.dumps экранирует не-ASCII как \uXXXX
    return json.dumps(obj, ensure_ascii=True).replace("\x7f", "\\u007f")


class DropboxProvider(OAuthProvider):
    kind = "dropbox"
    title = "Dropbox"
    token_url = "https://api.dropboxapi.com/oauth2/token"
    note = "API v2"
    tint = "#5560C9"
    fields = [("client_id", "App key", False, "")]
    hint = ("Create app → Scoped access → Full Dropbox. В Settings → Redirect URIs\n"
            "добавьте http://127.0.0.1:53682/, в Permissions отметьте\n"
            "files.metadata.read/write и files.content.read/write.")
    links = [("Dropbox App Console", "https://www.dropbox.com/developers/apps")]

    @classmethod
    def login(cls, data, cancel_event=None):
        from ..oauth import browser_login
        return browser_login("https://www.dropbox.com/oauth2/authorize", cls.token_url,
                             data["client_id"], None, pkce=True,
                             params={"token_access_type": "offline"},
                             cancel_event=cancel_event)

    def error_for(self, r):
        if r.status_code == 409:
            try:
                summary = r.json().get("error_summary", "")
            except ValueError:
                summary = r.text[:200]
            if "not_found" in summary:
                return NotFound(summary)
            if "conflict" in summary:
                return AlreadyExists(summary)
            return ProviderError(summary)
        return super().error_for(r)

    @staticmethod
    def _p(path):
        p = "/" + path.strip("/")
        return "" if p == "/" else p

    def _rpc(self, endpoint, args):
        body = b"null" if args is None else json.dumps(args).encode()
        return self.request("POST", API + endpoint, data=body,
                            headers={"Content-Type": "application/json"}).json()

    @staticmethod
    def _entry(md):
        is_dir = md.get(".tag") == "folder"
        return Entry(md["name"], is_dir, 0 if is_dir else int(md.get("size") or 0),
                     _ts(md.get("server_modified")), id=md.get("id"))

    def list_dir(self, path):
        js = self._rpc("/files/list_folder", {"path": self._p(path), "limit": 2000})
        out = [self._entry(m) for m in js["entries"] if m.get(".tag") != "deleted"]
        while js.get("has_more"):
            js = self._rpc("/files/list_folder/continue", {"cursor": js["cursor"]})
            out += [self._entry(m) for m in js["entries"] if m.get(".tag") != "deleted"]
        return out

    def fetch(self, path, entry, ctx, start=None, end=None):
        headers = {"Dropbox-API-Arg": _arg({"path": self._p(path)})}
        if start is not None:
            headers["Range"] = f"bytes={start}-{'' if end is None else end}"
        return self.request("POST", CONTENT + "/files/download", headers=headers, stream=True)

    def upload(self, path, entry, fileobj, size):
        commit = {"path": self._p(path), "mode": "overwrite", "mute": True}
        octet = {"Content-Type": "application/octet-stream"}
        if size <= SIMPLE_LIMIT:
            self.request("POST", CONTENT + "/files/upload", data=fileobj, rewind=fileobj,
                         headers={**octet, "Dropbox-API-Arg": _arg(commit)})
            return None
        fileobj.seek(0)
        chunk = fileobj.read(CHUNK)
        sid = self.request("POST", CONTENT + "/files/upload_session/start", data=chunk,
                           headers={**octet, "Dropbox-API-Arg": _arg({"close": False})}
                           ).json()["session_id"]
        offset = len(chunk)
        while True:
            chunk = fileobj.read(CHUNK)
            if not chunk:
                break
            self.request("POST", CONTENT + "/files/upload_session/append_v2", data=chunk,
                         headers={**octet, "Dropbox-API-Arg": _arg(
                             {"cursor": {"session_id": sid, "offset": offset}})})
            offset += len(chunk)
        self.request("POST", CONTENT + "/files/upload_session/finish", data=b"",
                     headers={**octet, "Dropbox-API-Arg": _arg(
                         {"cursor": {"session_id": sid, "offset": offset}, "commit": commit})})
        return None

    def mkdir(self, path):
        self._rpc("/files/create_folder_v2", {"path": self._p(path), "autorename": False})

    def delete(self, path, entry):
        self._rpc("/files/delete_v2", {"path": self._p(path)})

    def move(self, src, dst, entry, dst_entry):
        if dst_entry is not None and src.lower() != dst.lower():
            self.delete(dst, dst_entry)
        self._rpc("/files/move_v2", {"from_path": self._p(src), "to_path": self._p(dst),
                                     "autorename": False})

    def quota(self):
        js = self._rpc("/users/get_space_usage", None)
        alloc = js.get("allocation") or {}
        return int(alloc.get("allocated") or 0), int(js.get("used") or 0)
