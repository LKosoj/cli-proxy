import os
import json
import asyncio

import pytest

from config import load_config
from agent.manager import ATOMICITY_MAX_REQS_PER_TASK, ManagerOrchestrator
from modes.sdk.runtime.contracts import DevTask, ProjectAnalysis, ProjectPlan


def test_fix_plan_falls_back_to_llm_when_cli_returns_same_plan(tmp_path, monkeypatch):
    cfg = load_config(os.path.join(os.path.dirname(__file__), "..", "config_example.yaml"))
    cfg.defaults.workdir = str(tmp_path)
    cfg.defaults.state_path = str(tmp_path / "state.json")
    cfg.defaults.manager_response_archive = False

    orch = ManagerOrchestrator(cfg)

    # Base plan has an obvious missing dependency: task_1 uses stuff from task_0.
    plan = ProjectPlan(
        project_goal="goal",
        analysis=ProjectAnalysis(current_state="x", already_done=[], remaining_work=[]),
        tasks=[
            DevTask(id="task_0", title="T0", description="D0", acceptance_criteria=["A0"], depends_on=[]),
            DevTask(id="task_1", title="T1", description="Uses get_analyst_templates_cached", acceptance_criteria=["A1"], depends_on=[]),
        ],
    )
    issues = ["task_1 должна зависеть от task_0"]

    # CLI "fix" returns exactly the same JSON (noop).
    class FakeSession:
        def __init__(self, workdir: str):
            self.workdir = workdir
            self.active_cli = "codex"
            self.tool = type("T", (), {"name": "codex"})()

        async def run_prompt(self, _instr: str, *_a, **_kw) -> str:
            return json.dumps(
                {
                    "project_goal": plan.project_goal,
                    "tasks": [
                        {
                            "id": "task_0",
                            "title": "T0",
                            "description": "D0",
                            "acceptance_criteria": ["A0"],
                            "depends_on": [],
                        },
                        {
                            "id": "task_1",
                            "title": "T1",
                            "description": "Uses get_analyst_templates_cached",
                            "acceptance_criteria": ["A1"],
                            "depends_on": [],
                        },
                    ],
                    "analysis": {
                        "current_state": "x",
                        "already_done": [],
                        "remaining_work": [],
                    },
                },
                ensure_ascii=False,
            )

        def interrupt(self) -> None:
            return None

        def set_active_cli(self, cli_name: str) -> None:
            self.active_cli = str(cli_name)
            self.tool = type("T", (), {"name": self.active_cli})()

    # LLM fixer returns corrected plan with dependency.
    async def fake_chat_completion(_cfg, system, user, **_kw):
        assert "редактор плана" in system.lower()
        assert "\"issues\"" in user
        return json.dumps(
            {
                "project_goal": plan.project_goal,
                "tasks": [
                    {
                        "id": "task_0",
                        "title": "T0",
                        "description": "D0",
                        "acceptance_criteria": ["A0"],
                        "depends_on": [],
                    },
                    {
                        "id": "task_1",
                        "title": "T1",
                        "description": "Uses get_analyst_templates_cached",
                        "acceptance_criteria": ["A1"],
                        "depends_on": ["task_0"],
                    },
                ],
                "analysis": {
                    "current_state": "x",
                    "already_done": [],
                    "remaining_work": [],
                },
            },
            ensure_ascii=False,
        )

    monkeypatch.setattr("agent.manager_core.chat_completion", fake_chat_completion)

    session = FakeSession(str(tmp_path))
    fixed = asyncio.run(
        orch._fix_plan_via_cli(
            session,
            plan,
            issues,
            user_goal=plan.project_goal,
            timeout=1,
            workdir=str(tmp_path),
        )
    )

    assert fixed is not None
    by_id = {t.id: t for t in fixed.tasks}
    assert by_id["task_1"].depends_on == ["task_0"]


