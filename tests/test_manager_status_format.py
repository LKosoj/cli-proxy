from __future__ import annotations

from agent.contracts import DevTask, ProjectPlan
from agent.manager import (
    _task_progress,
    format_manager_status,
    needs_failed_resume_choice,
    needs_resume_choice,
)


def test_format_manager_status_includes_emojis_and_depends_and_comments() -> None:
    plan = ProjectPlan(
        project_goal="Сделать X",
        tasks=[
            DevTask(
                id="t1",
                title="Сделать A",
                description="",
                acceptance_criteria=["ok"],
                depends_on=[],
                status="approved",
                attempt=1,
                max_attempts=3,
            ),
            DevTask(
                id="t2",
                title="Сделать B",
                description="",
                acceptance_criteria=["ok"],
                depends_on=["t1"],
                status="rejected",
                attempt=2,
                max_attempts=3,
                review_comments="Нужно поправить тесты",
            ),
        ],
        analysis=None,
        status="active",
        created_at="2026-02-07 00:00:00",
        updated_at="2026-02-07 00:01:00",
        current_task_id="t2",
    )
    out = format_manager_status(plan, max_comment_chars=1000)
    assert "📋 План" in out
    assert "✅" in out  # approved
    assert "❌" in out  # rejected
    assert "зависит от: t1" in out
    assert "Замечания: Нужно поправить тесты" in out


def test_needs_resume_choice_logic() -> None:
    plan = ProjectPlan(
        project_goal="Goal",
        tasks=[],
        analysis=None,
        status="active",
        created_at=None,
        updated_at=None,
        current_task_id=None,
    )
    assert needs_resume_choice(plan, auto_resume=False, user_text="сделай это") is True
    assert needs_resume_choice(plan, auto_resume=True, user_text="сделай это") is False
    assert needs_resume_choice(plan, auto_resume=False, user_text="  ") is False


def test_needs_failed_resume_choice_logic() -> None:
    from agent.contracts import DevTask

    plan = ProjectPlan(
        project_goal="Goal",
        tasks=[
            DevTask(
                id="t1",
                title="Retry me",
                description="",
                acceptance_criteria=["ok"],
                status="failed",
                attempt=1,
                max_attempts=3,
            )
        ],
        analysis=None,
        status="failed",
        created_at=None,
        updated_at=None,
        current_task_id=None,
    )
    assert needs_failed_resume_choice(plan, auto_resume=False, user_text="сделай это") is True
    assert needs_failed_resume_choice(plan, auto_resume=True, user_text="сделай это") is False
    assert needs_failed_resume_choice(plan, auto_resume=False, user_text="  ") is False


def test_task_progress_returns_position_and_total() -> None:
    t1 = DevTask(id="t1", title="A", description="", acceptance_criteria=["ok"])
    t2 = DevTask(id="t2", title="B", description="", acceptance_criteria=["ok"])
    t3 = DevTask(id="t3", title="C", description="", acceptance_criteria=["ok"])
    plan = ProjectPlan(project_goal="Goal", tasks=[t1, t2, t3], status="active")

    assert _task_progress(plan, t2) == (2, 3)


def test_task_progress_falls_back_to_task_id_match() -> None:
    plan = ProjectPlan(
        project_goal="Goal",
        tasks=[
            DevTask(id="t1", title="A", description="", acceptance_criteria=["ok"]),
            DevTask(id="t2", title="B", description="", acceptance_criteria=["ok"]),
        ],
        status="active",
    )
    detached_t2 = DevTask(id="t2", title="B copy", description="", acceptance_criteria=["ok"])

    assert _task_progress(plan, detached_t2) == (2, 2)
