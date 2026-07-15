"""Tests for final output delivery deduplication."""

from __future__ import annotations

from types import SimpleNamespace

from app.services.final_output_delivery import (
    FINAL_OUTPUT_DEDUP_WINDOW_SEC,
    clear_final_output_delivery_guard,
    should_deliver_final_output,
)


def test_should_deliver_final_output_allows_first_and_blocks_duplicate() -> None:
    session = SimpleNamespace(id="s1")
    text = "Финальный ответ"

    assert should_deliver_final_output(session, text, now=100.0) is True
    assert should_deliver_final_output(session, text, now=100.0 + 1.0) is False
    assert should_deliver_final_output(session, text, now=100.0 + FINAL_OUTPUT_DEDUP_WINDOW_SEC - 0.1) is False


def test_should_deliver_final_output_allows_after_window() -> None:
    session = SimpleNamespace(id="s1")
    text = "Финальный ответ"

    assert should_deliver_final_output(session, text, now=100.0) is True
    assert (
        should_deliver_final_output(
            session,
            text,
            now=100.0 + FINAL_OUTPUT_DEDUP_WINDOW_SEC + 0.1,
        )
        is True
    )


def test_should_deliver_final_output_allows_different_text() -> None:
    session = SimpleNamespace(id="s1")
    assert should_deliver_final_output(session, "A", now=10.0) is True
    assert should_deliver_final_output(session, "B", now=10.5) is True


def test_should_deliver_final_output_rejects_empty() -> None:
    session = SimpleNamespace(id="s1")
    assert should_deliver_final_output(session, "   ", now=1.0) is False
    assert should_deliver_final_output(session, "", now=1.0) is False


def test_clear_final_output_delivery_guard_resets_window() -> None:
    session = SimpleNamespace(id="s1")
    text = "same"
    assert should_deliver_final_output(session, text, now=50.0) is True
    assert should_deliver_final_output(session, text, now=51.0) is False
    clear_final_output_delivery_guard(session)
    assert should_deliver_final_output(session, text, now=51.5) is True
