from __future__ import annotations

from pathlib import Path

from app.services.lint_evolution import autopause, canary_metric, fingerprints, schema_store


def _seed(workdir: str, project_id: str, *, ts: float, fp: int, tp: int) -> None:
    for i in range(fp):
        fingerprints.insert_outcome(
            workdir, project_id=project_id, rule_id="r", outcome="reverted", ts=ts + i
        )
    for i in range(tp):
        fingerprints.insert_outcome(
            workdir, project_id=project_id, rule_id="r", outcome="committed", ts=ts + fp + i
        )


def test_no_trigger_with_clean_data(tmp_path: Path) -> None:
    workdir = str(tmp_path)
    now = 2_000_000.0
    # Stable rate ~20% across both windows
    _seed(workdir, "proj", ts=now - 86400 * 3, fp=2, tp=8)
    _seed(workdir, "proj", ts=now - 86400 * 20, fp=4, tp=16)

    cfg = canary_metric.CanaryConfig()
    rep = canary_metric.evaluate(workdir, project_id="proj", config=cfg, now=now)
    assert "fp_canary" not in rep.triggered
    assert autopause.is_paused(workdir, 1) is False
    assert autopause.is_paused(workdir, 2) is False


def test_fp_growth_triggers_autopause(tmp_path: Path) -> None:
    workdir = str(tmp_path)
    now = 2_000_000.0
    # Baseline 30d: low fp rate (~10%)
    _seed(workdir, "proj", ts=now - 86400 * 25, fp=2, tp=18)
    # Rolling 7d: high fp rate (~80%)
    _seed(workdir, "proj", ts=now - 86400 * 2, fp=16, tp=4)

    cfg = canary_metric.CanaryConfig(fp_growth_threshold_pct=50.0)
    rep = canary_metric.evaluate(workdir, project_id="proj", config=cfg, now=now)
    assert "fp_canary" in rep.triggered
    assert rep.fp_growth_pct > 50.0
    assert autopause.is_paused(workdir, 1) is True
    assert autopause.is_paused(workdir, 2) is True


def test_schema_thrash_triggers_autopause(tmp_path: Path) -> None:
    workdir = str(tmp_path)
    now = 1_780_000_000.0  # ~2026
    schema_store.bootstrap_schema(workdir)
    state = schema_store.load_state(workdir)
    state.active_version = 6  # 5 fields added since v1
    state.last_bump_ts = now - 86400 * 30  # within 180d
    schema_store.save_state(workdir, state)

    cfg = canary_metric.CanaryConfig(schema_max_fields_per_180d=3)
    rep = canary_metric.evaluate(workdir, project_id="proj", config=cfg, now=now)
    assert "schema_thrash" in rep.triggered
    assert autopause.is_paused(workdir, 2) is True


def test_no_trigger_when_baseline_too_small(tmp_path: Path) -> None:
    workdir = str(tmp_path)
    now = 2_000_000.0
    # very few outcomes — even a 100% fp rate must not pause
    _seed(workdir, "proj", ts=now - 86400 * 2, fp=3, tp=0)
    cfg = canary_metric.CanaryConfig()
    rep = canary_metric.evaluate(workdir, project_id="proj", config=cfg, now=now)
    assert "fp_canary" not in rep.triggered
