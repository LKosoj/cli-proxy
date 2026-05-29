from __future__ import annotations

import asyncio
import json
import os

from agent.manager import ManagerOrchestrator
from config import load_config
from modes.sdk.planning import load_plan
from modes.sdk.runtime.contracts import DevTask, ProjectAnalysis, ProjectPlan


def _make_config(tmp_path):
    cfg = load_config(os.path.join(os.path.dirname(__file__), "..", "config_example.yaml"))
    cfg.defaults.workdir = str(tmp_path)
    cfg.defaults.state_path = str(tmp_path / "state.json")
    cfg.defaults.manager_response_archive = False
    return cfg


def _make_plan() -> ProjectPlan:
    return ProjectPlan(
        project_goal="goal",
        analysis=ProjectAnalysis(current_state="initial", already_done=[], remaining_work=["x"]),
        tasks=[
            DevTask(id="task_1", title="T1", description="D1", acceptance_criteria=["A1"], status="approved"),
            DevTask(id="task_2", title="T2", description="D2", acceptance_criteria=["A2"], status="pending"),
        ],
    )


def test_reconcile_after_commit_uses_review_service_apply(tmp_path, monkeypatch) -> None:
    orch = ManagerOrchestrator(_make_config(tmp_path))
    plan = _make_plan()
    done_task = plan.tasks[0]

    calls = {"apply": 0}
    base_service = orch._review_service

    class _SpyService:
        def remaining_tasks_info(self, plan_obj):
            return base_service.remaining_tasks_info(plan_obj)

        def apply_reconcile_payload(self, plan_obj, payload, *, now_iso=None):
            calls["apply"] += 1
            return base_service.apply_reconcile_payload(plan_obj, payload, now_iso=now_iso)

    orch._review_service = _SpyService()  # type: ignore[assignment]

    async def fake_run_git(_workdir: str, _args: list[str]):
        return 0, "commit (abc123)"

    async def fake_chat_completion(*_args, **_kwargs):
        return json.dumps(
            {
                "updated_analysis": {"current_state": "after-commit"},
                "completed_task_ids": ["task_2"],
                "adjustments": [],
                "summary": "ok",
            },
            ensure_ascii=False,
        )

    monkeypatch.setattr(ManagerOrchestrator, "_run_git", staticmethod(fake_run_git))
    monkeypatch.setattr("agent.manager_core.chat_completion", fake_chat_completion)

    session = type("S", (), {"workdir": str(tmp_path)})()
    asyncio.run(
        orch._reconcile_plan_after_commit(
            session,
            done_task,
            plan,
            bot=None,
            context=None,
            dest={"kind": "telegram"},
        )
    )

    assert calls["apply"] == 1
    saved = load_plan(str(tmp_path))
    assert saved is not None
    by_id = {t.id: t for t in saved.tasks}
    assert by_id["task_2"].status == "approved"


def test_reconcile_after_change_audit_uses_review_service_apply(tmp_path, monkeypatch) -> None:
    orch = ManagerOrchestrator(_make_config(tmp_path))
    plan = _make_plan()
    done_task = plan.tasks[0]
    done_task.manager_change_audit = "changed files"

    calls = {"apply": 0}
    base_service = orch._review_service

    class _SpyService:
        def remaining_tasks_info(self, plan_obj):
            return base_service.remaining_tasks_info(plan_obj)

        def apply_reconcile_payload(self, plan_obj, payload, *, now_iso=None):
            calls["apply"] += 1
            return base_service.apply_reconcile_payload(plan_obj, payload, now_iso=now_iso)

    orch._review_service = _SpyService()  # type: ignore[assignment]

    async def fake_chat_completion(*_args, **_kwargs):
        return json.dumps(
            {
                "updated_analysis": {"current_state": "after-audit"},
                "completed_task_ids": [],
                "adjustments": [{"task_id": "task_2", "already_done_note": "progress"}],
                "summary": "ok",
            },
            ensure_ascii=False,
        )

    monkeypatch.setattr("agent.manager_core.chat_completion", fake_chat_completion)

    session = type("S", (), {"workdir": str(tmp_path)})()
    asyncio.run(
        orch._reconcile_plan_after_change_audit(
            session,
            done_task,
            plan,
            bot=None,
            context=None,
            dest={"kind": "telegram"},
        )
    )

    assert calls["apply"] == 1
    saved = load_plan(str(tmp_path))
    assert saved is not None
    by_id = {t.id: t for t in saved.tasks}
    assert by_id["task_2"].partial_work_note == "progress"