@pytest.mark.parametrize(
    "issue",
    [
        "TASK_COUNT_BELOW_MIN: tasks_count=1, min_tasks_dynamic=6",
        "TASK_TOO_BROAD_REQ_COVERAGE: task_id=task_1, req_count=3, max_per_task=2",
    ],
)
def test_fix_plan_cli_disables_no_new_tasks_rule_for_count_and_atomicity_issues(
    tmp_path, monkeypatch, issue
):
    cfg = load_config(os.path.join(os.path.dirname(__file__), "..", "config_example.yaml"))
    cfg.defaults.workdir = str(tmp_path)
    cfg.defaults.state_path = str(tmp_path / "state.json")
    cfg.defaults.manager_response_archive = False

    orch = ManagerOrchestrator(cfg)
    plan = ProjectPlan(
        project_goal="goal",
        analysis=ProjectAnalysis(current_state="x", already_done=[], remaining_work=[]),
        tasks=[
            DevTask(id="task_1", title="T1", description="D1", acceptance_criteria=["A1"], depends_on=[]),
        ],
    )

    captured = {"payload": None}

    async def _fake_cli(_session, _config, _work_type, prompt, **_kwargs):
        captured["payload"] = json.loads(str(prompt))
        return "planning", json.dumps(
            {
                "project_goal": plan.project_goal,
                "tasks": [
                    {
                        "id": "task_1",
                        "title": "T1",
                        "description": "D1",
                        "acceptance_criteria": ["A1"],
                        "depends_on": [],
                    },
                    {
                        "id": "task_2",
                        "title": "T2",
                        "description": "D2",
                        "acceptance_criteria": ["A2"],
                        "depends_on": ["task_1"],
                    },
                ],
                "analysis": {
                    "current_state": "x",
                    "already_done": [],
                    "remaining_work": [],
                },
            },
            ensure_ascii=False,
        )

    monkeypatch.setattr("agent.manager_core.run_prompt_routed_meta", _fake_cli)
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
            [issue],
            user_goal=plan.project_goal,
            timeout=1,
            workdir=str(tmp_path),
        )
    )

    assert fixed is not None
    assert captured["payload"] is not None
    assert captured["payload"]["rules"]["max_requirements_per_task"] == ATOMICITY_MAX_REQS_PER_TASK
    assert captured["payload"]["rules"]["no_new_tasks_by_default"] is False


def test_fix_plan_cli_keeps_no_new_tasks_rule_for_non_count_non_atomicity_issues(tmp_path, monkeypatch):
    cfg = load_config(os.path.join(os.path.dirname(__file__), "..", "config_example.yaml"))
    cfg.defaults.workdir = str(tmp_path)
    cfg.defaults.state_path = str(tmp_path / "state.json")
    cfg.defaults.manager_response_archive = False

    orch = ManagerOrchestrator(cfg)
    plan = ProjectPlan(
        project_goal="goal",
        analysis=ProjectAnalysis(current_state="x", already_done=[], remaining_work=[]),
        tasks=[
            DevTask(id="task_1", title="T1", description="D1", acceptance_criteria=["A1"], depends_on=[]),
        ],
    )

    captured = {"payload": None}

    async def _fake_cli(_session, _config, _work_type, prompt, **_kwargs):
        captured["payload"] = json.loads(str(prompt))
        return "planning", json.dumps(
            {
                "project_goal": plan.project_goal,
                "tasks": [
                    {
                        "id": "task_1",
                        "title": "T1",
                        "description": "D1 (updated)",
                        "acceptance_criteria": ["A1"],
                        "depends_on": [],
                    }
                ],
                "analysis": {
                    "current_state": "x",
                    "already_done": [],
                    "remaining_work": [],
                },
            },
            ensure_ascii=False,
        )

    monkeypatch.setattr("agent.manager_core.run_prompt_routed_meta", _fake_cli)
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
            ["Задача 'task_1': нет acceptance_criteria"],
            user_goal=plan.project_goal,
            timeout=1,
            workdir=str(tmp_path),
        )
    )

    assert fixed is not None
    assert captured["payload"] is not None
    assert captured["payload"]["rules"]["no_new_tasks_by_default"] is True


