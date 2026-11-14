"""Подключение библиотеки FUSE (WinFsp на Windows).

Порядок: mfusepy (поддерживаемый форк fusepy) -> fusepy.
Если ни одна не доступна, даём заглушки, чтобы логику ФС можно было тестировать.
"""
from __future__ import annotations

import os
import sys


def winfsp_dll() -> str | None:
    if sys.platform != "win32":
        return None
    import winreg
    for key in (r"SOFTWARE\WOW6432Node\WinFsp", r"SOFTWARE\WinFsp"):
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key) as k:
                d = winreg.QueryValueEx(k, "InstallDir")[0]
        except OSError:
            continue
        dll = os.path.join(d, "bin", "winfsp-x64.dll" if sys.maxsize > 2**32 else "winfsp-x86.dll")
        if os.path.exists(dll):
            return dll
    return None


if sys.platform == "win32" and "FUSE_LIBRARY_PATH" not in os.environ:
    _dll = winfsp_dll()
    if _dll:
        os.environ["FUSE_LIBRARY_PATH"] = _dll

FUSE = None
IMPORT_ERROR = None
try:
    from mfusepy import FUSE, FuseOSError, Operations  # type: ignore
except Exception as e1:  # noqa: BLE001
    try:
        from fuse import FUSE, FuseOSError, Operations  # type: ignore
    except Exception as e2:  # noqa: BLE001
        IMPORT_ERROR = f"{e1}; {e2}"

        class FuseOSError(OSError):  # type: ignore[no-redef]
            def __init__(self, errno_):
                super().__init__(errno_, os.strerror(errno_))

        class Operations:  # type: ignore[no-redef]
            pass
