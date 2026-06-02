from __future__ import annotations

from modes.sdk.runtime.contracts import DevTask, ProjectPlan
from modes.sdd.artifacts import parse_tasks_md, render_tasks_md


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_plan(**kwargs) -> ProjectPlan:
    defaults = dict(
        project_goal="Test project",
        tasks=[],
        status="active",
        created_at="2024-01-01T00:00:00",
        updated_at="2024-01-02T00:00:00",
        current_task_id=None,
        completion_report=None,
    )
    defaults.update(kwargs)
    return ProjectPlan(**defaults)


def _make_task(**kwargs) -> DevTask:
    defaults = dict(
        id="T01",
        title="Sample task",
        description="Do something",
        acceptance_criteria=["AC1", "AC2"],
        covers_requirements=["REQ-1"],
        depends_on=[],
        status="pending",
    )
    defaults.update(kwargs)
    return DevTask(**defaults)


def _roundtrip(plan: ProjectPlan) -> ProjectPlan:
    return parse_tasks_md(render_tasks_md(plan))


# ---------------------------------------------------------------------------
# Plan-level fields
# ---------------------------------------------------------------------------

def test_roundtrip_project_goal():
    plan = _make_plan(project_goal="Build auth service")
    assert _roundtrip(plan).project_goal == "Build auth service"


def test_roundtrip_plan_status():
    plan = _make_plan(status="completed")
    assert _roundtrip(plan).status == "completed"


def test_roundtrip_created_at_updated_at():
    plan = _make_plan(created_at="2025-06-01T12:00:00", updated_at="2025-06-02T09:00:00")
    rt = _roundtrip(plan)
    assert rt.created_at == "2025-06-01T12:00:00"
    assert rt.updated_at == "2025-06-02T09:00:00"


def test_roundtrip_current_task_id_present():
    plan = _make_plan(tasks=[_make_task()], current_task_id="T01")
    assert _roundtrip(plan).current_task_id == "T01"


def test_roundtrip_current_task_id_none():
    plan = _make_plan(tasks=[_make_task()], current_task_id=None)
    assert _roundtrip(plan).current_task_id is None


def test_roundtrip_completion_report_not_serialised():
    """completion_report is not in the MD format; should be None after roundtrip."""
    plan = _make_plan(completion_report="All done.")
    assert _roundtrip(plan).completion_report is None


# ---------------------------------------------------------------------------
# Task fields
# ---------------------------------------------------------------------------

def test_roundtrip_task_id_and_title():
    task = _make_task(id="T02", title="Second task")
    rt = _roundtrip(_make_plan(tasks=[task])).tasks[0]
    assert rt.id == "T02"
    assert rt.title == "Second task"


def test_roundtrip_task_description():
    task = _make_task(description="Short description")
    rt = _roundtrip(_make_plan(tasks=[task])).tasks[0]
    assert rt.description == "Short description"


def test_roundtrip_task_description_multiline():
    task = _make_task(description="Line one\nLine two\nLine three")
    rt = _roundtrip(_make_plan(tasks=[task])).tasks[0]
    assert rt.description == "Line one\nLine two\nLine three"


def test_roundtrip_acceptance_criteria_pending():
    task = _make_task(acceptance_criteria=["Write unit tests", "Lint passes"])
    rt = _roundtrip(_make_plan(tasks=[task])).tasks[0]
    assert rt.acceptance_criteria == ["Write unit tests", "Lint passes"]


def test_roundtrip_acceptance_criteria_done_marker():
    task = _make_task(acceptance_criteria=["Tests pass:done", "Deploy works"])
    rt = _roundtrip(_make_plan(tasks=[task])).tasks[0]
    assert rt.acceptance_criteria == ["Tests pass:done", "Deploy works"]


def test_roundtrip_task_no_ac():
    task = _make_task(acceptance_criteria=[])
    rt = _roundtrip(_make_plan(tasks=[task])).tasks[0]
    assert rt.acceptance_criteria == []


def test_roundtrip_covers_requirements():
    task = _make_task(covers_requirements=["REQ-1", "REQ-3"])
    rt = _roundtrip(_make_plan(tasks=[task])).tasks[0]
    assert rt.covers_requirements == ["REQ-1", "REQ-3"]