def test_fix_plan_cli_passes_min_tasks_and_oscillation_rule_into_payload(tmp_path, monkeypatch):
    cfg = load_config(os.path.join(os.path.dirname(__file__), "..", "config_example.yaml"))
    cfg.defaults.workdir = str(tmp_path)
    cfg.defaults.state_path = str(tmp_path / "state.json")
    cfg.defaults.manager_response_archive = False

    orch = ManagerOrchestrator(cfg)
    plan = ProjectPlan(
        project_goal="goal",
        analysis=ProjectAnalysis(current_state="x", already_done=[], remaining_work=[]),
        tasks=[
            DevTask(id="task_1", title="T1", description="D1", acceptance_criteria=["A1"], depends_on=[]),
        ],
    )

    captured = {"payload": None}

    async def _fake_cli(_session, _config, _work_type, prompt, **_kwargs):
        captured["payload"] = json.loads(str(prompt))
        return "planning", json.dumps(
            {
                "tasks": [
                    {
                        "id": "task_1",
                        "title": "T1",
                        "description": "D1 updated",
                        "acceptance_criteria": ["A1"],
                        "depends_on": [],
                    }
                ]
            },
            ensure_ascii=False,
        )

    monkeypatch.setattr("agent.manager_core.run_prompt_routed_meta", _fake_cli)
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
            ["Несоответствие между планом и projectgoal: ..."],
            user_goal=plan.project_goal,
            timeout=1,
            workdir=str(tmp_path),
            min_tasks_dynamic=9,
            stabilize_task_count=True,
        )
    )

    assert fixed is not None
    assert captured["payload"] is not None
    assert captured["payload"]["rules"]["min_tasks"] == 9
    assert captured["payload"]["rules"]["max_requirements_per_task"] == ATOMICITY_MAX_REQS_PER_TASK
    assert captured["payload"]["rules"]["prevent_count_oscillation"] is True


@pytest.mark.parametrize(
    "issue",
    [
        "TASK_COUNT_BELOW_MIN: tasks_count=1, min_tasks_dynamic=6",
        "TASK_TOO_BROAD_REQ_COVERAGE: task_id=task_1, req_count=3, max_per_task=2",
    ],
)
def test_fix_plan_llm_disables_no_new_tasks_rule_for_count_and_atomicity_issues(
    tmp_path, monkeypatch, issue
):
    cfg = load_config(os.path.join(os.path.dirname(__file__), "..", "config_example.yaml"))
    cfg.defaults.workdir = str(tmp_path)
    cfg.defaults.state_path = str(tmp_path / "state.json")
    cfg.defaults.manager_response_archive = False

    orch = ManagerOrchestrator(cfg)
    plan = ProjectPlan(
        project_goal="goal",
        analysis=ProjectAnalysis(current_state="x", already_done=[], remaining_work=[]),
        tasks=[
            DevTask(id="task_1", title="T1", description="D1", acceptance_criteria=["A1"], depends_on=[]),
        ],
    )

    captured = {"payload": None}

    async def _fake_chat_completion(_cfg, _system, user, **_kwargs):
        captured["payload"] = json.loads(str(user))
        return json.dumps(
            {
                "project_goal": plan.project_goal,
                "tasks": [
                    {
                        "id": "task_1",
                        "title": "T1",
                        "description": "D1",
                        "acceptance_criteria": ["A1"],
                        "depends_on": [],
                    },
                    {
                        "id": "task_2",
                        "title": "T2",
                        "description": "D2",
                        "acceptance_criteria": ["A2"],
                        "depends_on": ["task_1"],
                    },
                ],
                "analysis": {
                    "current_state": "x",
                    "already_done": [],
                    "remaining_work": [],
                },
            },
            ensure_ascii=False,
        )

    monkeypatch.setattr("agent.manager_core.chat_completion", _fake_chat_completion)
    monkeypatch.setattr(ManagerOrchestrator, "_manager_prompt", lambda _self, _workdir, _key: "plan_fix max={max_tasks}")
    monkeypatch.setattr(ManagerOrchestrator, "_with_invariant_policy", lambda _self, _workdir, text: text)

    fixed = asyncio.run(
        orch._fix_plan_via_llm(
            plan,
            [issue],
            user_goal=plan.project_goal,
            workdir=str(tmp_path),
        )
    )

    assert fixed is not None
    assert captured["payload"] is not None
    assert captured["payload"]["rules"]["max_requirements_per_task"] == ATOMICITY_MAX_REQS_PER_TASK
    assert captured["payload"]["rules"]["no_new_tasks_by_default"] is False


