from __future__ import annotations

from pathlib import Path

import pytest

from app.services.cli_limits_trend import UsageTrendTracker
from app.services.state_repository import JsonStateRepository


@pytest.fixture()
def tracker(tmp_path: Path) -> UsageTrendTracker:
    return UsageTrendTracker(JsonStateRepository(tmp_path / "state.json"))


def _advance(monkeypatch: pytest.MonkeyPatch, value: float) -> None:
    monkeypatch.setattr("app.services.cli_limits_trend.time.time", lambda: value)


def test_first_measurement_has_no_trend(tracker: UsageTrendTracker, monkeypatch: pytest.MonkeyPatch) -> None:
    _advance(monkeypatch, 1_000.0)

    assert tracker.record("claude:five_hour", 10.0, window_marker="w1") is None


def test_trend_reports_rate_and_forecast(tracker: UsageTrendTracker, monkeypatch: pytest.MonkeyPatch) -> None:
    _advance(monkeypatch, 1_000.0)
    tracker.record("claude:five_hour", 10.0, window_marker="w1")
    _advance(monkeypatch, 1_000.0 + 3600.0)

    trend = tracker.record("claude:five_hour", 20.0, window_marker="w1")

    assert trend is not None
    assert trend.percent_per_hour == pytest.approx(10.0)
    # 80% остатка при 10%/ч — восемь часов.
    assert trend.seconds_to_exhaust == pytest.approx(8 * 3600.0)


def test_trend_is_skipped_until_history_spans_ten_minutes(
    tracker: UsageTrendTracker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _advance(monkeypatch, 1_000.0)
    tracker.record("codex:primary", 10.0, window_marker="w1")
    _advance(monkeypatch, 1_000.0 + 300.0)

    assert tracker.record("codex:primary", 12.0, window_marker="w1") is None

    _advance(monkeypatch, 1_000.0 + 900.0)

    assert tracker.record("codex:primary", 13.0, window_marker="w1") is not None


def test_measurements_closer_than_a_minute_are_not_stored(
    tracker: UsageTrendTracker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _advance(monkeypatch, 1_000.0)
    tracker.record("codex:secondary", 10.0, window_marker="w1")
    _advance(monkeypatch, 1_030.0)
    tracker.record("codex:secondary", 12.0, window_marker="w1")

    bucket = tracker._repository.read_namespace(UsageTrendTracker.NAMESPACE)

    assert bucket["codex:secondary"]["points"] == [[1_000.0, 10.0]]


def test_trend_is_skipped_when_rate_is_noise(tracker: UsageTrendTracker, monkeypatch: pytest.MonkeyPatch) -> None:
    _advance(monkeypatch, 1_000.0)
    tracker.record("codex:primary", 10.0, window_marker="w1")
    _advance(monkeypatch, 1_000.0 + 3600.0)

    assert tracker.record("codex:primary", 10.0, window_marker="w1") is None


def test_history_resets_when_text_window_changes(tracker: UsageTrendTracker, monkeypatch: pytest.MonkeyPatch) -> None:
    _advance(monkeypatch, 1_000.0)
    tracker.record("grok:weekly", 40.0, window_marker="July 11, 17:03 PT")
    _advance(monkeypatch, 1_000.0 + 3600.0)

    assert tracker.record("grok:weekly", 60.0, window_marker="July 18, 17:03 PT") is None


def test_history_resets_after_declared_reset_moment(tracker: UsageTrendTracker, monkeypatch: pytest.MonkeyPatch) -> None:
    _advance(monkeypatch, 1_000.0)
    tracker.record("claude:five_hour", 40.0, window_marker=1_000.0 + 1_800.0)
    _advance(monkeypatch, 1_000.0 + 3600.0)

    assert tracker.record("claude:five_hour", 60.0, window_marker=1_000.0 + 5_400.0) is None


def test_sliding_reset_moment_keeps_history(tracker: UsageTrendTracker, monkeypatch: pytest.MonkeyPatch) -> None:
    # Claude отдаёт resets_at как «сейчас + остаток окна», поэтому маркер уезжает
    # вперёд при каждом запросе и не должен обнулять историю.
    _advance(monkeypatch, 1_000.0)
    tracker.record("claude:five_hour", 10.0, window_marker=1_000.0 + 18_000.0)
    _advance(monkeypatch, 1_000.0 + 3600.0)

    trend = tracker.record("claude:five_hour", 20.0, window_marker=1_000.0 + 3600.0 + 18_000.0)

    assert trend is not None
    assert trend.percent_per_hour == pytest.approx(10.0)


def test_history_resets_when_quota_drops(tracker: UsageTrendTracker, monkeypatch: pytest.MonkeyPatch) -> None:
    _advance(monkeypatch, 1_000.0)
    tracker.record("claude:seven_day", 40.0, window_marker="w1")
    _advance(monkeypatch, 1_000.0 + 3600.0)

    assert tracker.record("claude:seven_day", 5.0, window_marker="w1") is None


def test_stale_points_are_dropped(tracker: UsageTrendTracker, monkeypatch: pytest.MonkeyPatch) -> None:
    _advance(monkeypatch, 1_000.0)
    tracker.record("grok:weekly", 10.0, window_marker="w1")
    _advance(monkeypatch, 1_000.0 + 25 * 3600.0)

    assert tracker.record("grok:weekly", 90.0, window_marker="w1") is None


def test_history_is_capped(tracker: UsageTrendTracker, monkeypatch: pytest.MonkeyPatch) -> None:
    base = 1_000.0
    for step in range(UsageTrendTracker.MAX_POINTS + 5):
        _advance(monkeypatch, base + step * 120.0)
        tracker.record("codex:secondary", float(step), window_marker="w1")

    bucket = tracker._repository.read_namespace(UsageTrendTracker.NAMESPACE)

    assert len(bucket["codex:secondary"]["points"]) == UsageTrendTracker.MAX_POINTS


def test_tracker_without_repository_is_inert() -> None:
    assert UsageTrendTracker(None).record("codex:primary", 10.0) is None


def test_non_numeric_measurement_is_ignored(tracker: UsageTrendTracker) -> None:
    assert tracker.record("codex:primary", "n/a") is None
    assert tracker.record("", 10.0) is None
