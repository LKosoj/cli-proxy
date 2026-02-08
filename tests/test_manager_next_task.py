"""Tests for _next_ready_task logic: dependencies, normalization, cascade blocking."""

from __future__ import annotations

from agent.contracts import DevTask, ProjectPlan
from agent.manager import ManagerOrchestrator


def _make_orchestrator():
    """Create a minimal ManagerOrchestrator for testing _next_ready_task."""
    obj = object.__new__(ManagerOrchestrator)
    obj._config = type("C", (), {"defaults": type("D", (), {
        "manager_max_tasks": 10,
        "manager_max_attempts": 3,
    })()})()
    return obj


def _task(id: str, status: str = "pending", depends_on=None, attempt: int = 0, max_attempts: int = 3) -> DevTask:
    return DevTask(
        id=id,
        title=f"Task {id}",
        description="desc",
        acceptance_criteria=["ok"],
        depends_on=depends_on or [],
        status=status,
        attempt=attempt,
        max_attempts=max_attempts,
    )


def _plan(tasks) -> ProjectPlan:
    return ProjectPlan(project_goal="goal", tasks=tasks, status="active")


class TestNextReadyTask:

    def test_simple_pending(self):
        orch = _make_orchestrator()
        plan = _plan([_task("t1"), _task("t2")])
        task = orch._next_ready_task(plan)
        assert task is not None
        assert task.id == "t1"

    def test_skip_approved(self):
        orch = _make_orchestrator()
        plan = _plan([_task("t1", status="approved"), _task("t2")])
        task = orch._next_ready_task(plan)
        assert task is not None
        assert task.id == "t2"

    def test_dependency_not_ready(self):
        orch = _make_orchestrator()
        plan = _plan([_task("t1"), _task("t2", depends_on=["t1"])])
        task = orch._next_ready_task(plan)
        assert task is not None
        assert task.id == "t1"

    def test_dependency_approved(self):
        orch = _make_orchestrator()
        plan = _plan([
            _task("t1", status="approved"),
            _task("t2", depends_on=["t1"]),
        ])
        task = orch._next_ready_task(plan)
        assert task is not None
        assert task.id == "t2"

    def test_cascade_block_on_failed_dep(self):
        orch = _make_orchestrator()
        plan = _plan([
            _task("t1", status="failed", attempt=3, max_attempts=3),
            _task("t2", depends_on=["t1"]),
        ])
        task = orch._next_ready_task(plan)
        assert task is None
        # t2 should be marked as blocked
        assert plan.tasks[1].status == "blocked"

    def test_normalize_rejected_to_pending(self):
        orch = _make_orchestrator()
        plan = _plan([_task("t1", status="rejected", attempt=1)])
        task = orch._next_ready_task(plan)
        assert task is not None
        assert task.id == "t1"
        assert task.status == "pending"

    def test_keep_in_progress_stage_on_resume(self):
        """After restart, in_progress should stay in_progress (resume from development)."""
        orch = _make_orchestrator()
        plan = _plan([_task("t1", status="in_progress", attempt=1)])
        task = orch._next_ready_task(plan)
        assert task is not None
        assert task.id == "t1"
        assert task.status == "in_progress"

    def test_keep_in_review_stage_on_resume(self):
        """After restart, in_review should stay in_review (resume from review)."""
        orch = _make_orchestrator()
        plan = _plan([_task("t1", status="in_review", attempt=2)])
        task = orch._next_ready_task(plan)
        assert task is not None
        assert task.id == "t1"
        assert task.status == "in_review"

    def test_max_attempts_exceeded_becomes_failed(self):
        orch = _make_orchestrator()
        plan = _plan([_task("t1", status="rejected", attempt=3, max_attempts=3)])
        task = orch._next_ready_task(plan)
        assert task is None
        assert plan.tasks[0].status == "failed"

    def test_in_progress_max_attempts_becomes_failed(self):
        orch = _make_orchestrator()
        plan = _plan([_task("t1", status="in_progress", attempt=3, max_attempts=3)])
        task = orch._next_ready_task(plan)
        assert task is None
        assert plan.tasks[0].status == "failed"

    def test_in_review_max_attempts_becomes_failed(self):
        orch = _make_orchestrator()
        plan = _plan([_task("t1", status="in_review", attempt=3, max_attempts=3)])
        task = orch._next_ready_task(plan)
        assert task is None
        assert plan.tasks[0].status == "failed"

    def test_failed_with_attempts_left_becomes_pending(self):
        orch = _make_orchestrator()
        plan = _plan([_task("t1", status="failed", attempt=1, max_attempts=3)])
        task = orch._next_ready_task(plan)
        assert task is not None
        assert task.id == "t1"
        assert task.status == "pending"

    def test_all_approved_returns_none(self):
        orch = _make_orchestrator()
        plan = _plan([_task("t1", status="approved"), _task("t2", status="approved")])
        task = orch._next_ready_task(plan)
        assert task is None

    def test_mixed_statuses(self):
        orch = _make_orchestrator()
        plan = _plan([
            _task("t1", status="approved"),
            _task("t2", status="failed", attempt=3, max_attempts=3),
            _task("t3", depends_on=["t1"]),     # ready
            _task("t4", depends_on=["t2"]),     # will be blocked on next call
        ])
        task = orch._next_ready_task(plan)
        assert task is not None
        assert task.id == "t3"

        # After t3 is done, next call processes t4 and blocks it
        plan.tasks[2].status = "approved"
        task = orch._next_ready_task(plan)
        assert task is None
        assert plan.tasks[3].status == "blocked"  # t4 blocked by t2

    def test_is_plan_blocked_all_done(self):
        orch = _make_orchestrator()
        plan = _plan([_task("t1", status="approved"), _task("t2", status="failed")])
        assert orch._is_plan_blocked(plan) is True

    def test_is_plan_blocked_has_pending(self):
        orch = _make_orchestrator()
        plan = _plan([_task("t1", status="approved"), _task("t2", status="pending")])
        assert orch._is_plan_blocked(plan) is False
