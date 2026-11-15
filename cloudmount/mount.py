"""Процесс монтирования одного аккаунта: CloudMount.exe --mount <id>.

Каждый диск работает в своём процессе: если одно облако зависнет, остальные
продолжат работать. Завершение процесса автоматически отключает диск (WinFsp).
"""
from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time

from . import config, winshell


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


def run_dir() -> str:
    return config.local_dir("run")


def status_path(acc_id: str) -> str:
    return os.path.join(run_dir(), f"{acc_id}.status")


def stop_path(acc_id: str) -> str:
    return os.path.join(run_dir(), f"{acc_id}.stop")


def _write_status(acc_id: str, data: dict):
    tmp = status_path(acc_id) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(tmp, status_path(acc_id))


def _service_threads(acc_id: str, fs, log, cleanup=lambda: None):
    """Статус для окна программы, догрузка после сбоя, мягкое отключение."""
    state = {"stopping": False}

    def status_loop():
        while True:
            try:
                st = fs.status()
                st["stopping"] = state["stopping"]
                st["time"] = time.time()
                _write_status(acc_id, st)
            except Exception:  # noqa: BLE001
                pass
            time.sleep(1)

    def stop_loop():
        while not os.path.exists(stop_path(acc_id)):
            time.sleep(0.5)
        state["stopping"] = True
        log.info("Запрошено отключение: отправляю несохранённые изменения")
        failed = fs.upload_all()
        log.info("Отключение (ошибок загрузки: %d)", failed)
        for p in (stop_path(acc_id), status_path(acc_id)):
            try:
                os.remove(p)
            except OSError:
                pass
        cleanup()
        logging.shutdown()
        os._exit(0)   # WinFsp сам отключит диск при завершении процесса

    def recover():
        time.sleep(3)
        if fs.pending():
            n = fs.recover_pending()
            log.info("Догружено после прошлого сбоя: %d", n)

    for fn in (status_loop, stop_loop, recover):
        threading.Thread(target=fn, daemon=True, name=fn.__name__).start()


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

    for p in (stop_path(acc_id), status_path(acc_id)):
        try:
            os.remove(p)
        except OSError:
            pass
    fs = CloudFS(provider, config.local_dir("cache", acc_id),
                 defer_large=acc.get("defer_large", True),
                 size_gb=float(acc.get("size_gb") or 0))
    letter = (acc.get("letter") or "").strip(":").upper()
    mountpoint = f"{letter}:" if letter else config.local_dir("mnt", acc_id)
    title = acc.get("name") or provider.title
    volname = title[:32]
    opts = {"uid": -1, "gid": -1, "umask": 0, "volname": volname,
            "FileInfoTimeout": 2000, "DirInfoTimeout": 2000, "VolumeInfoTimeout": 30000}
    share = None
    if sys.platform == "win32" and acc.get("netdrive", True) and letter:
        # читаемое сетевое имя: \\CloudMount\YandexDisk (а не тип и номер аккаунта)
        share = winshell.share_name(title, acc["type"])
        others = {winshell.share_name(a.get("name") or a["type"], a["type"]).lower()
                  for a in config.load_accounts() if a["id"] != acc_id}
        if share.lower() in others:
            share = f"{share}-{letter}"
        opts["VolumePrefix"] = f"/{winshell.SERVER}/{share}"
    icon_path = acc.get("icon_path") or ""
    if not (icon_path and os.path.exists(icon_path)):
        try:
            from .icons import ensure_ico
            icon_path = ensure_ico(os.path.join(config.local_dir("icons"),
                                                f"{acc['type']}-{provider.tint.lstrip('#')}.ico"),
                                   provider.tint, provider.icon)
        except Exception as e:  # noqa: BLE001
            log.warning("Значок диска не создан: %s", e)
            icon_path = ""
    if letter:
        winshell.forget_old_names()
        winshell.apply(letter, share, title, icon_path or None)
    _service_threads(acc_id, fs, log, cleanup=lambda: winshell.clear(letter))
    fs.prefetch_quota()
    total, used = fs.disk_size()
    log.info("Размер диска: %.1f ГБ, занято %.1f ГБ%s", total / 2**30, used / 2**30,
             "" if fs._quota[1][0] or fs.size_override else " (облако не сообщило размер)")
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
    winshell.clear(letter)
    log.info("Диск отключён")
    return 0