def test_fix_plan_llm_keeps_no_new_tasks_rule_for_non_count_non_atomicity_issues(tmp_path, monkeypatch):
    cfg = load_config(os.path.join(os.path.dirname(__file__), "..", "config_example.yaml"))
    cfg.defaults.workdir = str(tmp_path)
    cfg.defaults.state_path = str(tmp_path / "state.json")
    cfg.defaults.manager_response_archive = False

    orch = ManagerOrchestrator(cfg)
    plan = ProjectPlan(
        project_goal="goal",
        analysis=ProjectAnalysis(current_state="x", already_done=[], remaining_work=[]),
        tasks=[
            DevTask(id="task_1", title="T1", description="D1", acceptance_criteria=["A1"], depends_on=[]),
        ],
    )

    captured = {"payload": None}

    async def _fake_chat_completion(_cfg, _system, user, **_kwargs):
        captured["payload"] = json.loads(str(user))
        return json.dumps(
            {
                "project_goal": plan.project_goal,
                "tasks": [
                    {
                        "id": "task_1",
                        "title": "T1",
                        "description": "D1 (updated)",
                        "acceptance_criteria": ["A1"],
                        "depends_on": [],
                    }
                ],
                "analysis": {
                    "current_state": "x",
                    "already_done": [],
                    "remaining_work": [],
                },
            },
            ensure_ascii=False,
        )

    monkeypatch.setattr("agent.manager_core.chat_completion", _fake_chat_completion)
    monkeypatch.setattr(ManagerOrchestrator, "_manager_prompt", lambda _self, _workdir, _key: "plan_fix max={max_tasks}")
    monkeypatch.setattr(ManagerOrchestrator, "_with_invariant_policy", lambda _self, _workdir, text: text)

    fixed = asyncio.run(
        orch._fix_plan_via_llm(
            plan,
            ["Задача 'task_1': нет acceptance_criteria"],
            user_goal=plan.project_goal,
            workdir=str(tmp_path),
        )
    )

    assert fixed is not None
    assert captured["payload"] is not None
    assert captured["payload"]["rules"]["no_new_tasks_by_default"] is True


def test_fix_plan_llm_passes_min_tasks_and_oscillation_rule_into_payload(tmp_path, monkeypatch):
    cfg = load_config(os.path.join(os.path.dirname(__file__), "..", "config_example.yaml"))
    cfg.defaults.workdir = str(tmp_path)
    cfg.defaults.state_path = str(tmp_path / "state.json")
    cfg.defaults.manager_response_archive = False

    orch = ManagerOrchestrator(cfg)
    plan = ProjectPlan(
        project_goal="goal",
        analysis=ProjectAnalysis(current_state="x", already_done=[], remaining_work=[]),
        tasks=[
            DevTask(id="task_1", title="T1", description="D1", acceptance_criteria=["A1"], depends_on=[]),
        ],
    )

    captured = {"payload": None}

    async def _fake_chat_completion(_cfg, _system, user, **_kwargs):
        captured["payload"] = json.loads(str(user))
        return json.dumps(
            {
                "tasks": [
                    {
                        "id": "task_1",
                        "title": "T1",
                        "description": "D1 updated",
                        "acceptance_criteria": ["A1"],
                        "depends_on": [],
                    }
                ]
            },
            ensure_ascii=False,
        )

    monkeypatch.setattr("agent.manager_core.chat_completion", _fake_chat_completion)
    monkeypatch.setattr(ManagerOrchestrator, "_manager_prompt", lambda _self, _workdir, _key: "plan_fix max={max_tasks}")
    monkeypatch.setattr(ManagerOrchestrator, "_with_invariant_policy", lambda _self, _workdir, text: text)

    fixed = asyncio.run(
        orch._fix_plan_via_llm(
            plan,
            ["Несоответствие между планом и projectgoal: ..."],
            user_goal=plan.project_goal,
            workdir=str(tmp_path),
            min_tasks_dynamic=7,
            stabilize_task_count=True,
        )
    )

    assert fixed is not None
    assert captured["payload"] is not None
    assert captured["payload"]["rules"]["min_tasks"] == 7
    assert captured["payload"]["rules"]["max_requirements_per_task"] == ATOMICITY_MAX_REQS_PER_TASK
    assert captured["payload"]["rules"]["prevent_count_oscillation"] is True


