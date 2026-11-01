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


def _points(tracker: UsageTrendTracker, key: str) -> list[list[float]]:
    bucket = tracker._repository.read_namespace(UsageTrendTracker.NAMESPACE)
    return bucket[key]["points"]


def test_record_stores_a_point_and_does_not_describe_pace(
    tracker: UsageTrendTracker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _advance(monkeypatch, 1_000.0)

    assert tracker.record("claude:five_hour", 10.0, window_marker="w1") is None
    assert _points(tracker, "claude:five_hour") == [[1_000.0, 10.0]]


def test_measurements_closer_than_the_sample_interval_are_not_stored(
    tracker: UsageTrendTracker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _advance(monkeypatch, 1_000.0)
    tracker.record("codex:secondary", 10.0, window_marker="w1")
    _advance(monkeypatch, 1_000.0 + 20 * 60)
    tracker.record("codex:secondary", 12.0, window_marker="w1")

    assert _points(tracker, "codex:secondary") == [[1_000.0, 10.0]]


def test_history_resets_when_text_window_changes(
    tracker: UsageTrendTracker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _advance(monkeypatch, 1_000.0)
    tracker.record("grok:weekly", 40.0, window_marker="July 11, 17:03 PT")
    _advance(monkeypatch, 1_000.0 + 3600.0)
    tracker.record("grok:weekly", 60.0, window_marker="July 18, 17:03 PT")

    assert _points(tracker, "grok:weekly") == [[1_000.0 + 3600.0, 60.0]]


def test_history_resets_after_declared_reset_moment(
    tracker: UsageTrendTracker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _advance(monkeypatch, 1_000.0)
    tracker.record("claude:five_hour", 40.0, window_marker=1_000.0 + 1_800.0)
    _advance(monkeypatch, 1_000.0 + 3600.0)
    tracker.record("claude:five_hour", 60.0, window_marker=1_000.0 + 5_400.0)

    assert _points(tracker, "claude:five_hour") == [[1_000.0 + 3600.0, 60.0]]


def test_sliding_reset_moment_keeps_history(
    tracker: UsageTrendTracker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _advance(monkeypatch, 1_000.0)
    tracker.record("claude:five_hour", 10.0, window_marker=1_000.0 + 18_000.0)
    _advance(monkeypatch, 1_000.0 + 3600.0)
    tracker.record("claude:five_hour", 20.0, window_marker=1_000.0 + 3600.0 + 18_000.0)

    assert _points(tracker, "claude:five_hour") == [
        [1_000.0, 10.0],
        [1_000.0 + 3600.0, 20.0],
    ]


def test_sliding_window_keeps_history_when_usage_dips(
    tracker: UsageTrendTracker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _advance(monkeypatch, 1_000.0)
    tracker.record("claude:five_hour", 40.0, window_marker=1_000.0 + 18_000.0)
    _advance(monkeypatch, 1_000.0 + 3600.0)
    tracker.record("claude:five_hour", 30.0, window_marker=1_000.0 + 3600.0 + 18_000.0)

    assert [point[1] for point in _points(tracker, "claude:five_hour")] == [40.0, 30.0]


def test_history_resets_when_quota_drops_without_a_future_reset(
    tracker: UsageTrendTracker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _advance(monkeypatch, 1_000.0)
    tracker.record("claude:seven_day", 40.0, window_marker="w1")
    _advance(monkeypatch, 1_000.0 + 3600.0)
    tracker.record("claude:seven_day", 5.0, window_marker="w1")

    assert _points(tracker, "claude:seven_day") == [[1_000.0 + 3600.0, 5.0]]


def test_stale_points_are_dropped(tracker: UsageTrendTracker, monkeypatch: pytest.MonkeyPatch) -> None:
    _advance(monkeypatch, 1_000.0)
    tracker.record("grok:weekly", 10.0, window_marker="w1")
    _advance(monkeypatch, 1_000.0 + 31 * 3600.0)
    tracker.record("grok:weekly", 90.0, window_marker="w1")

    assert _points(tracker, "grok:weekly") == [[1_000.0 + 31 * 3600.0, 90.0]]


def test_history_is_capped(tracker: UsageTrendTracker, monkeypatch: pytest.MonkeyPatch) -> None:
    base = 1_000.0
    step = UsageTrendTracker.MIN_INTERVAL_SEC
    for index in range(UsageTrendTracker.MAX_POINTS + 5):
        _advance(monkeypatch, base + index * step)
        tracker.record("codex:secondary", float(index), window_marker="w1")

    assert len(_points(tracker, "codex:secondary")) == UsageTrendTracker.MAX_POINTS


def test_load_points_hides_history_after_the_reset_moment(
    tracker: UsageTrendTracker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _advance(monkeypatch, 1_000.0)
    tracker.record("claude:five_hour", 40.0, window_marker=10_000.0)

    assert tracker.load_points("claude:five_hour", 20_000.0, now=10_500.0) == []
    assert tracker.load_points("claude:five_hour", 12_000.0, now=2_000.0) == [[1_000.0, 40.0]]


def test_tracker_without_repository_is_inert() -> None:
    assert UsageTrendTracker(None).record("codex:primary", 10.0) is None
    assert UsageTrendTracker(None).load_points("codex:primary", "", 1.0) == []


def test_non_numeric_measurement_is_ignored(tracker: UsageTrendTracker) -> None:
    assert tracker.record("codex:primary", "n/a") is None
    assert tracker.record("", 10.0) is None
    assert tracker._repository.read_namespace(UsageTrendTracker.NAMESPACE) == {}
