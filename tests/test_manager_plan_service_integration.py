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


def _analysis(seed: str = "seed") -> ProjectAnalysis:
    return ProjectAnalysis(
        current_state=f"{seed}-state",
        already_done=[f"{seed}-done"],
        remaining_work=[f"{seed}-remaining"],
        requirements=[f"REQ-1: {seed}-requirement"],
        checklist_table=[
            {"item": "REQ-1", "status": "not_done", "how": "", "why_not": f"{seed}-why-not"},
        ],
    )


def _plan_with_two_tasks(analysis: ProjectAnalysis) -> ProjectPlan:
    return ProjectPlan(
        project_goal="goal",
        analysis=analysis,
        tasks=[
            DevTask(
                id="task_1",
                title="Done task",
                description="d1",
                acceptance_criteria=["a1"],
                depends_on=[],
                status="approved",
            ),
            DevTask(
                id="task_2",
                title="Pending task",
                description="d2",
                acceptance_criteria=["a2"],
                depends_on=[],
                status="pending",
            ),
        ],
    )


def test_fix_plan_via_cli_serializes_full_analysis_payload(tmp_path, monkeypatch):
    cfg = _make_config(tmp_path)
    orch = ManagerOrchestrator(cfg)
    plan = _plan_with_two_tasks(_analysis("initial"))

    captured = {"prompt": None, "serialize_calls": 0}
    original_serialize = orch._plan_service.serialize_analysis

    def serialize_spy(analysis):
        captured["serialize_calls"] += 1
        return original_serialize(analysis)

    orch._plan_service.serialize_analysis = serialize_spy  # type: ignore[method-assign]

    async def fake_cli(_session, _config, _work_type, prompt, **_kwargs):
        captured["prompt"] = str(prompt)
        return "planning", json.dumps(
            {
                "project_goal": plan.project_goal,
                "analysis": original_serialize(plan.analysis),
                "tasks": [
                    {
                        "id": "task_1",
                        "title": "Done task",
                        "description": "d1",
                        "acceptance_criteria": ["a1"],
                        "depends_on": [],
                    },
                    {
                        "id": "task_2",
                        "title": "Pending task",
                        "description": "d2-updated",
                        "acceptance_criteria": ["a2"],
                        "depends_on": [],
                    },
                ],
            },
            ensure_ascii=False,
        )

    monkeypatch.setattr("agent.manager_core.run_prompt_routed_meta", fake_cli)
    monkeypatch.setattr(ManagerOrchestrator, "_manager_prompt", lambda _self, _workdir, _key: "{payload_json}")
    monkeypatch.setattr(ManagerOrchestrator, "_with_invariant_policy", lambda _self, _workdir, text: text)

    class _Session:
        workdir = str(tmp_path)

        def interrupt(self) -> None:
            return None

    fixed = asyncio.run(
        orch._fix_plan_via_cli(
            _Session(),
            plan,
            ["ISSUE"],
            user_goal=plan.project_goal,
            timeout=1,
            workdir=str(tmp_path),
        )
    )

    assert fixed is not None
    assert captured["serialize_calls"] >= 1
    assert captured["prompt"] is not None

    payload = json.loads(captured["prompt"])
    expected_analysis = original_serialize(plan.analysis)
    assert payload["project_analysis"] == expected_analysis
    assert payload["checklist_table"] == expected_analysis["checklist_table"]
    assert fixed.analysis is not None
    assert fixed.analysis.requirements == plan.analysis.requirements
    assert fixed.analysis.checklist_table == plan.analysis.checklist_table


def test_validate_plan_semantics_serializes_full_analysis_payload(tmp_path, monkeypatch):
    cfg = _make_config(tmp_path)
    orch = ManagerOrchestrator(cfg)
    plan = _plan_with_two_tasks(_analysis("validation"))

    captured = {"user": None, "serialize_calls": 0}
    original_serialize = orch._plan_service.serialize_analysis

    def serialize_spy(analysis):
        captured["serialize_calls"] += 1
        return original_serialize(analysis)

    orch._plan_service.serialize_analysis = serialize_spy  # type: ignore[method-assign]

    async def fake_chat_completion(_cfg, _system, user, **_kwargs):
        captured["user"] = user
        return json.dumps({"valid": True, "issues": []}, ensure_ascii=False)

    monkeypatch.setattr("agent.manager_core.chat_completion", fake_chat_completion)

    issues = asyncio.run(orch._validate_plan_semantics(plan, str(tmp_path)))
    assert issues == []
    assert captured["serialize_calls"] >= 1
    assert captured["user"] is not None

    payload = json.loads(captured["user"])
    assert payload["project_analysis"] == original_serialize(plan.analysis)


