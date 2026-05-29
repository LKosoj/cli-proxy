from __future__ import annotations

from modes.manager.services import PlanManagementService


def test_plan_service_builds_plan_and_serializes() -> None:
    service = PlanManagementService(max_attempts=7)
    payload = {
        "project_goal": " Ship feature ",
        "project_analysis": {
            "current_state": " draft ",
            "already_done": ["setup", " "],
            "remaining_work": ["impl"],
            "requirements": ["REQ-1", " "],
            "checklist_table": [{"item": "REQ-1", "status": "done", "how": "ok", "why_not": ""}],
        },
        "tasks": [
            {
                "id": "  ",
                "title": "  Prepare  ",
                "description": "  Init  ",
                "acceptance_criteria": ["done", ""],
                "covers_requirements": ["REQ-1", ""],
                "depends_on": ["", "task_1", "task_1"],
            },
            {"id": "task_2", "title": "Implement", "description": "Do work", "acceptance_criteria": ["ok"]},
            {"id": "task_3", "title": "Extra", "description": "Ignored", "acceptance_criteria": ["ok"]},
        ],
    }

    plan = service.plan_from_payload(payload, user_goal="fallback", max_tasks=2)
    assert plan is not None
    assert plan.project_goal == "Ship feature"
    assert len(plan.tasks) == 2
    assert plan.tasks[0].id == "task_1"
    assert plan.tasks[0].title == "Prepare"
    assert plan.tasks[0].description == "Init"
    assert plan.tasks[0].depends_on == ["task_1"]
    assert plan.tasks[0].max_attempts == 7
    assert int(getattr(plan, "_manager_max_tasks_limit", 0)) == 2

    analysis = service.serialize_analysis(plan.analysis)
    assert analysis == {
        "current_state": " draft ",
        "already_done": ["setup"],
        "remaining_work": ["impl"],
        "requirements": ["REQ-1"],
        "checklist_table": [{"item": "REQ-1", "status": "done", "how": "ok", "why_not": ""}],
    }

    serialized_plan = service.serialize_plan(plan)
    assert serialized_plan["manager_max_tasks_limit"] == 2
    assert len(serialized_plan["tasks"]) == 2


def test_plan_service_update_plan_analysis_changes_and_idempotency() -> None:
    service = PlanManagementService()
    plan = service.create_plan(project_goal="Goal")

    payload = {
        "current_state": "new state",
        "already_done": ["one", " "],
        "remaining_work": ["two"],
        "requirements": ["REQ-2"],
        "checklist_table": [{"item": "REQ-2", "status": "not_done", "how": "", "why_not": "missing"}],
    }

    assert service.update_plan_analysis(plan, payload) is True
    assert plan.analysis is not None
    assert plan.analysis.current_state == "new state"
    assert plan.analysis.already_done == ["one"]
    assert plan.analysis.remaining_work == ["two"]
    assert plan.analysis.requirements == ["REQ-2"]
    assert plan.analysis.checklist_table == [
        {"item": "REQ-2", "status": "not_done", "how": "", "why_not": "missing"}
    ]

    assert service.update_plan_analysis(plan, payload) is False


def test_plan_service_merge_analysis_context_keeps_limits_and_copies_analysis() -> None:
    service = PlanManagementService()
    source = service.create_plan(
        project_goal="source",
        analysis=service.analysis_from_payload(
            {
                "project_analysis": {
                    "current_state": "s",
                    "already_done": ["a"],
                    "remaining_work": ["b"],
                    "requirements": ["REQ-1"],
                    "checklist_table": [{"item": "REQ-1", "status": "done", "how": "ok", "why_not": ""}],
                }
            }
        ),
        max_tasks_limit=10,
    )
    setattr(source, "_manager_min_tasks_dynamic", 8)
    target = service.create_plan(project_goal="target")

    merged = service.merge_analysis_context(source, target)
    assert merged is target
    assert int(getattr(target, "_manager_max_tasks_limit", 0)) == 10
    assert int(getattr(target, "_manager_min_tasks_dynamic", 0)) == 8
    assert target.analysis is not None
    assert target.analysis.current_state == "s"
    assert target.analysis.checklist_table == [
        {"item": "REQ-1", "status": "done", "how": "ok", "why_not": ""}
    ]

    # Deep-copy behavior for checklist rows.
    source.analysis.checklist_table[0]["status"] = "not_done"  # type: ignore[index]
    assert target.analysis.checklist_table[0]["status"] == "done"


