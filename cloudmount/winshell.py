"""Интеграция с Проводником: подпись и значок диска (только HKCU, без прав администратора)."""
from __future__ import annotations

import logging
import re
import sys

log = logging.getLogger(__name__)
IS_WIN = sys.platform == "win32"
SERVER = "CloudMount"

_TR = dict(zip("абвгдеёжзийклмнопрстуфхцчшщъыьэюя",
               ["a", "b", "v", "g", "d", "e", "e", "zh", "z", "i", "y", "k", "l", "m", "n", "o", "p",
                "r", "s", "t", "u", "f", "h", "ts", "ch", "sh", "sch", "", "y", "", "e", "yu", "ya"]))


def share_name(name: str, fallback: str) -> str:
    """«Яндекс Диск» → «YandexDisk»: читаемое сетевое имя без цифр-идентификаторов."""
    out = []
    for word in re.split(r"\s+", name.strip()):
        w = "".join(_TR.get(ch.lower(), ch) if ch.lower() in _TR else ch for ch in word)
        w = re.sub(r"[^A-Za-z0-9_-]", "", w)
        if w:
            out.append(w[:1].upper() + w[1:])
    s = "".join(out)[:40]
    return s or fallback


def unc_key(share: str) -> str:
    return f"##{SERVER}#{share}"


def _notify():
    try:
        import ctypes
        ctypes.windll.shell32.SHChangeNotify(0x08000000, 0, None, None)  # SHCNE_ASSOCCHANGED
    except Exception:  # noqa: BLE001
        pass


def apply(letter: str, share: str | None, label: str, icon_path: str | None):
    """Подпись диска в «Этом компьютере» и его значок."""
    if not IS_WIN:
        return
    import winreg
    try:
        if share:
            key = r"Software\Microsoft\Windows\CurrentVersion\Explorer\MountPoints2" + "\\" + unc_key(share)
            with winreg.CreateKey(winreg.HKEY_CURRENT_USER, key) as k:
                winreg.SetValueEx(k, "_LabelFromReg", 0, winreg.REG_SZ, label)
        if letter and icon_path:
            key = rf"Software\Classes\Applications\Explorer.exe\Drives\{letter}\DefaultIcon"
            with winreg.CreateKey(winreg.HKEY_CURRENT_USER, key) as k:
                winreg.SetValueEx(k, "", 0, winreg.REG_SZ, icon_path)
        _notify()
    except OSError as e:
        log.warning("Не удалось задать подпись/значок диска: %s", e)


def forget_old_names():
    """Убирает из кэша Проводника старые имена дисков вида cloudmount/yandex-a7872ea34122."""
    if not IS_WIN:
        return
    import winreg
    base = r"Software\Microsoft\Windows\CurrentVersion\Explorer\MountPoints2"
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, base) as k:
            names, i = [], 0
            while True:
                try:
                    names.append(winreg.EnumKey(k, i))
                    i += 1
                except OSError:
                    break
    except OSError:
        return
    for n in names:
        # старый формат 1.x/2.0: ##cloudmount#<тип>-<12 hex>
        if re.fullmatch(r"##cloudmount#[a-z0-9]+-[0-9a-f]{12}", n, flags=re.IGNORECASE):
            try:
                winreg.DeleteKey(winreg.HKEY_CURRENT_USER, base + "\\" + n)
            except OSError:
                pass


def clear(letter: str):
    """Убирает значок с буквы, чтобы он не остался у другого диска с той же буквой."""
    if not IS_WIN or not letter:
        return
    import winreg
    base = rf"Software\Classes\Applications\Explorer.exe\Drives\{letter}"
    for sub in (base + r"\DefaultIcon", base):
        try:
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, sub)
        except OSError:
            pass
    _notify()
