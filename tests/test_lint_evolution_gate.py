from __future__ import annotations

from pathlib import Path

from app.services.lint_evolution import rules_store
from app.services.lint_evolution.gate_service import LintGateService


def _add_rule(workdir: str, *, rid: str, pattern: str, kind: str = "tests_failing", detector_type: str = "regex") -> None:
    rules_store.add_rule(
        workdir,
        rules_store.Rule(
            id=rid,
            rule_kind=kind,
            detector_type=detector_type,
            detector_payload=rules_store.DetectorPayload(pattern=pattern, target_glob="**/*.py"),
            metadata=rules_store.RuleMetadata(added_run_id=1, schema_v=1, example_signal=""),
            state="active",
        ),
    )


def test_gate_finds_pattern_in_file(tmp_path: Path) -> None:
    workdir = str(tmp_path)
    _add_rule(workdir, rid="r1", pattern=r"# TODO\b")
    src = tmp_path / "src.py"
    src.write_text("first line\n# TODO finish later\nlast line\n", encoding="utf-8")

    gate = LintGateService(workdir, project_root=tmp_path)
    result = gate.run_on_files([src])
    assert result.rules_evaluated == 1
    assert result.files_scanned == 1
    assert len(result.findings) == 1
    finding = result.findings[0]
    assert finding.rule_id == "r1"
    assert finding.line == 2
    assert "TODO" in finding.snippet


def test_gate_skips_non_regex_rules(tmp_path: Path) -> None:
    workdir = str(tmp_path)
    rules_store.add_rule(
        workdir,
        rules_store.Rule(
            id="ast1",
            rule_kind="logic_error",
            detector_type="ast",
            detector_payload=rules_store.DetectorPayload(ast_check="some_check"),
            metadata=rules_store.RuleMetadata(),
            state="active",
        ),
    )
    src = tmp_path / "x.py"
    src.write_text("nothing here\n", encoding="utf-8")
    gate = LintGateService(workdir, project_root=tmp_path)
    res = gate.run_on_files([src])
    assert res.skipped_rules == ["ast1"]
    assert res.rules_evaluated == 0
    assert res.findings == []


def test_gate_handles_invalid_regex_gracefully(tmp_path: Path) -> None:
    workdir = str(tmp_path)
    _add_rule(workdir, rid="bad", pattern="(unclosed")
    src = tmp_path / "x.py"
    src.write_text("anything\n", encoding="utf-8")
    res = LintGateService(workdir, project_root=tmp_path).run_on_files([src])
    assert "bad" in res.skipped_rules


def test_gate_skips_demoted_rules(tmp_path: Path) -> None:
    workdir = str(tmp_path)
    _add_rule(workdir, rid="r1", pattern=r"foo")
    rules_store.update_rule_state(workdir, "r1", state="demoted")
    src = tmp_path / "x.py"
    src.write_text("foo bar\n", encoding="utf-8")
    res = LintGateService(workdir, project_root=tmp_path).run_on_files([src])
    assert res.findings == []
    assert res.rules_evaluated == 0
