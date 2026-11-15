import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

SCRIPT = r"""
import sys, os, json, time
sys.path.insert(0, ROOT)
from cloudmount import config, donate
now = time.time()
assert donate.donate_due(now) is False                     # первый запуск — только запоминаем дату
assert donate.donate_due(now + 3 * 86400) is False          # три дня — рано
assert donate.donate_due(now + 15 * 86400) is True          # две недели — пора
# или после 10 ГБ, отправленных в облака
config.set_setting("first_run", now)
with open(os.path.join(config.local_dir("stats"), "a.json"), "w") as f:
    json.dump({"uploaded": 11 * 2**30}, f)
assert donate.donate_due(now + 60) is True
config.set_setting("donate_snooze", now + 30 * 86400)       # «✕» — напомнить через месяц
assert donate.donate_due(now + 60) is False
assert donate.donate_due(now + 31 * 86400) is True
config.set_setting("donate_never", True)                    # «Больше не показывать»
assert donate.donate_due(now + 365 * 86400) is False
print("DONATE-OK")
"""


def test_donate_schedule(tmp_path):
    env = dict(os.environ, APPDATA=str(tmp_path / "r"), LOCALAPPDATA=str(tmp_path / "l"),
               HOME=str(tmp_path))
    out = subprocess.run([sys.executable, "-c", f"ROOT={ROOT!r}\n" + SCRIPT], env=env,
                         capture_output=True, text=True, timeout=60)
    assert "DONATE-OK" in out.stdout, out.stdout + out.stderr