def test_fix_plan_cli_fallback_llm_resolves_task_too_broad_req_coverage(tmp_path, monkeypatch):
    cfg = load_config(os.path.join(os.path.dirname(__file__), "..", "config_example.yaml"))
    cfg.defaults.workdir = str(tmp_path)
    cfg.defaults.state_path = str(tmp_path / "state.json")
    cfg.defaults.manager_response_archive = False

    orch = ManagerOrchestrator(cfg)
    req_ids = [f"REQ-{i}" for i in range(1, int(ATOMICITY_MAX_REQS_PER_TASK) + 2)]
    plan = ProjectPlan(
        project_goal="goal",
        analysis=ProjectAnalysis(
            current_state="x",
            already_done=[],
            remaining_work=[],
            requirements=list(req_ids),
        ),
        tasks=[
            DevTask(
                id="task_1",
                title="T1",
                description="D1",
                acceptance_criteria=["A1"],
                covers_requirements=list(req_ids),
                depends_on=[],
            ),
            DevTask(id="task_2", title="T2", description="D2", acceptance_criteria=["A2"], depends_on=[]),
            DevTask(id="task_3", title="T3", description="D3", acceptance_criteria=["A3"], depends_on=[]),
            DevTask(id="task_4", title="T4", description="D4", acceptance_criteria=["A4"], depends_on=[]),
            DevTask(id="task_5", title="T5", description="D5", acceptance_criteria=["A5"], depends_on=[]),
            DevTask(id="task_6", title="T6", description="D6", acceptance_criteria=["A6"], depends_on=[]),
        ],
    )
    issues_before = ManagerOrchestrator._validate_plan_structure(plan)
    broad_issues = [x for x in issues_before if str(x).startswith("TASK_TOO_BROAD_REQ_COVERAGE:")]
    assert broad_issues

    async def _fake_cli(_session, _config, _work_type, _prompt, **_kwargs):
        # Return equivalent plan to force LLM fallback in _fix_plan_via_cli.
        return "planning", json.dumps(
            {
                "project_goal": plan.project_goal,
                "tasks": [
                    {
                        "id": "task_1",
                        "title": "T1",
                        "description": "D1",
                        "acceptance_criteria": ["A1"],
                        "covers_requirements": list(req_ids),
                        "depends_on": [],
                    },
                    {"id": "task_2", "title": "T2", "description": "D2", "acceptance_criteria": ["A2"], "depends_on": []},
                    {"id": "task_3", "title": "T3", "description": "D3", "acceptance_criteria": ["A3"], "depends_on": []},
                    {"id": "task_4", "title": "T4", "description": "D4", "acceptance_criteria": ["A4"], "depends_on": []},
                    {"id": "task_5", "title": "T5", "description": "D5", "acceptance_criteria": ["A5"], "depends_on": []},
                    {"id": "task_6", "title": "T6", "description": "D6", "acceptance_criteria": ["A6"], "depends_on": []},
                ],
                "analysis": {
                    "current_state": "x",
                    "already_done": [],
                    "remaining_work": [],
                    "requirements": list(req_ids),
                },
            },
            ensure_ascii=False,
        )

    async def _fake_chat_completion(_cfg, _system, user, **_kwargs):
        payload = json.loads(str(user))
        assert payload["atomicity_hotspots"] == [
            {
                "task_id": "task_1",
                "title": "T1",
                "covers_requirements": len(req_ids),
                "max_allowed": ATOMICITY_MAX_REQS_PER_TASK,
                "requirement_ids": list(req_ids),
            }
        ]
        assert payload["rules"]["max_requirements_per_task"] == ATOMICITY_MAX_REQS_PER_TASK
        assert payload["rules"]["no_new_tasks_by_default"] is False
        assert any(str(x).startswith("TASK_TOO_BROAD_REQ_COVERAGE:") for x in payload.get("issues", []))
        return json.dumps(
            {
                "project_goal": plan.project_goal,
                "tasks": [
                    {
                        "id": "task_1",
                        "title": "T1",
                        "description": "D1",
                        "acceptance_criteria": ["A1"],
                        "covers_requirements": list(req_ids[: int(ATOMICITY_MAX_REQS_PER_TASK)]),
                        "depends_on": [],
                    },
                    {
                        "id": "task_2",
                        "title": "T2",
                        "description": "D2",
                        "acceptance_criteria": ["A2"],
                        "covers_requirements": [req_ids[-1]],
                        "depends_on": [],
                    },
                    {"id": "task_3", "title": "T3", "description": "D3", "acceptance_criteria": ["A3"], "depends_on": []},
                    {"id": "task_4", "title": "T4", "description": "D4", "acceptance_criteria": ["A4"], "depends_on": []},
                    {"id": "task_5", "title": "T5", "description": "D5", "acceptance_criteria": ["A5"], "depends_on": []},
                    {"id": "task_6", "title": "T6", "description": "D6", "acceptance_criteria": ["A6"], "depends_on": []},
                ],
                "analysis": {
                    "current_state": "x",
                    "already_done": [],
                    "remaining_work": [],
                    "requirements": list(req_ids),
                },
            },
            ensure_ascii=False,
        )

    monkeypatch.setattr("agent.manager_core.run_prompt_routed_meta", _fake_cli)
    monkeypatch.setattr("agent.manager_core.chat_completion", _fake_chat_completion)
    monkeypatch.setattr(
        ManagerOrchestrator,
        "_manager_prompt",
        lambda _self, _workdir, key: "{payload_json}" if key == "plan_fix_minimal_instruction" else "plan_fix max={max_tasks}",
    )
    monkeypatch.setattr(ManagerOrchestrator, "_with_invariant_policy", lambda _self, _workdir, text: text)

    class _Session:
        workdir = str(tmp_path)

        def interrupt(self) -> None:
            return None

    fixed = asyncio.run(
        orch._fix_plan_via_cli(
            _Session(),
            plan,
            [str(broad_issues[0])],
            user_goal=plan.project_goal,
            timeout=1,
            workdir=str(tmp_path),
        )
    )

    assert fixed is not None
    issues_after = ManagerOrchestrator._validate_plan_structure(fixed)
    assert not any(str(x).startswith("TASK_TOO_BROAD_REQ_COVERAGE:") for x in issues_after)


