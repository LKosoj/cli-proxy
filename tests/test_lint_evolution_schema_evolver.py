from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from app.services.lint_evolution import candidates_store, schema_store
from app.services.lint_evolution.paths import db_path
from app.services.lint_evolution.schema_decision import SchemaDecisionConfig
from app.services.lint_evolution.schema_evolver import L2Config, run_level2


def _seed_notes(workdir: str, notes: list[str]) -> None:
    for i, n in enumerate(notes):
        candidates_store.add_rejected(
            workdir,
            candidates_store.Candidate(
                rule_kind=f"unknown_{i}",
                decision="reject",
                reason="n/a",
                score=0.0,
                classification={"notes": n},
            ),
        )


def _config() -> L2Config:
    cfg = L2Config(
        decision_config=SchemaDecisionConfig(
            min_examples=5,
            min_distinct_cases=3,
            min_bump_interval_days=30,
            max_pending=1,
            extend_threshold=1.5,
            propose_threshold=0.8,
            weights={"examples_count_norm": 1.0, "distinct_cases_norm": 1.0, "proposed_type_bool": 0.5},
        ),
        notes_per_run=10,
    )
    return cfg


@pytest.mark.asyncio
async def test_extend_schema_when_meta_returns_strong_field(tmp_path: Path) -> None:
    workdir = str(tmp_path)
    _seed_notes(workdir, [f"note about env-specific behaviour {i}" for i in range(6)])

    async def meta(notes: list[str], existing: list[str]):
        return [
            {
                "proposed_name": "environment_specific",
                "proposed_type": "bool",
                "examples_count": 10,
                "distinct_cases": 5,
                "sample_notes": notes[:3],
                "rationale_extracted": "rule depends on prod vs dev",
                "covered_by_existing_field": None,
            }
        ]

    # bump cooldown bypass — schema_store.bootstrap sets last_bump_ts=now, so days_since_last_bump=0
    schema_store.bootstrap_schema(workdir)
    state = schema_store.load_state(workdir)
    state.last_bump_ts = 0.0
    schema_store.save_state(workdir, state)

    report = await run_level2(
        workdir=workdir,
        project_id="proj",
        project_root=tmp_path,
        meta_classify_fn=meta,
        config=_config(),
    )

    assert report.status == "ok"
    assert any(d.decision == "extend_schema" for d in report.decisions)
    schema = schema_store.load_active_schema(workdir)
    assert "environment_specific" in (schema.get("properties") or {})
    assert schema_store.load_state(workdir).active_version == 2
    with sqlite3.connect(str(db_path(workdir))) as conn:
        rows = conn.execute("SELECT level, status, candidates_count, applied_count FROM runs").fetchall()
    assert rows == [(2, "ok", 1, 1)]


@pytest.mark.asyncio
async def test_no_op_when_no_notes(tmp_path: Path) -> None:
    workdir = str(tmp_path)

    async def meta(notes: list[str], existing: list[str]):
        return []

    report = await run_level2(
        workdir=workdir,
        project_id="proj",
        project_root=tmp_path,
        meta_classify_fn=meta,
        config=_config(),
    )
    assert report.status == "ok"
    assert "no_notes_to_meta_classify" in report.errors


@pytest.mark.asyncio
async def test_defer_when_recent_bump(tmp_path: Path) -> None:
    workdir = str(tmp_path)
    _seed_notes(workdir, [f"thing {i}" for i in range(5)])
    schema_store.bootstrap_schema(workdir)  # last_bump_ts=now → cooldown active

    async def meta(notes: list[str], existing: list[str]):
        return [
            {
                "proposed_name": "another_field",
                "proposed_type": "bool",
                "examples_count": 10,
                "distinct_cases": 5,
                "sample_notes": notes[:2],
                "rationale_extracted": "x",
                "covered_by_existing_field": None,
            }
        ]

    report = await run_level2(
        workdir=workdir,
        project_id="proj",
        project_root=tmp_path,
        meta_classify_fn=meta,
        config=_config(),
    )
    assert any(d.decision == "defer" for d in report.decisions)
    assert schema_store.load_state(workdir).active_version == 1


@pytest.mark.asyncio
async def test_meta_failure_marks_error(tmp_path: Path) -> None:
    workdir = str(tmp_path)
    _seed_notes(workdir, ["a", "b", "c"])

    async def meta(notes: list[str], existing: list[str]):
        return None

    report = await run_level2(
        workdir=workdir,
        project_id="proj",
        project_root=tmp_path,
        meta_classify_fn=meta,
        config=_config(),
    )
    assert report.status == "error"
