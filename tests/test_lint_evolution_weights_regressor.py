from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from app.services.lint_evolution import fingerprints, rules_store, weights_store
from app.services.lint_evolution.paths import db_path
from app.services.lint_evolution.weights_regressor import L3Config, L3Status, run_level3


def _add_active_rule(
    workdir: str,
    *,
    rule_id: str,
    rule_kind: str,
    classification: dict,
) -> None:
    rules_store.add_rule(
        workdir,
        rules_store.Rule(
            id=rule_id,
            rule_kind=rule_kind,
            detector_type="regex",
            detector_payload=rules_store.DetectorPayload(pattern="x"),
            metadata=rules_store.RuleMetadata(classification=classification),
            state="active",
        ),
    )


def _seed_outcomes(
    workdir: str,
    *,
    project_id: str,
    rule_id: str,
    fp_count: int,
    tp_count: int,
    ts: float,
) -> None:
    for i in range(fp_count):
        fingerprints.insert_outcome(
            workdir,
            project_id=project_id,
            rule_id=rule_id,
            outcome="reverted",
            weight=1.0,
            ts=ts + i,
        )
    for i in range(tp_count):
        fingerprints.insert_outcome(
            workdir,
            project_id=project_id,
            rule_id=rule_id,
            outcome="committed",
            weight=1.0,
            ts=ts + fp_count + i,
        )


def test_skip_when_total_below_threshold(tmp_path: Path) -> None:
    workdir = str(tmp_path)
    _add_active_rule(
        workdir,
        rule_id="r1",
        rule_kind="tests_failing",
        classification={"category": "correctness"},
    )
    _seed_outcomes(workdir, project_id="proj", rule_id="r1", fp_count=10, tp_count=10, ts=1000.0)

    cfg = L3Config(min_outcomes_total=200, min_outcomes_per_rule=50)
    res = run_level3(workdir=workdir, project_id="proj", config=cfg, now=2_000_000.0, record_report=False)
    assert res.status is L3Status.SKIP_INSUFFICIENT_DATA
    assert res.outcomes_total == 20


def test_skip_on_fresh_workdir_without_outcomes_table(tmp_path: Path) -> None:
    workdir = str(tmp_path)

    cfg = L3Config(min_outcomes_total=1, min_outcomes_per_rule=1)
    res = run_level3(workdir=workdir, project_id="proj", config=cfg, now=2_000_000.0, record_report=False)

    assert res.status is L3Status.SKIP_INSUFFICIENT_DATA
    assert res.outcomes_total == 0


def test_skip_when_per_rule_below_threshold(tmp_path: Path) -> None:
    workdir = str(tmp_path)
    _add_active_rule(
        workdir,
        rule_id="r1",
        rule_kind="tests_failing",
        classification={"category": "correctness"},
    )
    _add_active_rule(
        workdir,
        rule_id="r2",
        rule_kind="syntax_error",
        classification={"category": "correctness"},
    )
    _seed_outcomes(workdir, project_id="proj", rule_id="r1", fp_count=5, tp_count=15, ts=1000.0)
    _seed_outcomes(workdir, project_id="proj", rule_id="r2", fp_count=2, tp_count=3, ts=2000.0)

    cfg = L3Config(min_outcomes_total=10, min_outcomes_per_rule=10)
    res = run_level3(workdir=workdir, project_id="proj", config=cfg, now=2_000_000.0, record_report=False)
    assert res.status is L3Status.SKIP_INSUFFICIENT_DATA


def test_ok_with_small_drift_writes_weights(tmp_path: Path) -> None:
    workdir = str(tmp_path)
    weights_store.bootstrap(workdir)
    _add_active_rule(
        workdir,
        rule_id="r1",
        rule_kind="tests_failing",
        classification={"category": "correctness", "detector_type": "regex"},
    )
    # Balanced outcomes: feature_fp_rate ≈ global_fp_rate → tiny adjustment
    _seed_outcomes(workdir, project_id="proj", rule_id="r1", fp_count=10, tp_count=10, ts=1000.0)

    cfg = L3Config(
        min_outcomes_total=10,
        min_outcomes_per_rule=10,
        max_weight_drift=0.5,
        learning_rate=0.05,
    )
    res = run_level3(workdir=workdir, project_id="proj", config=cfg, now=2_000_000.0, record_report=False)
    assert res.status is L3Status.OK
    assert res.outcomes_total == 20
    assert res.fp_rate_global == pytest.approx(0.5, abs=1e-6)
    # Drift recorded for keys that exist in bundled weights
    assert "category_correctness" in res.drift_summary
    # Persisted
    reloaded = weights_store.load_active(workdir)
    assert reloaded.generated_by == "regressor"
    assert weights_store.history_count(workdir) == 1


