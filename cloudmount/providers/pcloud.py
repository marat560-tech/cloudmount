"""pCloud через официальный API (серверы в США и Европе)."""
from __future__ import annotations

import email.utils
import time

from .base import AlreadyExists, AuthError, Entry, HttpProvider, NotFound, ProviderError, split

CHUNK = 16 * 1024 * 1024
NOT_FOUND = {2002, 2005, 2009, 2010}
EXISTS = {2004}
AUTH = {1000, 2000, 2094, 2095}


def _ts(s):
    try:
        return email.utils.parsedate_to_datetime(s).timestamp()
    except (TypeError, ValueError):
        return 0.0


class PCloudProvider(HttpProvider):
    kind = "pcloud"
    title = "pCloud"
    note = "официальный API"
    tint = "#2A8C9E"
    oauth = True
    fields = [
        ("client_id", "Client ID", False, ""),
        ("client_secret", "Client secret", True, ""),
    ]
    hint = ("Создайте приложение в pCloud: My applications → New app,\n"
            "Redirect URI: http://127.0.0.1:53682/, права: доступ ко всем файлам.\n"
            "Скопируйте Client ID и Client secret.")
    links = [("Приложения pCloud", "https://docs.pcloud.com/my_apps/")]

    @classmethod
    def login(cls, data, cancel_event=None):
        from ..oauth import browser_login

        def token_url(params):
            host = params.get("hostname") or "api.pcloud.com"
            return f"https://{host}/oauth2_token"

        def parse(js):
            if js.get("result", 0) != 0 or "access_token" not in js:
                raise RuntimeError(f"pCloud: {js.get('error', js)}")
            return {"access_token": js["access_token"],
                    "hostname": "eapi.pcloud.com" if js.get("locationid") == 2 else "api.pcloud.com"}
        return browser_login("https://my.pcloud.com/oauth2/authorize", token_url,
                             data["client_id"], data.get("client_secret"),
                             token_in_query=True, parse_token=parse, cancel_event=cancel_event)

    @property
    def api(self) -> str:
        return "https://" + ((self.data.get("token") or {}).get("hostname") or "api.pcloud.com")

    def _call(self, method, http="GET", data=None, **params):
        tok = (self.data.get("token") or {}).get("access_token")
        if not tok:
            raise AuthError("Нет токена — войдите в аккаунт в настройках")
        params["access_token"] = tok
        js = self.request(http, f"{self.api}/{method}", params=params, data=data).json()
        res = js.get("result", 0)
        if res == 0:
            return js
        msg = js.get("error", f"код {res}")
        if res in NOT_FOUND:
            raise NotFound(msg)
        if res in EXISTS:
            raise AlreadyExists(msg)
        if res in AUTH:
            raise AuthError(f"pCloud: {msg}")
        raise ProviderError(f"pCloud: {msg}")

    @staticmethod
    def _p(path):
        return "/" + path.strip("/")

    def list_dir(self, path):
        js = self._call("listfolder", path=self._p(path))
        out = []
        for it in (js.get("metadata") or {}).get("contents", []):
            d = bool(it.get("isfolder"))
            out.append(Entry(it["name"], d, 0 if d else int(it.get("size") or 0),
                             _ts(it.get("modified")),
                             id=str(it.get("folderid") if d else it.get("fileid"))))
        return out

    def _link(self, path, ctx, force=False):
        if force or not ctx.get("link") or time.time() - ctx.get("link_t", 0) > 3000:
            js = self._call("getfilelink", path=self._p(path))
            ctx["link"] = "https://" + js["hosts"][0] + js["path"]
            ctx["link_t"] = time.time()
        return ctx["link"]

    def fetch(self, path, entry, ctx, start=None, end=None):
        headers = {}
        if start is not None:
            headers["Range"] = f"bytes={start}-{'' if end is None else end}"
        try:
            return self.request("GET", self._link(path, ctx), auth=False, headers=headers, stream=True)
        except ProviderError:
            return self.request("GET", self._link(path, ctx, True), auth=False,
                                headers=headers, stream=True)

    def upload(self, path, entry, fileobj, size):
        parent, name = split(path)
        uid = self._call("upload_create")["uploadid"]
        fileobj.seek(0)
        offset = 0
        while True:
            chunk = fileobj.read(CHUNK)
            if not chunk:
                break
            self._call("upload_write", http="PUT", data=chunk,
                       uploadid=uid, uploadoffset=offset)
            offset += len(chunk)
        self._call("upload_save", uploadid=uid, path=self._p(parent), name=name)
        return None

    def mkdir(self, path):
        self._call("createfolder", path=self._p(path))

    def delete(self, path, entry):
        if entry.is_dir:
            self._call("deletefolderrecursive", path=self._p(path))
        else:
            self._call("deletefile", path=self._p(path))

    def move(self, src, dst, entry, dst_entry):
        if dst_entry is not None and src.lower() != dst.lower():
            self.delete(dst, dst_entry)
        if entry.is_dir:
            self._call("renamefolder", path=self._p(src), topath=self._p(dst))
        else:
            self._call("renamefile", path=self._p(src), topath=self._p(dst))

    def quota(self):
        js = self._call("userinfo")
        return int(js.get("quota") or 0), int(js.get("usedquota") or 0)