def test_fix_plan_cli_preserves_project_analysis_when_fix_response_omits_it(tmp_path, monkeypatch):
    cfg = load_config(os.path.join(os.path.dirname(__file__), "..", "config_example.yaml"))
    cfg.defaults.workdir = str(tmp_path)
    cfg.defaults.state_path = str(tmp_path / "state.json")
    cfg.defaults.manager_response_archive = False

    orch = ManagerOrchestrator(cfg)
    plan = ProjectPlan(
        project_goal="goal",
        analysis=ProjectAnalysis(
            current_state="baseline",
            already_done=["bootstrap"],
            remaining_work=[f"rw_{i}" for i in range(1, 13)],
            requirements=["REQ-1: сделать A"],
            checklist_table=[{"item": "A", "status": "not_done", "how": "", "why_not": "pending"}],
        ),
        tasks=[
            DevTask(id="task_1", title="T1", description="D1", acceptance_criteria=["A1"], depends_on=[]),
        ],
    )

    captured = {"payload": None}

    async def _fake_cli(_session, _config, _work_type, prompt, **_kwargs):
        captured["payload"] = json.loads(str(prompt))
        return "planning", json.dumps(
            {
                "tasks": [
                    {
                        "id": "task_1",
                        "title": "T1",
                        "description": "D1 updated",
                        "acceptance_criteria": ["A1"],
                        "depends_on": [],
                    }
                ]
            },
            ensure_ascii=False,
        )

    monkeypatch.setattr("agent.manager_core.run_prompt_routed_meta", _fake_cli)
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
            ["Задача 'task_1': нет acceptance_criteria"],
            user_goal=plan.project_goal,
            timeout=1,
            workdir=str(tmp_path),
        )
    )

    assert fixed is not None
    assert captured["payload"] is not None
    assert captured["payload"]["project_analysis"]["remaining_work"] == list(plan.analysis.remaining_work)
    assert fixed.analysis is not None
    assert fixed.analysis.current_state == "baseline"
    assert fixed.analysis.remaining_work == list(plan.analysis.remaining_work)
    assert fixed.analysis.requirements == list(plan.analysis.requirements)
    assert fixed.analysis.checklist_table == list(plan.analysis.checklist_table)