def test_plan_service_sequential_runs_with_different_intents_do_not_leak_state() -> None:
    service = PlanManagementService(max_attempts=5)

    first_payload = {
        "tasks": [
            {
                "id": "task_1",
                "title": "First intent task",
                "description": "Do first intent",
                "acceptance_criteria": ["first-ok"],
            }
        ],
        "project_analysis": {
            "current_state": "first-state",
            "already_done": ["first-done"],
            "remaining_work": ["first-remaining"],
            "requirements": ["REQ-1: first-req"],
            "checklist_table": [{"item": "REQ-1", "status": "not_done", "how": "", "why_not": "first"}],
        },
    }
    second_payload = {
        "tasks": [
            {
                "id": "task_10",
                "title": "Second intent task",
                "description": "Do second intent",
                "acceptance_criteria": ["second-ok"],
            }
        ],
        "project_analysis": {
            "current_state": "second-state",
            "already_done": ["second-done"],
            "remaining_work": ["second-remaining"],
            "requirements": ["REQ-2: second-req"],
            "checklist_table": [{"item": "REQ-2", "status": "done", "how": "second-how", "why_not": ""}],
        },
    }

    first_plan = service.plan_from_payload(first_payload, user_goal="intent-first", max_tasks=3)
    second_plan = service.plan_from_payload(second_payload, user_goal="intent-second", max_tasks=2)

    assert first_plan is not None
    assert second_plan is not None
    assert first_plan.project_goal == "intent-first"
    assert second_plan.project_goal == "intent-second"
    assert int(getattr(first_plan, "_manager_max_tasks_limit", 0)) == 3
    assert int(getattr(second_plan, "_manager_max_tasks_limit", 0)) == 2
    assert first_plan.tasks[0].id == "task_1"
    assert second_plan.tasks[0].id == "task_10"
    assert first_plan.analysis is not None
    assert second_plan.analysis is not None
    assert first_plan.analysis.current_state == "first-state"
    assert second_plan.analysis.current_state == "second-state"
    assert first_plan.analysis.requirements == ["REQ-1: first-req"]
    assert second_plan.analysis.requirements == ["REQ-2: second-req"]

    # Mutating the first result must not affect the second one.
    first_plan.analysis.checklist_table[0]["status"] = "mutated"  # type: ignore[index]
    first_plan.tasks[0].title = "mutated-first-title"
    assert second_plan.analysis.checklist_table[0]["status"] == "done"
    assert second_plan.tasks[0].title == "Second intent task"


def test_plan_service_mark_task_completed_and_get_next_pending_task() -> None:
    service = PlanManagementService(max_attempts=3)
    plan = service.create_plan(project_goal="goal")
    service.add_task(
        plan,
        {
            "id": "task_1",
            "title": "First",
            "description": "d1",
            "acceptance_criteria": ["ok"],
        },
        fallback_index=1,
    )
    service.add_task(
        plan,
        {
            "id": "task_2",
            "title": "Second",
            "description": "d2",
            "acceptance_criteria": ["ok"],
            "depends_on": ["task_1"],
        },
        fallback_index=2,
    )

    next_task = service.get_next_pending_task(plan)
    assert next_task is not None
    assert next_task.id == "task_1"

    assert service.mark_task_completed(plan, "task_1", now_iso=lambda: "2026-03-05 00:00:00") is True
    assert plan.tasks[0].status == "approved"
    assert plan.tasks[0].completed_at == "2026-03-05 00:00:00"
    assert plan.tasks[0].review_verdict == "approved"

    next_task = service.get_next_pending_task(plan)
    assert next_task is not None
    assert next_task.id == "task_2"


def test_plan_service_update_analysis_alias() -> None:
    service = PlanManagementService()
    plan = service.create_plan(project_goal="goal")

    changed = service.update_analysis(
        plan,
        {
            "current_state": "state",
            "already_done": ["done"],
            "remaining_work": ["todo"],
            "requirements": ["REQ-1"],
        },
    )
    assert changed is True
    assert plan.analysis is not None
    assert plan.analysis.current_state == "state"
