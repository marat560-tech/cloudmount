"""Буфер обмена в полях ввода: Ctrl+V/C/X/A при любой раскладке и меню по правой кнопке."""
from __future__ import annotations

import tkinter as tk

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


