from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from app.services.lint_evolution import candidates_store, rules_store
from app.services.lint_evolution.evolver import L1Config, run_level1
from app.services.lint_evolution.lint_decision import DecisionConfig
from app.services.lint_evolution.paths import db_path


_REVIEW_TEMPLATE = """# Review Result [{tag}]

**Timestamp:** 2026-04-15 12:34:{sec:02d}

---

{{
  "approved": false,
  "summary": "Тесты не проходят: модуль {tag}.",
  "comments": "{comment}",
  "tests_passed": false,
  "files_reviewed": [],
  "not_done_assessment": []
}}
"""


def _seed_review_corpus(root: Path, *, count: int) -> None:
    review_dir = root / ".cli-proxy" / ".manager" / "response"
    review_dir.mkdir(parents=True)
    for i in range(count):
        f = review_dir / f"20260415_120000_manager_review_result_T{i}.md"
        f.write_text(
            _REVIEW_TEMPLATE.format(
                tag=f"T{i}",
                sec=i,
                comment=f"Тест падает в test_module_{i}: ассерт сломан",
            ),
            encoding="utf-8",
        )


def _good_classification() -> dict:
    return {
        "rule_kind": "tests_failing",
        "category": "correctness",
        "false_positive_risk": "low",
        "detector_type": "regex",
        "scope_subjectivity": "objective",
        "duplicate_of_active": None,
        "extends_active": None,
        "test_generatable": True,
        "confidence": 0.9,
        "notes": "",
    }


def _decision_config() -> DecisionConfig:
    return DecisionConfig(
        min_distinct_subjects=3,
        min_weighted_count=5.0,
        apply_threshold=2.5,
        revise_threshold=1.0,
        merge_threshold=1.5,
        weights={
            "category_correctness": 1.5,
            "detector_regex": 1.0,
            "fp_risk_low": 0.0,
            "scope_objective": 0.5,
            "confidence_multiplier": 0.5,
        },
    )


@pytest.mark.asyncio
async def test_evolver_applies_rule_when_classifier_stable_and_score_high(tmp_path: Path) -> None:
    _seed_review_corpus(tmp_path, count=5)
    workdir = str(tmp_path)

    async def classify(text: str, examples: list[str] | None) -> dict:
        return _good_classification()

    config = L1Config(decision_config=_decision_config())
    report = await run_level1(
        workdir=workdir,
        project_id="proj-x",
        project_root=tmp_path,
        classify_fn=classify,
        config=config,
    )

    assert report.status == "ok"
    assert report.signals_ingested >= 5
    assert report.candidates_examined >= 1
    apply_decisions = [d for d in report.decisions if d.decision == "apply"]
    assert apply_decisions, f"expected at least one apply, got {report.decisions}"
    rules = rules_store.load_rules(workdir)
    assert any(r.rule_kind == "tests_failing" and r.state == "active" for r in rules)
    with sqlite3.connect(str(db_path(workdir))) as conn:
        rows = conn.execute("SELECT level, status, finished_ts FROM runs").fetchall()
    assert rows == [(1, "ok", pytest.approx(report.finished_ts))]


@pytest.mark.asyncio
async def test_evolver_holds_when_classifier_unstable(tmp_path: Path) -> None:
    _seed_review_corpus(tmp_path, count=5)
    workdir = str(tmp_path)
    counter = {"n": 0}

    async def flaky_classify(text: str, examples: list[str] | None) -> dict:
        counter["n"] += 1
        flip = counter["n"] % 2 == 0
        return {**_good_classification(), "false_positive_risk": "high" if flip else "low"}

    report = await run_level1(
        workdir=workdir,
        project_id="proj-x",
        project_root=tmp_path,
        classify_fn=flaky_classify,
        config=L1Config(decision_config=_decision_config()),
    )
    assert report.status == "ok"
    holds = [d for d in report.decisions if d.decision == "hold"]
    assert any("unstable_classifier" in d.reason for d in holds)


@pytest.mark.asyncio
async def test_evolver_writes_candidate_on_revise(tmp_path: Path) -> None:
    _seed_review_corpus(tmp_path, count=5)
    workdir = str(tmp_path)

    async def classify(text: str, examples: list[str] | None) -> dict:
        return {**_good_classification(), "category": "performance", "false_positive_risk": "medium"}

    config = L1Config(decision_config=_decision_config())
    config.decision_config.weights.update({"category_performance": 1.0, "fp_risk_medium": -1.0})
    await run_level1(
        workdir=workdir,
        project_id="proj-x",
        project_root=tmp_path,
        classify_fn=classify,
        config=config,
    )
    pending = candidates_store.load_pending(workdir)
    assert pending, "expected revise/hold candidate to be persisted"


@pytest.mark.asyncio
async def test_evolver_skips_active_rule_kind(tmp_path: Path) -> None:
    _seed_review_corpus(tmp_path, count=5)
    workdir = str(tmp_path)
    rules_store.add_rule(
        workdir,
        rules_store.Rule(
            id="tests_failing-existing",
            rule_kind="tests_failing",
            detector_type="regex",
            detector_payload=rules_store.DetectorPayload(pattern="x"),
            state="active",
        ),
    )

    async def classify(text: str, examples: list[str] | None) -> dict:
        return _good_classification()

    report = await run_level1(
        workdir=workdir,
        project_id="proj-x",
        project_root=tmp_path,
        classify_fn=classify,
        config=L1Config(decision_config=_decision_config()),
    )
    apply_for_kind = [d for d in report.decisions if d.rule_kind == "tests_failing"]
    assert apply_for_kind == []
