from __future__ import annotations

import pytest

from app.services.rich_draft_coordinator import (
    DRAFT_ID_MAX,
    DRAFT_PREVIEW_TEXT_LIMIT,
    MAX_REFRESH_INTERVAL_SECONDS,
    REFRESH_INTERVAL_SECONDS,
    RICH_DRAFT_TEXT_LIMIT,
    RichDraftCoordinator,
    build_draft_text,
    format_timer,
    limit_preview_text,
    stable_draft_id,
)


def test_stable_draft_id_is_repeatable_and_non_zero() -> None:
    draft_id = stable_draft_id("session-42:run-7")

    assert draft_id == stable_draft_id("session-42:run-7")
    assert draft_id != stable_draft_id("session-42:run-8")
    assert 0 < draft_id <= DRAFT_ID_MAX


@pytest.mark.parametrize(
    ("elapsed_seconds", "expected"),
    [
        (-5, "00:00"),
        (0, "00:00"),
        (9.9, "00:09"),
        (65, "01:05"),
        (3605, "60:05"),
    ],
)
def test_format_timer_uses_mm_ss(elapsed_seconds: float, expected: str) -> None:
    assert format_timer(elapsed_seconds) == expected


def test_refresh_constants_stay_inside_raw_api_window() -> None:
    assert REFRESH_INTERVAL_SECONDS == 20.0
    assert MAX_REFRESH_INTERVAL_SECONDS <= 25.0


def test_limit_preview_text_caps_body_before_rich_limit() -> None:
    text = "x" * (DRAFT_PREVIEW_TEXT_LIMIT + 50)

    preview = limit_preview_text(text)
    rendered = build_draft_text(text, started_at=100.0, now=125.0)

    assert len(preview) == DRAFT_PREVIEW_TEXT_LIMIT
    assert preview.endswith("...")
    assert len(rendered) <= RICH_DRAFT_TEXT_LIMIT
    assert rendered.startswith("00:25")


def test_update_reuses_draft_id_and_keeps_timer_from_first_output() -> None:
    coordinator = RichDraftCoordinator()

    first = coordinator.update("run-a", "first text", now=100.0)
    second = coordinator.update("run-a", "current text", now=145.0)

    assert second.draft_id == first.draft_id
    assert second.timer == "00:45"
    assert second.text.startswith("00:45")
    assert "current text" in second.text
    assert "first text" not in second.text
    assert second.started_at == 100.0
    assert second.updated_at == 145.0


def test_refresh_interval_rejects_values_over_limit() -> None:
    with pytest.raises(ValueError):
        RichDraftCoordinator(refresh_interval_seconds=25.1)


def test_cancel_removes_state_by_run_key() -> None:
    coordinator = RichDraftCoordinator()
    coordinator.update("run-a", "text", now=1.0)

    assert coordinator.get_state("run-a") is not None
    assert coordinator.cancel("run-a") is True
    assert coordinator.get_state("run-a") is None
    assert coordinator.cancel("run-a") is False
