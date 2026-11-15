"""Процессы дисков: запуск, мягкое отключение, статус."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time

from . import config

IS_WIN = sys.platform == "win32"
CREATE_NO_WINDOW = 0x08000000

EXIT_CODES = {
    2: "аккаунт не найден",
    3: "не установлен WinFsp",
    4: "нет связи с облаком или ошибка входа",
    5: "не удалось создать диск (буква занята?)",
}


class MountManager:
    """Процессы дисков. Отключение — мягкое: процесс сначала отправляет изменения."""

    def __init__(self):
        self.procs: dict[str, subprocess.Popen] = {}
        self.errors: dict[str, str] = {}
        self.stopping: set[str] = set()
        self.restart: dict[str, dict] = {}

    @staticmethod
    def _cmd(acc_id):
        if getattr(sys, "frozen", False):
            return [sys.executable, "--mount", acc_id]
        main = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "main.py")
        return [sys.executable, main, "--mount", acc_id]

    @staticmethod
    def _run_file(acc_id, ext):
        return os.path.join(config.local_dir("run"), f"{acc_id}.{ext}")

    def running(self, acc_id) -> bool:
        p = self.procs.get(acc_id)
        return p is not None and p.poll() is None

    def any_running(self) -> bool:
        return any(self.running(a) for a in list(self.procs))

    def mount(self, acc):
        if acc["id"] in self.stopping and self.running(acc["id"]):
            self.restart[acc["id"]] = acc       # подключим, когда старый процесс закончит
            return
        if self.running(acc["id"]):
            return
        self.errors.pop(acc["id"], None)
        kw = {"creationflags": CREATE_NO_WINDOW} if IS_WIN else {}
        self.procs[acc["id"]] = subprocess.Popen(
            self._cmd(acc["id"]), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, **kw)

    def unmount(self, acc_id, force=False):
        p = self.procs.get(acc_id)
        if p is None or p.poll() is not None:
            return
        if force:
            p.terminate()
            try:
                p.wait(5)
            except subprocess.TimeoutExpired:
                p.kill()
            return
        self.stopping.add(acc_id)
        with open(self._run_file(acc_id, "stop"), "w") as f:
            f.write("stop")

    def unmount_all(self, force=False):
        for acc_id in list(self.procs):
            self.unmount(acc_id, force)

    def info(self, acc_id) -> dict:
        """Статус из процесса диска (что загружается, что открыто)."""
        try:
            path = self._run_file(acc_id, "status")
            if time.time() - os.path.getmtime(path) > 10:
                return {}
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def poll(self):
        """Проверяет завершившиеся процессы. Вызывать периодически."""
        for acc_id, p in list(self.procs.items()):
            code = p.poll()
            if code is None:
                continue
            del self.procs[acc_id]
            was_stopping = acc_id in self.stopping
            self.stopping.discard(acc_id)
            if code != 0 and not was_stopping:
                self.errors[acc_id] = EXIT_CODES.get(code, f"код {code}") + " — см. журнал"
            acc = self.restart.pop(acc_id, None)
            if acc is not None:
                self.mount(acc)

    def status(self, acc) -> str:
        acc_id = acc["id"]
        if not self.running(acc_id):
            err = self.errors.get(acc_id)
            return f"Ошибка: {err}" if err else "Отключён"
        st = self.info(acc_id)
        up = ""
        if st.get("uploading"):
            up = f"загрузка «{st['uploading']}» {st.get('percent', 0)}%"
        if acc_id in self.stopping:
            return "Отключение: " + (up or "отправка изменений…")
        letter = (acc.get("letter") or "").upper()
        if letter and IS_WIN and not os.path.exists(f"{letter}:\\"):
            return "Подключение…"
        text = f"Подключён ({letter}:)" if letter else "Подключён"
        if up:
            text += " · " + up
        elif st.get("unsaved"):
            text += f" · ждут загрузки при закрытии: {', '.join(st['unsaved'][:2])}"
        elif st.get("pending"):
            text += f" · догружается после сбоя: {st['pending']}"
        return text



    def card(self, acc) -> dict:
        """Всё, что показывает карточка диска: статус, полоса, подпись."""
        acc_id = acc["id"]
        letter = (acc.get("letter") or "").upper()
        if not self.running(acc_id):
            err = self.errors.get(acc_id)
            if err:
                return {"state": "error", "chip": "Ошибка", "note": err, "progress": 0}
            return {"state": "off", "chip": "Отключён", "note": "Не подключён", "progress": 0}
        st = self.info(acc_id)
        if st.get("uploading"):
            pct = int(st.get("percent") or 0)
            prefix = "Отключение: " if acc_id in self.stopping else ""
            return {"state": "busy", "chip": "Загрузка", "progress": pct,
                    "note": f"{prefix}отправляется «{st['uploading']}» — {pct}%"}
        if acc_id in self.stopping:
            return {"state": "busy", "chip": "Отключение", "note": "Отправка изменений…",
                    "progress": 0}
        if letter and IS_WIN and not os.path.exists(f"{letter}:\\"):
            return {"state": "busy", "chip": "Подключение", "note": "Подключаю…", "progress": 0}
        total, used = int(st.get("total") or 0), int(st.get("used") or 0)
        gb = 2 ** 30
        if total:
            note = f"Занято {used / gb:.1f} из {total / gb:.1f} ГБ".replace(".", ",")
            pct = min(100, int(used * 100 / total))
        else:
            note, pct = "Подключён", 0
        if st.get("unsaved"):
            note = "Ждут отправки при закрытии: " + ", ".join(st["unsaved"][:2])
        elif st.get("pending"):
            note = f"Догружаются изменения после сбоя: {st['pending']}"
        return {"state": "ok", "chip": "Подключён", "note": note, "progress": pct}
