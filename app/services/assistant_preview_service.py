from __future__ import annotations

import asyncio
import time
from typing import Any, Awaitable, Callable, Optional

from utils.text import is_time_only_text, strip_ansi


ASSISTANT_PREVIEW_MAX_CHARS = 3500
ASSISTANT_PREVIEW_MARKER = "⏳"
ASSISTANT_PREVIEW_POLL_INTERVAL_SEC = 0.35
ASSISTANT_PREVIEW_TIMER_REFRESH_INTERVAL_SEC = 20.0


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

    # Take raw suffix but skip a leading partial word (if any) so the preview
    # does not start with garbage like "еременная" / "nv-переменная".
    # We only ever drop the first word *fragment*; normal spaces later are kept.
    oversize = max_len - 3
    tail = raw[-oversize:]
    if len(raw) > oversize and tail:
        # Does the cut land in the middle of a word?
        prev = raw[-(oversize + 1)] if len(raw) > oversize + 1 else ""
        starts_mid_word = bool(tail[0]) and not tail[0].isspace() and (prev.isalnum() or prev in "_-")
        if starts_mid_word:
            # Find first break (space or punctuation) and drop the fragment
            for i, ch in enumerate(tail[:50]):
                if ch.isspace() or ch in ".,;:!?—–()[]{}«»'\"-":
                    tail = tail[i + 1 :].lstrip()
                    break
    if not tail:
        tail = raw[-oversize:]
    shown = "..." + tail if len(raw) > len(tail) + 5 else tail
    return prefix + shown + truncated_suffix


def _build_elapsed_preview_text(text: str, elapsed_seconds: float) -> str:
    value = str(text or "").strip()
    marker_prefix = ASSISTANT_PREVIEW_MARKER + " "
    body = value[len(marker_prefix):] if value.startswith(marker_prefix) else value
    total_seconds = max(0, int(elapsed_seconds))
    minutes, seconds = divmod(total_seconds, 60)
    header = f"{ASSISTANT_PREVIEW_MARKER} {minutes:02d}:{seconds:02d}"
    return f"{header}\n\n{body}" if body else header


async def watch_session_assistant_preview(
    session: Any,
    *,
    emit_update: Callable[[str], Awaitable[None]],
    stop_event: asyncio.Event,
    poll_interval_sec: float = ASSISTANT_PREVIEW_POLL_INTERVAL_SEC,
    build_text: Callable[[Any], Optional[str]] = build_assistant_preview_text,
    refresh_interval_sec: Optional[float] = None,
    include_elapsed_time: bool = False,
    clock: Callable[[], float] = time.monotonic,
) -> None:
    last_sent_source: Optional[str] = None
    last_sent_at: Optional[float] = None
    started_at: Optional[float] = None
    interval = max(0.05, float(poll_interval_sec))
    refresh_interval = None
    if refresh_interval_sec is not None:
        refresh_interval = max(interval, float(refresh_interval_sec))
    while True:
        source = build_text(getattr(session, "last_assistant_text_value", None))
        now = clock()
        should_refresh = (
            source
            and refresh_interval is not None
            and last_sent_source is not None
            and last_sent_at is not None
            and now - last_sent_at >= refresh_interval
        )
        if source and (source != last_sent_source or should_refresh):
            if started_at is None:
                started_at = now
            current = (
                _build_elapsed_preview_text(source, now - started_at)
                if include_elapsed_time
                else source
            )
            await emit_update(current)
            last_sent_source = source
            last_sent_at = now
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
            return
        except asyncio.TimeoutError:
            continue
