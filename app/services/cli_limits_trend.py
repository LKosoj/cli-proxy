from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Optional

from app.services.state_repository import JsonStateRepository

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class UsageTrend:
    """Скорость расхода квоты и прогноз её исчерпания."""

    percent_per_hour: float
    seconds_to_exhaust: Optional[float]


class UsageTrendTracker:
    """Копит замеры расхода квот в durable-сторе и оценивает скорость выгорания."""

    NAMESPACE = "_cli_limits_trend"
    MAX_POINTS = 12
    MAX_AGE_SEC = 24 * 3600
    MIN_INTERVAL_SEC = 60.0
    # По двум близким замерам скорость получается случайной: один процент за минуту
    # экстраполируется в 60%/ч, поэтому прогноз ждёт хотя бы десятиминутную историю.
    MIN_FORECAST_SPAN_SEC = 600.0
    # Ниже этой скорости прогноз бессмысленен: это шум округления процентов.
    MIN_RATE_PERCENT_PER_HOUR = 0.05

    def __init__(self, repository: Optional[JsonStateRepository]) -> None:
        self._repository = repository

    def record(self, key: str, used_percent: Any, *, window_marker: Any = "") -> Optional[UsageTrend]:
        """Сохраняет замер и возвращает тренд, когда истории хватает для оценки."""
        if self._repository is None:
            return None
        item_key = str(key or "").strip()
        if not item_key:
            return None
        try:
            used = float(used_percent)
        except (TypeError, ValueError):
            return None
        now = time.time()
        marker = window_marker if isinstance(window_marker, (int, float)) else str(window_marker or "")
        result: dict[str, Optional[UsageTrend]] = {"trend": None}

        def _update(bucket: dict[str, Any]) -> dict[str, Any]:
            entry = bucket.get(item_key)
            points = _normalize_points(entry.get("points") if isinstance(entry, dict) else None)
            if isinstance(entry, dict) and _window_changed(entry.get("window"), marker, now):
                points = []
            points = [point for point in points if 0 <= now - point[0] <= self.MAX_AGE_SEC]
            if points and used + 1e-9 < points[-1][1]:
                # Квота сброшена внутри того же окна — прежние замеры больше не сравнимы.
                points = []
            result["trend"] = _forecast(points, used, now, self.MIN_RATE_PERCENT_PER_HOUR, self.MIN_FORECAST_SPAN_SEC)
            if not points or now - points[-1][0] >= self.MIN_INTERVAL_SEC:
                points.append([now, used])
            bucket[item_key] = {"window": marker, "points": points[-self.MAX_POINTS:]}
            return bucket

        try:
            self._repository.update_namespace(self.NAMESPACE, _update)
        except Exception:
            logger.debug("usage trend update failed key=%s", item_key, exc_info=True)
            return None
        return result["trend"]


def _window_changed(stored: Any, marker: Any, now: float) -> bool:
    """Понимает, началось ли новое окно квоты с момента прошлого замера."""
    if isinstance(stored, (int, float)) and not isinstance(stored, bool):
        # Момент сброса известен: окно новое, только когда прошлый сброс уже наступил.
        # Сравнивать сами значения нельзя — у скользящих окон Claude они уезжают вперёд
        # при каждом запросе.
        return now >= float(stored)
    return str(stored or "") != str(marker or "")


def _normalize_points(raw: Any) -> list[list[float]]:
    if not isinstance(raw, list):
        return []
    points: list[list[float]] = []
    for item in raw:
        if not isinstance(item, (list, tuple)) or len(item) != 2:
            continue
        try:
            points.append([float(item[0]), float(item[1])])
        except (TypeError, ValueError):
            continue
    points.sort(key=lambda point: point[0])
    return points


def _forecast(
    points: list[list[float]],
    used: float,
    now: float,
    min_rate: float,
    min_span: float,
) -> Optional[UsageTrend]:
    if not points:
        return None
    first_ts, first_used = points[0]
    elapsed = now - first_ts
    if elapsed < min_span:
        return None
    rate = (used - first_used) / elapsed * 3600.0
    if rate < min_rate:
        return None
    remaining = max(0.0, 100.0 - used)
    return UsageTrend(percent_per_hour=rate, seconds_to_exhaust=remaining / rate * 3600.0)
