from __future__ import annotations

import asyncio
import json
import os

from agent.manager import ManagerOrchestrator
from config import load_config
from modes.sdk.runtime.contracts import DevTask, ProjectAnalysis, ProjectPlan


def test_validate_plan_semantics_sends_tz_and_context_payload(tmp_path, monkeypatch):
    cfg = load_config(os.path.join(os.path.dirname(__file__), "..", "config_example.yaml"))
    cfg.defaults.workdir = str(tmp_path)
    cfg.defaults.state_path = str(tmp_path / "state.json")
    cfg.defaults.manager_response_archive = False

    orch = ManagerOrchestrator(cfg)
    captured = {}

    async def fake_chat_completion(_cfg, system, user, **_kwargs):
        captured["system"] = system
        captured["user"] = user
        return json.dumps({"valid": True, "issues": []}, ensure_ascii=False)

    monkeypatch.setattr("agent.manager_core.chat_completion", fake_chat_completion)

    plan = ProjectPlan(
        project_goal="Сделать API и UI для управления задачами",
        analysis=ProjectAnalysis(
            current_state="Есть базовый бот и mode manager",
            already_done=["Базовая маршрутизация"],
            remaining_work=["REST API", "UI формы", "тесты"],
        ),
        tasks=[
            DevTask(
                id="task_1",
                title="Реализовать API",
                description="Добавить эндпоинты",
                acceptance_criteria=["Эндпоинт /tasks отвечает 200"],
                depends_on=[],
            )
        ],
    )

    issues = asyncio.run(orch._validate_plan_semantics(plan, str(tmp_path)))
    assert issues == []

    payload = json.loads(captured["user"])
    assert payload["project_goal"] == "Сделать API и UI для управления задачами"
    assert payload["project_analysis"]["current_state"] == "Есть базовый бот и mode manager"
    assert isinstance(payload["tasks"], list) and payload["tasks"]


def test_validate_plan_semantics_returns_degraded_issue_on_invalid_json(tmp_path, monkeypatch):
    cfg = load_config(os.path.join(os.path.dirname(__file__), "..", "config_example.yaml"))
    cfg.defaults.workdir = str(tmp_path)
    cfg.defaults.state_path = str(tmp_path / "state.json")
    cfg.defaults.manager_response_archive = False

    orch = ManagerOrchestrator(cfg)

    async def fake_chat_completion(_cfg, _system, _user, **_kwargs):
        return "not-json"

    monkeypatch.setattr("agent.manager_core.chat_completion", fake_chat_completion)

    plan = ProjectPlan(
        project_goal="Сделать API и UI для управления задачами",
        analysis=ProjectAnalysis(
            current_state="Есть базовый бот и mode manager",
            already_done=["Базовая маршрутизация"],
            remaining_work=["REST API", "UI формы", "тесты"],
        ),
        tasks=[
            DevTask(
                id="task_1",
                title="Реализовать API",
                description="Добавить эндпоинты",
                acceptance_criteria=["Эндпоинт /tasks отвечает 200"],
                depends_on=[],
            )
        ],
    )

    issues = asyncio.run(orch._validate_plan_semantics(plan, str(tmp_path)))
    assert issues == ["semantic_validator_parse_error"]


def test_validate_plan_semantics_retries_once_and_accepts_second_valid_json(tmp_path, monkeypatch):
    cfg = load_config(os.path.join(os.path.dirname(__file__), "..", "config_example.yaml"))
    cfg.defaults.workdir = str(tmp_path)
    cfg.defaults.state_path = str(tmp_path / "state.json")
    cfg.defaults.manager_response_archive = False

    orch = ManagerOrchestrator(cfg)
    calls = {"n": 0}

    async def fake_chat_completion(_cfg, _system, _user, **_kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return "not-json"
        return json.dumps({"valid": True, "issues": []}, ensure_ascii=False)

    monkeypatch.setattr("agent.manager_core.chat_completion", fake_chat_completion)

    plan = ProjectPlan(
        project_goal="Сделать API и UI для управления задачами",
        analysis=ProjectAnalysis(
            current_state="Есть базовый бот и mode manager",
            already_done=["Базовая маршрутизация"],
            remaining_work=["REST API", "UI формы", "тесты"],
        ),
        tasks=[
            DevTask(
                id="task_1",
                title="Реализовать API",
                description="Добавить эндпоинты",
                acceptance_criteria=["Эндпоинт /tasks отвечает 200"],
                depends_on=[],
            )
        ],
    )

    issues = asyncio.run(orch._validate_plan_semantics(plan, str(tmp_path)))
    assert issues == []
    assert calls["n"] == 2


def test_validate_plan_semantics_returns_degraded_issue_on_empty_response(tmp_path, monkeypatch):
    cfg = load_config(os.path.join(os.path.dirname(__file__), "..", "config_example.yaml"))
    cfg.defaults.workdir = str(tmp_path)
    cfg.defaults.state_path = str(tmp_path / "state.json")
    cfg.defaults.manager_response_archive = False

    orch = ManagerOrchestrator(cfg)

    async def fake_chat_completion(_cfg, _system, _user, **_kwargs):
        return ""

    monkeypatch.setattr("agent.manager_core.chat_completion", fake_chat_completion)

    plan = ProjectPlan(
        project_goal="Сделать API и UI для управления задачами",
        analysis=ProjectAnalysis(
            current_state="Есть базовый бот и mode manager",
            already_done=["Базовая маршрутизация"],
            remaining_work=["REST API", "UI формы", "тесты"],
        ),
        tasks=[
            DevTask(
                id="task_1",
                title="Реализовать API",
                description="Добавить эндпоинты",
                acceptance_criteria=["Эндпоинт /tasks отвечает 200"],
                depends_on=[],
            )
        ],
    )

    issues = asyncio.run(orch._validate_plan_semantics(plan, str(tmp_path)))
    assert issues == ["semantic_validator_parse_error"]


def test_validate_plan_uses_config_max_tasks_when_loaded_plan_has_no_runtime_limit(tmp_path, monkeypatch):
    cfg = load_config(os.path.join(os.path.dirname(__file__), "..", "config_example.yaml"))
    cfg.defaults.workdir = str(tmp_path)
    cfg.defaults.state_path = str(tmp_path / "state.json")
    cfg.defaults.manager_response_archive = False
    cfg.defaults.manager_max_tasks = 10

    orch = ManagerOrchestrator(cfg)

    async def fake_validate_semantics(_self, _plan, _workdir):
        return []

    monkeypatch.setattr(ManagerOrchestrator, "_validate_plan_semantics", fake_validate_semantics)

    plan = ProjectPlan(
        project_goal="Сделать большой проект",
        analysis=ProjectAnalysis(
            current_state="Есть базовый бот и mode manager",
            already_done=["Базовая маршрутизация"],
            remaining_work=[f"chunk_{idx}" for idx in range(1, 21)],
        ),
        tasks=[
            DevTask(
                id=f"task_{idx}",
                title=f"Task {idx}",
                description="Добавить часть функционала",
                acceptance_criteria=["Проверка проходит"],
                depends_on=[],
            )
            for idx in range(1, 11)
        ],
    )

    issues = asyncio.run(orch._validate_plan(plan, str(tmp_path)))
    assert issues == []
