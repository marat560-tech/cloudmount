"""Дымовой тест окна: открывает главное окно и все диалоги (на Windows в CI)."""
import os
import subprocess
import sys
import textwrap

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

SCRIPT = textwrap.dedent(r"""
    import sys, tkinter as tk
    sys.path.insert(0, ROOT)
    from cloudmount import config, gui
    from cloudmount.providers import PROVIDERS
    for i, kind in enumerate(PROVIDERS):
        a = config.new_account(kind)
        a.update(name="Диск " + kind, letter="DEFGHIJKLMNOPQRSTUVWXYZ"[i])
        config.save_account(a)
    config.set_setting("first_run", 1)          # «давно пользуется» — покажем строку о поддержке
    app = gui.App()
    app.root.update()
    assert app.banner is not None
    app.banner.later(); app.root.update()
    assert not gui.donate_due()
    assert len(app.cards) == len(PROVIDERS)
    for card in app.cards.values():
        card.refresh()
    d = gui.AddCloudDialog(app.root); app.root.update(); d.q.set("диск"); app.root.update(); d.destroy()
    for a in app.accounts:
        dlg = gui.AccountDialog(app.root, a, set(), new=False); app.root.update(); dlg._close()
    ab = gui.AboutDialog(app.root); app.root.update(); ab.destroy()
    app.finish()
    print("GUI-OK")
""")


def test_gui_smoke(tmp_path):
    try:
        import tkinter as tk
        r = tk.Tk()
        r.destroy()
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"нет графической среды: {e}")
    env = dict(os.environ, CLOUDMOUNT_TEST="1", APPDATA=str(tmp_path / "roaming"),
               LOCALAPPDATA=str(tmp_path / "local"), HOME=str(tmp_path))
    out = subprocess.run([sys.executable, "-c", f"ROOT={ROOT!r}\n" + SCRIPT], env=env,
                         capture_output=True, text=True, timeout=120)
    assert "GUI-OK" in out.stdout, out.stdout + out.stderr
