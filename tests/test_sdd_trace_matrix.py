from __future__ import annotations

from modes.sdk.runtime.contracts import DevTask, ProjectPlan
from modes.sdd.phases import (
    _extract_files_from_audit,
    _is_test_file,
    parse_spec_requirements,
    render_trace_md,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_task(**kwargs) -> DevTask:
    defaults = dict(
        id="T01",
        title="Sample task",
        description="Do something",
        acceptance_criteria=[],
        covers_requirements=["REQ-1"],
        depends_on=[],
        status="pending",
    )
    defaults.update(kwargs)
    return DevTask(**defaults)


def _make_plan(*tasks: DevTask, goal: str = "Test project") -> ProjectPlan:
    return ProjectPlan(project_goal=goal, tasks=list(tasks))


# ---------------------------------------------------------------------------
# 1. All REQs appear, including uncovered one
# ---------------------------------------------------------------------------

def test_all_reqs_present_including_uncovered():
    reqs = [
        {"id": "REQ-1", "text": "Login via OAuth"},
        {"id": "REQ-2", "text": "Dashboard view"},
        {"id": "REQ-3", "text": "Export to CSV"},
    ]
    task = _make_task(id="T01", covers_requirements=["REQ-1", "REQ-2"])
    plan = _make_plan(task)
    out = render_trace_md(plan, reqs)
    assert "REQ-1" in out
    assert "REQ-2" in out
    assert "REQ-3" in out
    assert "(не покрыто)" in out
    # REQ-3 row must have не покрыто, REQ-1/2 must not
    lines = [ln for ln in out.splitlines() if "REQ-3" in ln]
    assert lines, "REQ-3 row missing"
    assert "(не покрыто)" in lines[0]


# ---------------------------------------------------------------------------
# 2. Files extracted from manager_change_audit; tests go to tests column
# ---------------------------------------------------------------------------

def test_files_and_tests_from_audit():
    audit = "M  modes/sdd/phases.py\nA  tests/test_sdd.py"
    task = _make_task(
        id="T02",
        covers_requirements=["REQ-1"],
        manager_change_audit=audit,
    )
    plan = _make_plan(task)
    reqs = [{"id": "REQ-1", "text": "Feature"}]
    out = render_trace_md(plan, reqs)
    assert "modes/sdd/phases.py" in out
    assert "tests/test_sdd.py" in out
    # test file must appear in tests column (last column), not files column
    rows = [ln for ln in out.splitlines() if "REQ-1" in ln and "T02" in ln]
    assert rows, "REQ-1/T02 row missing"
    row = rows[0]
    cols = row.split("|")
    # cols[0]=empty, [1]=REQ, [2]=text, [3]=task, [4]=status, [5]=files, [6]=tests, [7]=empty
    files_col = cols[5] if len(cols) > 5 else ""
    tests_col = cols[6] if len(cols) > 6 else ""
    assert "tests/test_sdd.py" in tests_col
    assert "modes/sdd/phases.py" in files_col


# ---------------------------------------------------------------------------
# 3. Failed and blocked statuses shown verbatim
# ---------------------------------------------------------------------------

def test_failed_and_blocked_shown_verbatim():
    reqs = [{"id": "REQ-1", "text": "Auth"}, {"id": "REQ-2", "text": "Logs"}]
    t_failed = _make_task(id="T01", covers_requirements=["REQ-1"], status="failed")
    t_blocked = _make_task(id="T02", covers_requirements=["REQ-2"], status="blocked")
    plan = _make_plan(t_failed, t_blocked)
    out = render_trace_md(plan, reqs)
    assert "failed" in out
    assert "blocked" in out
    assert "approved" not in out


# ---------------------------------------------------------------------------
# 4. Empty plan does not crash; returns string with header
# ---------------------------------------------------------------------------

def test_empty_plan_does_not_crash():
    plan = ProjectPlan(project_goal="", tasks=[])
    out = render_trace_md(plan, [])
    assert isinstance(out, str)
    assert "Трассируемость" in out


# ---------------------------------------------------------------------------
# 5. parse_spec_requirements
# ---------------------------------------------------------------------------

_SAMPLE_SPEC = """
# Specification: my-feature

**Intent:** Build something cool

## User Stories

- As a user I want to login

## Requirements

- **REQ-1**: User can log in with email
- **REQ-2**: Password is hashed at rest

## Acceptance Criteria

- REQ-1: WHEN user submits credentials THEN system authenticates
"""


def test_parse_spec_requirements_finds_reqs():
    reqs = parse_spec_requirements(_SAMPLE_SPEC)
    assert len(reqs) == 2
    assert reqs[0] == {"id": "REQ-1", "text": "User can log in with email"}
    assert reqs[1] == {"id": "REQ-2", "text": "Password is hashed at rest"}


def test_parse_spec_requirements_no_section():
    reqs = parse_spec_requirements("# Just a doc\n\nNo requirements here.")
    assert reqs == []


def test_parse_spec_requirements_empty_string():
    assert parse_spec_requirements("") == []


def test_parse_spec_requirements_stops_at_next_section():
    spec = "## Requirements\n\n- **REQ-1**: First\n\n## Acceptance Criteria\n\n- **REQ-2**: Not a req\n"
    reqs = parse_spec_requirements(spec)
    assert len(reqs) == 1
    assert reqs[0]["id"] == "REQ-1"


# ---------------------------------------------------------------------------
# 6. One REQ → multiple tasks: each task on separate row
# ---------------------------------------------------------------------------

def test_one_req_multiple_tasks_separate_rows():
    reqs = [{"id": "REQ-1", "text": "Big feature"}]
    t1 = _make_task(id="T01", title="Implement backend", covers_requirements=["REQ-1"])
    t2 = _make_task(id="T02", title="Write tests", covers_requirements=["REQ-1"])
    plan = _make_plan(t1, t2)
    out = render_trace_md(plan, reqs)
    rows = [ln for ln in out.splitlines() if "REQ-1" in ln and "|" in ln]
    # Both tasks should appear as separate rows
    assert len(rows) == 2
    task_ids_in_rows = " ".join(rows)
    assert "T01" in task_ids_in_rows
    assert "T02" in task_ids_in_rows


# ---------------------------------------------------------------------------
# 7. Test-classification: "test" substring inside path components is NOT a test
# ---------------------------------------------------------------------------

def test_is_test_file_no_false_positive_on_substring():
    # Реальные тестовые пути
    assert _is_test_file("tests/test_sdd.py")
    assert _is_test_file("test_foo.py")
    assert _is_test_file("a/b_test.py")
    assert _is_test_file("src/test_utils/helper.py")
    # НЕ тесты — "test" лишь как подстрока внутри слова
    assert not _is_test_file("latest/config.py")
    assert not _is_test_file("protest/foo.py")
    assert not _is_test_file("contest_results.py")
    assert not _is_test_file("modes/sdd/phases.py")


def test_latest_file_goes_to_files_column_not_tests():
    audit = "M  latest/config.py"
    task = _make_task(id="T03", covers_requirements=["REQ-1"], manager_change_audit=audit)
    plan = _make_plan(task)
    out = render_trace_md(plan, [{"id": "REQ-1", "text": "Feature"}])
    row = [ln for ln in out.splitlines() if "T03" in ln][0]
    cols = row.split("|")
    files_col, tests_col = cols[5], cols[6]
    assert "latest/config.py" in files_col
    assert "latest/config.py" not in tests_col


# ---------------------------------------------------------------------------
# 8. git porcelain rename "old -> new" → new path captured, not garbage
# ---------------------------------------------------------------------------

def test_extract_files_handles_rename():
    files = _extract_files_from_audit("R  old/path.py -> new/path.py")
    assert files == ["new/path.py"]


# ---------------------------------------------------------------------------
# 9. Custom status with table-breaking chars is escaped (not raw pipe/newline)
# ---------------------------------------------------------------------------

def test_status_with_pipe_is_escaped():
    task = _make_task(id="T04", covers_requirements=["REQ-1"], status="weird|status")
    plan = _make_plan(task)
    out = render_trace_md(plan, [{"id": "REQ-1", "text": "Feature"}])
    row = [ln for ln in out.splitlines() if "T04" in ln][0]
    # Pipe внутри статуса экранирован — ячейки таблицы не разъезжаются
    assert r"weird\|status" in row
