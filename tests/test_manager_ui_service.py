from __future__ import annotations

from modes.manager.services import ManagerUIService
from modes.sdk.runtime.contracts import DevTask, ProjectPlan


def test_ui_service_formats_status_and_brief_messages() -> None:
    plan = ProjectPlan(
        project_goal="Сделать X",
        tasks=[
            DevTask(
                id="t1",
                title="Сделать A",
                description="",
                acceptance_criteria=["ok"],
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
        status="active",
        created_at="2026-03-05 10:00:00",
        updated_at="2026-03-05 10:10:00",
        current_task_id="t2",
    )

    full = ManagerUIService.format_manager_status(plan, max_comment_chars=1000)
    brief = ManagerUIService.format_manager_status_brief(plan, max_comment_chars=1000)
    notify = ManagerUIService.format_plan_notification(plan)

    assert "📋 План: «Сделать X»" in full
    assert "✅" in full
    assert "❌" in full
    assert "зависит от: t1" in full
    assert "Замечания: Нужно поправить тесты" in full
    assert "📋 План" not in brief
    assert "План: 1/2 задач выполнено. Статус: active." in brief
    assert notify.startswith("📋 План: Сделать X")
    assert "2. Сделать B [rejected] (depends_on: t1)" in notify


def test_ui_service_sequential_runs_with_different_intents_are_isolated() -> None:
    service = ManagerUIService()

    plan_a = ProjectPlan(
        project_goal="intent-a",
        tasks=[DevTask(id="a1", title="A1", description="", acceptance_criteria=["ok"], status="pending")],
        status="active",
    )
    plan_b = ProjectPlan(
        project_goal="intent-b",
        tasks=[
            DevTask(id="b1", title="B1", description="", acceptance_criteria=["ok"], status="approved"),
            DevTask(id="b2", title="B2", description="", acceptance_criteria=["ok"], status="pending"),
        ],
        status="paused",
    )

    text_a = service.format_plan_notification(plan_a)
    text_b = service.format_plan_notification(plan_b)
    status_a = service.plan_summary(plan_a)
    status_b = service.plan_summary(plan_b)

    assert "intent-a" in text_a
    assert "intent-b" not in text_a
    assert "intent-b" in text_b
    assert "intent-a" not in text_b
    assert status_a == "План: 0/1 задач выполнено. Статус: active."
    assert status_b == "План: 1/2 задач выполнено. Статус: paused."
