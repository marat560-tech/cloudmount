"""Окно управления дисками + значок в трее."""
from __future__ import annotations

import json
import os
import queue
import time
import string
import subprocess
import sys
import threading
import tkinter as tk
import webbrowser
from tkinter import messagebox, ttk

from . import about, config
from .fusecompat import winfsp_dll
from .oauth import REDIRECT_URI
from .providers import PROVIDERS, make_provider

try:
    import pystray
    from PIL import Image, ImageDraw
except Exception:  # noqa: BLE001
    pystray = None

IS_WIN = sys.platform == "win32"
CREATE_NO_WINDOW = 0x08000000

HINTS = {
    "yandex": "Создайте приложение на oauth.yandex.ru (платформа «Веб-сервисы»),\n"
              f"Redirect URI: {REDIRECT_URI}\n"
              "Доступы: Яндекс Диск REST API — чтение, запись, информация о Диске.",
    "beeline": "Включите WebDAV: cloudbeeline.ru → «Профиль и настройки» →\n"
               "«Безопасность» → «Доступ через WebDAV». Там же логин и пароль.",
    "dropbox": "Создайте приложение на dropbox.com/developers/apps (Full Dropbox),\n"
               f"в Redirect URIs добавьте {REDIRECT_URI}\n"
               "Права: files.metadata.read/write, files.content.read/write.",
    "gdrive": "console.cloud.google.com → включите Google Drive API →\n"
              "OAuth-клиент типа «Desktop app». Добавьте себя в тестовые пользователи.",
    "webdav": "Любой WebDAV-сервер: Nextcloud, Mail.ru (платно), webdav.yandex.ru и др.",
}

EXIT_CODES = {
    2: "аккаунт не найден",
    3: "не установлен WinFsp",
    4: "нет связи с облаком или ошибка входа",
    5: "не удалось создать диск (буква занята?)",
}


def used_letters() -> set[str]:
    if not IS_WIN:
        return set()
    import ctypes
    mask = ctypes.windll.kernel32.GetLogicalDrives()
    return {string.ascii_uppercase[i] for i in range(26) if mask >> i & 1}


def log_path(acc_id: str) -> str:
    return os.path.join(config.local_dir("logs"), f"{acc_id}.log")


def open_path(p: str):
    if IS_WIN:
        os.startfile(p)  # noqa: S606
    else:
        webbrowser.open("file://" + p)


# ---------------- буфер обмена ----------------
# Tk не понимает Ctrl+V/C/X/A при русской раскладке (keysym другой), а меню по
# правой кнопке у полей ввода нет. Добавляем и то, и другое для всех полей.
_KEYCODES = {86: "<<Paste>>", 67: "<<Copy>>", 88: "<<Cut>>"}   # V, C, X (коды Windows)


def _on_ctrl_key(event):
    w = event.widget
    if event.keycode == 65:                       # Ctrl+A — выделить всё
        try:
            w.select_range(0, "end")
            w.icursor("end")
        except tk.TclError:
            pass
        return "break"
    virtual = _KEYCODES.get(event.keycode)
    if virtual and event.keysym.lower() not in ("v", "c", "x"):
        w.event_generate(virtual)                 # не латинская раскладка
        return "break"
    return None


def _show_menu(event):
    w = event.widget
    readonly = str(w.cget("state")) in ("readonly", "disabled")
    m = tk.Menu(w, tearoff=0)
    m.add_command(label="Вырезать", state="disabled" if readonly else "normal",
                  command=lambda: w.event_generate("<<Cut>>"))
    m.add_command(label="Копировать", command=lambda: w.event_generate("<<Copy>>"))
    m.add_command(label="Вставить", state="disabled" if readonly else "normal",
                  command=lambda: w.event_generate("<<Paste>>"))
    m.add_separator()
    m.add_command(label="Выделить всё", command=lambda: w.select_range(0, "end"))
    w.focus_set()
    m.tk_popup(event.x_root, event.y_root)


