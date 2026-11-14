"""Хранение аккаунтов. Секреты (пароли, токены) шифруются Windows DPAPI —
расшифровать их может только текущий пользователь Windows на этом компьютере."""
from __future__ import annotations

import base64
import json
import os
import sys
import tempfile
import threading
import uuid

APP = "CloudMount"


def app_dir() -> str:
    base = os.environ.get("APPDATA") or os.path.expanduser("~/.config")
    d = os.path.join(base, APP)
    os.makedirs(d, exist_ok=True)
    return d


def local_dir(*sub) -> str:
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~/.cache")
    d = os.path.join(base, APP, *sub)
    os.makedirs(d, exist_ok=True)
    return d


CONFIG_PATH = os.path.join(app_dir(), "config.json")
_lock = threading.Lock()


# ---------- DPAPI ----------
if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    class _Blob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    _crypt32 = ctypes.windll.crypt32
    _kernel32 = ctypes.windll.kernel32
    _kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    _crypt32.CryptProtectData.argtypes = [
        ctypes.POINTER(_Blob), wintypes.LPCWSTR, ctypes.POINTER(_Blob),
        ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_Blob)]
    _crypt32.CryptUnprotectData.argtypes = [
        ctypes.POINTER(_Blob), ctypes.c_void_p, ctypes.POINTER(_Blob),
        ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_Blob)]

    def _call(fn, data: bytes) -> bytes:
        buf = ctypes.create_string_buffer(data, len(data))
        blob_in = _Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
        blob_out = _Blob()
        if fn is _crypt32.CryptProtectData:
            ok = fn(ctypes.byref(blob_in), APP, None, None, None, 0x01, ctypes.byref(blob_out))
        else:
            ok = fn(ctypes.byref(blob_in), None, None, None, None, 0x01, ctypes.byref(blob_out))
        if not ok:
            raise ctypes.WinError()
        try:
            return ctypes.string_at(blob_out.pbData, blob_out.cbData)
        finally:
            _kernel32.LocalFree(blob_out.pbData)

    def protect(data: bytes) -> bytes:
        return _call(_crypt32.CryptProtectData, data)

    def unprotect(data: bytes) -> bytes:
        return _call(_crypt32.CryptUnprotectData, data)
else:  # для разработки на других ОС (без шифрования)
    def protect(data: bytes) -> bytes:
        return data

    def unprotect(data: bytes) -> bytes:
        return data


def _enc(d: dict) -> str:
    return base64.b64encode(protect(json.dumps(d).encode())).decode()


def _dec(s: str) -> dict:
    try:
        return json.loads(unprotect(base64.b64decode(s)).decode())
    except Exception:  # noqa: BLE001
        return {}


# ---------- конфиг ----------
def _read_raw() -> dict:
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {"accounts": [], "settings": {}}


def _write_raw(raw: dict):
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(CONFIG_PATH), suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(raw, f, ensure_ascii=False, indent=2)
    os.replace(tmp, CONFIG_PATH)


def load_accounts() -> list[dict]:
    with _lock:
        raw = _read_raw()
    out = []
    for a in raw.get("accounts", []):
        a = dict(a)
        a["data"] = _dec(a.pop("secret", "")) if a.get("secret") else {}
        out.append(a)
    return out


def get_account(acc_id: str) -> dict | None:
    for a in load_accounts():
        if a["id"] == acc_id:
            return a
    return None


def save_account(acc: dict):
    """Добавляет или обновляет аккаунт."""
    with _lock:
        raw = _read_raw()
        stored = {k: v for k, v in acc.items() if k != "data"}
        stored["secret"] = _enc(acc.get("data") or {})
        accs = raw.setdefault("accounts", [])
        for i, a in enumerate(accs):
            if a["id"] == acc["id"]:
                accs[i] = stored
                break
        else:
            accs.append(stored)
        _write_raw(raw)


def delete_account(acc_id: str):
    with _lock:
        raw = _read_raw()
        raw["accounts"] = [a for a in raw.get("accounts", []) if a["id"] != acc_id]
        _write_raw(raw)


def new_account(kind: str) -> dict:
    return {"id": uuid.uuid4().hex[:12], "name": "", "type": kind, "letter": "",
            "automount": True, "netdrive": True, "data": {}}


def get_setting(key, default=None):
    with _lock:
        return _read_raw().get("settings", {}).get(key, default)


def set_setting(key, value):
    with _lock:
        raw = _read_raw()
        raw.setdefault("settings", {})[key] = value
        _write_raw(raw)
