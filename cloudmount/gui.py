"""Окно управления дисками + значок в трее."""
from __future__ import annotations

import os
import queue
import string
import subprocess
import sys
import threading
import tkinter as tk
import webbrowser
from tkinter import messagebox, ttk

from . import config
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


# ---------------- монтирование ----------------
class MountManager:
    def __init__(self):
        self.procs: dict[str, subprocess.Popen] = {}
        self.errors: dict[str, str] = {}

    @staticmethod
    def _cmd(acc_id):
        if getattr(sys, "frozen", False):
            return [sys.executable, "--mount", acc_id]
        main = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "main.py")
        return [sys.executable, main, "--mount", acc_id]

    def running(self, acc_id) -> bool:
        p = self.procs.get(acc_id)
        return p is not None and p.poll() is None

    def mount(self, acc):
        if self.running(acc["id"]):
            return
        self.errors.pop(acc["id"], None)
        kw = {"creationflags": CREATE_NO_WINDOW} if IS_WIN else {}
        self.procs[acc["id"]] = subprocess.Popen(
            self._cmd(acc["id"]), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, **kw)

    def unmount(self, acc_id):
        p = self.procs.pop(acc_id, None)
        if p and p.poll() is None:
            p.terminate()
            try:
                p.wait(5)
            except subprocess.TimeoutExpired:
                p.kill()

    def unmount_all(self):
        for acc_id in list(self.procs):
            self.unmount(acc_id)

    def status(self, acc) -> str:
        p = self.procs.get(acc["id"])
        if p is None:
            err = self.errors.get(acc["id"])
            return f"Ошибка: {err}" if err else "Отключён"
        code = p.poll()
        if code is None:
            letter = (acc.get("letter") or "").upper()
            if letter and IS_WIN and not os.path.exists(f"{letter}:\\"):
                return "Подключение…"
            return f"Подключён ({letter}:)" if letter else "Подключён"
        del self.procs[acc["id"]]
        if code != 0:
            self.errors[acc["id"]] = EXIT_CODES.get(code, f"код {code}") + " — см. журнал"
            return "Ошибка: " + self.errors[acc["id"]]
        return "Отключён"


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

        self.fields_frame = ttk.LabelFrame(frm, text="Подключение", padding=8)
        self.fields_frame.grid(row=5, column=0, columnspan=2, sticky="we", pady=(8, 4))

        self.status_var = tk.StringVar()
        ttk.Label(frm, textvariable=self.status_var, foreground="#555", wraplength=380
                  ).grid(row=6, column=0, columnspan=2, sticky="w", pady=4)

        btns = ttk.Frame(frm)
        btns.grid(row=7, column=0, columnspan=2, sticky="e", pady=(6, 0))
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
                q = f", занято {used / 2**30:.1f} из {total / 2**30:.1f} ГБ" if total else ""
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
        acc = dict(self.acc)
        acc.update(type=self.kind, name=self.name_var.get().strip() or cls.title,
                   letter=self.letter_var.get(), automount=self.auto_var.get(),
                   netdrive=self.net_var.get(), data=d)
        self.result = acc
        self._close()

    def _close(self):
        self.cancel.set()
        self.grab_release()
        self.destroy()


# ---------------- главное окно ----------------
class App:
    def __init__(self, minimized=False):
        self.root = tk.Tk()
        self.root.title("CloudMount — облачные диски")
        self.root.geometry("720x380")
        self.root.minsize(600, 300)
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
        img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        blue = (33, 118, 255, 255)
        d.ellipse((8, 26, 34, 52), fill=blue)
        d.ellipse((20, 14, 50, 44), fill=blue)
        d.ellipse((34, 26, 58, 52), fill=blue)
        d.rectangle((20, 38, 46, 52), fill=blue)
        put = self.q.put
        menu = pystray.Menu(
            pystray.MenuItem("Открыть", lambda i, it: put(self.show), default=True),
            pystray.MenuItem("Подключить все", lambda i, it: put(self.mount_all)),
            pystray.MenuItem("Отключить все", lambda i, it: put(self.mm.unmount_all)),
            pystray.MenuItem("Выход", lambda i, it: put(self.quit)),
        )
        self.icon = pystray.Icon("CloudMount", img, "CloudMount", menu)
        self.icon.run_detached()

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
        self.mm.unmount_all()
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
            was = self.mm.running(a["id"])
            self.mm.unmount(a["id"])
            config.save_account(dlg.result)
            self.accounts = config.load_accounts()
            self.refresh_list()
            if was:
                self.mm.mount(dlg.result)

    def remove(self):
        a = self.selected()
        if a and messagebox.askyesno("CloudMount", f"Удалить диск «{a['name']}»?\n"
                                     "Файлы в облаке не пострадают."):
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
        if a:
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
