"""Окно CloudMount: карточки дисков, добавление облака плитками, трей.

Оформление — тема Sun Valley (как в Windows 11), светлая или тёмная по настройкам
Windows. Если пакета sv_ttk нет, используется стандартная тема.
"""
from __future__ import annotations

import os
import queue
import string
import sys
import threading
import tkinter as tk
import webbrowser
from tkinter import filedialog, messagebox, ttk

from . import about, config
from .fusecompat import winfsp_dll
from .icons import draw as draw_icon
from .mounts import MountManager
from .oauth import redirect_uri
from .providers import PROVIDERS, make_provider
from .donate import donate_due  # noqa: F401  (используется окном и тестами)
from .ui_clip import enable_clipboard

try:
    import pystray
except Exception:  # noqa: BLE001
    pystray = None
try:
    from PIL import ImageTk
except Exception:  # noqa: BLE001
    ImageTk = None
try:
    import sv_ttk
except Exception:  # noqa: BLE001
    sv_ttk = None

IS_WIN = sys.platform == "win32"
TEST_MODE = os.environ.get("CLOUDMOUNT_TEST") == "1"
FONT = "Segoe UI"


# ---------------- оформление ----------------
def system_dark() -> bool:
    if not IS_WIN:
        return False
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as k:
            return winreg.QueryValueEx(k, "AppsUseLightTheme")[0] == 0
    except OSError:
        return False


LIGHT = {"bg": "#F3F3F3", "card": "#FFFFFF", "ink": "#1B1B1B", "muted": "#5C5C5C",
         "line": "#E2E2E2", "hover": "#F5F8FE", "accent": "#2F7BF0", "sel": "#E8F0FE",
         "ok": ("#DFF6E6", "#0F6B33"), "busy": ("#E3EDFD", "#1450B8"),
         "off": ("#EDEDED", "#4A4A4A"), "error": ("#FDE7E7", "#A3261B"), "link": "#1A66E0"}
DARK = {"bg": "#202020", "card": "#2B2B2B", "ink": "#F3F3F3", "muted": "#B3B3B3",
        "line": "#3A3A3A", "hover": "#323840", "accent": "#4C8DF6", "sel": "#22344F",
        "ok": ("#1F3B2A", "#7FD79D"), "busy": ("#1E3354", "#9EC2FF"),
        "off": ("#3A3A3A", "#C8C8C8"), "error": ("#4A2222", "#FFA8A0"), "link": "#8AB4FF"}
P = dict(LIGHT)


def apply_theme(root: tk.Tk):
    dark = system_dark()
    P.clear()
    P.update(DARK if dark else LIGHT)
    if sv_ttk is not None:
        try:
            sv_ttk.set_theme("dark" if dark else "light")
        except Exception:  # noqa: BLE001
            pass
    else:
        try:
            ttk.Style(root).theme_use("vista" if IS_WIN else "clam")
        except tk.TclError:
            pass
    root.configure(bg=P["bg"])
    st = ttk.Style(root)
    st.configure("Muted.TLabel", foreground=P["muted"])
    st.configure("Title.TLabel", font=(FONT, 20, "bold"))
    st.configure("H2.TLabel", font=(FONT, 13, "bold"))


def dark_titlebar(win: tk.Misc):
    """Тёмная рамка окна в Windows 10/11 при тёмной теме."""
    if not IS_WIN or P is LIGHT or P.get("bg") != DARK["bg"]:
        return
    try:
        import ctypes
        win.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(win.winfo_id())
        val = ctypes.c_int(1)
        for attr in (20, 19):
            if ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(val), 4) == 0:
                break
    except Exception:  # noqa: BLE001
        pass


def resource(*parts) -> str:
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    return os.path.join(base, *parts)


def set_window_icon(win: tk.Misc, cache: dict):
    ico = resource("assets", "icon.ico")
    try:
        if IS_WIN and os.path.exists(ico):
            win.iconbitmap(default=ico)
        if ImageTk is not None and "app" not in cache:
            cache["app"] = ImageTk.PhotoImage(draw_icon("#2176FF", 64))
        if "app" in cache:
            win.iconphoto(True, cache["app"])
    except tk.TclError:
        pass


_img_cache: dict = {}


def tile_image(cls, size=40):
    key = (cls.kind, size)
    if key not in _img_cache and ImageTk is not None:
        _img_cache[key] = ImageTk.PhotoImage(draw_icon(cls.tint, size, cls.icon))
    return _img_cache.get(key)


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


def link(parent, text, url, bg=None):
    """Кликабельная ссылка."""
    lab = tk.Label(parent, text=text, fg=P["link"], bg=bg or P["bg"], cursor="hand2",
                   font=(FONT, 10, "underline"), anchor="w", justify="left")
    lab.bind("<Button-1>", lambda e: webbrowser.open(url))
    return lab


