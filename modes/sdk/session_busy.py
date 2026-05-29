from __future__ import annotations

from typing import Any


def is_session_busy(session: Any, run_lock: Any) -> bool:
    if session is None:
        return False
    busy = bool(getattr(session, "busy", False))
    locked = False
    if run_lock is not None and hasattr(run_lock, "locked"):
        try:
            locked = bool(run_lock.locked())
        except Exception:
            locked = False
    ticking = False
    is_active_by_tick = getattr(session, "is_active_by_tick", None)
    if callable(is_active_by_tick):
        try:
            ticking = bool(is_active_by_tick())
        except Exception:
            ticking = False
    return bool(busy or locked or ticking)
