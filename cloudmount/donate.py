"""Когда показывать ненавязчивую строку «поддержите автора»."""
from __future__ import annotations

import json
import os
import time

from . import config

DONATE_AFTER_DAYS = 14
DONATE_AFTER_BYTES = 10 * 2**30


def uploaded_total() -> int:
    total = 0
    d = config.local_dir("stats")
    for fn in os.listdir(d):
        if fn.endswith(".json"):
            try:
                with open(os.path.join(d, fn), encoding="utf-8") as f:
                    total += int(json.load(f).get("uploaded", 0))
            except (OSError, ValueError):
                pass
    return total


def donate_due(now: float | None = None) -> bool:
    """Показывать строку о поддержке? Только после пользы и если не скрыта."""
    now = now or time.time()
    if config.get_setting("donate_never") or now < float(config.get_setting("donate_snooze", 0) or 0):
        return False
    first = float(config.get_setting("first_run", 0) or 0)
    if not first:
        config.set_setting("first_run", now)
        return False
    return now - first >= DONATE_AFTER_DAYS * 86400 or uploaded_total() >= DONATE_AFTER_BYTES


