from __future__ import annotations

from modes.manager.services import ManagerUIService
from modes.sdk.runtime.contracts import DevTask, ProjectPlan
from modes.sdk.planning import needs_failed_resume_choice, needs_resume_choice


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
    out = ManagerUIService.format_manager_status(plan, max_comment_chars=1000)
    assert "📋 План" in out
    assert "✅" in out  # approved
    assert "❌" in out  # rejected
    assert "зависит от: t1" in out
    assert "Замечания: Нужно поправить тесты" in out


def test_format_manager_status_brief_excludes_header_and_numbering() -> None:
    plan = ProjectPlan(
        project_goal="Очень длинная цель\n(в статусе не нужна)",
        tasks=[
            DevTask(
                id="t1",
                title="Сделать A",
                description="",
                acceptance_criteria=["ok"],
                depends_on=[],
                status="in_progress",
                attempt=1,
                max_attempts=3,
            ),
            DevTask(
                id="t2",
                title="Сделать B",
                description="",
                acceptance_criteria=["ok"],
                depends_on=["t1"],
                status="pending",
                attempt=0,
                max_attempts=3,
            ),
        ],
        analysis=None,
        status="active",
        created_at="2026-02-07 00:00:00",
        updated_at="2026-02-07 00:01:00",
        current_task_id="t1",
    )
    out = ManagerUIService.format_manager_status_brief(plan)
    assert "📋 План" not in out
    assert "План: " in out
    assert "\n1." in out
    assert "1. 🔧 Сделать A [in_progress]" in out
    assert "2. ⏳ Сделать B [pending]" in out


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
    plan.status = "paused"
    # Paused plans should be resumed explicitly: ask even if auto_resume=True.
    assert needs_resume_choice(plan, auto_resume=False, user_text="сделай это") is True
    assert needs_resume_choice(plan, auto_resume=True, user_text="сделай это") is True


def test_needs_failed_resume_choice_logic() -> None:
    from modes.sdk.runtime.contracts import DevTask

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

    assert ManagerUIService.task_progress(plan, t2) == (2, 3)


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

    assert ManagerUIService.task_progress(plan, detached_t2) == (2, 2)


def test_describe_failed_plan_reason_prefers_review_comments() -> None:
    plan = ProjectPlan(
        project_goal="Goal",
        tasks=[
            DevTask(
                id="t1",
                title="Retry me",
                description="",
                acceptance_criteria=["ok"],
                status="failed",
                attempt=2,
                max_attempts=3,
                review_comments="Упали интеграционные тесты",
            )
        ],
        analysis=None,
        status="failed",
    )

    reason = ManagerUIService.describe_failed_plan_reason(plan)
    assert "Retry me" in reason
    assert "Упали интеграционные тесты" in reason
