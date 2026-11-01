"""Темп квоты по получасовым замерам и текст для /limits."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Optional, Sequence
from zoneinfo import ZoneInfo

from i18n import t
from i18n.resolver import SUPPORTED_LANGS

# Экран /usage у Grok всегда показывает тихоокеанское время.
# В 1.0.70 буквы PT пропали, часы остались теми же.
_PACIFIC = ZoneInfo("America/Los_Angeles")
_GROK_RESET_RE = re.compile(
    r"^(?P<month>[A-Za-z]+)\s+(?P<day>\d{1,2}),\s*(?P<hour>\d{1,2}):(?P<minute>\d{2})"
    r"(?:\s*(?P<tz>PT|PDT|PST))?\s*$",
    re.IGNORECASE,
)
_MONTHS = {
    "january": 1,
    "february": 2,
    "march": 3,
    "april": 4,
    "may": 5,
    "june": 6,
    "july": 7,
    "august": 8,
    "september": 9,
    "october": 10,
    "november": 11,
    "december": 12,
}

# Замер «час назад» берём только из узкой полосы: пропуск съёма не растягивается в час.
_HOUR_TARGET_SEC = 3600.0
_HOUR_MIN_SEC = 45 * 60
_HOUR_MAX_SEC = 80 * 60
_DAY_TARGET_SEC = 24 * 3600.0
_DAY_MIN_SEC = 20 * 3600.0
_DAY_MAX_SEC = 28 * 3600.0
# Второе число длинного окна короче суток показываем только если оно старше часовой полосы.
_SPAN_MIN_SEC = 90 * 60
_WINDOW_MIN_SEC = 30 * 60
_COMMA_LANGS = frozenset({"ru", "de"})


@dataclass(frozen=True)
class QuotaPace:
    """Два темпа и вывод, из которого собирается вторая строка."""

    hour_rate: Optional[float]
    longer_rate: Optional[float]
    longer_span_sec: Optional[float]
    longer_kind: str
    basis: str
    seconds_to_exhaust: Optional[float]
    remaining_at_reset: Optional[float]


def parse_grok_reset(text: str, now: float) -> Optional[float]:
    """Переводит «November 8, 03:03» в момент сброса. Год — ближайший будущий."""
    match = _GROK_RESET_RE.match(str(text or "").strip())
    if match is None:
        return None
    month = _MONTHS.get(str(match.group("month") or "").lower())
    if month is None:
        return None
    day = int(match.group("day"))
    hour = int(match.group("hour"))
    minute = int(match.group("minute"))
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    year = datetime.fromtimestamp(now, _PACIFIC).year
    candidate = _pacific(year, month, day, hour, minute)
    if candidate is None:
        return None
    if candidate < now - 3600:
        candidate = _pacific(year + 1, month, day, hour, minute)
    return candidate


def compute_pace(
    points: Sequence[Sequence[float]],
    used_now: float,
    now: float,
    *,
    long_window: bool,
    quantum: float,
    reset_ts: Optional[float],
) -> QuotaPace:
    """Считает темп за час и за сутки (или с начала окна) от свежего процента."""
    history = _usable_points(points, used_now, now, reset_ts)
    hour = _closest(history, now, _HOUR_TARGET_SEC, _HOUR_MIN_SEC, _HOUR_MAX_SEC)
    longer = _longer_anchor(history, now, long_window=long_window)
    hour_rate, hour_flat = _rate_from_anchor(hour, used_now, quantum)
    longer_rate, longer_flat = _rate_from_anchor(longer, used_now, quantum)
    basis, rate = _choose_basis(
        hour_rate,
        hour_flat,
        hour is not None,
        longer_rate,
        longer_flat,
        longer[3] if longer is not None else "none",
    )
    seconds, remaining = _project(used_now, now, rate, reset_ts)
    return QuotaPace(
        hour_rate=hour_rate,
        longer_rate=longer_rate,
        longer_span_sec=None if longer is None else longer[2],
        longer_kind="none" if longer is None else longer[3],
        basis=basis,
        seconds_to_exhaust=seconds,
        remaining_at_reset=remaining,
    )


def render_quota_block(
    base: str,
    pace: Optional[QuotaPace],
    *,
    reset_ts: Optional[float],
    long_window: bool,
    lang: str,
    now: float,
) -> str:
    """Добавляет к полоске момент сброса и вторую строку со скоростями."""
    language = _language(lang)
    line = str(base or "").rstrip()
    phrase = reset_phrase(reset_ts, language, now)
    if phrase:
        line = f"{line} · {phrase}"
    if pace is None:
        return line
    stats = _stats_line(pace, language, now, long_window=long_window)
    if not stats:
        return line
    return f"{line}\n{stats}"


def reset_phrase(reset_ts: Optional[float], lang: str, now: float) -> str:
    if reset_ts is None or reset_ts <= 0:
        return ""
    language = _language(lang)
    moment = datetime.fromtimestamp(float(reset_ts)).astimezone()
    clock = moment.strftime("%H:%M")
    if moment.date() == datetime.fromtimestamp(now).astimezone().date():
        return t("msg.error.limits_stats.reset_today", language, time=clock)
    return t(
        "msg.error.limits_stats.reset_on",
        language,
        when=_format_date(moment, language),
        time=clock,
    )


def _pacific(year: int, month: int, day: int, hour: int, minute: int) -> Optional[float]:
    try:
        return datetime(year, month, day, hour, minute, tzinfo=_PACIFIC).timestamp()
    except ValueError:
        return None


def _usable_points(
    points: Sequence[Sequence[float]],
    used_now: float,
    now: float,
    reset_ts: Optional[float],
) -> list[tuple[float, float]]:
    cleaned: list[tuple[float, float]] = []
    for item in points:
        if len(item) != 2:
            continue
        stamp = float(item[0])
        if stamp <= now:
            cleaned.append((stamp, float(item[1])))
    cleaned.sort(key=lambda item: item[0])
    if not cleaned:
        return []
    reset_ahead = reset_ts is not None and now < float(reset_ts)
    if used_now + 1e-9 < cleaned[-1][1] and not reset_ahead:
        return []
    return cleaned


def _closest(
    points: Sequence[tuple[float, float]],
    now: float,
    target_age: float,
    min_age: float,
    max_age: float,
) -> Optional[tuple[float, float, float]]:
    best: Optional[tuple[float, float, float]] = None
    best_distance: Optional[float] = None
    for stamp, used in points:
        age = now - stamp
        if age < min_age or age > max_age:
            continue
        distance = abs(age - target_age)
        if best is None or best_distance is None or distance < best_distance:
            best = (stamp, used, age)
            best_distance = distance
    return best


def _longer_anchor(
    points: Sequence[tuple[float, float]],
    now: float,
    *,
    long_window: bool,
) -> Optional[tuple[float, float, float, str]]:
    if not points:
        return None
    if long_window:
        day = _closest(points, now, _DAY_TARGET_SEC, _DAY_MIN_SEC, _DAY_MAX_SEC)
        if day is not None:
            return (day[0], day[1], day[2], "day")
        oldest_stamp, oldest_used = points[0]
        age = now - oldest_stamp
        if age > _SPAN_MIN_SEC:
            return (oldest_stamp, oldest_used, age, "span")
        return None
    oldest_stamp, oldest_used = points[0]
    age = now - oldest_stamp
    if age >= _WINDOW_MIN_SEC:
        return (oldest_stamp, oldest_used, age, "window")
    return None


def _rate_from_anchor(
    anchor: Optional[tuple[float, ...]],
    used_now: float,
    quantum: float,
) -> tuple[Optional[float], bool]:
    if anchor is None:
        return None, False
    age = float(anchor[2])
    if age <= 0:
        return None, False
    delta = used_now - float(anchor[1])
    if abs(delta) < max(0.0, quantum):
        return 0.0, True
    return delta / age * 3600.0, False


def _choose_basis(
    hour_rate: Optional[float],
    hour_flat: bool,
    hour_known: bool,
    longer_rate: Optional[float],
    longer_flat: bool,
    longer_kind: str,
) -> tuple[str, Optional[float]]:
    if hour_known and not hour_flat and hour_rate is not None and hour_rate > 0:
        return "hour", hour_rate
    if longer_rate is not None and longer_rate > 0 and not longer_flat:
        return ("day" if longer_kind == "day" else "window"), longer_rate
    if hour_known and hour_flat and (longer_rate is None or longer_flat or longer_rate <= 0):
        return "flat", None
    return "none", None


def _project(
    used_now: float,
    now: float,
    rate: Optional[float],
    reset_ts: Optional[float],
) -> tuple[Optional[float], Optional[float]]:
    if rate is None or rate <= 0:
        return None, None
    remaining = max(0.0, 100.0 - used_now)
    seconds = remaining / rate * 3600.0
    if reset_ts is None or reset_ts <= now:
        return seconds, None
    if now + seconds >= float(reset_ts):
        hours = (float(reset_ts) - now) / 3600.0
        projected = min(100.0, used_now + rate * hours)
        return None, max(0.0, 100.0 - projected)
    return seconds, None


def _stats_line(pace: QuotaPace, lang: str, now: float, *, long_window: bool) -> str:
    parts = [_hour_part(pace, lang)]
    longer = _longer_part(pace, lang, long_window=long_window)
    if longer:
        parts.append(longer)
    conclusion = _conclusion(pace, lang, now)
    if conclusion:
        parts.append(conclusion)
    return " · ".join(part for part in parts if part)


def _hour_part(pace: QuotaPace, lang: str) -> str:
    if pace.hour_rate is None:
        return t("msg.error.limits_stats.hour_pending", lang)
    return t("msg.error.limits_stats.hour", lang, rate=_per_hour(pace.hour_rate, lang))


def _longer_part(pace: QuotaPace, lang: str, *, long_window: bool) -> str:
    if pace.longer_kind == "day" and pace.longer_rate is not None:
        return t("msg.error.limits_stats.day", lang, rate=_per_hour(pace.longer_rate, lang))
    if pace.longer_kind == "span" and pace.longer_rate is not None and pace.longer_span_sec:
        return t(
            "msg.error.limits_stats.span",
            lang,
            span=_format_span(pace.longer_span_sec, lang),
            rate=_per_hour(pace.longer_rate, lang),
        )
    if pace.longer_kind == "window" and pace.longer_rate is not None:
        return t("msg.error.limits_stats.window", lang, rate=_per_hour(pace.longer_rate, lang))
    if long_window:
        return t("msg.error.limits_stats.day_pending", lang)
    return t("msg.error.limits_stats.window_pending", lang)


def _conclusion(pace: QuotaPace, lang: str, now: float) -> str:
    if pace.basis == "flat":
        return t("msg.error.limits_stats.flat", lang)
    if pace.basis not in ("hour", "day", "window"):
        return ""
    prefix, extra = _conclusion_basis(pace, lang)
    if pace.remaining_at_reset is not None:
        return t(
            f"msg.error.limits_stats.left_{prefix}",
            lang,
            left=_percent(pace.remaining_at_reset, lang),
            **extra,
        )
    if pace.seconds_to_exhaust is None:
        return ""
    when = _format_when(now + pace.seconds_to_exhaust, lang, now)
    return t(f"msg.error.limits_stats.hit_{prefix}", lang, when=when, **extra)


def _conclusion_basis(pace: QuotaPace, lang: str) -> tuple[str, dict[str, str]]:
    """Имя скорости в прогнозе. «За 3ч» нельзя называть окном: окно — весь период до сброса."""
    if pace.basis == "hour":
        return "hour", {}
    if pace.basis == "day":
        return "day", {}
    if pace.longer_kind == "span" and pace.longer_span_sec:
        return "span", {"span": _format_span(pace.longer_span_sec, lang)}
    return "window", {}


def _format_when(ts: float, lang: str, now: float) -> str:
    moment = datetime.fromtimestamp(ts).astimezone()
    clock = moment.strftime("%H:%M")
    if moment.date() == datetime.fromtimestamp(now).astimezone().date():
        return clock
    return f"{_format_date(moment, lang)}, {clock}"


def _format_date(moment: datetime, lang: str) -> str:
    month = t(f"msg.error.limits_stats.month_{moment.month}", lang)
    return t("msg.error.limits_stats.date", lang, day=moment.day, month=month)


def _format_span(seconds: float, lang: str) -> str:
    minutes = max(1, int(round(seconds / 60.0)))
    if minutes >= 60:
        return t("msg.error.limits_stats.span_hours", lang, n=max(1, int(round(minutes / 60.0))))
    return t("msg.error.limits_stats.span_minutes", lang, n=minutes)


def _per_hour(value: float, lang: str) -> str:
    return t("msg.error.limits_stats.per_hour", lang, value=_percent(value, lang))


def _percent(value: float, lang: str) -> str:
    number = float(value)
    rounded = round(number, 1)
    # 0,04 %/ч за сутки иначе схлопывается в «0%», хотя квота сдвинулась.
    if abs(rounded) < 1e-9 and abs(number) >= 0.005:
        text = f"{number:.2f}%"
    elif abs(rounded - round(rounded)) < 1e-9:
        text = f"{int(round(rounded))}%"
    else:
        text = f"{rounded:.1f}%"
    if lang in _COMMA_LANGS:
        text = text.replace(".", ",")
    return text


def _language(lang: str) -> str:
    if lang in SUPPORTED_LANGS:
        return lang
    return "ru"