def test_roundtrip_covers_requirements_empty():
    task = _make_task(covers_requirements=[])
    rt = _roundtrip(_make_plan(tasks=[task])).tasks[0]
    assert rt.covers_requirements == []


def test_roundtrip_depends_on():
    task = _make_task(id="T02", depends_on=["T01"])
    rt = _roundtrip(_make_plan(tasks=[task])).tasks[0]
    assert rt.depends_on == ["T01"]


def test_roundtrip_task_status_variants():
    for status in ("pending", "in_progress", "in_review", "approved", "rejected", "failed", "blocked"):
        task = _make_task(status=status)
        rt = _roundtrip(_make_plan(tasks=[task])).tasks[0]
        assert rt.status == status, f"status mismatch for {status!r}"


def test_roundtrip_unknown_status_preserved():
    task = _make_task(status="custom_state")
    rt = _roundtrip(_make_plan(tasks=[task])).tasks[0]
    assert rt.status == "custom_state"


def test_roundtrip_review_verdict():
    task = _make_task(review_verdict="approved")
    rt = _roundtrip(_make_plan(tasks=[task])).tasks[0]
    assert rt.review_verdict == "approved"


def test_roundtrip_review_verdict_none():
    task = _make_task(review_verdict=None)
    rt = _roundtrip(_make_plan(tasks=[task])).tasks[0]
    assert rt.review_verdict is None


def test_roundtrip_partial_work_note():
    task = _make_task(status="failed", partial_work_note="50% implemented, stopped at auth")
    rt = _roundtrip(_make_plan(tasks=[task])).tasks[0]
    assert rt.partial_work_note == "50% implemented, stopped at auth"


def test_roundtrip_blocked_with_partial_work_note():
    task = _make_task(status="blocked", partial_work_note="Waiting on dependency")
    rt = _roundtrip(_make_plan(tasks=[task])).tasks[0]
    assert rt.status == "blocked"
    assert rt.partial_work_note == "Waiting on dependency"


def test_roundtrip_partial_work_note_none():
    task = _make_task(partial_work_note=None)
    rt = _roundtrip(_make_plan(tasks=[task])).tasks[0]
    assert rt.partial_work_note is None


# ---------------------------------------------------------------------------
# Subtask hierarchy and auto-deps
# ---------------------------------------------------------------------------

def test_roundtrip_subtask_hierarchy_order():
    parent = _make_task(id="T03", title="Parent task", depends_on=[])
    sub1 = _make_task(id="T03.1", title="Sub one", depends_on=[], status="approved")
    sub2 = _make_task(id="T03.2", title="Sub two", depends_on=["T03.1"], status="in_progress")
    plan = _make_plan(tasks=[parent, sub1, sub2])
    rt = _roundtrip(plan)
    ids = [t.id for t in rt.tasks]
    assert ids == ["T03", "T03.1", "T03.2"]


def test_subtask_auto_dep_added_when_empty():
    """When a subtask has no explicit deps and parent is directly preceding, parent is auto-added."""
    parent = _make_task(id="T03", title="Parent")
    sub = _make_task(id="T03.1", title="Sub", depends_on=[])
    plan = _make_plan(tasks=[parent, sub])
    rt = _roundtrip(plan)
    sub_rt = next(t for t in rt.tasks if t.id == "T03.1")
    assert "T03" in sub_rt.depends_on


def test_subtask_explicit_dep_preserved():
    """Subtask with explicit deps keeps them (no extra auto-dep added)."""
    parent = _make_task(id="T03", title="Parent")
    sub = _make_task(id="T03.1", title="Sub", depends_on=["T01"])
    plan = _make_plan(tasks=[parent, sub])
    rt = _roundtrip(plan)
    sub_rt = next(t for t in rt.tasks if t.id == "T03.1")
    assert sub_rt.depends_on == ["T01"]