def enable_clipboard(root: tk.Misc):
    for cls in ("TEntry", "Entry", "TCombobox"):
        root.bind_class(cls, "<Control-KeyPress>", _on_ctrl_key, add="+")
        root.bind_class(cls, "<Button-3>", _show_menu, add="+")


# ---------------- монтирование ----------------
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


# ---------------- диалог аккаунта ----------------
class AccountDialog(tk.Toplevel):
    def __init__(self, master, acc: dict | None, taken_letters: set[str]):
        super().__init__(master)
        self.transient(master)
        self.title("Изменить диск" if acc else "Новый диск")
        self.resizable(False, False)
        self.result = None
        self.acc = acc or config.new_account("yandex")
        self.data = dict(self.acc.get("data") or {})
        self.token = self.data.get("token")
        self.q: queue.Queue = queue.Queue()
        self.cancel = threading.Event()
        self.vars: dict[str, tk.StringVar] = {}

        frm = ttk.Frame(self, padding=12)
        frm.grid(sticky="nsew")
        kinds = list(PROVIDERS)
        titles = [PROVIDERS[k].title for k in kinds]
        self.kinds = kinds

        ttk.Label(frm, text="Облако:").grid(row=0, column=0, sticky="w", pady=2)
        self.kind_var = tk.StringVar(value=PROVIDERS[self.acc["type"]].title)
        cb = ttk.Combobox(frm, textvariable=self.kind_var, values=titles, state="readonly", width=34)
        cb.grid(row=0, column=1, sticky="we", pady=2)
        if acc:
            cb.configure(state="disabled")
        cb.bind("<<ComboboxSelected>>", lambda e: self._kind_changed())

        ttk.Label(frm, text="Название:").grid(row=1, column=0, sticky="w", pady=2)
        self.name_var = tk.StringVar(value=self.acc.get("name", ""))
        ttk.Entry(frm, textvariable=self.name_var, width=36).grid(row=1, column=1, sticky="we", pady=2)

        ttk.Label(frm, text="Буква диска:").grid(row=2, column=0, sticky="w", pady=2)
        cur = (self.acc.get("letter") or "").upper()
        busy = (used_letters() | taken_letters) - {cur}
        letters = [c for c in "DEFGHIJKLMNOPQRSTUVWXYZ" if c not in busy]
        self.letter_var = tk.StringVar(value=cur or (letters[-1] if letters else ""))
        ttk.Combobox(frm, textvariable=self.letter_var, values=letters, state="readonly",
                     width=6).grid(row=2, column=1, sticky="w", pady=2)

        self.auto_var = tk.BooleanVar(value=self.acc.get("automount", True))
        ttk.Checkbutton(frm, text="Подключать при запуске", variable=self.auto_var
                        ).grid(row=3, column=1, sticky="w")
        self.net_var = tk.BooleanVar(value=self.acc.get("netdrive", True))
        ttk.Checkbutton(frm, text="Показывать как сетевой диск", variable=self.net_var
                        ).grid(row=4, column=1, sticky="w")

        self.defer_var = tk.BooleanVar(value=self.acc.get("defer_large", True))
        ttk.Checkbutton(frm, variable=self.defer_var,
                        text="Большие файлы (от 64 МБ, контейнеры VeraCrypt)\n"
                             "отправлять в облако только при закрытии"
                        ).grid(row=5, column=1, sticky="w")

        size_row = ttk.Frame(frm)
        size_row.grid(row=6, column=1, sticky="w", pady=(4, 0))
        self.size_var = tk.StringVar(value=str(self.acc.get("size_gb") or ""))
        ttk.Entry(size_row, textvariable=self.size_var, width=7).pack(side="left")
        ttk.Label(size_row, text=" ГБ — размер диска (пусто = как сообщает облако)"
                  ).pack(side="left")
        ttk.Label(frm, text="Размер:").grid(row=6, column=0, sticky="w", pady=(4, 0))

        self.fields_frame = ttk.LabelFrame(frm, text="Подключение", padding=8)
        self.fields_frame.grid(row=7, column=0, columnspan=2, sticky="we", pady=(8, 4))

        self.status_var = tk.StringVar()
        ttk.Label(frm, textvariable=self.status_var, foreground="#555", wraplength=380
                  ).grid(row=8, column=0, columnspan=2, sticky="w", pady=4)

        btns = ttk.Frame(frm)
        btns.grid(row=9, column=0, columnspan=2, sticky="e", pady=(6, 0))
        ttk.Button(btns, text="Проверить", command=self._test).pack(side="left", padx=3)
        ttk.Button(btns, text="Сохранить", command=self._ok).pack(side="left", padx=3)
        ttk.Button(btns, text="Отмена", command=self._close).pack(side="left", padx=3)

        self._kind_changed(initial=True)
        self.protocol("WM_DELETE_WINDOW", self._close)
        self.after(200, self._poll)
        self.grab_set()

    @property
    def kind(self) -> str:
        return self.kinds[[PROVIDERS[k].title for k in self.kinds].index(self.kind_var.get())]

    def _kind_changed(self, initial=False):
        for w in self.fields_frame.winfo_children():
            w.destroy()
        cls = PROVIDERS[self.kind]
        if not initial:
            self.data, self.token = {}, None
        self.vars = {}
        row = 0
        for key, label, secret, default in cls.fields:
            ttk.Label(self.fields_frame, text=label + ":").grid(row=row, column=0, sticky="w", pady=2)
            v = tk.StringVar(value=self.data.get(key, default))
            ttk.Entry(self.fields_frame, textvariable=v, width=34, show="•" if secret else ""
                      ).grid(row=row, column=1, sticky="we", pady=2)
            self.vars[key] = v
            row += 1
        hint = HINTS.get(self.kind)
        if hint:
            ttk.Label(self.fields_frame, text=hint, foreground="#666", justify="left"
                      ).grid(row=row, column=0, columnspan=2, sticky="w", pady=(6, 2))
            row += 1
        if cls.oauth:
            ttk.Button(self.fields_frame, text="Войти через браузер", command=self._login
                       ).grid(row=row, column=0, columnspan=2, sticky="w", pady=(6, 0))
        if self.name_var.get() in ("",) + tuple(p.title for p in PROVIDERS.values()):
            self.name_var.set(cls.title)
        self._show_token_status()

    def _show_token_status(self):
        if PROVIDERS[self.kind].oauth:
            self.status_var.set("Вход выполнен ✓" if self.token else "Нужно войти через браузер")
        else:
            self.status_var.set("")

    def _collect(self) -> dict:
        d = dict(self.data)
        for k, v in self.vars.items():
            d[k] = v.get().strip()
        if self.token:
            d["token"] = self.token
        return d

    def _login(self):
        d = self._collect()
        if not d.get("client_id"):
            messagebox.showwarning("CloudMount", "Сначала укажите ключ приложения (Client ID / App key).", parent=self)
            return
        self.status_var.set("Откройте браузер и разрешите доступ…")
        cls = PROVIDERS[self.kind]
        self.cancel.clear()

        def work():
            try:
                self.q.put(("token", cls.login(d, cancel_event=self.cancel)))
            except Exception as e:  # noqa: BLE001
                self.q.put(("error", f"Вход не выполнен: {e}"))
        threading.Thread(target=work, daemon=True).start()

    def _test(self):
        acc = {"type": self.kind, "data": self._collect()}
        self.status_var.set("Проверяю…")

        def work():
            try:
                p = make_provider(acc)
                n = len(p.list_dir("/"))
                total, used = p.quota()
                if total:
                    q = f", занято {used / 2**30:.1f} из {total / 2**30:.1f} ГБ"
                else:
                    q = (", облако не сообщает свой размер — укажите его в поле «Размер»"
                         + (f" (занято {used / 2**30:.1f} ГБ)" if used else ""))
                self.q.put(("info", f"Работает ✓ В корне объектов: {n}{q}"))
            except Exception as e:  # noqa: BLE001
                self.q.put(("info", f"Ошибка: {e}"))
        threading.Thread(target=work, daemon=True).start()

    def _poll(self):
        try:
            while True:
                kind, val = self.q.get_nowait()
                if kind == "token":
                    self.token = val
                    self._show_token_status()
                else:
                    self.status_var.set(val)
        except queue.Empty:
            pass
        if self.winfo_exists():
            self.after(200, self._poll)

    def _ok(self):
        cls = PROVIDERS[self.kind]
        d = self._collect()
        if not self.letter_var.get():
            messagebox.showwarning("CloudMount", "Выберите букву диска.", parent=self)
            return
        for key, label, secret, _ in cls.fields:
            if not d.get(key) and not (key == "client_secret" and self.kind == "gdrive"):
                messagebox.showwarning("CloudMount", f"Заполните поле «{label}».", parent=self)
                return
        if cls.oauth and not self.token:
            messagebox.showwarning("CloudMount", "Нажмите «Войти через браузер».", parent=self)
            return
        size_txt = self.size_var.get().strip().replace(",", ".")
        try:
            size_gb = float(size_txt) if size_txt else 0.0
            if size_gb < 0:
                raise ValueError
        except ValueError:
            messagebox.showwarning("CloudMount", "Размер диска — число в гигабайтах, например 10.", parent=self)
            return
        acc = dict(self.acc)
        acc.update(type=self.kind, name=self.name_var.get().strip() or cls.title,
                   letter=self.letter_var.get(), automount=self.auto_var.get(),
                   netdrive=self.net_var.get(), defer_large=self.defer_var.get(), size_gb=size_gb, data=d)
        self.result = acc
        self._close()

    def _close(self):
        self.cancel.set()
        self.grab_release()
        self.destroy()


