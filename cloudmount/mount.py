"""Процесс монтирования одного аккаунта: CloudMount.exe --mount <id>.

Каждый диск работает в своём процессе: если одно облако зависнет, остальные
продолжат работать. Завершение процесса автоматически отключает диск (WinFsp).
"""
from __future__ import annotations

import logging
import os
import re
import sys

from . import config


def setup_logging(acc_id: str) -> str:
    path = os.path.join(config.local_dir("logs"), f"{acc_id}.log")
    from logging.handlers import RotatingFileHandler
    h = RotatingFileHandler(path, maxBytes=2_000_000, backupCount=2, encoding="utf-8")
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(threadName)s %(message)s"))
    root = logging.getLogger()
    root.handlers[:] = [h]
    root.setLevel(logging.INFO)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    return path


def run(acc_id: str) -> int:
    setup_logging(acc_id)
    log = logging.getLogger("mount")
    acc = config.get_account(acc_id)
    if acc is None:
        log.error("Аккаунт %s не найден", acc_id)
        return 2

    from .fs import CloudFS
    from .fusecompat import FUSE, IMPORT_ERROR
    from .providers import make_provider

    if FUSE is None:
        log.error("Библиотека FUSE недоступна (установлен ли WinFsp?): %s", IMPORT_ERROR)
        return 3

    def save():
        fresh = config.get_account(acc_id) or acc
        fresh["data"] = acc["data"]
        config.save_account(fresh)

    provider = make_provider(acc, save)
    try:
        provider.list_dir("/")
    except Exception as e:  # noqa: BLE001
        log.error("Не удалось подключиться к облаку: %s", e)
        return 4

    fs = CloudFS(provider, config.local_dir("cache", acc_id))
    letter = (acc.get("letter") or "").strip(":").upper()
    mountpoint = f"{letter}:" if letter else config.local_dir("mnt", acc_id)
    volname = (acc.get("name") or provider.title)[:32]
    opts = {"uid": -1, "gid": -1, "umask": 0, "volname": volname,
            "FileInfoTimeout": 2000, "DirInfoTimeout": 2000, "VolumeInfoTimeout": 30000}
    if sys.platform == "win32" and acc.get("netdrive", True) and letter:
        share = re.sub(r"[^A-Za-z0-9_-]", "", acc["type"] + "-" + acc_id)
        opts["VolumePrefix"] = f"/cloudmount/{share}"   # выглядит как сетевой диск
    if sys.platform != "win32":
        opts = {}
    log.info("Подключаю %s (%s) как %s", volname, acc["type"], mountpoint)
    try:
        FUSE(fs, mountpoint, foreground=True, nothreads=False, **opts)
    except Exception as e:  # noqa: BLE001
        log.warning("Монтирование с расширенными опциями не удалось (%s), пробую базовые", e)
        basic = {k: opts[k] for k in ("uid", "gid", "umask", "volname") if k in opts}
        try:
            FUSE(fs, mountpoint, foreground=True, nothreads=False, **basic)
        except Exception as e2:  # noqa: BLE001
            log.exception("Ошибка FUSE: %s", e2)
            return 5
    log.info("Диск отключён")
    return 0