def test_fix_plan_llm_preserves_project_analysis_when_fix_response_omits_it(tmp_path, monkeypatch):
    cfg = load_config(os.path.join(os.path.dirname(__file__), "..", "config_example.yaml"))
    cfg.defaults.workdir = str(tmp_path)
    cfg.defaults.state_path = str(tmp_path / "state.json")
    cfg.defaults.manager_response_archive = False

    orch = ManagerOrchestrator(cfg)
    plan = ProjectPlan(
        project_goal="goal",
        analysis=ProjectAnalysis(
            current_state="baseline",
            already_done=["bootstrap"],
            remaining_work=[f"rw_{i}" for i in range(1, 13)],
            requirements=["REQ-1: сделать A"],
            checklist_table=[{"item": "A", "status": "not_done", "how": "", "why_not": "pending"}],
        ),
        tasks=[
            DevTask(id="task_1", title="T1", description="D1", acceptance_criteria=["A1"], depends_on=[]),
        ],
    )

    captured = {"payload": None}

    async def _fake_chat_completion(_cfg, _system, user, **_kwargs):
        captured["payload"] = json.loads(str(user))
        return json.dumps(
            {
                "tasks": [
                    {
                        "id": "task_1",
                        "title": "T1",
                        "description": "D1 updated",
                        "acceptance_criteria": ["A1"],
                        "depends_on": [],
                    }
                ]
            },
            ensure_ascii=False,
        )

    monkeypatch.setattr("agent.manager_core.chat_completion", _fake_chat_completion)
    monkeypatch.setattr(ManagerOrchestrator, "_manager_prompt", lambda _self, _workdir, _key: "plan_fix max={max_tasks}")
    monkeypatch.setattr(ManagerOrchestrator, "_with_invariant_policy", lambda _self, _workdir, text: text)

    fixed = asyncio.run(
        orch._fix_plan_via_llm(
            plan,
            ["Задача 'task_1': нет acceptance_criteria"],
            user_goal=plan.project_goal,
            workdir=str(tmp_path),
        )
    )

    assert fixed is not None
    assert captured["payload"] is not None
    assert captured["payload"]["project_analysis"]["remaining_work"] == list(plan.analysis.remaining_work)
    assert fixed.analysis is not None
    assert fixed.analysis.current_state == "baseline"
    assert fixed.analysis.remaining_work == list(plan.analysis.remaining_work)
    assert fixed.analysis.requirements == list(plan.analysis.requirements)
    assert fixed.analysis.checklist_table == list(plan.analysis.checklist_table)


