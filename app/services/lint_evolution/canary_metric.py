from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from . import autopause, fingerprints, schema_store

logger = logging.getLogger(__name__)


@dataclass
class CanaryConfig:
    fp_growth_threshold_pct: float = 50.0
    rolling_window_days: float = 7.0
    baseline_window_days: float = 30.0
    schema_max_fields_per_180d: int = 3


@dataclass
class CanaryReport:
    fp_rolling: float = 0.0
    fp_baseline: float = 0.0
    fp_growth_pct: float = 0.0
    schema_growth_180d: int = 0
    triggered: list[str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.triggered is None:
            self.triggered = []


def _rate(fp: int, total: int) -> float:
    return (fp / total) if total > 0 else 0.0


def evaluate(
    workdir: str,
    *,
    project_id: str,
    config: CanaryConfig,
    now: float | None = None,
) -> CanaryReport:
    when = float(now if now is not None else time.time())
    rolling_seconds = config.rolling_window_days * 86400.0
    baseline_seconds = config.baseline_window_days * 86400.0

    fp_r, total_r = fingerprints.fp_rate_window(
        workdir, project_id=project_id, window_seconds=rolling_seconds, now=when
    )
    fp_b, total_b = fingerprints.fp_rate_window(
        workdir, project_id=project_id, window_seconds=baseline_seconds, now=when
    )
    rate_rolling = _rate(fp_r, total_r)
    rate_baseline = _rate(fp_b, total_b)
    growth_pct = 0.0
    if rate_baseline > 0:
        growth_pct = (rate_rolling - rate_baseline) / rate_baseline * 100.0

    report = CanaryReport(
        fp_rolling=rate_rolling,
        fp_baseline=rate_baseline,
        fp_growth_pct=growth_pct,
    )

    if total_b >= 10 and growth_pct > config.fp_growth_threshold_pct:
        autopause.pause(workdir, 1, reason=f"fp_growth={growth_pct:.1f}%>{config.fp_growth_threshold_pct}%")
        autopause.pause(workdir, 2, reason=f"fp_growth={growth_pct:.1f}%>{config.fp_growth_threshold_pct}%")
        report.triggered.append("fp_canary")

    schema_growth = _schema_growth_within(workdir, days=180.0, now=when)
    report.schema_growth_180d = schema_growth
    if schema_growth > config.schema_max_fields_per_180d:
        autopause.pause(workdir, 2, reason=f"schema_growth={schema_growth} fields/180d")
        report.triggered.append("schema_thrash")

    return report


def _schema_growth_within(workdir: str, *, days: float, now: float) -> int:
    state = schema_store.load_state(workdir)
    if state.last_bump_ts <= 0:
        return 0
    seconds = days * 86400.0
    if (now - state.last_bump_ts) > seconds:
        return 0
    return max(0, state.active_version - 1)
