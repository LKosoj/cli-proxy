from __future__ import annotations

import logging
import time
from collections import defaultdict
from dataclasses import dataclass, field
from enum import Enum
from typing import Iterable

from . import fingerprints, rules_store, weights_store
from .reports import RunReport, write_report

logger = logging.getLogger(__name__)

_DEFAULT_WINDOW_SECONDS = 90 * 24 * 3600.0


class L3Status(str, Enum):
    OK = "ok"
    SKIP_INSUFFICIENT_DATA = "skip_insufficient_data"
    SKIP_NO_REGRESSION = "skip_no_regression"
    PAUSED_DRIFT = "paused_drift"
    ERROR = "error"


@dataclass
class L3Config:
    min_outcomes_total: int = 200
    min_outcomes_per_rule: int = 50
    max_weight_drift: float = 0.5
    learning_rate: float = 0.2
    window_seconds: float = _DEFAULT_WINDOW_SECONDS


@dataclass
class RegressionResult:
    status: L3Status
    new_weights: dict[str, float] = field(default_factory=dict)
    drift_summary: dict[str, float] = field(default_factory=dict)
    fp_rate_global: float = 0.0
    outcomes_total: int = 0
    notes: str = ""


def _is_fp(outcome: str) -> bool:
    return outcome in {"reverted", "ignored"}


def _load_outcomes(workdir: str, *, project_id: str, since_ts: float) -> list[tuple[str, str, float]]:
    return fingerprints.outcomes_since(workdir, project_id=project_id, since_ts=since_ts)


def _per_rule_buckets(outcomes: Iterable[tuple[str, str, float]]) -> dict[str, list[tuple[str, float]]]:
    buckets: dict[str, list[tuple[str, float]]] = defaultdict(list)
    for rule_id, outcome, weight in outcomes:
        buckets[rule_id].append((outcome, weight))
    return buckets


def _feature_keys_for_rule(rule: rules_store.Rule) -> list[str]:
    cls = rule.metadata.classification or {}
    keys: list[str] = []
    cat = cls.get("category")
    if cat:
        keys.append(f"category_{cat}")
    det = cls.get("detector_type") or rule.detector_type
    if det:
        keys.append(f"detector_{det}")
    fpr = cls.get("false_positive_risk")
    if fpr:
        keys.append(f"fp_risk_{fpr}")
    scope = cls.get("scope_subjectivity")
    if scope:
        keys.append(f"scope_{scope}")
    return keys


def _aggregate_feature_fp(
    rules: list[rules_store.Rule],
    rule_buckets: dict[str, list[tuple[str, float]]],
) -> dict[str, tuple[float, float]]:
    """Return key -> (fp_weight, total_weight) summed across all rules carrying that feature."""
    agg: dict[str, list[float]] = defaultdict(lambda: [0.0, 0.0])
    for rule in rules:
        outs = rule_buckets.get(rule.id) or []
        if not outs:
            continue
        feature_keys = _feature_keys_for_rule(rule)
        if not feature_keys:
            continue
        for outcome, weight in outs:
            for key in feature_keys:
                agg[key][1] += weight
                if _is_fp(outcome):
                    agg[key][0] += weight
    return {k: (v[0], v[1]) for k, v in agg.items()}