def test_fix_plan_cli_ignores_mutated_project_analysis_from_fix_response(tmp_path, monkeypatch):
    cfg = load_config(os.path.join(os.path.dirname(__file__), "..", "config_example.yaml"))
    cfg.defaults.workdir = str(tmp_path)
    cfg.defaults.state_path = str(tmp_path / "state.json")
    cfg.defaults.manager_response_archive = False

    orch = ManagerOrchestrator(cfg)
    baseline_checklist = [{"item": "A", "status": "not_done", "how": "", "why_not": "pending"}]
    plan = ProjectPlan(
        project_goal="goal",
        analysis=ProjectAnalysis(
            current_state="baseline",
            already_done=["bootstrap"],
            remaining_work=["rw_1", "rw_2"],
            requirements=["REQ-1: сделать A"],
            checklist_table=list(baseline_checklist),
        ),
        tasks=[
            DevTask(id="task_1", title="T1", description="D1", acceptance_criteria=["A1"], depends_on=[]),
        ],
    )

    async def _fake_cli(_session, _config, _work_type, _prompt, **_kwargs):
        return "planning", json.dumps(
            {
                "tasks": [
                    {
                        "id": "task_1",
                        "title": "T1",
                        "description": "D1 updated",
                        "acceptance_criteria": ["A1"],
                        "depends_on": [],
                    }
                ],
                "analysis": {
                    "current_state": "mutated",
                    "already_done": ["changed"],
                    "remaining_work": ["new_rw_1", "new_rw_2", "new_rw_3"],
                    "requirements": ["REQ-999: noisy change"],
                    "checklist_table": [{"item": "M", "status": "done", "how": "mutated", "why_not": ""}],
                },
                "checklist_table": [{"item": "M", "status": "done", "how": "mutated", "why_not": ""}],
            },
            ensure_ascii=False,
        )

    monkeypatch.setattr("agent.manager_core.run_prompt_routed_meta", _fake_cli)
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
            ["Задача 'task_1': нет acceptance_criteria"],
            user_goal=plan.project_goal,
            timeout=1,
            workdir=str(tmp_path),
        )
    )

    assert fixed is not None
    assert fixed.analysis is not None
    assert fixed.analysis.current_state == "baseline"
    assert fixed.analysis.already_done == ["bootstrap"]
    assert fixed.analysis.remaining_work == ["rw_1", "rw_2"]
    assert fixed.analysis.requirements == ["REQ-1: сделать A"]
    assert fixed.analysis.checklist_table == baseline_checklist


def test_fix_plan_llm_ignores_mutated_project_analysis_from_fix_response(tmp_path, monkeypatch):
    cfg = load_config(os.path.join(os.path.dirname(__file__), "..", "config_example.yaml"))
    cfg.defaults.workdir = str(tmp_path)
    cfg.defaults.state_path = str(tmp_path / "state.json")
    cfg.defaults.manager_response_archive = False

    orch = ManagerOrchestrator(cfg)
    baseline_checklist = [{"item": "A", "status": "not_done", "how": "", "why_not": "pending"}]
    plan = ProjectPlan(
        project_goal="goal",
        analysis=ProjectAnalysis(
            current_state="baseline",
            already_done=["bootstrap"],
            remaining_work=["rw_1", "rw_2"],
            requirements=["REQ-1: сделать A"],
            checklist_table=list(baseline_checklist),
        ),
        tasks=[
            DevTask(id="task_1", title="T1", description="D1", acceptance_criteria=["A1"], depends_on=[]),
        ],
    )

    async def _fake_chat_completion(_cfg, _system, _user, **_kwargs):
        return json.dumps(
            {
                "tasks": [
                    {
                        "id": "task_1",
                        "title": "T1",
                        "description": "D1 updated",
                        "acceptance_criteria": ["A1"],
                        "depends_on": [],
                    }
                ],
                "analysis": {
                    "current_state": "mutated",
                    "already_done": ["changed"],
                    "remaining_work": ["new_rw_1", "new_rw_2", "new_rw_3"],
                    "requirements": ["REQ-999: noisy change"],
                    "checklist_table": [{"item": "M", "status": "done", "how": "mutated", "why_not": ""}],
                },
                "checklist_table": [{"item": "M", "status": "done", "how": "mutated", "why_not": ""}],
            },
            ensure_ascii=False,
        )

    monkeypatch.setattr("agent.manager_core.chat_completion", _fake_chat_completion)
    monkeypatch.setattr(ManagerOrchestrator, "_manager_prompt", lambda _self, _workdir, _key: "plan_fix max={max_tasks}")
    monkeypatch.setattr(ManagerOrchestrator, "_with_invariant_policy", lambda _self, _workdir, text: text)

    fixed = asyncio.run(
        orch._fix_plan_via_llm(
            plan,
            ["Задача 'task_1': нет acceptance_criteria"],
            user_goal=plan.project_goal,
            workdir=str(tmp_path),
        )
    )

    assert fixed is not None
    assert fixed.analysis is not None
    assert fixed.analysis.current_state == "baseline"
    assert fixed.analysis.already_done == ["bootstrap"]
    assert fixed.analysis.remaining_work == ["rw_1", "rw_2"]
    assert fixed.analysis.requirements == ["REQ-1: сделать A"]
    assert fixed.analysis.checklist_table == baseline_checklist
