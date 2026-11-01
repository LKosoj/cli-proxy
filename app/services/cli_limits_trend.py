from __future__ import annotations

import logging
import time
from typing import Any, Optional

from app.services.state_repository import JsonStateRepository

logger = logging.getLogger(__name__)


class UsageTrendTracker:
    """Хранит получасовые замеры квот. Скорость считает cli_limits_pace."""

    NAMESPACE = "_cli_limits_trend"
    # Сутки с запасом: точка «около 24 часов назад» не должна выпадать из-за джиттера.
    MAX_POINTS = 64
    MAX_AGE_SEC = 30 * 3600
    # Съём идёт раз в полчаса. Повтор внутри того же захода не пишется.
    MIN_INTERVAL_SEC = 25 * 60

    def __init__(self, repository: Optional[JsonStateRepository]) -> None:
        self._repository = repository

    def record(self, key: str, used_percent: Any, *, window_marker: Any = "") -> None:
        """Сохраняет замер. Команда /limits сюда не заходит."""
        if self._repository is None:
            return
        item_key = str(key or "").strip()
        if not item_key:
            return
        try:
            used = float(used_percent)
        except (TypeError, ValueError):
            return
        now = time.time()
        marker = _normalize_marker(window_marker)

        def _update(bucket: dict[str, Any]) -> dict[str, Any]:
            entry = bucket.get(item_key)
            points = _normalize_points(entry.get("points") if isinstance(entry, dict) else None)
            stored_window = entry.get("window") if isinstance(entry, dict) else None
            if isinstance(entry, dict) and _window_changed(stored_window, marker, now):
                points = []
            points = [point for point in points if 0 <= now - point[0] <= self.MAX_AGE_SEC]
            if points and used + 1e-9 < points[-1][1] and not _reset_still_ahead(marker, now):
                # Квота сбросилась без нового маркера — старые точки больше не сравнимы.
                points = []
            if not points or now - points[-1][0] >= self.MIN_INTERVAL_SEC:
                points.append([now, used])
            bucket[item_key] = {"window": marker, "points": points[-self.MAX_POINTS :]}
            return bucket

        try:
            self._repository.update_namespace(self.NAMESPACE, _update)
        except Exception:
            logger.debug("usage trend update failed key=%s", item_key, exc_info=True)

    def load_points(self, key: str, window_marker: Any, now: float) -> list[list[float]]:
        """Возвращает точки текущего окна. После сброса — пустой список, без записи."""
        if self._repository is None:
            return []
        item_key = str(key or "").strip()
        if not item_key:
            return []
        try:
            bucket = self._repository.read_namespace(self.NAMESPACE)
        except Exception:
            logger.debug("usage trend read failed key=%s", item_key, exc_info=True)
            return []
        entry = bucket.get(item_key) if isinstance(bucket, dict) else None
        if not isinstance(entry, dict):
            return []
        marker = _normalize_marker(window_marker)
        if _window_changed(entry.get("window"), marker, now):
            return []
        points = _normalize_points(entry.get("points"))
        return [point for point in points if 0 <= now - point[0] <= self.MAX_AGE_SEC]


def _normalize_marker(marker: Any) -> Any:
    if isinstance(marker, bool):
        return str(marker)
    if isinstance(marker, (int, float)):
        return float(marker)
    return str(marker or "")


def _reset_still_ahead(marker: Any, now: float) -> bool:
    """У скользящего окна Claude процент может чуть упасть, пока сброс ещё впереди."""
    return isinstance(marker, (int, float)) and not isinstance(marker, bool) and now < float(marker)


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
