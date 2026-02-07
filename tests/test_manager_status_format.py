from __future__ import annotations

from agent.contracts import DevTask, ProjectPlan
from agent.manager import format_manager_status, needs_resume_choice


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