def test_subtask_independent_statuses():
    parent = _make_task(id="T03", title="Parent", status="approved")
    sub1 = _make_task(id="T03.1", title="Sub one", status="approved")
    sub2 = _make_task(id="T03.2", title="Sub two", status="failed")
    plan = _make_plan(tasks=[parent, sub1, sub2])
    rt = _roundtrip(plan)
    by_id = {t.id: t for t in rt.tasks}
    assert by_id["T03"].status == "approved"
    assert by_id["T03.1"].status == "approved"
    assert by_id["T03.2"].status == "failed"


def test_subtask_partial_work_preserved():
    parent = _make_task(id="T03", title="Parent")
    sub = _make_task(id="T03.1", title="Sub", status="failed", partial_work_note="half done")
    plan = _make_plan(tasks=[parent, sub])
    rt = _roundtrip(plan)
    sub_rt = next(t for t in rt.tasks if t.id == "T03.1")
    assert sub_rt.partial_work_note == "half done"


# ---------------------------------------------------------------------------
# Non-serialised fields get dataclass defaults after roundtrip
# ---------------------------------------------------------------------------

def test_non_serialised_fields_get_defaults():
    task = _make_task()
    task.attempt = 5
    task.max_attempts = 7
    task.dev_report = "some report"
    task.review_comments = "some comments"
    task.started_at = "2025-01-01"
    task.completed_at = "2025-01-02"
    plan = _make_plan(tasks=[task])
    rt = _roundtrip(plan).tasks[0]
    # These fields are NOT serialised, so they revert to defaults
    assert rt.attempt == 0
    assert rt.max_attempts == 3
    assert rt.dev_report is None
    assert rt.review_comments is None
    assert rt.started_at is None
    assert rt.completed_at is None


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------

def test_empty_plan():
    plan = _make_plan(tasks=[])
    rt = _roundtrip(plan)
    assert rt.project_goal == "Test project"
    assert rt.tasks == []


def test_multiple_tasks_order_preserved():
    tasks = [_make_task(id=f"T{i:02d}", title=f"Task {i}") for i in range(1, 6)]
    plan = _make_plan(tasks=tasks)
    rt = _roundtrip(plan)
    assert [t.id for t in rt.tasks] == [f"T{i:02d}" for i in range(1, 6)]


def test_failed_task_partial_work_not_lost():
    """Regression: failed tasks with partial_work_note survive roundtrip."""
    task = _make_task(
        id="T05",
        status="failed",
        partial_work_note="Got halfway through the refactor",
        review_verdict="rejected",
    )
    plan = _make_plan(tasks=[task])
    rt = _roundtrip(plan).tasks[0]
    assert rt.status == "failed"
    assert rt.partial_work_note == "Got halfway through the refactor"
    assert rt.review_verdict == "rejected"


def test_render_omits_covers_when_empty():
    task = _make_task(covers_requirements=[])
    md = render_tasks_md(_make_plan(tasks=[task]))
    assert "covers:" not in md


def test_render_includes_covers_when_present():
    task = _make_task(covers_requirements=["REQ-5"])
    md = render_tasks_md(_make_plan(tasks=[task]))
    assert "covers: REQ-5" in md


def test_render_omits_current_task_when_none():
    plan = _make_plan(current_task_id=None)
    md = render_tasks_md(plan)
    assert "current_task:" not in md


def test_render_subtask_uses_h3():
    parent = _make_task(id="T01", title="Parent")
    sub = _make_task(id="T01.1", title="Sub")
    md = render_tasks_md(_make_plan(tasks=[parent, sub]))
    assert "### T01.1" in md
    assert "## T01 " in md


def test_parse_unknown_status_does_not_raise():
    task = _make_task(status="weird_status")
    rt = _roundtrip(_make_plan(tasks=[task])).tasks[0]
    assert rt.status == "weird_status"


# ---------------------------------------------------------------------------
# Regression: data-loss blockers found in review
# ---------------------------------------------------------------------------

def test_roundtrip_description_with_internal_blank_line():
    """B-1: a blank line inside a multiline description must survive."""
    task = _make_task(description="Line one\n\nLine three")
    rt = _roundtrip(_make_plan(tasks=[task])).tasks[0]
    assert rt.description == "Line one\n\nLine three"


