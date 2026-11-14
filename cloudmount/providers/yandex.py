"""Яндекс Диск через REST API (быстрее, чем их WebDAV, который режет скорость)."""
from __future__ import annotations

import time
from datetime import datetime

from .base import (AlreadyExists, Entry, NotFound, OAuthProvider, ProviderError)

API = "https://cloud-api.yandex.net/v1/disk"


def _ts(s: str | None) -> float:
    if not s:
        return 0.0
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


class YandexProvider(OAuthProvider):
    kind = "yandex"
    title = "Яндекс Диск"
    token_url = "https://oauth.yandex.ru/token"
    auth_scheme = "OAuth"
    fields = [
        ("client_id", "ClientID приложения", False, ""),
        ("client_secret", "Client secret", True, ""),
    ]

    @classmethod
    def login(cls, data, cancel_event=None):
        from ..oauth import browser_login
        return browser_login("https://oauth.yandex.ru/authorize", cls.token_url,
                             data["client_id"], data.get("client_secret"),
                             params={"force_confirm": "yes"}, cancel_event=cancel_event)

    def error_for(self, r):
        try:
            err = r.json().get("error", "")
            desc = r.json().get("description", "")
        except ValueError:
            err, desc = "", r.text[:200]
        if r.status_code == 404 or "DoesntExist" in err or "NotFound" in err:
            return NotFound(desc or err)
        if r.status_code == 409 and "Exist" in err:
            return AlreadyExists(desc or err)
        return super().error_for(r) if not err else ProviderError(f"{r.status_code} {err}: {desc}")

    @staticmethod
    def _p(path: str) -> str:
        return "disk:" + ("/" + path.strip("/"))

    def _wait(self, r):
        """Асинхронные операции (папки) возвращают 202 и ссылку на статус."""
        if r.status_code != 202:
            return
        href = r.json().get("href")
        if not href:
            return
        for _ in range(600):
            st = self.request("GET", href).json().get("status")
            if st == "success":
                return
            if st == "failed":
                raise ProviderError("Операция на Яндекс Диске завершилась ошибкой")
            time.sleep(1)

    def list_dir(self, path):
        out, offset, limit = [], 0, 1000
        fields = ("type,_embedded.total,_embedded.items.name,_embedded.items.type,"
                  "_embedded.items.size,_embedded.items.modified")
        while True:
            js = self.request("GET", API + "/resources", params={
                "path": self._p(path), "limit": limit, "offset": offset,
                "fields": fields}).json()
            emb = js.get("_embedded") or {}
            items = emb.get("items") or []
            for it in items:
                out.append(Entry(it["name"], it.get("type") == "dir",
                                 int(it.get("size") or 0), _ts(it.get("modified"))))
            offset += len(items)
            if not items or offset >= int(emb.get("total") or 0):
                return out

    def _href(self, path, ctx, force=False):
        h = ctx.get("href")
        if force or not h or time.time() - ctx.get("href_t", 0) > 3600:
            h = self.request("GET", API + "/resources/download",
                             params={"path": self._p(path)}).json()["href"]
            ctx["href"], ctx["href_t"] = h, time.time()
        return h

    def fetch(self, path, entry, ctx, start=None, end=None):
        headers = {}
        if start is not None:
            headers["Range"] = f"bytes={start}-{'' if end is None else end}"
        try:
            return self.request("GET", self._href(path, ctx), auth=False,
                                headers=headers, stream=True)
        except (NotFound, ProviderError):
            # ссылка на скачивание могла протухнуть
            return self.request("GET", self._href(path, ctx, force=True), auth=False,
                                headers=headers, stream=True)

    def upload(self, path, entry, fileobj, size):
        href = self.request("GET", API + "/resources/upload", params={
            "path": self._p(path), "overwrite": "true"}).json()["href"]
        self.request("PUT", href, auth=False, data=fileobj, rewind=fileobj,
                     headers={"Content-Length": str(size)})
        return None

    def mkdir(self, path):
        self.request("PUT", API + "/resources", params={"path": self._p(path)})

    def delete(self, path, entry):
        # permanently=false — в корзину Яндекс Диска, можно восстановить
        r = self.request("DELETE", API + "/resources",
                         params={"path": self._p(path), "permanently": "false"})
        self._wait(r)

    def move(self, src, dst, entry, dst_entry):
        r = self.request("POST", API + "/resources/move", params={
            "from": self._p(src), "path": self._p(dst), "overwrite": "true"})
        self._wait(r)

    def quota(self):
        js = self.request("GET", API, params={"fields": "total_space,used_space"}).json()
        return int(js.get("total_space") or 0), int(js.get("used_space") or 0)
