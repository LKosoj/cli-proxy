from __future__ import annotations

import asyncio
import json
import os
from types import SimpleNamespace

from agent.manager import ManagerOrchestrator
from config import load_config
from modes.sdk.runtime.contracts import DevTask, ProjectAnalysis, ProjectPlan


def test_delegate_review_rejects_invalid_executor_output_schema(tmp_path) -> None:
    cfg = load_config(os.path.join(os.path.dirname(__file__), "..", "config_example.yaml"))
    cfg.defaults.workdir = str(tmp_path)
    cfg.defaults.state_path = str(tmp_path / "state.json")
    cfg.defaults.manager_response_archive = False

    orch = ManagerOrchestrator(cfg)
    plan = ProjectPlan(
        project_goal="goal",
        analysis=ProjectAnalysis(current_state="ctx", already_done=[], remaining_work=[]),
        tasks=[
            DevTask(id="task_1", title="T1", description="D1", acceptance_criteria=["ok"]),
        ],
    )
    task = plan.tasks[0]
    session = SimpleNamespace(workdir=str(tmp_path))

    class _BadExecutor:
        async def run(self, *_args, **_kwargs):
            return SimpleNamespace(summary="summary", outputs="invalid-outputs")

    orch._executor = _BadExecutor()  # type: ignore[assignment]
    orch._manager_prompt = (
        lambda *_a, **_kw: "{task_title} {task_description} {task_acceptance} {dev_report} {last_commit_info}"
    )  # type: ignore[method-assign]
    orch._with_invariant_policy = lambda _workdir, text: text  # type: ignore[method-assign]

    # Bypass profile builder internals and call target directly.
    async def _call():
        return await orch._delegate_review(session, plan, task, bot=None, context=None, dest={})

    result = asyncio.run(_call())
    assert result.approved is False
    assert "executor_response_output schema validation failed" in (result.comments or "")


def test_delegate_review_degrades_when_review_normalizer_returns_non_json(tmp_path, monkeypatch) -> None:
    cfg = load_config(os.path.join(os.path.dirname(__file__), "..", "config_example.yaml"))
    cfg.defaults.workdir = str(tmp_path)
    cfg.defaults.state_path = str(tmp_path / "state.json")
    cfg.defaults.manager_response_archive = False

    orch = ManagerOrchestrator(cfg)
    plan = ProjectPlan(
        project_goal="goal",
        analysis=ProjectAnalysis(current_state="ctx", already_done=[], remaining_work=[]),
        tasks=[
            DevTask(id="task_1", title="T1", description="D1", acceptance_criteria=["ok"]),
        ],
    )
    task = plan.tasks[0]
    session = SimpleNamespace(workdir=str(tmp_path))

    class _Executor:
        async def run(self, *_args, **_kwargs):
            return SimpleNamespace(
                summary="summary",
                outputs=[
                    {
                        "type": "text",
                        "content": "⚠️ Достигнут лимит итераций (15). Возвращаю промежуточный результат.",
                    }
                ],
            )

    async def fake_chat_completion(_cfg, _system, user, **kwargs):
        assert "Достигнут лимит итераций" in user
        handler = kwargs.get("normalize_error_handler")
        assert callable(handler)
        return handler(
            "Промежуточный ответ normalizer без JSON.",
            json.JSONDecodeError("Expecting value", "Промежуточный ответ normalizer без JSON.", 0),
        )

    orch._executor = _Executor()  # type: ignore[assignment]
    orch._manager_prompt = (
        lambda *_a, **_kw: "{task_title} {task_description} {task_acceptance} {dev_report} {last_commit_info}"
    )  # type: ignore[method-assign]
    orch._with_invariant_policy = lambda _workdir, text: text  # type: ignore[method-assign]
    monkeypatch.setattr("agent.manager_core.chat_completion", fake_chat_completion)

    async def _call():
        return await orch._delegate_review(session, plan, task, bot=None, context=None, dest={})

    result = asyncio.run(_call())

    assert result.approved is False
    assert result.summary == "Не удалось нормализовать ответ ревьюера"
    assert "Исходный ответ ревьюера" in (result.comments or "")
    assert "Достигнут лимит итераций" in (result.comments or "")
    assert "Ответ normalizer LLM" in (result.comments or "")


