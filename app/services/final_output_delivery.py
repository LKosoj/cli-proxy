"""Deduplicate final user-facing output delivery for a single session run.

When the same final payload is delivered twice within a short window (preview path
races, dual desktop notify, double-scheduled send_output), the second delivery is
suppressed so the user sees one clean final message.
"""

from __future__ import annotations

import hashlib
import logging
import time
from typing import Any, Optional

_log = logging.getLogger(__name__)

# Window long enough to cover scheduled send_output + desktop outer notify races,
# but short enough to allow intentional repeated identical answers later.
FINAL_OUTPUT_DEDUP_WINDOW_SEC = 45.0

_ATTR_SIG = "_final_output_delivery_sig"
_ATTR_TS = "_final_output_delivery_ts"


def _fingerprint(output: str) -> str:
    return hashlib.sha256(str(output or "").encode("utf-8", errors="replace")).hexdigest()


def should_deliver_final_output(
    session: Any,
    output: str,
    *,
    now: Optional[float] = None,
    window_sec: float = FINAL_OUTPUT_DEDUP_WINDOW_SEC,
) -> bool:
    """Return True if *output* should be delivered to the user.

    Empty output is never delivered. Identical non-empty output for the same
    session object within *window_sec* is treated as a duplicate.
    """
    text = str(output or "").strip()
    if not text:
        return False
    if session is None:
        return True

    ts = float(time.monotonic() if now is None else now)
    sig = _fingerprint(text)
    prev_sig = str(getattr(session, _ATTR_SIG, "") or "")
    try:
        prev_ts = float(getattr(session, _ATTR_TS, 0.0) or 0.0)
    except (TypeError, ValueError):
        prev_ts = 0.0

    if prev_sig == sig and prev_ts > 0.0 and (ts - prev_ts) <= float(window_sec):
        _log.info(
            "skip duplicate final output delivery session=%s output_len=%d window_sec=%.1f",
            getattr(session, "id", "?"),
            len(text),
            float(window_sec),
        )
        return False

    try:
        setattr(session, _ATTR_SIG, sig)
        setattr(session, _ATTR_TS, ts)
    except Exception:
        _log.debug(
            "failed to store final output delivery fingerprint session=%s",
            getattr(session, "id", "?"),
            exc_info=True,
        )
    return True


def clear_final_output_delivery_guard(session: Any) -> None:
    """Reset delivery guard (e.g. at the start of a new prompt run)."""
    if session is None:
        return
    try:
        setattr(session, _ATTR_SIG, "")
        setattr(session, _ATTR_TS, 0.0)
    except Exception:
        _log.debug(
            "failed to clear final output delivery guard session=%s",
            getattr(session, "id", "?"),
            exc_info=True,
        )