class Scrollable(tk.Frame):
    """Прокручиваемая область для карточек и плиток."""

    def __init__(self, master, bg):
        super().__init__(master, bg=bg)
        self.canvas = tk.Canvas(self, bg=bg, highlightthickness=0, bd=0)
        self.bar = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.inner = tk.Frame(self.canvas, bg=bg)
        self.win = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self.canvas.configure(yscrollcommand=self.bar.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        self.bar.pack(side="right", fill="y")
        self.inner.bind("<Configure>", lambda e: self.canvas.configure(
            scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", lambda e: self.canvas.itemconfigure(self.win, width=e.width))
        self.bind_all("<MouseWheel>", self._wheel, add="+")

    def _wheel(self, e):
        try:
            if not self.winfo_exists():
                return
            w = self.winfo_containing(e.x_root, e.y_root)
        except (tk.TclError, KeyError):
            return
        while w is not None:
            if w is self:
                self.canvas.yview_scroll(int(-e.delta / 120), "units")
                return
            w = w.master


# ---------------- выбор облака ----------------
class AddCloudDialog(tk.Toplevel):
    def __init__(self, master):
        super().__init__(master, bg=P["bg"])
        self.transient(master)
        self.title("Добавить облако")
        self.geometry("760x600")
        self.minsize(560, 420)
        self.result = None
        body = ttk.Frame(self, padding=(24, 18, 24, 0))
        body.pack(fill="both", expand=True)
        ttk.Label(body, text="Какое облако подключить?", style="Title.TLabel").pack(anchor="w")
        ttk.Label(body, text="Выберите сервис — дальше программа подскажет, что ввести.",
                  style="Muted.TLabel").pack(anchor="w", pady=(2, 12))
        self.q = tk.StringVar()
        e = ttk.Entry(body, textvariable=self.q)
        e.pack(fill="x")
        e.insert(0, "")
        self.q.trace_add("write", lambda *a: self._fill())
        self.area = Scrollable(body, P["bg"])
        self.area.pack(fill="both", expand=True, pady=(12, 0))
        foot = ttk.Frame(self, padding=(24, 12))
        foot.pack(fill="x")
        ttk.Button(foot, text="Отмена", command=self.destroy).pack(side="right")
        self._fill()
        e.focus_set()
        dark_titlebar(self)
        self.grab_set()

    def _fill(self):
        for w in self.area.inner.winfo_children():
            w.destroy()
        q = self.q.get().strip().lower()
        kinds = [k for k, c in PROVIDERS.items() if not q or q in (c.title + " " + c.note).lower()]
        cols = 4
        for i in range(cols):
            self.area.inner.grid_columnconfigure(i, weight=1, uniform="t")
        for i, kind in enumerate(kinds):
            self._tile(kind).grid(row=i // cols, column=i % cols, sticky="nsew", padx=5, pady=5)
        if not kinds:
            tk.Label(self.area.inner, text="Ничего не найдено", bg=P["bg"], fg=P["muted"],
                     font=(FONT, 10)).grid(row=0, column=0, sticky="w")

    def _tile(self, kind):
        cls = PROVIDERS[kind]
        f = tk.Frame(self.area.inner, bg=P["card"], highlightthickness=1,
                     highlightbackground=P["line"], cursor="hand2", padx=12, pady=12)
        img = tile_image(cls, 34)
        parts = [f]
        if img is not None:
            il = tk.Label(f, image=img, bg=P["card"])
            il.pack(anchor="w")
            parts.append(il)
        t = tk.Label(f, text=cls.title, bg=P["card"], fg=P["ink"], font=(FONT, 10, "bold"),
                     anchor="w")
        t.pack(anchor="w", pady=(8, 0))
        n = tk.Label(f, text=cls.note, bg=P["card"], fg=P["muted"], font=(FONT, 8), anchor="w",
                     wraplength=150, justify="left")
        n.pack(anchor="w")
        parts += [t, n]

        def pick(e=None):
            self.result = kind
            self.destroy()

        def hover(on):
            color = P["hover"] if on else P["card"]
            for w in parts:
                w.configure(bg=color)
            f.configure(highlightbackground=P["accent"] if on else P["line"])
        for w in parts:
            w.bind("<Button-1>", pick)
            w.bind("<Enter>", lambda e: hover(True))
            w.bind("<Leave>", lambda e: hover(False))
        return f


# ---------------- настройки диска ----------------
class AccountDialog(tk.Toplevel):
    def __init__(self, master, acc: dict, taken_letters: set[str], new: bool):
        super().__init__(master, bg=P["bg"])
        self.transient(master)
        self.cls = PROVIDERS[acc["type"]]
        self.title(("Новый диск — " if new else "Настройки — ") + self.cls.title)
        self.resizable(False, False)
        self.result = None
        self.acc = acc
        self.data = dict(acc.get("data") or {})
        self.token = self.data.get("token")
        self.q: queue.Queue = queue.Queue()
        self.cancel = threading.Event()
        self.vars: dict[str, tk.StringVar] = {}

        frm = ttk.Frame(self, padding=(24, 18, 24, 18))
        frm.grid(sticky="nsew")
        frm.columnconfigure(1, weight=1)
        head = ttk.Frame(frm)
        head.grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 12))
        img = tile_image(self.cls, 40)
        if img is not None:
            tk.Label(head, image=img, bg=P["bg"]).pack(side="left", padx=(0, 12))
        ttk.Label(head, text=self.cls.title, style="Title.TLabel").pack(side="left")

        r = 1
        ttk.Label(frm, text="Название").grid(row=r, column=0, sticky="w", pady=4)
        self.name_var = tk.StringVar(value=acc.get("name") or self.cls.title)
        ttk.Entry(frm, textvariable=self.name_var, width=40).grid(row=r, column=1, sticky="we", pady=4)
        r += 1
        ttk.Label(frm, text="Буква диска").grid(row=r, column=0, sticky="w", pady=4)
        cur = (acc.get("letter") or "").upper()
        busy = (used_letters() | taken_letters) - {cur}
        letters = [c for c in "DEFGHIJKLMNOPQRSTUVWXYZ" if c not in busy]
        self.letter_var = tk.StringVar(value=cur or (letters[-1] if letters else ""))
        ttk.Combobox(frm, textvariable=self.letter_var, values=letters, state="readonly",
                     width=6).grid(row=r, column=1, sticky="w", pady=4)
        r += 1

        box = ttk.LabelFrame(frm, text="Подключение", padding=12)
        box.grid(row=r, column=0, columnspan=2, sticky="we", pady=(10, 6))
        box.columnconfigure(1, weight=1)
        br = 0
        for key, label, secret, default in self.cls.fields:
            ttk.Label(box, text=label).grid(row=br, column=0, sticky="w", pady=3, padx=(0, 10))
            v = tk.StringVar(value=self.data.get(key, default))
            ttk.Entry(box, textvariable=v, width=38, show="•" if secret else ""
                      ).grid(row=br, column=1, sticky="we", pady=3)
            self.vars[key] = v
            br += 1
        if self.cls.hint:
            hint = self.cls.hint
            if self.cls.oauth:
                hint = hint.replace("http://127.0.0.1:53682/", redirect_uri(self.cls.redirect_host))
            tk.Label(box, text=hint, bg=P["bg"], fg=P["muted"], justify="left", anchor="w",
                     font=(FONT, 9)).grid(row=br, column=0, columnspan=2, sticky="w", pady=(8, 2))
            br += 1
        for text, url in self.cls.links:
            link(box, "↗ " + text, url).grid(row=br, column=0, columnspan=2, sticky="w")
            br += 1
        if self.cls.oauth:
            ttk.Button(box, text="Войти через браузер", style="Accent.TButton",
                       command=self._login).grid(row=br, column=0, columnspan=2, sticky="w", pady=(10, 0))
            br += 1
        r += 1

        opts = ttk.LabelFrame(frm, text="Дополнительно", padding=12)
        opts.grid(row=r, column=0, columnspan=2, sticky="we", pady=6)
        self.auto_var = tk.BooleanVar(value=acc.get("automount", True))
        self.net_var = tk.BooleanVar(value=acc.get("netdrive", True))
        self.defer_var = tk.BooleanVar(value=acc.get("defer_large", True))
        for text, var in (("Подключать при запуске программы", self.auto_var),
                          ("Показывать как сетевой диск", self.net_var),
                          ("Большие файлы (от 64 МБ, контейнеры VeraCrypt) отправлять\n"
                           "в облако только при закрытии", self.defer_var)):
            ttk.Checkbutton(opts, text=text, variable=var).pack(anchor="w", pady=2)
        row = ttk.Frame(opts)
        row.pack(anchor="w", pady=(6, 0), fill="x")
        ttk.Label(row, text="Размер диска, ГБ").pack(side="left")
        self.size_var = tk.StringVar(value=str(acc.get("size_gb") or ""))
        ttk.Entry(row, textvariable=self.size_var, width=8).pack(side="left", padx=8)
        ttk.Label(row, text="пусто — как сообщает облако", style="Muted.TLabel").pack(side="left")
        row2 = ttk.Frame(opts)
        row2.pack(anchor="w", pady=(6, 0), fill="x")
        ttk.Label(row2, text="Значок диска (.ico)").pack(side="left")
        self.icon_var = tk.StringVar(value=acc.get("icon_path") or "")
        ttk.Entry(row2, textvariable=self.icon_var, width=26).pack(side="left", padx=8)
        ttk.Button(row2, text="Обзор…", command=self._pick_icon).pack(side="left")
        ttk.Label(opts, text="Пусто — цветной значок CloudMount для этого облака.",
                  style="Muted.TLabel").pack(anchor="w")
        r += 1

        self.status_var = tk.StringVar()
        ttk.Label(frm, textvariable=self.status_var, wraplength=460, style="Muted.TLabel"
                  ).grid(row=r, column=0, columnspan=2, sticky="w", pady=(8, 4))
        r += 1
        btns = ttk.Frame(frm)
        btns.grid(row=r, column=0, columnspan=2, sticky="e", pady=(6, 0))
        ttk.Button(btns, text="Проверить", command=self._test).pack(side="left", padx=4)
        ttk.Button(btns, text="Отмена", command=self._close).pack(side="left", padx=4)
        ttk.Button(btns, text="Сохранить", style="Accent.TButton", command=self._ok
                   ).pack(side="left", padx=4)

        self._show_token_status()
        self.protocol("WM_DELETE_WINDOW", self._close)
        self.after(200, self._poll)
        dark_titlebar(self)
        self.grab_set()

    def _pick_icon(self):
        p = filedialog.askopenfilename(parent=self, title="Значок диска",
                                       filetypes=[("Значки", "*.ico"), ("Все файлы", "*.*")])
        if p:
            self.icon_var.set(os.path.normpath(p))

    def _show_token_status(self):
        if self.cls.oauth:
            self.status_var.set("Вход выполнен ✓" if self.token else "Нужно войти через браузер")

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
            messagebox.showwarning("CloudMount", "Сначала укажите ключ приложения "
                                   "(Client ID / App key).", parent=self)
            return
        self.status_var.set("Откройте браузер и разрешите доступ…")
        self.cancel.clear()

        def work():
            try:
                self.q.put(("token", self.cls.login(d, cancel_event=self.cancel)))
            except Exception as e:  # noqa: BLE001
                self.q.put(("info", f"Вход не выполнен: {e}"))
        threading.Thread(target=work, daemon=True).start()

    def _test(self):
        acc = {"type": self.cls.kind, "data": self._collect()}
        self.status_var.set("Проверяю…")

        def work():
            try:
                p = make_provider(acc)
                n = len(p.list_dir("/"))
                total, used = p.quota()
                gb = 2 ** 30
                if total:
                    q = f", занято {used / gb:.1f} из {total / gb:.1f} ГБ"
                else:
                    q = ", облако не сообщает свой размер — его можно указать в поле «Размер диска»"
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
        d = self._collect()
        if not self.letter_var.get():
            messagebox.showwarning("CloudMount", "Выберите букву диска.", parent=self)
            return
        for key, label, secret, _ in self.cls.fields:
            if not d.get(key) and key not in self.cls.optional \
                    and not (key == "client_secret" and self.cls.kind == "gdrive"):
                messagebox.showwarning("CloudMount", f"Заполните поле «{label}».", parent=self)
                return
        if self.cls.oauth and not self.token:
            messagebox.showwarning("CloudMount", "Нажмите «Войти через браузер».", parent=self)
            return
        size_txt = self.size_var.get().strip().replace(",", ".")
        try:
            size_gb = float(size_txt) if size_txt else 0.0
            if size_gb < 0:
                raise ValueError
        except ValueError:
            messagebox.showwarning("CloudMount", "Размер диска — число в гигабайтах, например 10.",
                                   parent=self)
            return
        icon = self.icon_var.get().strip()
        if icon and not os.path.exists(icon):
            messagebox.showwarning("CloudMount", "Файл значка не найден.", parent=self)
            return
        acc = dict(self.acc)
        acc.update(name=self.name_var.get().strip() or self.cls.title,
                   letter=self.letter_var.get(), automount=self.auto_var.get(),
                   netdrive=self.net_var.get(), defer_large=self.defer_var.get(),
                   size_gb=size_gb, icon_path=icon, data=d)
        self.result = acc
        self._close()

    def _close(self):
        self.cancel.set()
        self.grab_release()
        self.destroy()


# ---------------- о программе ----------------
class AboutDialog(tk.Toplevel):
    def __init__(self, master):
        super().__init__(master, bg=P["bg"])
        self.transient(master)
        self.title("О программе")
        self.resizable(False, False)
        frm = ttk.Frame(self, padding=24)
        frm.pack(fill="both")
        head = ttk.Frame(frm)
        head.pack(anchor="w")
        if ImageTk is not None:
            self._img = ImageTk.PhotoImage(draw_icon("#2176FF", 56))
            tk.Label(head, image=self._img, bg=P["bg"]).pack(side="left", padx=(0, 14))
        t = ttk.Frame(head)
        t.pack(side="left")
        ttk.Label(t, text=about.APP_NAME, style="Title.TLabel").pack(anchor="w")
        ttk.Label(t, text=f"Версия {about.VERSION} · {about.LICENSE}", style="Muted.TLabel").pack(anchor="w")
        ttk.Label(frm, text=about.DESCRIPTION, justify="left").pack(anchor="w", pady=(14, 8))
        link(frm, "↗ Исходный код на GitHub", about.GITHUB_URL).pack(anchor="w")
        link(frm, "↗ Сайт программы", about.SITE_URL).pack(anchor="w")
        ttk.Label(frm, text=about.LICENSE_TEXT, style="Muted.TLabel", justify="left"
                  ).pack(anchor="w", pady=(12, 0))
        ttk.Label(frm, text="Использует " + about.WINFSP_NOTICE, style="Muted.TLabel",
                  justify="left").pack(anchor="w", pady=(10, 0))
        link(frm, "↗ " + about.WINFSP_URL, about.WINFSP_URL).pack(anchor="w")

        d = about.DONATE
        if any(d.values()):
            box = ttk.LabelFrame(frm, text="Поддержать автора", padding=12)
            box.pack(fill="x", pady=(16, 0))
            ttk.Label(box, text="Если программа полезна — можно отблагодарить переводом\n"
                                "по номеру телефона (СБП):", justify="left").grid(
                row=0, column=0, columnspan=3, sticky="w", pady=(0, 8))
            row = 1
            for label, key in (("Получатель", "name"), ("Банк", "bank"), ("Телефон", "phone")):
                if not d.get(key):
                    continue
                ttk.Label(box, text=label, style="Muted.TLabel").grid(row=row, column=0, sticky="w",
                                                                      padx=(0, 12), pady=2)
                ttk.Label(box, text=d[key], font=(FONT, 10, "bold")).grid(row=row, column=1, sticky="w")
                if key == "phone":
                    ttk.Button(box, text="Копировать номер",
                               command=lambda v=d[key]: self._copy(v)).grid(row=row, column=2, padx=(12, 0))
                row += 1
            self.copied = ttk.Label(box, text="", foreground="#2a8a2a")
            self.copied.grid(row=row, column=0, columnspan=3, sticky="w")
        ttk.Button(frm, text="Закрыть", command=self.destroy).pack(anchor="e", pady=(16, 0))
        dark_titlebar(self)
        self.grab_set()

    def _copy(self, value):
        self.clipboard_clear()
        self.clipboard_append(value.replace(" ", "").replace("-", ""))
        self.copied.configure(text="Номер скопирован ✓")


class ExitDialog(tk.Toplevel):
    """Ждёт, пока диски отправят изменения в облако, затем закрывает программу."""

    def __init__(self, app):
        super().__init__(app.root, bg=P["bg"])
        self.app = app
        self.title("CloudMount — выход")
        self.resizable(False, False)
        self.protocol("WM_DELETE_WINDOW", lambda: None)
        frm = ttk.Frame(self, padding=20)
        frm.pack()
        ttk.Label(frm, text="Отправляю изменения в облако и отключаю диски…", style="H2.TLabel").pack(anchor="w")
        self.var = tk.StringVar()
        ttk.Label(frm, textvariable=self.var, style="Muted.TLabel", wraplength=380).pack(anchor="w", pady=10)
        ttk.Button(frm, text="Выйти сразу", command=self._force).pack(anchor="e")
        dark_titlebar(self)
        self.after(300, self._wait)

    def _wait(self):
        mm = self.app.mm
        mm.poll()
        if not mm.any_running():
            self.app.finish()
            return
        self.var.set("\n".join(f"{a['name']}: {mm.card(a)['note']}" for a in self.app.accounts
                               if mm.running(a["id"])))
        self.after(500, self._wait)

    def _force(self):
        if messagebox.askyesno("CloudMount", "Выйти, не дожидаясь загрузки?\n"
                               "Несохранённые изменения останутся на компьютере и будут "
                               "отправлены в облако при следующем запуске.", parent=self):
            self.app.mm.unmount_all(force=True)
            self.app.finish()


# ---------------- карточка диска ----------------
class DriveCard(tk.Frame):
    def __init__(self, master, app, acc):
        super().__init__(master, bg=P["card"], highlightthickness=1,
                         highlightbackground=P["line"], padx=16, pady=14)
        self.app, self.acc = app, acc
        cls = PROVIDERS[acc["type"]]
        top = tk.Frame(self, bg=P["card"])
        top.pack(fill="x")
        img = tile_image(cls, 44)
        if img is not None:
            tk.Label(top, image=img, bg=P["card"]).pack(side="left", padx=(0, 12))
        names = tk.Frame(top, bg=P["card"])
        names.pack(side="left", fill="x", expand=True)
        tk.Label(names, text=acc.get("name") or cls.title, bg=P["card"], fg=P["ink"],
                 font=(FONT, 11, "bold"), anchor="w").pack(anchor="w")
        tk.Label(names, text=f"{(acc.get('letter') or '?').upper()}: · {cls.title}", bg=P["card"],
                 fg=P["muted"], font=(FONT, 9), anchor="w").pack(anchor="w")
        self.chip = tk.Label(top, text="", font=(FONT, 9, "bold"), padx=10, pady=2)
        self.chip.pack(side="right", anchor="n")
        self.bar = ttk.Progressbar(self, mode="determinate", maximum=100)
        self.bar.pack(fill="x", pady=(12, 4))
        self.note = tk.Label(self, text="", bg=P["card"], fg=P["muted"], font=(FONT, 9), anchor="w")
        self.note.pack(fill="x")
        btns = tk.Frame(self, bg=P["card"])
        btns.pack(fill="x", pady=(10, 0))
        self.more = ttk.Menubutton(btns, text="⋯", width=3)
        menu = tk.Menu(self.more, tearoff=0)
        menu.add_command(label="Настройки", command=lambda: app.edit(acc["id"]))
        menu.add_command(label="Журнал", command=lambda: app.show_log(acc["id"]))
        menu.add_separator()
        menu.add_command(label="Удалить диск", command=lambda: app.remove(acc["id"]))
        self.more["menu"] = menu
        self.more.pack(side="right", padx=(6, 0))
        self.primary = ttk.Button(btns, text="", width=11)
        self.primary.pack(side="right", padx=(6, 0))
        self.secondary = ttk.Button(btns, text="", width=11)
        self.secondary.pack(side="right")
        self._state = None
        self.refresh()

    def refresh(self):
        mm, aid = self.app.mm, self.acc["id"]
        c = mm.card(self.acc)
        bg, fg = P.get(c["state"], P["off"]) if c["state"] in ("ok", "busy", "off", "error") else P["off"]
        self.chip.configure(text=c["chip"], bg=bg, fg=fg)
        self.bar["value"] = c["progress"]
        self.note.configure(text=c["note"])
        running = mm.running(aid)
        state = (running, aid in mm.stopping)
        if state != self._state:
            self._state = state
            if running:
                self.secondary.configure(text="Отключить", command=lambda: self.app.unmount(aid),
                                         style="TButton")
                self.primary.configure(text="Открыть", command=lambda: self.app.open_drive(aid),
                                       style="TButton")
            else:
                self.secondary.configure(text="Настройки", command=lambda: self.app.edit(aid),
                                         style="TButton")
                self.primary.configure(text="Подключить", command=lambda: self.app.mount(aid),
                                       style="Accent.TButton")


# ---------------- напоминание о поддержке ----------------
class DonateBanner(tk.Frame):
    def __init__(self, master, app):
        super().__init__(master, bg=P["sel"], padx=16, pady=10)
        self.app = app
        tk.Label(self, text="♥", bg=P["sel"], fg=P["accent"], font=(FONT, 14)).pack(side="left", padx=(0, 10))
        txt = tk.Frame(self, bg=P["sel"])
        txt.pack(side="left", fill="x", expand=True)
        tk.Label(txt, text="CloudMount бесплатный. Если он вам полезен — поддержите автора.",
                 bg=P["sel"], fg=P["ink"], font=(FONT, 10, "bold"), anchor="w").pack(anchor="w")
        tk.Label(txt, text="RaiDrive и похожие программы просят подписку за полные возможности, "
                           "а CloudMount бесплатен навсегда.",
                 bg=P["sel"], fg=P["muted"], font=(FONT, 9), anchor="w").pack(anchor="w")
        close = tk.Label(self, text="✕", bg=P["sel"], fg=P["muted"], cursor="hand2", font=(FONT, 11))
        close.pack(side="right", padx=(10, 0))
        close.bind("<Button-1>", lambda e: self.later())
        ttk.Button(self, text="Больше не показывать", command=self.never).pack(side="right", padx=(8, 0))
        ttk.Button(self, text="Поддержать", style="Accent.TButton", command=self.support).pack(side="right")

    def support(self):
        self.app.show_about()

    def later(self):
        import time as _t
        config.set_setting("donate_snooze", _t.time() + 30 * 86400)   # напомним через месяц
        self.destroy()

    def never(self):
        config.set_setting("donate_never", True)
        self.destroy()


# ---------------- главное окно ----------------
class App:
    def __init__(self, minimized=False):
        self.root = tk.Tk()
        self._icons: dict = {}
        apply_theme(self.root)
        set_window_icon(self.root, self._icons)
        enable_clipboard(self.root)
        self.root.title("CloudMount")
        self.root.geometry("980x660")
        self.root.minsize(640, 460)
        self.mm = MountManager()
        self.q: queue.Queue = queue.Queue()
        self.accounts = config.load_accounts()
        self.cards: dict[str, DriveCard] = {}
        self.icon = None

        main = ttk.Frame(self.root, padding=(28, 20, 28, 14))
        main.pack(fill="both", expand=True)
        head = ttk.Frame(main)
        head.pack(fill="x")
        titles = ttk.Frame(head)
        titles.pack(side="left")
        ttk.Label(titles, text="Мои диски", style="Title.TLabel").pack(anchor="w")
        self.subtitle = ttk.Label(titles, text="", style="Muted.TLabel")
        self.subtitle.pack(anchor="w")
        ttk.Button(head, text="＋  Добавить облако", style="Accent.TButton",
                   command=self.add).pack(side="right")

        self.banner = None
        try:
            if donate_due():
                self.banner = DonateBanner(main, self)
                self.banner.pack(fill="x", pady=(14, 0))
        except Exception:  # noqa: BLE001
            self.banner = None
        self.area = Scrollable(main, P["bg"])
        self.area.pack(fill="both", expand=True, pady=(16, 10))
        for i in (0, 1):
            self.area.inner.grid_columnconfigure(i, weight=1, uniform="c")

        foot = ttk.Frame(main)
        foot.pack(fill="x")
        ttk.Separator(main).pack(fill="x", before=foot, pady=(0, 10))
        self.autostart_var = tk.BooleanVar(value=self._autostart_get())
        style = "Switch.TCheckbutton" if sv_ttk is not None else "TCheckbutton"
        ttk.Checkbutton(foot, text="Запускать вместе с Windows", variable=self.autostart_var,
                        command=self._autostart_set, style=style).pack(side="left")
        lk = link(foot, "О программе", "", bg=P["bg"])
        lk.configure(fg=P["muted"], font=(FONT, 10))
        lk.unbind("<Button-1>")
        lk.bind("<Button-1>", lambda e: self.show_about())
        lk.pack(side="right")
        lg = link(foot, "Журналы", "", bg=P["bg"])
        lg.configure(fg=P["muted"], font=(FONT, 10))
        lg.unbind("<Button-1>")
        lg.bind("<Button-1>", lambda e: open_path(config.local_dir("logs")))
        lg.pack(side="right", padx=18)

        self.root.protocol("WM_DELETE_WINDOW", self.hide)
        self.root.bind("<Unmap>", self._on_unmap)
        if not TEST_MODE:
            self._setup_tray()
        self.rebuild()
        dark_titlebar(self.root)
        if not TEST_MODE:
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
            pystray.MenuItem("Открыть CloudMount", lambda i, it: put(self.show), default=True),
            pystray.MenuItem("Подключить все", lambda i, it: put(self.mount_all)),
            pystray.MenuItem("Отключить все", lambda i, it: put(self.mm.unmount_all)),
            pystray.MenuItem("О программе", lambda i, it: put(self.show_about)),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Выход", lambda i, it: put(self.quit)),
        )
        self.icon = pystray.Icon("CloudMount", draw_icon("#2176FF", 64), "CloudMount", menu)
        self.icon.run_detached()

    def _on_unmap(self, event):
        # «Свернуть» тоже убирает окно в трей
        if event.widget is self.root and self.icon is not None and self.root.state() == "iconic":
            self.root.after(10, self.root.withdraw)

    def show(self):
        self.root.deiconify()
        self.root.state("normal")
        self.root.lift()
        self.root.focus_force()

    def hide(self):
        if self.icon is None:
            if TEST_MODE or messagebox.askyesno("CloudMount", "Выйти и отключить все диски?"):
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

    # ----- карточки -----
    def rebuild(self):
        for w in self.area.inner.winfo_children():
            w.destroy()
        self.cards = {}
        if not self.accounts:
            empty = tk.Frame(self.area.inner, bg=P["bg"], pady=60)
            empty.grid(row=0, column=0, columnspan=2)
            tk.Label(empty, text="Пока нет ни одного диска", bg=P["bg"], fg=P["ink"],
                     font=(FONT, 14, "bold")).pack()
            tk.Label(empty, text="Нажмите «Добавить облако», чтобы подключить первое хранилище.",
                     bg=P["bg"], fg=P["muted"], font=(FONT, 10)).pack(pady=(6, 14))
            ttk.Button(empty, text="Добавить облако", style="Accent.TButton", command=self.add).pack()
        for i, a in enumerate(self.accounts):
            card = DriveCard(self.area.inner, self, a)
            card.grid(row=i // 2, column=i % 2, sticky="nsew", padx=6, pady=6)
            self.cards[a["id"]] = card
        self._subtitle()

    def _subtitle(self):
        n = len(self.accounts)
        on = sum(1 for a in self.accounts if self.mm.running(a["id"]))
        self.subtitle.configure(text=f"Облаков: {n} · подключено: {on}" if n else
                                "Облака как обычные диски Windows")

    def _tick(self):
        try:
            while True:
                self.q.get_nowait()()
        except queue.Empty:
            pass
        if not self.root.winfo_exists():
            return
        self.mm.poll()
        for card in self.cards.values():
            card.refresh()
        self._subtitle()
        self.root.after(1000, self._tick)

    def _acc(self, acc_id):
        return next((a for a in self.accounts if a["id"] == acc_id), None)

    def _taken(self, except_id=None) -> set[str]:
        return {(a.get("letter") or "").upper() for a in self.accounts if a["id"] != except_id}

    def _reload(self):
        self.accounts = config.load_accounts()
        self.rebuild()

    # ----- действия -----
    def add(self):
        pick = AddCloudDialog(self.root)
        self.root.wait_window(pick)
        if not pick.result:
            return
        acc = config.new_account(pick.result)
        dlg = AccountDialog(self.root, acc, self._taken(), new=True)
        self.root.wait_window(dlg)
        if dlg.result:
            config.save_account(dlg.result)
            self._reload()
            self.mm.mount(dlg.result)

    def edit(self, acc_id):
        a = config.get_account(acc_id) or self._acc(acc_id)
        if a is None:
            return
        dlg = AccountDialog(self.root, a, self._taken(acc_id), new=False)
        self.root.wait_window(dlg)
        if dlg.result:
            config.save_account(dlg.result)
            self._reload()
            if self.mm.running(acc_id):
                self.mm.unmount(acc_id)
                self.mm.restart[acc_id] = dlg.result     # переподключим после отключения

    def remove(self, acc_id):
        a = self._acc(acc_id)
        if a and messagebox.askyesno("CloudMount", f"Удалить диск «{a['name']}»?\n"
                                     "Файлы в облаке не пострадают."):
            self.mm.restart.pop(acc_id, None)
            self.mm.unmount(acc_id)
            config.delete_account(acc_id)
            self._reload()

    def mount(self, acc_id):
        a = self._acc(acc_id)
        if a:
            self.mm.mount(a)

    def mount_all(self):
        for a in self.accounts:
            self.mm.mount(a)

    def unmount(self, acc_id):
        if not self.mm.running(acc_id):
            return
        if acc_id in self.mm.stopping:
            if messagebox.askyesno("CloudMount", "Диск ещё отправляет изменения в облако.\n"
                                   "Отключить принудительно? Изменения останутся на компьютере "
                                   "и догрузятся при следующем подключении."):
                self.mm.unmount(acc_id, force=True)
            return
        opened = self.mm.info(acc_id).get("open") or []
        if opened and not messagebox.askyesno(
                "CloudMount", "Сейчас открыты файлы: " + ", ".join(opened[:3]) + "\n\n"
                "Если это контейнер VeraCrypt — сначала отключите его в VeraCrypt,\n"
                "иначе последние изменения могут не попасть в облако.\n\nВсё равно отключить диск?"):
            return
        self.mm.unmount(acc_id)

    def open_drive(self, acc_id):
        a = self._acc(acc_id)
        if a and a.get("letter") and self.mm.running(acc_id):
            open_path(f"{a['letter']}:\\")

    def show_log(self, acc_id):
        p = log_path(acc_id)
        if os.path.exists(p):
            open_path(p)
        else:
            messagebox.showinfo("CloudMount", "Журнал пока пуст.")

    def show_about(self):
        self.show()
        AboutDialog(self.root)

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
            if messagebox.askyesno("CloudMount", "Не найден драйвер WinFsp, без него диски не работают.\n"
                                   "Обычно он ставится установщиком CloudMount. Открыть страницу WinFsp?"):
                webbrowser.open("https://winfsp.dev/rel/")

    def run(self):
        self.root.mainloop()


def _single_instance() -> bool:
    if not IS_WIN or TEST_MODE:
        return True
    import ctypes
    _single_instance.handle = ctypes.windll.kernel32.CreateMutexW(None, False, "CloudMount_single_instance")
    return ctypes.windll.kernel32.GetLastError() != 183  # ERROR_ALREADY_EXISTS


def run_gui(minimized=False):
    if IS_WIN:
        try:  # своя группа на панели задач и свой значок вместо значка Python
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("CloudMount.App")
        except Exception:  # noqa: BLE001
            pass
    if not _single_instance():
        r = tk.Tk()
        r.withdraw()
        messagebox.showinfo("CloudMount", "CloudMount уже запущен — значок в трее у часов.")
        return
    App(minimized).run()