def test_reconcile_after_commit_updates_analysis_via_service_without_loss(tmp_path, monkeypatch):
    cfg = _make_config(tmp_path)
    orch = ManagerOrchestrator(cfg)
    plan = _plan_with_two_tasks(_analysis("before-commit"))
    done_task = plan.tasks[0]

    captured = {"update_calls": 0, "last_payload": None}
    original_update = orch._plan_service.update_plan_analysis

    def update_spy(plan_obj, updated_analysis):
        captured["update_calls"] += 1
        captured["last_payload"] = dict(updated_analysis or {})
        return original_update(plan_obj, updated_analysis)

    orch._plan_service.update_plan_analysis = update_spy  # type: ignore[method-assign]

    updated_analysis = {
        "current_state": "after-commit-state",
        "already_done": ["after-commit-done"],
        "remaining_work": ["after-commit-remaining"],
        "requirements": ["REQ-2: after-commit-requirement"],
        "checklist_table": [
            {"item": "REQ-2", "status": "done", "how": "applied", "why_not": ""},
        ],
    }

    async def fake_run_git(_workdir: str, _args: list[str]):
        return 0, "commit (abc123)"

    async def fake_chat_completion(*_args, **_kwargs):
        return json.dumps(
            {
                "updated_analysis": updated_analysis,
                "completed_task_ids": [],
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

    assert captured["update_calls"] == 1
    assert captured["last_payload"] == updated_analysis
    saved = load_plan(str(tmp_path))
    assert saved is not None and saved.analysis is not None
    assert saved.analysis.current_state == "after-commit-state"
    assert saved.analysis.already_done == ["after-commit-done"]
    assert saved.analysis.remaining_work == ["after-commit-remaining"]
    assert saved.analysis.requirements == ["REQ-2: after-commit-requirement"]
    assert saved.analysis.checklist_table == [
        {"item": "REQ-2", "status": "done", "how": "applied", "why_not": ""},
    ]


def test_reconcile_after_change_audit_updates_analysis_via_service_without_loss(tmp_path, monkeypatch):
    cfg = _make_config(tmp_path)
    orch = ManagerOrchestrator(cfg)
    plan = _plan_with_two_tasks(_analysis("before-audit"))
    done_task = plan.tasks[0]
    done_task.manager_change_audit = "changed files"

    captured = {"update_calls": 0, "last_payload": None}
    original_update = orch._plan_service.update_plan_analysis

    def update_spy(plan_obj, updated_analysis):
        captured["update_calls"] += 1
        captured["last_payload"] = dict(updated_analysis or {})
        return original_update(plan_obj, updated_analysis)

    orch._plan_service.update_plan_analysis = update_spy  # type: ignore[method-assign]

    updated_analysis = {
        "current_state": "after-audit-state",
        "already_done": ["after-audit-done"],
        "remaining_work": ["after-audit-remaining"],
        "requirements": ["REQ-3: after-audit-requirement"],
        "checklist_table": [
            {"item": "REQ-3", "status": "not_done", "how": "", "why_not": "pending"},
        ],
    }

    async def fake_chat_completion(*_args, **_kwargs):
        return json.dumps(
            {
                "updated_analysis": updated_analysis,
                "completed_task_ids": [],
                "adjustments": [],
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

    assert captured["update_calls"] == 1
    assert captured["last_payload"] == updated_analysis
    saved = load_plan(str(tmp_path))
    assert saved is not None and saved.analysis is not None
    assert saved.analysis.current_state == "after-audit-state"
    assert saved.analysis.already_done == ["after-audit-done"]
    assert saved.analysis.remaining_work == ["after-audit-remaining"]
    assert saved.analysis.requirements == ["REQ-3: after-audit-requirement"]
    assert saved.analysis.checklist_table == [
        {"item": "REQ-3", "status": "not_done", "how": "", "why_not": "pending"},
    ]
