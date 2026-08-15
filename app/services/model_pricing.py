from __future__ import annotations

import json
import logging
import os
import re
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Mapping, Optional

from app.services.path_normalization import normalize_state_path
from modes.sdk.runtime.json_normalizer import loads_safe

logger = logging.getLogger(__name__)

_DATE_SUFFIX_RE = re.compile(r"-(?:\d{8}|\d{4}-\d{2}-\d{2}|v\d+(?:[.-]\d+)*)$")

# Ключи стоимости LiteLLM по видам токенов; для кэша используется базовая
# input-ставка, когда отдельной цены в прайсе нет.
_COST_KEYS: dict[str, tuple[str, ...]] = {
    "input": ("input_cost_per_token",),
    "output": ("output_cost_per_token",),
    "cache_read": ("cache_read_input_token_cost", "input_cost_per_token"),
    "cache_write": ("cache_creation_input_token_cost", "input_cost_per_token"),
    "reasoning": ("output_cost_per_token",),
}


class ModelPricing:
    """Оценивает стоимость потраченных токенов по прайс-листу LiteLLM."""

    PRICES_URL = "https://raw.githubusercontent.com/BerriAI/litellm/main/model_prices_and_context_window.json"
    CACHE_TTL_SEC = 24 * 3600
    RETRY_AFTER_FAILURE_SEC = 600

    def __init__(
        self,
        *,
        cache_path: str | Path,
        enabled: bool = True,
        network_timeout_sec: float = 5.0,
        prices_url: Optional[str] = None,
    ) -> None:
        self._cache_path = Path(os.path.abspath(str(cache_path)))
        self._enabled = bool(enabled)
        self._prices_url = str(prices_url or self.PRICES_URL)
        try:
            timeout_value = float(network_timeout_sec)
        except Exception:
            timeout_value = 5.0
        self._network_timeout_sec = max(0.1, timeout_value)
        self._prices: Optional[dict[str, Any]] = None
        self._next_fetch_ts = 0.0

    def estimate_usd(self, model: str, usage: Mapping[str, Any]) -> Optional[float]:
        """Возвращает оценку стоимости в долларах или None, если ставки неизвестны."""
        entry = self._lookup(model)
        if entry is None:
            return None
        total = 0.0
        priced = False
        for usage_key, cost_keys in _COST_KEYS.items():
            tokens = _safe_int(usage.get(usage_key))
            if tokens <= 0:
                continue
            rate = _first_rate(entry, cost_keys)
            if rate is None:
                continue
            total += tokens * rate
            priced = True
        return total if priced else None

    def _lookup(self, model: str) -> Optional[dict[str, Any]]:
        name = str(model or "").strip()
        if not name:
            return None
        prices = self._load_prices()
        if not prices:
            return None
        for candidate in _model_candidates(name):
            entry = prices.get(candidate)
            if isinstance(entry, dict):
                return entry
        return self._lookup_by_suffix(prices, _model_candidates(name))

    @staticmethod
    def _lookup_by_suffix(prices: dict[str, Any], candidates: list[str]) -> Optional[dict[str, Any]]:
        """Ищет `<provider>/<model>` — в прайсе одна модель встречается под разными провайдерами."""
        suffixes = tuple("/" + candidate for candidate in candidates)
        for key, entry in prices.items():
            if isinstance(entry, dict) and str(key).lower().endswith(suffixes):
                return entry
        return None

    def _load_prices(self) -> dict[str, Any]:
        if self._prices is not None:
            return self._prices
        if not self._enabled:
            self._prices = {}
            return self._prices
        cached = self._read_cache()
        if cached is not None and not self._cache_expired():
            self._prices = cached
            return self._prices
        fetched = self._fetch_prices()
        if fetched:
            self._write_cache(fetched)
            self._prices = fetched
        else:
            # Просроченный кэш всё равно точнее, чем полное отсутствие цен.
            self._prices = cached or {}
        return self._prices

    def _cache_expired(self) -> bool:
        try:
            age = time.time() - self._cache_path.stat().st_mtime
        except Exception:
            return True
        return age > self.CACHE_TTL_SEC

    def _read_cache(self) -> Optional[dict[str, Any]]:
        try:
            payload = loads_safe(self._cache_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except Exception:
            logger.debug("model pricing cache read failed path=%s", self._cache_path, exc_info=True)
            return None
        return payload if isinstance(payload, dict) and payload else None

    def _write_cache(self, prices: dict[str, Any]) -> None:
        try:
            self._cache_path.parent.mkdir(parents=True, exist_ok=True)
            temp_path = self._cache_path.with_suffix(self._cache_path.suffix + ".tmp")
            temp_path.write_text(json.dumps(prices, ensure_ascii=False), encoding="utf-8")
            temp_path.replace(self._cache_path)
        except Exception:
            logger.debug("model pricing cache write failed path=%s", self._cache_path, exc_info=True)

    def _fetch_prices(self) -> Optional[dict[str, Any]]:
        if time.monotonic() < self._next_fetch_ts:
            return None
        self._next_fetch_ts = time.monotonic() + self.RETRY_AFTER_FAILURE_SEC
        request = urllib.request.Request(
            self._prices_url,
            headers={"Accept": "application/json"},
            method="GET",
        )
        try:
            with urllib.request.urlopen(request, timeout=self._network_timeout_sec) as response:
                payload = loads_safe(response.read().decode("utf-8"))
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError):
            return None
        except Exception:
            logger.exception("model pricing fetch failed url=%s", self._prices_url)
            return None
        if not isinstance(payload, dict) or not payload:
            return None
        return payload


def build_model_pricing(state_path: Any) -> ModelPricing:
    """Собирает прайс-сервис с кэшем рядом с файлом состояния."""
    base_dir = Path(normalize_state_path(state_path)).parent
    return ModelPricing(cache_path=base_dir / ".cli-proxy" / "runtime" / "model_prices.json")


def _model_candidates(model: str) -> list[str]:
    raw = str(model or "").strip().lower()
    candidates: list[str] = []
    seen: set[str] = set()
    for value in (raw, raw.split("/")[-1], _DATE_SUFFIX_RE.sub("", raw), _DATE_SUFFIX_RE.sub("", raw.split("/")[-1])):
        item = value.strip()
        if item and item not in seen:
            seen.add(item)
            candidates.append(item)
    return candidates


def _first_rate(entry: Mapping[str, Any], keys: tuple[str, ...]) -> Optional[float]:
    for key in keys:
        value = entry.get(key)
        if value is None:
            continue
        try:
            rate = float(value)
        except Exception:
            continue
        if rate > 0:
            return rate
    return None


def _safe_int(value: Any) -> int:
    try:
        return int(value or 0)
    except Exception:
        return 0