def test_delegate_review_rejects_action_payload_from_review_normalizer(tmp_path, monkeypatch) -> None:
    cfg = load_config(os.path.join(os.path.dirname(__file__), "..", "config_example.yaml"))
    cfg.defaults.workdir = str(tmp_path)
    cfg.defaults.state_path = str(tmp_path / "state.json")
    cfg.defaults.manager_response_archive = False

    orch = ManagerOrchestrator(cfg)
    plan = ProjectPlan(
        project_goal="goal",
        analysis=ProjectAnalysis(current_state="ctx", already_done=[], remaining_work=[]),
        tasks=[
            DevTask(id="task_1", title="T1", description="D1", acceptance_criteria=["ok"]),
        ],
    )
    task = plan.tasks[0]
    session = SimpleNamespace(workdir=str(tmp_path))

    class _Executor:
        async def run(self, *_args, **_kwargs):
            return SimpleNamespace(
                summary="Инструменты возвращают ошибки и прогресс остановился.",
                outputs=[
                    {
                        "type": "text",
                        "content": (
                            "Инструменты возвращают ошибки и прогресс остановился. "
                            "Последняя ошибка инструмента: Invalid args for read_file: ['missing required: path']"
                        ),
                    }
                ],
            )

    async def fake_chat_completion(_cfg, _system, user, **_kwargs):
        assert "Инструменты возвращают ошибки" in user
        return json.dumps(
            {
                "path": "/srv/git_projects/demo/project/file.py",
                "summary": "Инструменты возвращают ошибки и прогресс остановился.",
            },
            ensure_ascii=False,
        )

    orch._executor = _Executor()  # type: ignore[assignment]
    orch._manager_prompt = (
        lambda *_a, **_kw: "{task_title} {task_description} {task_acceptance} {dev_report} {last_commit_info}"
    )  # type: ignore[method-assign]
    orch._with_invariant_policy = lambda _workdir, text: text  # type: ignore[method-assign]
    monkeypatch.setattr("agent.manager_core.chat_completion", fake_chat_completion)

    async def _call():
        return await orch._delegate_review(session, plan, task, bot=None, context=None, dest={})

    result = asyncio.run(_call())

    assert result.approved is False
    assert result.summary == "Инструменты возвращают ошибки и прогресс остановился."
    assert "Такой ответ трактуется как rejected" in (result.comments or "")
    assert "path='/srv/git_projects/demo/project/file.py'" in (result.comments or "")


def test_delegate_review_normalizes_raw_executor_action_payload_before_final_verdict(
    tmp_path,
    monkeypatch,
) -> None:
    cfg = load_config(os.path.join(os.path.dirname(__file__), "..", "config_example.yaml"))
    cfg.defaults.workdir = str(tmp_path)
    cfg.defaults.state_path = str(tmp_path / "state.json")
    cfg.defaults.manager_response_archive = False

    orch = ManagerOrchestrator(cfg)
    plan = ProjectPlan(
        project_goal="goal",
        analysis=ProjectAnalysis(current_state="ctx", already_done=[], remaining_work=[]),
        tasks=[
            DevTask(id="task_1", title="T1", description="D1", acceptance_criteria=["ok"]),
        ],
    )
    task = plan.tasks[0]
    session = SimpleNamespace(workdir=str(tmp_path))

    class _Executor:
        async def run(self, *_args, **_kwargs):
            return SimpleNamespace(
                summary="",
                outputs=[
                    {
                        "type": "text",
                        "content": json.dumps(
                            {
                                "path": "tests/test_miniapp_rc_settings_put.py",
                                "offset": 1,
                                "limit": 200,
                            },
                            ensure_ascii=False,
                        ),
                    }
                ],
            )

    async def fake_chat_completion(_cfg, _system, user, **_kwargs):
        assert "tests/test_miniapp_rc_settings_put.py" in user
        return json.dumps(
            {
                "approved": False,
                "summary": "Есть замечания",
                "comments": "Нормализованный reviewer verdict",
                "tests_passed": None,
                "files_reviewed": ["tests/test_miniapp_rc_settings_put.py"],
                "not_done_assessment": [],
            },
            ensure_ascii=False,
        )

    orch._executor = _Executor()  # type: ignore[assignment]
    orch._manager_prompt = (
        lambda *_a, **_kw: "{task_title} {task_description} {task_acceptance} {dev_report} {last_commit_info}"
    )  # type: ignore[method-assign]
    orch._with_invariant_policy = lambda _workdir, text: text  # type: ignore[method-assign]
    monkeypatch.setattr("agent.manager_core.chat_completion", fake_chat_completion)

    async def _call():
        return await orch._delegate_review(session, plan, task, bot=None, context=None, dest={})

    result = asyncio.run(_call())

    assert result.approved is False
    assert result.summary == "Есть замечания"
    assert result.comments == "Нормализованный reviewer verdict"
    assert result.files_reviewed == ["tests/test_miniapp_rc_settings_put.py"]
