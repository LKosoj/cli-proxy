from __future__ import annotations

from modes.manager.services import PlanManagementService, ReviewAndMergeService
from modes.sdk.runtime.contracts import DevTask, ProjectAnalysis, ProjectPlan


def _make_plan(prefix: str) -> ProjectPlan:
    return ProjectPlan(
        project_goal=f"goal-{prefix}",
        analysis=ProjectAnalysis(
            current_state=f"state-{prefix}",
            already_done=[],
            remaining_work=[f"rem-{prefix}"],
            requirements=[f"REQ-{prefix}"],
        ),
        tasks=[
            DevTask(id=f"{prefix}_1", title="T1", description="D1", acceptance_criteria=["A1"], status="approved"),
            DevTask(id=f"{prefix}_2", title="T2", description="D2", acceptance_criteria=["A2"], status="pending"),
            DevTask(id=f"{prefix}_3", title="T3", description="D3", acceptance_criteria=["A3"], status="pending"),
        ],
    )


def test_review_service_diff_snapshot_and_audit_format(tmp_path) -> None:
    service = ReviewAndMergeService(plan_service=PlanManagementService())
    workdir = tmp_path / "intent_one"
    workdir.mkdir(parents=True, exist_ok=True)
    file_a = workdir / "a.txt"
    file_b = workdir / "b.txt"

    file_a.write_text("before", encoding="utf-8")
    before = service.snapshot_workdir(str(workdir))

    file_a.write_text("after", encoding="utf-8")
    file_b.write_text("new", encoding="utf-8")
    after = service.snapshot_workdir(str(workdir))
    diff = service.diff_snapshots(before, after)
    audit = service.format_change_audit(diff)

    assert diff["created"] == ["b.txt"]
    assert "a.txt" in diff["modified"]
    assert diff["deleted"] == []
    assert "Создано: 1 | Изменено: 1 | Удалено: 0" in audit


def test_review_service_apply_reconcile_payload_updates_plan() -> None:
    service = ReviewAndMergeService(plan_service=PlanManagementService())
    plan = _make_plan("x")

    payload = {
        "updated_analysis": {
            "current_state": "state-updated",
            "already_done": ["done-1"],
            "remaining_work": ["rem-1"],
            "requirements": ["REQ-1"],
            "checklist_table": [{"item": "REQ-1", "status": "done", "how": "ok", "why_not": ""}],
        },
        "completed_task_ids": ["x_2"],
        "adjustments": [
            {
                "task_id": "x_3",
                "reason": "partial done",
                "already_done_note": "implemented models",
                "updated_description": "refined description",
                "updated_acceptance_criteria": ["new criteria"],
            }
        ],
    }

    result = service.apply_reconcile_payload(plan, payload, now_iso=lambda: "2026-03-04 10:00:00")

    assert result["analysis_changed"] is True
    assert result["changes_made"] is True
    assert result["completed_ids"] == ["x_2"]
    assert result["adjusted_ids"] == ["x_3"]
    assert plan.analysis is not None
    assert plan.analysis.current_state == "state-updated"
    assert plan.analysis.already_done == ["done-1"]
    assert plan.analysis.checklist_table == [{"item": "REQ-1", "status": "done", "how": "ok", "why_not": ""}]

    by_id = {t.id: t for t in plan.tasks}
    assert by_id["x_2"].status == "approved"
    assert by_id["x_2"].completed_at == "2026-03-04 10:00:00"
    assert "Автоматически" in (by_id["x_2"].review_comments or "")
    assert by_id["x_3"].description == "refined description"
    assert by_id["x_3"].acceptance_criteria == ["new criteria"]
    assert by_id["x_3"].partial_work_note == "implemented models"


def test_review_service_sequential_runs_with_different_intents_are_isolated() -> None:
    service = ReviewAndMergeService(plan_service=PlanManagementService())

    plan_a = _make_plan("intent_a")
    payload_a = {
        "updated_analysis": {"current_state": "state-A"},
        "completed_task_ids": ["intent_a_2"],
        "adjustments": [],
    }
    result_a = service.apply_reconcile_payload(plan_a, payload_a, now_iso=lambda: "2026-03-04 10:00:00")

    plan_b = _make_plan("intent_b")
    payload_b = {
        "updated_analysis": {"current_state": "state-B"},
        "completed_task_ids": ["intent_b_2"],
        "adjustments": [
            {
                "task_id": "intent_b_3",
                "reason": "b-reason",
                "already_done_note": "b-note",
            }
        ],
    }
    result_b = service.apply_reconcile_payload(plan_b, payload_b, now_iso=lambda: "2026-03-05 11:00:00")

    assert result_a["completed_ids"] == ["intent_a_2"]
    assert result_b["completed_ids"] == ["intent_b_2"]
    assert plan_a.analysis is not None and plan_a.analysis.current_state == "state-A"
    assert plan_b.analysis is not None and plan_b.analysis.current_state == "state-B"

    by_id_b = {t.id: t for t in plan_b.tasks}
    assert by_id_b["intent_b_2"].completed_at == "2026-03-05 11:00:00"
    assert by_id_b["intent_b_3"].partial_work_note == "b-note"
    assert by_id_b["intent_b_3"].partial_work_note != "implemented models"