def resource(*parts) -> str:
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    return os.path.join(base, *parts)


def load_icon_image():
    try:
        return Image.open(resource("assets", "icon.png"))
    except Exception:  # noqa: BLE001
        img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        blue = (33, 118, 255, 255)
        d.ellipse((8, 26, 34, 52), fill=blue)
        d.ellipse((20, 14, 50, 44), fill=blue)
        d.ellipse((34, 26, 58, 52), fill=blue)
        d.rectangle((20, 38, 46, 52), fill=blue)
        return img


class AboutDialog(tk.Toplevel):
    def __init__(self, master):
        super().__init__(master)
        self.transient(master)
        self.title("О программе")
        self.resizable(False, False)
        frm = ttk.Frame(self, padding=16)
        frm.pack(fill="both")
        ttk.Label(frm, text=f"{about.APP_NAME} {about.VERSION}",
                  font=("Segoe UI", 14, "bold")).pack(anchor="w")
        ttk.Label(frm, text=about.DESCRIPTION, justify="left").pack(anchor="w", pady=(4, 8))
        link = ttk.Label(frm, text=about.GITHUB_URL, foreground="#1a66d9", cursor="hand2")
        link.pack(anchor="w")
        link.bind("<Button-1>", lambda e: webbrowser.open(about.GITHUB_URL))

        d = about.DONATE
        if any(d.values()):
            box = ttk.LabelFrame(frm, text="Поддержать автора", padding=10)
            box.pack(fill="x", pady=(14, 0))
            ttk.Label(box, text="Если программа полезна — можно отблагодарить переводом\n"
                                "по номеру телефона (СБП):", justify="left").grid(
                row=0, column=0, columnspan=3, sticky="w", pady=(0, 6))
            row = 1
            for label, key in (("Получатель:", "name"), ("Банк:", "bank"), ("Телефон:", "phone")):
                if not d.get(key):
                    continue
                ttk.Label(box, text=label).grid(row=row, column=0, sticky="w", padx=(0, 8))
                e = ttk.Entry(box, width=26)
                e.insert(0, d[key])
                e.configure(state="readonly")
                e.grid(row=row, column=1, sticky="w", pady=2)
                if key == "phone":
                    ttk.Button(box, text="Копировать",
                               command=lambda v=d[key]: self._copy(v)).grid(row=row, column=2, padx=(6, 0))
                row += 1
            self.copied = ttk.Label(box, text="", foreground="#2a8a2a")
            self.copied.grid(row=row, column=0, columnspan=3, sticky="w")
        ttk.Button(frm, text="Закрыть", command=self.destroy).pack(anchor="e", pady=(14, 0))
        self.grab_set()

    def _copy(self, value):
        self.clipboard_clear()
        self.clipboard_append(value.replace(" ", "").replace("-", ""))
        self.copied.configure(text="Номер скопирован ✓")


