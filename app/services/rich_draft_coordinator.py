from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass
from typing import Callable


REFRESH_INTERVAL_SECONDS = 20.0
MAX_REFRESH_INTERVAL_SECONDS = 25.0
DRAFT_ID_MAX = 2_147_483_647
DRAFT_PREVIEW_TEXT_LIMIT = 4000
RICH_DRAFT_TEXT_LIMIT = 4096
TRUNCATION_SUFFIX = "..."

ClockFn = Callable[[], float]


@dataclass(slots=True)
class RichDraftState:
    run_key: str
    draft_id: int
    started_at: float
    updated_at: float
    current_text: str


@dataclass(frozen=True, slots=True)
class RichDraftPayload:
    run_key: str
    draft_id: int
    text: str
    timer: str
    started_at: float
    updated_at: float
    refresh_after_seconds: float


def stable_draft_id(run_key: str) -> int:
    digest = hashlib.blake2s(str(run_key).encode("utf-8"), digest_size=8).digest()
    return (int.from_bytes(digest, "big") % DRAFT_ID_MAX) + 1


def format_timer(elapsed_seconds: float) -> str:
    total_seconds = max(0, int(elapsed_seconds))
    minutes, seconds = divmod(total_seconds, 60)
    return f"{minutes:02d}:{seconds:02d}"


def limit_preview_text(text: str, *, max_chars: int = DRAFT_PREVIEW_TEXT_LIMIT) -> str:
    value = str(text or "")
    limit = max(0, int(max_chars))
    if len(value) <= limit:
        return value
    if limit <= len(TRUNCATION_SUFFIX):
        return value[:limit]
    return value[: limit - len(TRUNCATION_SUFFIX)] + TRUNCATION_SUFFIX


def build_draft_text(
    current_text: str,
    *,
    started_at: float,
    now: float,
    preview_limit: int = DRAFT_PREVIEW_TEXT_LIMIT,
    rich_limit: int = RICH_DRAFT_TEXT_LIMIT,
) -> str:
    header = format_timer(now - started_at)
    limit = max(0, int(rich_limit))
    if len(header) >= limit:
        return limit_preview_text(header, max_chars=limit)

    separator = "\n\n" if current_text else ""
    available_preview = min(
        max(0, int(preview_limit)),
        max(0, limit - len(header) - len(separator)),
    )
    preview = limit_preview_text(current_text, max_chars=available_preview)
    if not preview:
        return header
    return f"{header}{separator}{preview}"


class RichDraftCoordinator:
    def __init__(
        self,
        *,
        clock: ClockFn | None = None,
        refresh_interval_seconds: float = REFRESH_INTERVAL_SECONDS,
    ) -> None:
        if refresh_interval_seconds <= 0 or refresh_interval_seconds > MAX_REFRESH_INTERVAL_SECONDS:
            raise ValueError("refresh_interval_seconds must be within the raw API refresh window")
        self.refresh_interval_seconds = float(refresh_interval_seconds)
        self._clock = clock or time.monotonic
        self._states_by_run_key: dict[str, RichDraftState] = {}

    def update(self, run_key: str, current_text: str, *, now: float | None = None) -> RichDraftPayload:
        timestamp = self._timestamp(now)
        key = str(run_key)
        state = self._states_by_run_key.get(key)
        if state is None:
            state = RichDraftState(
                run_key=key,
                draft_id=stable_draft_id(key),
                started_at=timestamp,
                updated_at=timestamp,
                current_text=str(current_text or ""),
            )
            self._states_by_run_key[key] = state
        else:
            state.current_text = str(current_text or "")
            state.updated_at = timestamp
        return self._build_payload(state)

    def get_state(self, run_key: str) -> RichDraftState | None:
        return self._states_by_run_key.get(str(run_key))

    def cancel(self, run_key: str) -> bool:
        key = str(run_key)
        state = self._states_by_run_key.pop(key, None)
        if state is None:
            return False
        return True

    def _build_payload(self, state: RichDraftState) -> RichDraftPayload:
        return RichDraftPayload(
            run_key=state.run_key,
            draft_id=state.draft_id,
            text=build_draft_text(
                state.current_text,
                started_at=state.started_at,
                now=state.updated_at,
            ),
            timer=format_timer(state.updated_at - state.started_at),
            started_at=state.started_at,
            updated_at=state.updated_at,
            refresh_after_seconds=self.refresh_interval_seconds,
        )

    def _timestamp(self, now: float | None) -> float:
        if now is not None:
            return float(now)
        return float(self._clock())
