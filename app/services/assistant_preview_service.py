from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import timedelta
import logging
import time
from typing import Any, Awaitable, Callable, Optional

from telegram.error import RetryAfter

from utils.text import is_time_only_text, strip_ansi


ASSISTANT_PREVIEW_MAX_CHARS = 3500
ASSISTANT_PREVIEW_MARKER = "⏳"
ASSISTANT_PREVIEW_POLL_INTERVAL_SEC = 0.35
ASSISTANT_PREVIEW_TEXT_EDIT_INTERVAL_SEC = 5.0
ASSISTANT_PREVIEW_TIMER_REFRESH_INTERVAL_SEC = 120.0

_log = logging.getLogger(__name__)


@dataclass
class _PendingPreviewUpdate:
    owner: str
    owner_version: int
    timer_only: bool
    apply_update: Callable[[], Awaitable[None]]


@dataclass
class _ChatPreviewRateState:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    wake_event: asyncio.Event = field(default_factory=asyncio.Event)
    pending: Optional[_PendingPreviewUpdate] = None
    worker: Optional[asyncio.Task] = None
    last_request_at: Optional[float] = None
    cooldown_started_at: Optional[float] = None
    cooldown_until: float = 0.0
    cooldown_suppressed_updates: int = 0
    owner_versions: dict[str, int] = field(default_factory=dict)


class AssistantPreviewRateLimiter:
    def __init__(
        self,
        *,
        text_edit_interval_sec: float = ASSISTANT_PREVIEW_TEXT_EDIT_INTERVAL_SEC,
        timer_edit_interval_sec: float = ASSISTANT_PREVIEW_TIMER_REFRESH_INTERVAL_SEC,
    ) -> None:
        self._text_edit_interval_sec = text_edit_interval_sec
        self._timer_edit_interval_sec = timer_edit_interval_sec
        self._states: dict[int, _ChatPreviewRateState] = {}

    async def submit(
        self,
        *,
        chat_id: int,
        owner: str,
        timer_only: bool,
        apply_update: Callable[[], Awaitable[None]],
    ) -> None:
        state = self._states.setdefault(chat_id, _ChatPreviewRateState())
        async with state.lock:
            if time.monotonic() < state.cooldown_until:
                state.cooldown_suppressed_updates += 1
            state.pending = _PendingPreviewUpdate(
                owner=owner,
                owner_version=state.owner_versions.get(owner, 0),
                timer_only=timer_only,
                apply_update=apply_update,
            )
            state.wake_event.set()
            if state.worker is None or state.worker.done():
                state.worker = asyncio.create_task(
                    self._run_worker(chat_id, state),
                    name=f"assistant-preview-rate-limit:{chat_id}",
                )

    async def cancel(self, *, chat_id: int, owner: str) -> None:
        state = self._states.get(chat_id)
        if state is None:
            return
        async with state.lock:
            state.owner_versions[owner] = state.owner_versions.get(owner, 0) + 1
            if state.pending is not None and state.pending.owner == owner:
                state.pending = None
            state.wake_event.set()

    def _request_due_at(
        self,
        state: _ChatPreviewRateState,
        update: _PendingPreviewUpdate,
    ) -> float:
        interval = (
            self._timer_edit_interval_sec
            if update.timer_only
            else self._text_edit_interval_sec
        )
        rate_due_at = 0.0
        if state.last_request_at is not None:
            rate_due_at = state.last_request_at + interval
        return max(rate_due_at, state.cooldown_until)

    async def _run_worker(self, chat_id: int, state: _ChatPreviewRateState) -> None:
        while True:
            async with state.lock:
                update = state.pending
                if update is None:
                    state.worker = None
                    return
                delay = self._request_due_at(state, update) - time.monotonic()
                state.wake_event.clear()
            if delay > 0:
                try:
                    await asyncio.wait_for(state.wake_event.wait(), timeout=delay)
                except asyncio.TimeoutError:
                    pass
                continue

            async with state.lock:
                update = state.pending
                if update is None:
                    continue
                now = time.monotonic()
                if self._request_due_at(state, update) > now:
                    continue
                state.pending = None
                state.last_request_at = now
                if state.cooldown_started_at is not None:
                    duration = max(0.0, state.cooldown_until - state.cooldown_started_at)
                    _log.info(
                        "assistant preview cooldown finished chat_id=%s duration=%.3fs "
                        "suppressed_updates=%s",
                        chat_id,
                        duration,
                        state.cooldown_suppressed_updates,
                    )
                    state.cooldown_started_at = None
                    state.cooldown_suppressed_updates = 0

            try:
                await update.apply_update()
            except RetryAfter as exc:
                await self._start_cooldown(chat_id, state, update, exc)
            except Exception:
                _log.exception("assistant preview update failed chat_id=%s", chat_id)

    async def _start_cooldown(
        self,
        chat_id: int,
        state: _ChatPreviewRateState,
        update: _PendingPreviewUpdate,
        exc: RetryAfter,
    ) -> None:
        retry_after = exc.retry_after
        duration = (
            retry_after.total_seconds()
            if isinstance(retry_after, timedelta)
            else float(retry_after)
        )
        duration = max(0.0, duration)
        async with state.lock:
            now = time.monotonic()
            if state.owner_versions.get(update.owner, 0) == update.owner_version:
                if state.pending is None:
                    state.pending = update
                else:
                    state.cooldown_suppressed_updates += 1
            state.cooldown_started_at = now
            state.cooldown_until = now + duration
            state.wake_event.set()
            suppressed_updates = state.cooldown_suppressed_updates
        _log.warning(
            "assistant preview cooldown started chat_id=%s duration=%.3fs suppressed_updates=%s",
            chat_id,
            duration,
            suppressed_updates,
        )


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
                    tail = tail[i + 1:].lstrip()
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
    emit_update: Callable[[str, bool], Awaitable[None]],
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
            await emit_update(
                current,
                bool(should_refresh and source == last_sent_source),
            )
            last_sent_source = source
            last_sent_at = now
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
            return
        except asyncio.TimeoutError:
            continue