class ExitDialog(tk.Toplevel):
    """Ждёт, пока диски отправят изменения в облако, затем закрывает программу."""

    def __init__(self, app):
        super().__init__(app.root)
        self.app = app
        self.title("CloudMount — выход")
        self.resizable(False, False)
        self.protocol("WM_DELETE_WINDOW", lambda: None)
        frm = ttk.Frame(self, padding=16)
        frm.pack()
        ttk.Label(frm, text="Отправляю изменения в облако и отключаю диски…").pack(anchor="w")
        self.var = tk.StringVar()
        ttk.Label(frm, textvariable=self.var, foreground="#555", wraplength=360).pack(anchor="w", pady=8)
        ttk.Button(frm, text="Выйти сразу", command=self._force).pack(anchor="e")
        self.after(300, self._wait)

    def _wait(self):
        self.app.mm.poll()
        if not self.app.mm.any_running():
            self.app.finish()
            return
        parts = [f"{a['name']}: {self.app.mm.status(a)}" for a in self.app.accounts
                 if self.app.mm.running(a["id"])]
        self.var.set("\n".join(parts))
        self.after(500, self._wait)

    def _force(self):
        if messagebox.askyesno("CloudMount", "Выйти, не дожидаясь загрузки?\n"
                               "Несохранённые изменения останутся на компьютере и будут "
                               "отправлены в облако при следующем запуске.", parent=self):
            self.app.mm.unmount_all(force=True)
            self.app.finish()


