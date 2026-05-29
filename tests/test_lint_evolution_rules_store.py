from __future__ import annotations

from pathlib import Path

import pytest

from app.services.lint_evolution import rules_store


def _rule(rid: str = "r1", kind: str = "tests_failing", state: str = "active") -> rules_store.Rule:
    return rules_store.Rule(
        id=rid,
        rule_kind=kind,
        detector_type="regex",
        detector_payload=rules_store.DetectorPayload(pattern="x", target_glob="**/*.py"),
        metadata=rules_store.RuleMetadata(added_run_id=1, schema_v=1, example_signal="abc"),
        state=state,
    )


def test_add_load_round_trip(tmp_path: Path) -> None:
    workdir = str(tmp_path)
    rules_store.add_rule(workdir, _rule())
    loaded = rules_store.load_rules(workdir)
    assert len(loaded) == 1
    assert loaded[0].id == "r1"
    assert loaded[0].rule_kind == "tests_failing"
    assert loaded[0].metadata.added_ts > 0


def test_duplicate_id_rejected(tmp_path: Path) -> None:
    workdir = str(tmp_path)
    rules_store.add_rule(workdir, _rule())
    with pytest.raises(ValueError):
        rules_store.add_rule(workdir, _rule())


def test_invalid_detector_type_rejected(tmp_path: Path) -> None:
    workdir = str(tmp_path)
    rule = _rule()
    rule.detector_type = "llm_only"
    with pytest.raises(ValueError):
        rules_store.add_rule(workdir, rule)


def test_update_state(tmp_path: Path) -> None:
    workdir = str(tmp_path)
    rules_store.add_rule(workdir, _rule())
    assert rules_store.update_rule_state(workdir, "r1", state="demoted") is True
    assert rules_store.update_rule_state(workdir, "missing", state="demoted") is False
    rule = rules_store.load_rules(workdir)[0]
    assert rule.state == "demoted"


def test_increment_metric(tmp_path: Path) -> None:
    workdir = str(tmp_path)
    rules_store.add_rule(workdir, _rule())
    assert rules_store.increment_metric(workdir, "r1", hits=2, fp=1) is True
    rule = rules_store.load_rules(workdir)[0]
    assert rule.metrics.hits == 2
    assert rule.metrics.fp_count == 1


def test_find_active_by_kind(tmp_path: Path) -> None:
    workdir = str(tmp_path)
    rules_store.add_rule(workdir, _rule(rid="a", kind="tests_failing"))
    rules_store.add_rule(workdir, _rule(rid="b", kind="syntax_error", state="demoted"))
    rules = rules_store.load_rules(workdir)
    assert rules_store.find_active_by_kind(rules, "tests_failing").id == "a"
    assert rules_store.find_active_by_kind(rules, "syntax_error") is None
