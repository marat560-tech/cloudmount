"""S3-совместимые хранилища (Yandex Object Storage, VK Cloud, Selectel, Backblaze B2,
Cloudflare R2, MinIO…). Подпись AWS Signature V4 без сторонних библиотек."""
from __future__ import annotations

import hashlib
import hmac
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

from .base import AuthError, Entry, HttpProvider, NotFound, ProviderError

EMPTY_SHA = hashlib.sha256(b"").hexdigest()
UNSIGNED = "UNSIGNED-PAYLOAD"
SIMPLE_LIMIT = 64 * 1024 * 1024
PART = 16 * 1024 * 1024


def _q(s: str, safe: str = "") -> str:
    return urllib.parse.quote(s, safe="~" + safe)


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _ts(s):
    try:
        return datetime.fromisoformat((s or "").replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


class S3Provider(HttpProvider):
    kind = "s3"
    title = "S3-хранилище"
    note = "Yandex, VK, Selectel, B2, R2"
    tint = "#8A5A2B"
    icon = "bucket"
    fields = [
        ("endpoint", "Адрес (endpoint)", False, "https://storage.yandexcloud.net"),
        ("region", "Регион", False, "ru-central1"),
        ("bucket", "Бакет", False, ""),
        ("access_key", "Access key ID", False, ""),
        ("secret_key", "Secret access key", True, ""),
        ("prefix", "Папка в бакете (необязательно)", False, ""),
    ]
    optional = {"prefix"}
    hint = ("Адреса и регионы:\n"
            "Yandex Object Storage — https://storage.yandexcloud.net, ru-central1\n"
            "VK Cloud — https://hb.ru-msk.vkcloud-solutions.ru, ru-msk\n"
            "Selectel — https://s3.ru-1.storage.selcloud.ru, ru-1\n"
            "Cloudflare R2 — https://<ID аккаунта>.r2.cloudflarestorage.com, auto\n"
            "Ключи доступа создаются в консоли хранилища. Размер диска задайте вручную.")
    links = [("Ключи Yandex Object Storage",
              "https://yandex.cloud/ru/docs/iam/operations/authentication/manage-access-keys")]

    def __init__(self, data, save=None):
        super().__init__(data, save)
        self.endpoint = (data.get("endpoint") or "").rstrip("/")
        if not self.endpoint.startswith("http"):
            self.endpoint = "https://" + self.endpoint
        self.host = urllib.parse.urlparse(self.endpoint).netloc
        self.region = data.get("region") or "us-east-1"
        self.bucket = data.get("bucket") or ""
        self.base = (data.get("prefix") or "").strip("/")
        if not self.bucket:
            raise ProviderError("Не указан бакет")

    # ---------- подпись ----------
    def _signed(self, method: str, key: str, query: dict | None = None,
                headers: dict | None = None, payload: str = EMPTY_SHA) -> tuple[str, dict]:
        now = datetime.now(timezone.utc)
        amzdate, date = now.strftime("%Y%m%dT%H%M%SZ"), now.strftime("%Y%m%d")
        path = "/" + self.bucket + ("/" + key if key else "")
        canon_uri = _q(path, "/")
        canon_query = "&".join(f"{_q(k)}={_q(str(v))}" for k, v in sorted((query or {}).items()))
        h = {k.lower(): str(v).strip() for k, v in (headers or {}).items()}
        h.update({"host": self.host, "x-amz-date": amzdate, "x-amz-content-sha256": payload})
        names = sorted(h)
        canon_headers = "".join(f"{n}:{h[n]}\n" for n in names)
        signed_headers = ";".join(names)
        creq = "\n".join([method, canon_uri, canon_query, canon_headers, signed_headers, payload])
        scope = f"{date}/{self.region}/s3/aws4_request"
        sts = "\n".join(["AWS4-HMAC-SHA256", amzdate, scope,
                         hashlib.sha256(creq.encode()).hexdigest()])

        def hm(k, m):
            return hmac.new(k, m.encode(), hashlib.sha256).digest()
        k = hm(("AWS4" + self.data.get("secret_key", "")).encode(), date)
        k = hm(hm(hm(k, self.region), "s3"), "aws4_request")
        sig = hmac.new(k, sts.encode(), hashlib.sha256).hexdigest()
        h["authorization"] = (f"AWS4-HMAC-SHA256 Credential={self.data.get('access_key', '')}/"
                              f"{scope}, SignedHeaders={signed_headers}, Signature={sig}")
        del h["host"]
        url = self.endpoint + canon_uri + ("?" + canon_query if canon_query else "")
        return url, h

    def _s3(self, method, key, query=None, headers=None, data=None, payload=EMPTY_SHA, **kw):
        url, h = self._signed(method, key, query, headers, payload)
        return self.request(method, url, auth=False, headers=h, data=data, **kw)

    def error_for(self, r):
        code = ""
        try:
            code = ET.fromstring(r.content).findtext("Code") or ""
        except ET.ParseError:
            pass
        if r.status_code == 404 or code in ("NoSuchKey", "NoSuchBucket"):
            return NotFound(code or "404")
        if r.status_code == 403:
            return AuthError(f"Доступ запрещён ({code}) — проверьте ключи, регион и бакет")
        return ProviderError(f"S3 {r.status_code} {code}: {r.text[:200]}")

    # ---------- пути ----------
    def _key(self, path: str) -> str:
        p = path.strip("/")
        return "/".join(x for x in (self.base, p) if x)

    def _dir_prefix(self, path: str) -> str:
        k = self._key(path)
        return k + "/" if k else ""

    def _list(self, prefix: str, delimiter: bool = True):
        token = None
        while True:
            q = {"list-type": "2", "prefix": prefix, "max-keys": "1000"}
            if delimiter:
                q["delimiter"] = "/"
            if token:
                q["continuation-token"] = token
            root = ET.fromstring(self._s3("GET", "", q).content)
            for el in root:
                tag = _local(el.tag)
                if tag == "Contents":
                    vals = {_local(c.tag): c.text or "" for c in el}
                    yield "file", vals
                elif tag == "CommonPrefixes":
                    yield "dir", {_local(c.tag): c.text or "" for c in el}
            trunc = next((el.text for el in root if _local(el.tag) == "IsTruncated"), "false")
            token = next((el.text for el in root if _local(el.tag) == "NextContinuationToken"), None)
            if trunc != "true" or not token:
                return

    # ---------- операции ----------
    def list_dir(self, path):
        prefix = self._dir_prefix(path)
        out = []
        for kind, v in self._list(prefix):
            if kind == "dir":
                name = v.get("Prefix", "")[len(prefix):].rstrip("/")
                if name:
                    out.append(Entry(name, True))
            else:
                name = v.get("Key", "")[len(prefix):]
                if name and "/" not in name:
                    out.append(Entry(name, False, int(v.get("Size") or 0), _ts(v.get("LastModified"))))
        return out

    def fetch(self, path, entry, ctx, start=None, end=None):
        headers = {}
        if start is not None:
            headers["Range"] = f"bytes={start}-{'' if end is None else end}"
        return self._s3("GET", self._key(path), headers=headers, stream=True)

    def upload(self, path, entry, fileobj, size):
        key = self._key(path)
        if size <= SIMPLE_LIMIT:
            self._s3("PUT", key, headers={"Content-Length": str(size)}, data=fileobj,
                     payload=UNSIGNED, rewind=fileobj)
            return None
        root = ET.fromstring(self._s3("POST", key, {"uploads": ""}).content)
        upload_id = next(el.text for el in root.iter() if _local(el.tag) == "UploadId")
        parts = []
        try:
            fileobj.seek(0)
            n = 1
            while True:
                chunk = fileobj.read(PART)
                if not chunk:
                    break
                r = self._s3("PUT", key, {"partNumber": str(n), "uploadId": upload_id},
                             headers={"Content-Length": str(len(chunk))}, data=chunk,
                             payload=hashlib.sha256(chunk).hexdigest())
                parts.append((n, r.headers.get("ETag", "")))
                n += 1
            body = ("<CompleteMultipartUpload>" + "".join(
                f"<Part><PartNumber>{i}</PartNumber><ETag>{e}</ETag></Part>" for i, e in parts)
                + "</CompleteMultipartUpload>").encode()
            self._s3("POST", key, {"uploadId": upload_id}, data=body,
                     headers={"Content-Type": "application/xml"},
                     payload=hashlib.sha256(body).hexdigest())
        except Exception:
            try:
                self._s3("DELETE", key, {"uploadId": upload_id})
            except Exception:  # noqa: BLE001
                pass
            raise
        return None

    def mkdir(self, path):
        self._s3("PUT", self._key(path) + "/", headers={"Content-Length": "0"}, data=b"")

    def _all_keys(self, path):
        prefix = self._dir_prefix(path)
        return [v["Key"] for kind, v in self._list(prefix, delimiter=False) if kind == "file"]

    def delete(self, path, entry):
        if entry.is_dir:
            for k in self._all_keys(path):
                self._s3("DELETE", k)
            self._s3("DELETE", self._key(path) + "/")
        else:
            self._s3("DELETE", self._key(path))

    def _copy(self, src_key, dst_key):
        source = "/" + self.bucket + "/" + _q(src_key, "/")
        self._s3("PUT", dst_key, headers={"x-amz-copy-source": source, "Content-Length": "0"},
                 data=b"")

    def move(self, src, dst, entry, dst_entry):
        if not entry.is_dir:
            self._copy(self._key(src), self._key(dst))
            self._s3("DELETE", self._key(src))
            return
        sp, dp = self._dir_prefix(src), self._dir_prefix(dst)
        keys = self._all_keys(src)
        for k in keys:
            self._copy(k, dp + k[len(sp):])
        self.mkdir(dst)
        for k in keys:
            self._s3("DELETE", k)
        self._s3("DELETE", self._key(src) + "/")