# ---------------- главное окно ----------------
class App:
    def __init__(self, minimized=False):
        self.root = tk.Tk()
        self.root.title("CloudMount — облачные диски")
        self.root.geometry("720x380")
        self.root.minsize(600, 300)
        enable_clipboard(self.root)
        if IS_WIN:
            try:
                self.root.iconbitmap(default=resource("assets", "icon.ico"))
            except tk.TclError:
                pass
        self.mm = MountManager()
        self.q: queue.Queue = queue.Queue()
        self.accounts = config.load_accounts()
        self.icon = None

        top = ttk.Frame(self.root, padding=8)
        top.pack(fill="both", expand=True)
        cols = ("name", "type", "letter", "status")
        self.tree = ttk.Treeview(top, columns=cols, show="headings", selectmode="browse")
        for c, t, w in zip(cols, ("Название", "Облако", "Диск", "Состояние"), (180, 150, 60, 280)):
            self.tree.heading(c, text=t)
            self.tree.column(c, width=w, anchor="w")
        self.tree.pack(fill="both", expand=True)
        self.tree.bind("<Double-1>", lambda e: self.toggle())

        bar = ttk.Frame(top)
        bar.pack(fill="x", pady=(8, 0))
        for text, cmd in (("Добавить", self.add), ("Изменить", self.edit), ("Удалить", self.remove),
                          ("Подключить", self.mount_sel), ("Отключить", self.unmount_sel),
                          ("Открыть", self.open_sel), ("Журнал", self.show_log)):
            ttk.Button(bar, text=text, command=cmd).pack(side="left", padx=2)

        bottom = ttk.Frame(top)
        bottom.pack(fill="x", pady=(8, 0))
        self.autostart_var = tk.BooleanVar(value=self._autostart_get())
        ttk.Checkbutton(bottom, text="Запускать вместе с Windows", variable=self.autostart_var,
                        command=self._autostart_set).pack(side="left")
        ttk.Button(bottom, text="О программе", command=self.show_about).pack(side="right")

        self.root.protocol("WM_DELETE_WINDOW", self.hide)
        self._setup_tray()
        self.refresh_list()
        self._check_winfsp()
        for a in self.accounts:
            if a.get("automount"):
                self.mm.mount(a)
        if minimized and self.icon is not None:
            self.root.withdraw()
        self.root.after(1000, self._tick)

    # ----- трей -----
    def _setup_tray(self):
        if pystray is None:
            return
        put = self.q.put
        menu = pystray.Menu(
            pystray.MenuItem("Открыть", lambda i, it: put(self.show), default=True),
            pystray.MenuItem("Подключить все", lambda i, it: put(self.mount_all)),
            pystray.MenuItem("Отключить все", lambda i, it: put(self.mm.unmount_all)),
            pystray.MenuItem("О программе", lambda i, it: put(self.show_about)),
            pystray.MenuItem("Выход", lambda i, it: put(self.quit)),
        )
        self.icon = pystray.Icon("CloudMount", load_icon_image(), "CloudMount", menu)
        self.icon.run_detached()

    def show_about(self):
        self.show()
        AboutDialog(self.root)

    def show(self):
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

    def hide(self):
        if self.icon is None:
            if messagebox.askyesno("CloudMount", "Выйти и отключить все диски?"):
                self.quit()
        else:
            self.root.withdraw()

    def quit(self):
        self.mm.poll()
        if self.mm.any_running():
            self.show()
            self.mm.unmount_all()
            ExitDialog(self)
        else:
            self.finish()

    def finish(self):
        if self.icon is not None:
            self.icon.stop()
        self.root.destroy()

    # ----- список -----
    def refresh_list(self):
        sel = self.tree.selection()
        self.tree.delete(*self.tree.get_children())
        for a in self.accounts:
            self.tree.insert("", "end", iid=a["id"], values=(
                a.get("name"), PROVIDERS[a["type"]].title,
                (a.get("letter") or "") + ":", self.mm.status(a)))
        if sel and self.tree.exists(sel[0]):
            self.tree.selection_set(sel)

    def _tick(self):
        try:
            while True:
                self.q.get_nowait()()
        except queue.Empty:
            pass
        if not self.root.winfo_exists():
            return
        self.mm.poll()
        for a in self.accounts:
            if self.tree.exists(a["id"]):
                self.tree.set(a["id"], "status", self.mm.status(a))
        self.root.after(1000, self._tick)

    def selected(self) -> dict | None:
        sel = self.tree.selection()
        if not sel:
            messagebox.showinfo("CloudMount", "Выберите диск в списке.")
            return None
        return next((a for a in self.accounts if a["id"] == sel[0]), None)

    def _taken(self, except_id=None) -> set[str]:
        return {(a.get("letter") or "").upper() for a in self.accounts if a["id"] != except_id}

    def add(self):
        dlg = AccountDialog(self.root, None, self._taken())
        self.root.wait_window(dlg)
        if dlg.result:
            config.save_account(dlg.result)
            self.accounts = config.load_accounts()
            self.refresh_list()
            self.mm.mount(dlg.result)

    def edit(self):
        a = self.selected()
        if not a:
            return
        # берём свежие данные (токен мог обновиться в процессе диска)
        a = config.get_account(a["id"]) or a
        dlg = AccountDialog(self.root, a, self._taken(a["id"]))
        self.root.wait_window(dlg)
        if dlg.result:
            config.save_account(dlg.result)
            self.accounts = config.load_accounts()
            self.refresh_list()
            if self.mm.running(a["id"]):
                self.mm.unmount(a["id"])
                self.mm.restart[a["id"]] = dlg.result    # переподключим после отключения

    def remove(self):
        a = self.selected()
        if a and messagebox.askyesno("CloudMount", f"Удалить диск «{a['name']}»?\n"
                                     "Файлы в облаке не пострадают."):
            self.mm.restart.pop(a["id"], None)
            self.mm.unmount(a["id"])
            config.delete_account(a["id"])
            self.accounts = config.load_accounts()
            self.refresh_list()

    def mount_sel(self):
        a = self.selected()
        if a:
            self.mm.mount(a)

    def unmount_sel(self):
        a = self.selected()
        if not a or not self.mm.running(a["id"]):
            return
        if a["id"] in self.mm.stopping:
            if messagebox.askyesno("CloudMount", "Диск ещё отправляет изменения в облако.\n"
                                   "Отключить принудительно? Изменения останутся на компьютере "
                                   "и догрузятся при следующем подключении."):
                self.mm.unmount(a["id"], force=True)
            return
        opened = self.mm.info(a["id"]).get("open") or []
        if opened and not messagebox.askyesno(
                "CloudMount", "Сейчас открыты файлы: " + ", ".join(opened[:3]) + "\n\n"
                "Если это контейнер VeraCrypt — сначала отключите его в VeraCrypt,\n"
                "иначе последние изменения могут не попасть в облако.\n\nВсё равно отключить диск?"):
            return
        self.mm.unmount(a["id"])

    def toggle(self):
        a = self.selected()
        if a:
            if self.mm.running(a["id"]):
                self.open_sel()
            else:
                self.mm.mount(a)

    def open_sel(self):
        a = self.selected()
        if a and a.get("letter") and self.mm.running(a["id"]):
            open_path(f"{a['letter']}:\\")

    def show_log(self):
        a = self.selected()
        if a:
            p = log_path(a["id"])
            if os.path.exists(p):
                open_path(p)
            else:
                messagebox.showinfo("CloudMount", "Журнал пока пуст.")

    def mount_all(self):
        for a in self.accounts:
            self.mm.mount(a)

    # ----- автозапуск / WinFsp -----
    @staticmethod
    def _run_cmd() -> str:
        if getattr(sys, "frozen", False):
            return f'"{sys.executable}" --minimized'
        exe = sys.executable.replace("python.exe", "pythonw.exe")
        main = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "main.py")
        return f'"{exe}" "{main}" --minimized'

    def _autostart_get(self) -> bool:
        if not IS_WIN:
            return False
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                                r"Software\Microsoft\Windows\CurrentVersion\Run") as k:
                winreg.QueryValueEx(k, "CloudMount")
                return True
        except OSError:
            return False

    def _autostart_set(self):
        if not IS_WIN:
            return
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Run", 0,
                            winreg.KEY_SET_VALUE) as k:
            if self.autostart_var.get():
                winreg.SetValueEx(k, "CloudMount", 0, winreg.REG_SZ, self._run_cmd())
            else:
                try:
                    winreg.DeleteValue(k, "CloudMount")
                except OSError:
                    pass

    def _check_winfsp(self):
        if IS_WIN and not winfsp_dll():
            if messagebox.askyesno("CloudMount", "Для работы дисков нужен бесплатный драйвер WinFsp.\n"
                                   "Открыть страницу загрузки?"):
                webbrowser.open("https://winfsp.dev/rel/")

    def run(self):
        self.root.mainloop()


def _single_instance() -> bool:
    if not IS_WIN:
        return True
    import ctypes
    ctypes.windll.kernel32.CreateMutexW(None, False, "CloudMount_single_instance")
    return ctypes.windll.kernel32.GetLastError() != 183  # ERROR_ALREADY_EXISTS


def run_gui(minimized=False):
    if not _single_instance():
        r = tk.Tk()
        r.withdraw()
        messagebox.showinfo("CloudMount", "CloudMount уже запущен — значок в трее у часов.")
        return
    App(minimized).run()