def run_level3(
    *,
    workdir: str,
    project_id: str,
    config: L3Config,
    now: float | None = None,
    record_report: bool = True,
) -> RegressionResult:
    weights_store.bootstrap_if_missing(workdir)
    started = float(now if now is not None else time.time())
    run_id = (
        fingerprints.record_run(
            workdir,
            project_id=project_id,
            level=3,
            started_ts=started,
            finished_ts=None,
            status="running",
        )
        if record_report
        else None
    )

    def _finish(result: RegressionResult) -> RegressionResult:
        finished = time.time()
        if record_report:
            _write_report(workdir, project_id, started, result, finished_ts=finished)
        if run_id is not None:
            try:
                fingerprints.finish_run(
                    workdir,
                    run_id,
                    finished_ts=finished,
                    status=result.status.value,
                    candidates_count=result.outcomes_total,
                    applied_count=1 if result.status is L3Status.OK and result.new_weights else 0,
                    notes=result.notes,
                )
            except Exception as exc:
                logger.warning("lint_evolution.level3: cannot record run row: %s", exc)
        return result

    bundle = weights_store.load_active(workdir)
    rules = [r for r in rules_store.load_rules(workdir) if r.state == "active"]
    outcomes = _load_outcomes(workdir, project_id=project_id, since_ts=started - config.window_seconds)
    total_count = len(outcomes)

    if total_count < config.min_outcomes_total:
        result = RegressionResult(
            status=L3Status.SKIP_INSUFFICIENT_DATA,
            outcomes_total=total_count,
            notes=f"need {config.min_outcomes_total}, have {total_count}",
        )
        return _finish(result)

    rule_buckets = _per_rule_buckets(outcomes)
    if any(len(rule_buckets.get(r.id) or []) < config.min_outcomes_per_rule for r in rules):
        result = RegressionResult(
            status=L3Status.SKIP_INSUFFICIENT_DATA,
            outcomes_total=total_count,
            notes=f"per_rule_min={config.min_outcomes_per_rule}",
        )
        return _finish(result)

    fp_total = sum(w for _, o, w in outcomes if _is_fp(o))
    total_weight = sum(w for _, _, w in outcomes)
    if total_weight <= 0:
        return _finish(RegressionResult(status=L3Status.SKIP_NO_REGRESSION, notes="zero total weight"))
    fp_rate_global = fp_total / total_weight

    feature_agg = _aggregate_feature_fp(rules, rule_buckets)
    if not feature_agg:
        return _finish(RegressionResult(status=L3Status.SKIP_NO_REGRESSION, notes="no features observed"))

    new_weights = dict(bundle.weights)
    drift_summary: dict[str, float] = {}
    for key, (fp_w, tot_w) in feature_agg.items():
        if tot_w <= 0:
            continue
        feature_fp_rate = fp_w / tot_w
        delta = -(feature_fp_rate - fp_rate_global) * 2.0
        adjustment = config.learning_rate * delta
        if key not in new_weights:
            continue
        proposed = new_weights[key] + adjustment
        drift = proposed - bundle.weights.get(key, 0.0)
        drift_summary[key] = drift
        new_weights[key] = proposed

    max_abs_drift = max((abs(v) for v in drift_summary.values()), default=0.0)
    if max_abs_drift > config.max_weight_drift:
        result = RegressionResult(
            status=L3Status.PAUSED_DRIFT,
            new_weights=new_weights,
            drift_summary=drift_summary,
            fp_rate_global=fp_rate_global,
            outcomes_total=total_count,
            notes=f"max_drift={max_abs_drift:.3f}>{config.max_weight_drift}",
        )
        return _finish(result)

    bundle.weights = new_weights
    bundle.generated_by = "regressor"
    weights_store.save_active(
        workdir,
        bundle,
        history_reason=f"l3_run total={total_count} fp_rate={fp_rate_global:.3f}",
    )
    result = RegressionResult(
        status=L3Status.OK,
        new_weights=new_weights,
        drift_summary=drift_summary,
        fp_rate_global=fp_rate_global,
        outcomes_total=total_count,
        notes=f"applied; max_drift={max_abs_drift:.3f}",
    )
    return _finish(result)


def _write_report(
    workdir: str,
    project_id: str,
    started: float,
    result: RegressionResult,
    *,
    finished_ts: float | None = None,
) -> None:
    report = RunReport(
        project_id=project_id,
        level=3,
        started_ts=started,
        finished_ts=float(finished_ts if finished_ts is not None else time.time()),
        status=result.status.value,
    )
    if result.notes:
        report.errors.append(result.notes)
    write_report(workdir, report)
