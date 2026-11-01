from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from app.services.cli_limits_pace import compute_pace, parse_grok_reset, render_quota_block, reset_phrase


def test_parse_grok_reset_treats_bare_clock_as_pacific_and_picks_the_next_year() -> None:
    pacific = ZoneInfo("America/Los_Angeles")
    now = datetime(2026, 11, 1, 1, 12, tzinfo=pacific).timestamp()

    upcoming = parse_grok_reset("November 8, 03:03", now)
    previous = parse_grok_reset("July 11, 17:03 PT", now)

    assert upcoming == datetime(2026, 11, 8, 3, 3, tzinfo=pacific).timestamp()
    assert previous == datetime(2027, 7, 11, 17, 3, tzinfo=pacific).timestamp()


def test_reset_phrase_uses_local_time_and_the_requested_language() -> None:
    pacific = ZoneInfo("America/Los_Angeles")
    now = datetime(2026, 11, 1, 11, 12, tzinfo=ZoneInfo("Europe/Moscow")).timestamp()
    reset = datetime(2026, 11, 8, 3, 3, tzinfo=pacific).timestamp()
    local = datetime.fromtimestamp(reset).astimezone()

    russian = reset_phrase(reset, "ru", now)
    english = reset_phrase(reset, "en", now)

    assert russian == f"сброс {local.day} ноя, {local.strftime('%H:%M')}"
    assert english == f"resets {local.strftime('%b')} {local.day}, {local.strftime('%H:%M')}"
    assert "Nov" in english


def test_hour_rate_uses_the_sample_about_an_hour_ago() -> None:
    now = 10_000.0
    points = [[now - 3600.0, 10.0], [now - 1800.0, 12.0]]

    pace = compute_pace(points, 20.0, now, long_window=False, quantum=0.05, reset_ts=None)

    assert pace.hour_rate == 10.0
    assert pace.basis == "hour"
    assert pace.seconds_to_exhaust == 8 * 3600.0


def test_gap_longer_than_the_hour_band_is_not_called_an_hour() -> None:
    now = 10_000.0
    points = [[now - 3 * 3600.0, 10.0]]

    pace = compute_pace(points, 40.0, now, long_window=True, quantum=0.05, reset_ts=None)

    assert pace.hour_rate is None
    assert pace.longer_kind == "span"
    assert pace.basis == "window"


def test_daily_rate_uses_the_sample_about_a_day_ago() -> None:
    now = 100_000.0
    points = [[now - 24 * 3600.0, 4.0], [now - 3600.0, 10.0]]

    pace = compute_pace(points, 10.0, now, long_window=True, quantum=1.0, reset_ts=now + 7 * 24 * 3600.0)

    assert pace.hour_rate == 0.0
    assert pace.longer_kind == "day"
    assert pace.longer_rate == 6.0 / 24.0
    assert pace.basis == "day"
    assert pace.remaining_at_reset is not None
    assert pace.remaining_at_reset < 100.0


def test_flat_hour_on_a_whole_percent_falls_back_to_the_day() -> None:
    now = 100_000.0
    reset = now + 6 * 3600.0
    points = [[now - 24 * 3600.0, 4.0], [now - 3600.0, 5.0]]

    pace = compute_pace(points, 5.0, now, long_window=True, quantum=1.0, reset_ts=reset)

    assert pace.hour_rate == 0.0
    assert pace.basis == "day"
    assert pace.seconds_to_exhaust is None
    assert pace.remaining_at_reset == 95.0 - (1.0 / 24.0) * 6.0


def test_both_flat_rates_do_not_predict_exhaustion() -> None:
    now = 10_000.0
    points = [[now - 24 * 3600.0, 4.0], [now - 3600.0, 4.0]]

    pace = compute_pace(points, 4.0, now, long_window=True, quantum=1.0, reset_ts=now + 86_400.0)

    assert pace.basis == "flat"
    assert pace.seconds_to_exhaust is None
    assert pace.remaining_at_reset is None


def test_short_window_second_rate_starts_at_the_oldest_sample() -> None:
    now = 10_000.0
    points = [[now - 3 * 3600.0, 10.0], [now - 3600.0, 20.0]]

    pace = compute_pace(points, 22.0, now, long_window=False, quantum=0.05, reset_ts=now + 2 * 3600.0)

    assert pace.longer_kind == "window"
    assert pace.longer_rate == 4.0
    assert pace.hour_rate == 2.0
    assert pace.basis == "hour"


def test_render_quota_block_translates_the_stats_line() -> None:
    now = 100_000.0
    reset = now + 6 * 3600.0
    points = [[now - 24 * 3600.0, 4.0], [now - 3600.0, 5.0]]
    pace = compute_pace(points, 5.0, now, long_window=True, quantum=1.0, reset_ts=reset)

    russian = render_quota_block("полоска", pace, reset_ts=reset, long_window=True, lang="ru", now=now)
    english = render_quota_block("bar", pace, reset_ts=reset, long_window=True, lang="en", now=now)
    german = render_quota_block("balken", pace, reset_ts=reset, long_window=True, lang="de", now=now)
    chinese = render_quota_block("条", pace, reset_ts=reset, long_window=True, lang="zh", now=now)

    assert "за час 0%/ч" in russian
    assert "за сутки 0,04%/ч" in russian
    assert "по суткам к сбросу" in russian
    assert "last hour 0%/h" in english
    assert "last day" in english
    assert "letzte Stunde 0%/h" in german
    assert "letzte 24 Stunden" in german
    assert "近一小时 0%/小时" in chinese
    assert "近一天" in chinese
