import os
import json
import asyncio

from config import load_config
from agent.manager import ManagerOrchestrator
from modes.sdk.runtime.contracts import DevTask, ProjectAnalysis, ProjectPlan
from modes.sdk.planning import load_plan


def test_reconcile_marks_completed_and_applies_adjustments(tmp_path, monkeypatch):
    cfg = load_config(os.path.join(os.path.dirname(__file__), "..", "config_example.yaml"))
    cfg.defaults.workdir = str(tmp_path)
    cfg.defaults.state_path = str(tmp_path / "state.json")
    cfg.defaults.manager_response_archive = False

    orch = ManagerOrchestrator(cfg)

    async def fake_run_git(workdir: str, args: list[str]):
        # Return something realistic; format is validated elsewhere.
        if args[:2] == ["log", "-1"]:
            return 0, "Commit msg (abc123)\n file1.py | 1 +\n 1 file changed, 1 insertion(+)"
        return 0, ""

    payload = {
        "updated_analysis": {
            "current_state": "Project with User model implemented",
            "already_done": ["User model"],
            "remaining_work": ["Add API"],
        },
        "completed_task_ids": ["task_2"],
        "adjustments": [
            {
                "task_id": "task_3",
                "reason": "Partially done",
                "already_done_note": "Models created",
                "updated_description": "Finish the remaining API bits",
                "updated_acceptance_criteria": ["Endpoint returns 200"],
            }
        ],
        "summary": "Adjusted plan",
    }

    async def fake_chat_completion(*_a, **_kw):
        return json.dumps(payload, ensure_ascii=False)

    monkeypatch.setattr(ManagerOrchestrator, "_run_git", staticmethod(fake_run_git))
    monkeypatch.setattr("agent.manager_core.chat_completion", fake_chat_completion)

    # Task 1 is the just-completed task that triggered reconcile.
    plan = ProjectPlan(
        project_goal="goal",
        analysis=ProjectAnalysis(current_state="Empty project", already_done=[], remaining_work=["X"]),
        tasks=[
            DevTask(id="task_1", title="T1", description="D1", acceptance_criteria=["A1"], depends_on=[], status="approved"),
            DevTask(id="task_2", title="T2", description="D2", acceptance_criteria=["A2"], depends_on=[], status="pending"),
            DevTask(id="task_3", title="T3", description="D3", acceptance_criteria=["A3"], depends_on=[], status="pending"),
        ],
    )

    session = type("S", (), {"workdir": str(tmp_path)})()
    done_task = plan.tasks[0]

    asyncio.run(orch._reconcile_plan_after_commit(session, done_task, plan, bot=None, context=None, dest={"kind": "telegram"}))

    # Persisted changes should be visible in the saved plan.
    saved = load_plan(str(tmp_path))
    assert saved is not None
    assert saved.analysis is not None
    assert saved.analysis.current_state == "Project with User model implemented"
    assert saved.analysis.already_done == ["User model"]
    assert saved.analysis.remaining_work == ["Add API"]

    by_id = {t.id: t for t in saved.tasks}

    # Completed task should be auto-approved.
    t2 = by_id["task_2"]
    assert t2.status == "approved"
    assert t2.completed_at is not None
    assert t2.review_verdict == "approved"
    assert "Автоматически" in (t2.review_comments or "")

    # Adjustment should update description, criteria, and accumulate partial_work_note.
    t3 = by_id["task_3"]
    assert t3.description == "Finish the remaining API bits"
    assert t3.acceptance_criteria == ["Endpoint returns 200"]
    assert t3.partial_work_note == "Models created"