def test_level3_records_run_row_when_report_is_recorded(tmp_path: Path) -> None:
    workdir = str(tmp_path)
    weights_store.bootstrap(workdir)
    _add_active_rule(
        workdir,
        rule_id="r1",
        rule_kind="tests_failing",
        classification={"category": "correctness", "detector_type": "regex"},
    )
    _seed_outcomes(workdir, project_id="proj", rule_id="r1", fp_count=5, tp_count=5, ts=1000.0)

    cfg = L3Config(min_outcomes_total=10, min_outcomes_per_rule=10, learning_rate=0.05)
    res = run_level3(workdir=workdir, project_id="proj", config=cfg, now=2_000_000.0, record_report=True)

    assert res.status is L3Status.OK
    with sqlite3.connect(str(db_path(workdir))) as conn:
        rows = conn.execute("SELECT level, status, candidates_count, applied_count FROM runs").fetchall()
    assert rows == [(3, "ok", 10, 1)]


def test_paused_drift_does_not_persist(tmp_path: Path) -> None:
    workdir = str(tmp_path)
    weights_store.bootstrap(workdir)
    before = weights_store.load_active(workdir)
    baseline_weight = before.weights["category_correctness"]

    _add_active_rule(
        workdir,
        rule_id="r1",
        rule_kind="tests_failing",
        classification={"category": "correctness", "detector_type": "regex"},
    )
    # All outcomes for the only active feature are FP → feature_fp - global_fp = 0
    # Force divergence: add inert outcomes for an unknown rule_id (no classification carried)
    _seed_outcomes(workdir, project_id="proj", rule_id="r1", fp_count=20, tp_count=0, ts=1000.0)
    # Inflate global pool with wins for an inactive rule (carries no feature)
    _seed_outcomes(workdir, project_id="proj", rule_id="ghost", fp_count=0, tp_count=80, ts=2000.0)

    # Now feature_fp_rate=1.0, global_fp_rate=20/100=0.2 → delta=-(1-0.2)*2=-1.6
    # learning_rate * delta = 1.0 * -1.6 = -1.6 → exceeds max_weight_drift=0.5
    cfg = L3Config(
        min_outcomes_total=10,
        min_outcomes_per_rule=10,
        max_weight_drift=0.5,
        learning_rate=1.0,
    )
    res = run_level3(workdir=workdir, project_id="proj", config=cfg, now=2_000_000.0, record_report=False)
    assert res.status is L3Status.PAUSED_DRIFT
    after = weights_store.load_active(workdir)
    assert after.weights["category_correctness"] == baseline_weight
    assert weights_store.history_count(workdir) == 0


def test_skip_no_regression_when_no_active_rules(tmp_path: Path) -> None:
    workdir = str(tmp_path)
    weights_store.bootstrap(workdir)
    # Outcomes exist but no active rules carry features → empty feature_agg
    _seed_outcomes(workdir, project_id="proj", rule_id="ghost", fp_count=5, tp_count=5, ts=1000.0)

    cfg = L3Config(min_outcomes_total=10, min_outcomes_per_rule=10)
    res = run_level3(workdir=workdir, project_id="proj", config=cfg, now=2_000_000.0, record_report=False)
    assert res.status is L3Status.SKIP_NO_REGRESSION


def test_outcomes_outside_window_are_ignored(tmp_path: Path) -> None:
    workdir = str(tmp_path)
    weights_store.bootstrap(workdir)
    _add_active_rule(
        workdir,
        rule_id="r1",
        rule_kind="tests_failing",
        classification={"category": "correctness"},
    )
    now = 2_000_000.0
    # Inside window
    _seed_outcomes(workdir, project_id="proj", rule_id="r1", fp_count=5, tp_count=5, ts=now - 3600)
    # Far outside default 90d window
    old_ts = now - (200 * 24 * 3600.0)
    _seed_outcomes(workdir, project_id="proj", rule_id="r1", fp_count=100, tp_count=100, ts=old_ts)

    cfg = L3Config(min_outcomes_total=200, min_outcomes_per_rule=50)
    res = run_level3(workdir=workdir, project_id="proj", config=cfg, now=now, record_report=False)
    assert res.status is L3Status.SKIP_INSUFFICIENT_DATA
    assert res.outcomes_total == 10
