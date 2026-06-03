from __future__ import annotations

import types
from pathlib import Path

from modes.sdd.mode import SddMode
from modes.sdd.phases import (
    PHASE_ORDER,
    analyze_coverage,
    next_phase,
    parse_plan_decisions,
    render_analyze_md,
)
from modes.sdd.state import get_sdd_state
from modes.sdk.runtime.contracts import DevTask, ProjectPlan


def _task(tid: str, covers) -> DevTask:
    return DevTask(
        id=tid, title=f"title {tid}", description="", acceptance_criteria=[],
        covers_requirements=list(covers), depends_on=[], status="pending",
    )


def _plan(tasks) -> ProjectPlan:
    return ProjectPlan(project_goal="g", tasks=tasks, status="active", created_at="", updated_at="")


REQS = [{"id": "REQ-1", "text": "do X"}, {"id": "REQ-2", "text": "do Y"}]


# ---------------------------------------------------------------------------
# PHASE_ORDER / next_phase
# ---------------------------------------------------------------------------

def test_phase_order_includes_analyze_as_terminal() -> None:
    assert PHASE_ORDER == ("specify", "plan", "tasks", "analyze")
    assert next_phase("tasks") == "analyze"
    assert next_phase("analyze") is None
    assert next_phase("plan") == "tasks"


# ---------------------------------------------------------------------------
# analyze_coverage
# ---------------------------------------------------------------------------

def test_analyze_coverage_all_covered() -> None:
    report = analyze_coverage(REQS, _plan([_task("TASK-1", ["REQ-1"]), _task("TASK-2", ["REQ-2"])]))
    assert report["covered"] == ["REQ-1", "REQ-2"]
    assert report["uncovered"] == []
    assert report["orphan_task_ids"] == []
    assert report["total_reqs"] == 2
    assert report["total_tasks"] == 2


def test_analyze_coverage_uncovered_and_orphan() -> None:
    report = analyze_coverage(REQS, _plan([_task("TASK-1", ["REQ-1"]), _task("TASK-2", [])]))
    assert report["covered"] == ["REQ-1"]
    assert report["uncovered"] == ["REQ-2"]
    assert report["orphan_task_ids"] == ["TASK-2"]


def test_analyze_coverage_orphan_when_covers_unknown_req() -> None:
    report = analyze_coverage(REQS, _plan([_task("TASK-1", ["REQ-99"])]))
    assert report["uncovered"] == ["REQ-1", "REQ-2"]
    assert report["orphan_task_ids"] == ["TASK-1"]


def test_analyze_coverage_empty_plan() -> None:
    report = analyze_coverage(REQS, _plan([]))
    assert report["uncovered"] == ["REQ-1", "REQ-2"]
    assert report["orphan_task_ids"] == []
    assert report["total_tasks"] == 0


# ---------------------------------------------------------------------------
# render_analyze_md
# ---------------------------------------------------------------------------

def test_render_analyze_md_full_coverage_message() -> None:
    report = analyze_coverage(REQS, _plan([_task("TASK-1", ["REQ-1"]), _task("TASK-2", ["REQ-2"])]))
    md = render_analyze_md(report, REQS, _plan([_task("TASK-1", ["REQ-1"]), _task("TASK-2", ["REQ-2"])]))
    assert "# Анализ покрытия" in md
    assert "Все требования покрыты" in md
    assert "## Непокрытые требования" not in md


def test_render_analyze_md_lists_uncovered_and_orphans() -> None:
    plan = _plan([_task("TASK-1", ["REQ-1"]), _task("TASK-2", [])])
    report = analyze_coverage(REQS, plan)
    md = render_analyze_md(report, REQS, plan)
    assert "## Непокрытые требования" in md
    assert "REQ-2: do Y" in md
    assert "## Задачи вне требований" in md
    assert "TASK-2: title TASK-2" in md


# ---------------------------------------------------------------------------
# parse_plan_decisions
# ---------------------------------------------------------------------------

def test_parse_plan_decisions_extracts_architecture_and_constraints() -> None:
    plan = (
        "# Technical Plan\n\n## Architecture\n\nLayered design here.\nMore detail.\n\n"
        "## Constraints\n\n- async only\n- no globals\n\n## Risks\n\n- x\n"
    )
    decs = parse_plan_decisions(plan)
    assert decs[0].startswith("Layered design here.")
    assert "async only" in decs
    assert "no globals" in decs


def test_parse_plan_decisions_empty_when_no_sections() -> None:
    assert parse_plan_decisions("# Technical Plan\n\n## Risks\n\n- x\n") == []


# ---------------------------------------------------------------------------
# _append_feature_decisions (D2) — best-effort, idempotent
# ---------------------------------------------------------------------------

def _session_with_slug(tmp_path: Path, slug: str):
    from session import SddState
    session = types.SimpleNamespace(sdd=SddState(), workdir=str(tmp_path))
    get_sdd_state(session).feature_slug = slug
    return session


def test_append_feature_decisions_writes_and_idempotent(tmp_path: Path) -> None:
    spec_dir = tmp_path / "specs" / "001-feat"
    spec_dir.mkdir(parents=True)
    (spec_dir / "spec.md").write_text(
        "# Spec\n\n## Out of Scope\n\n- caching\n- i18n\n", encoding="utf-8")
    (spec_dir / "plan.md").write_text(
        "# Plan\n\n## Architecture\n\nUse a layered service.\n\n## Constraints\n\n- async only\n",
        encoding="utf-8")
    session = _session_with_slug(tmp_path, "my-feature")
    mode = SddMode()

    mode._append_feature_decisions(session, str(tmp_path), str(spec_dir))
    dpath = tmp_path / ".cli-proxy" / "decisions.md"
    assert dpath.is_file()
    text = dpath.read_text(encoding="utf-8")
    assert "## my-feature" in text
    assert "caching" in text
    assert "Use a layered service." in text
    assert "async only" in text

    size1 = dpath.stat().st_size
    mode._append_feature_decisions(session, str(tmp_path), str(spec_dir))  # re-accept
    assert dpath.stat().st_size == size1  # idempotent per slug, no growth


def test_append_feature_decisions_never_raises(monkeypatch, tmp_path: Path) -> None:
    import modes.sdd.mode as sdd_mode

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(sdd_mode, "append_decision", boom)
    session = _session_with_slug(tmp_path, "f")
    mode = SddMode()
    # Must swallow the error so the handoff is never blocked.
    mode._append_feature_decisions(session, str(tmp_path), str(tmp_path))
