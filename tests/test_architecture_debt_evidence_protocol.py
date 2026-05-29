from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DOC = ROOT / "docs" / "architecture-debt-remediation-evidence.md"


def _read_doc() -> str:
    return DOC.read_text(encoding="utf-8")


def _section(text: str, heading: str) -> str:
    pattern = rf"^## {re.escape(heading)}\n(?P<body>.*?)(?=^## |\Z)"
    match = re.search(pattern, text, flags=re.MULTILINE | re.DOTALL)
    assert match is not None, f"missing section: {heading}"
    return match.group("body")


def _ordered_positions(text: str, needles: list[str]) -> list[int]:
    positions = [text.index(needle) for needle in needles]
    assert positions == sorted(positions)
    return positions


def test_per_pr_evidence_protocol_has_required_order_and_report_template() -> None:
    text = _read_doc()
    scope = _section(text, "Scope And Source Of Truth")
    evidence_order = _section(text, "Per-PR Evidence Order")

    assert "does not assert current defect counts" in scope
    assert "changed code" in scope
    assert "relevant executable tests" in scope
    assert "must not be used to claim that a code path is fixed" in scope

    _ordered_positions(
        evidence_order,
        [
            "1. Outcome:",
            "2. Constraints:",
            "3. Targeted checks:",
            "4. Nearest integration checks:",
            "5. Evidence gaps:",
        ],
    )
    assert "`not_done`" in evidence_order
    assert "task_35 only records this protocol" in evidence_order
    assert "must not include secrets" in evidence_order
    assert "task_39 depends only on task_27" in evidence_order
    assert "task_29 depends only on task_28 and can close without waiting for task_39" in evidence_order
    assert "task_39 is a hardening follow-up, not a subtask or blocker for task_29" in evidence_order

    report_template = text.split("## Report Template", 1)[1]
    _ordered_positions(
        report_template,
        [
            "## Outcome",
            "## Constraints",
            "## Targeted Checks",
            "## Nearest Integration Checks",
            "## Guard Checks",
            "## Evidence Gaps",
        ],
    )


def test_full_suite_gate_is_not_required_for_every_narrow_pr() -> None:
    full_gate = _section(_read_doc(), "Release, Smoke, And Full Suite Gate")

    assert "not required for every narrow PR" in full_gate
    assert "before a release or smoke gate" in full_gate
    assert "user explicitly asks" in full_gate
    assert "shared runtime change is too broad" in full_gate
    assert "ps -eo pid,ppid,stat,etime,command" in full_gate
    assert "rg '(^|[ /])(python3?|pytest)( |$)|\\.venv/bin/pytest'" in full_gate
    assert "Do not kill or recycle anything on port 8088" in full_gate
    assert "changed-file flake8 is the required per-PR gate" in full_gate


def test_guard_inventory_lists_required_families_without_implementing_gates() -> None:
    inventory = _section(_read_doc(), "Existing Guard Inventory")

    assert "does not implement new grep gates" in inventory
    assert "does not claim that every listed family currently has an open defect" in inventory
    required_items = [
        "`md2=False` usage allowlist and plain-text messaging boundary.",
        "Direct `json.loads` in mode/runtime JSON-sensitive code.",
        "Direct `await request.json()` outside centralized MiniApp JSON helpers.",
        "Direct `_persist_sessions` access outside documented legacy boundaries.",
        "`except Exception: pass` silent fallback policy and audit allowlist.",
        "Queue append allowlist and queue item normalization.",
        "Codebase map counts and tracked map contract.",
    ]
    for item in required_items:
        assert f"- {item}" in inventory
    assert "mark that checklist item `not_done`" in inventory


def test_fallback_rule_requires_logging_context_category_and_not_done_gap() -> None:
    fallback_rule = _section(_read_doc(), "Fallback Logging Rule")

    assert "must log with enough context" in fallback_rule
    assert "`legacy_fallback`" in fallback_rule
    assert "`best_effort`" in fallback_rule
    assert "`best_effort_cleanup`" in fallback_rule
    assert "without logging and one of these categories" in fallback_rule
    assert "remains `not_done`" in fallback_rule