def test_roundtrip_description_leading_blank_line():
    task = _make_task(description="\nsecond line")
    rt = _roundtrip(_make_plan(tasks=[task])).tasks[0]
    assert rt.description == "\nsecond line"


def test_roundtrip_description_indented_continuation():
    task = _make_task(description="Steps:\n    - do x\n    - do y")
    rt = _roundtrip(_make_plan(tasks=[task])).tasks[0]
    assert rt.description == "Steps:\n    - do x\n    - do y"


def test_roundtrip_title_with_deps_bracket():
    """B-3: title containing '[deps: ...]' must not corrupt parsed depends_on."""
    task = _make_task(id="T07", title="Fix [deps: T00] handler", depends_on=["T02"])
    rt = _roundtrip(_make_plan(tasks=[task])).tasks[0]
    assert rt.title == "Fix [deps: T00] handler"
    assert rt.depends_on == ["T02"]


def test_roundtrip_title_with_status_keyword():
    """B-3: title containing 'status:' must not be swallowed."""
    task = _make_task(id="T08", title="Update status: parser", status="approved")
    rt = _roundtrip(_make_plan(tasks=[task])).tasks[0]
    assert rt.title == "Update status: parser"
    assert rt.status == "approved"


def test_roundtrip_title_with_covers_and_dash():
    task = _make_task(id="T09", title="Refactor [covers: REQ-9] — phase 2", covers_requirements=["REQ-1"])
    rt = _roundtrip(_make_plan(tasks=[task])).tasks[0]
    assert rt.title == "Refactor [covers: REQ-9] — phase 2"
    assert rt.covers_requirements == ["REQ-1"]


def test_roundtrip_partial_work_note_multiline():
    """B-7: a partial_work_note with newlines must not be truncated."""
    note = "Implemented login\nStuck on OAuth callback\nNeeds review"
    task = _make_task(status="failed", partial_work_note=note)
    rt = _roundtrip(_make_plan(tasks=[task])).tasks[0]
    assert rt.partial_work_note == note


def test_roundtrip_created_at_with_space():
    """W-5: planning uses '%Y-%m-%d %H:%M:%S' (with a space); must not be truncated."""
    plan = _make_plan(created_at="2025-06-01 12:00:00", updated_at="2025-06-02 09:30:15")
    rt = _roundtrip(plan)
    assert rt.created_at == "2025-06-01 12:00:00"
    assert rt.updated_at == "2025-06-02 09:30:15"


def test_orphan_subtask_does_not_raise_and_has_no_autodep():
    """W-3: a subtask whose parent is absent must not crash; no auto-dep added."""
    sub = _make_task(id="T99.1", title="Orphan sub", depends_on=[])
    plan = _make_plan(tasks=[sub])
    rt = _roundtrip(plan).tasks[0]
    assert rt.id == "T99.1"
    assert rt.depends_on == []


def test_subtask_with_multiline_description_then_sibling():
    """Multiline field must terminate cleanly at the next header."""
    parent = _make_task(id="T03", title="Parent")
    sub1 = _make_task(id="T03.1", title="Sub one", description="alpha\n\nbeta")
    sub2 = _make_task(id="T03.2", title="Sub two", description="gamma")
    plan = _make_plan(tasks=[parent, sub1, sub2])
    rt = _roundtrip(plan)
    by_id = {t.id: t for t in rt.tasks}
    assert by_id["T03.1"].description == "alpha\n\nbeta"
    assert by_id["T03.2"].description == "gamma"


def test_roundtrip_crlf_input_is_tolerated():
    task = _make_task(description="one\ntwo")
    md = render_tasks_md(_make_plan(tasks=[task])).replace("\n", "\r\n")
    rt = parse_tasks_md(md).tasks[0]
    assert rt.description == "one\ntwo"


def test_roundtrip_ac_text_ending_with_done_word_pending():
    """An AC whose text literally ends in ':done' but is pending round-trips intact."""
    task = _make_task(acceptance_criteria=["mark task as :done"])
    rt = _roundtrip(_make_plan(tasks=[task])).tasks[0]
    assert rt.acceptance_criteria == ["mark task as :done"]
