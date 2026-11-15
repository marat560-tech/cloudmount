"""OneDrive через Microsoft Graph."""
from __future__ import annotations

import urllib.parse
from datetime import datetime

from .base import AlreadyExists, Entry, NotFound, OAuthProvider, split

API = "https://graph.microsoft.com/v1.0/me/drive"
AUTH = "https://login.microsoftonline.com/common/oauth2/v2.0"
SCOPE = "Files.ReadWrite offline_access"
SIMPLE_LIMIT = 4 * 1024 * 1024
CHUNK = 320 * 1024 * 32          # 10 МиБ, кратно 320 КиБ — требование Graph


def _ts(s):
    try:
        return datetime.fromisoformat((s or "").replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


class OneDriveProvider(OAuthProvider):
    kind = "onedrive"
    title = "OneDrive"
    note = "Microsoft Graph"
    tint = "#2C6EBF"
    token_url = AUTH + "/token"
    redirect_host = "localhost"
    fields = [("client_id", "ID приложения (клиента)", False, "")]
    hint = ("Зарегистрируйте приложение в Microsoft Entra: «Регистрация приложения» →\n"
            "типы учётных записей «…и личные учётные записи Майкрософт»,\n"
            "платформа «Мобильные и классические приложения»,\n"
            "URI перенаправления: http://localhost:53682/\n"
            "Скопируйте «Идентификатор приложения (клиента)».")
    links = [("Регистрация приложения в Microsoft Entra",
              "https://entra.microsoft.com/#view/Microsoft_AAD_RegisteredApps/ApplicationsListBlade")]

    @classmethod
    def login(cls, data, cancel_event=None):
        from ..oauth import browser_login
        return browser_login(AUTH + "/authorize", cls.token_url, data["client_id"], None,
                             pkce=True, params={"scope": SCOPE, "prompt": "select_account"},
                             cancel_event=cancel_event, redirect_host=cls.redirect_host)

    def error_for(self, r):
        if r.status_code == 409:
            return AlreadyExists(r.text[:200])
        return super().error_for(r)

    @staticmethod
    def _item(path: str) -> str:
        p = "/" + path.strip("/")
        if p == "/":
            return API + "/root"
        return API + "/root:" + urllib.parse.quote(p) + ":"

    @staticmethod
    def _entry(it) -> Entry:
        is_dir = "folder" in it
        return Entry(it["name"], is_dir, 0 if is_dir else int(it.get("size") or 0),
                     _ts(it.get("lastModifiedDateTime")), id=it.get("id"))

    def list_dir(self, path):
        url = self._item(path) + "/children"
        params = {"$top": 1000, "$select": "id,name,size,folder,file,lastModifiedDateTime"}
        out = []
        while url:
            js = self.request("GET", url, params=params).json()
            out += [self._entry(it) for it in js.get("value", [])
                    if "folder" in it or "file" in it]
            url, params = js.get("@odata.nextLink"), None
        return out

    def fetch(self, path, entry, ctx, start=None, end=None):
        headers = {}
        if start is not None:
            headers["Range"] = f"bytes={start}-{'' if end is None else end}"
        return self.request("GET", self._item(path) + "/content", headers=headers, stream=True)

    def upload(self, path, entry, fileobj, size):
        if size <= SIMPLE_LIMIT:
            js = self.request("PUT", self._item(path) + "/content", data=fileobj, rewind=fileobj,
                              headers={"Content-Type": "application/octet-stream",
                                       "Content-Length": str(size)}).json()
            return self._entry(js)
        sess = self.request("POST", self._item(path) + "/createUploadSession",
                            json={"item": {"@microsoft.graph.conflictBehavior": "replace"}}).json()
        url = sess["uploadUrl"]
        fileobj.seek(0)
        pos, js = 0, {}
        try:
            while pos < size:
                chunk = fileobj.read(CHUNK)
                if not chunk:
                    break
                end = pos + len(chunk) - 1
                r = self.request("PUT", url, auth=False, data=chunk,
                                 headers={"Content-Range": f"bytes {pos}-{end}/{size}",
                                          "Content-Length": str(len(chunk))})
                pos = end + 1
                if r.status_code in (200, 201):
                    js = r.json()
        except Exception:
            try:
                self.request("DELETE", url, auth=False)
            except Exception:  # noqa: BLE001
                pass
            raise
        return self._entry(js) if js.get("name") else None

    def mkdir(self, path):
        parent, name = split(path)
        self.request("POST", self._item(parent) + "/children",
                     json={"name": name, "folder": {},
                           "@microsoft.graph.conflictBehavior": "fail"})

    def delete(self, path, entry):
        self.request("DELETE", self._item(path))       # в корзину OneDrive

    def move(self, src, dst, entry, dst_entry):
        if dst_entry is not None and src.lower() != dst.lower():
            self.delete(dst, dst_entry)
        dparent, dname = split(dst)
        ppath = "/drive/root:" + ("" if dparent == "/" else dparent)
        self.request("PATCH", self._item(src),
                     json={"name": dname, "parentReference": {"path": ppath}})

    def quota(self):
        q = self.request("GET", API, params={"$select": "quota"}).json().get("quota") or {}
        return int(q.get("total") or 0), int(q.get("used") or 0)


__all__ = ["OneDriveProvider", "NotFound"]
