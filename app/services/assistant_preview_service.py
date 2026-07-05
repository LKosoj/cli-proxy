from __future__ import annotations

import asyncio
import time
from typing import Any, Awaitable, Callable, Optional

from utils.text import is_time_only_text, strip_ansi


ASSISTANT_PREVIEW_MAX_CHARS = 1500
ASSISTANT_PREVIEW_MARKER = "⏳"
ASSISTANT_PREVIEW_POLL_INTERVAL_SEC = 0.35


def assistant_preview_enabled(config: Any) -> bool:
    defaults = getattr(config, "defaults", None)
    return bool(getattr(defaults, "assistant_preview_enabled", False))


def assistant_preview_supported_dest(dest: Optional[dict]) -> bool:
    kind = str((dest or {}).get("kind") or "telegram").strip().lower()
    return kind in {"telegram", "desktop"}


def build_assistant_preview_text(text: Any, *, limit: int = ASSISTANT_PREVIEW_MAX_CHARS) -> Optional[str]:
    raw = strip_ansi(str(text or "")).strip()
    if not raw:
        return None
    if is_time_only_text(raw):
        return None
    max_len = max(1, int(limit))
    prefix = ASSISTANT_PREVIEW_MARKER + " "
    truncated_suffix = " ✂️"
    if len(raw) <= max_len:
        return prefix + raw
    if max_len <= 3:
        return prefix + raw[-max_len:] + truncated_suffix
    return prefix + "..." + raw[-(max_len - 3):] + truncated_suffix


async def watch_session_assistant_preview(
    session: Any,
    *,
    emit_update: Callable[[str], Awaitable[None]],
    stop_event: asyncio.Event,
    poll_interval_sec: float = ASSISTANT_PREVIEW_POLL_INTERVAL_SEC,
    build_text: Callable[[Any], Optional[str]] = build_assistant_preview_text,
    refresh_interval_sec: Optional[float] = None,
) -> None:
    last_sent: Optional[str] = None
    last_sent_at: Optional[float] = None
    interval = max(0.05, float(poll_interval_sec))
    refresh_interval = None
    if refresh_interval_sec is not None:
        refresh_interval = max(interval, float(refresh_interval_sec))
    while True:
        current = build_text(getattr(session, "last_assistant_text_value", None))
        now = time.monotonic()
        should_refresh = (
            current
            and refresh_interval is not None
            and last_sent is not None
            and last_sent_at is not None
            and now - last_sent_at >= refresh_interval
        )
        if current and (current != last_sent or should_refresh):
            await emit_update(current)
            last_sent = current
            last_sent_at = now
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
            return
        except asyncio.TimeoutError:
            continue
