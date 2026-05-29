from __future__ import annotations

import asyncio
import os
from types import SimpleNamespace

from agent.manager import ManagerOrchestrator
from config import load_config
from modes.sdk.runtime.contracts import DevTask, ProjectAnalysis, ProjectPlan


class _FakeBot:
    def __init__(self) -> None:
        self.messages = []
        self.outputs = []

    async def _send_message(self, _context, *, chat_id: int, text: str, **_kwargs) -> None:
        self.messages.append((chat_id, text))

    async def send_output(self, _session, _dest, output: str, _context, **kwargs) -> None:
        self.outputs.append((output, kwargs))


def _make_config(tmp_path):
    cfg = load_config(os.path.join(os.path.dirname(__file__), "..", "config_example.yaml"))
    cfg.defaults.workdir = str(tmp_path)
    cfg.defaults.state_path = str(tmp_path / "state.json")
    cfg.defaults.manager_response_archive = False
    return cfg


def test_orchestrator_constructor_accepts_injected_services(tmp_path) -> None:
    class _PlanSvc:
        pass

    class _ExecSvc:
        pass

    class _ReviewSvc:
        pass

    class _UiSvc:
        pass

    plan = _PlanSvc()
    execution = _ExecSvc()
    review = _ReviewSvc()
    ui = _UiSvc()

    orch = ManagerOrchestrator(
        _make_config(tmp_path),
        plan_service=plan,  # type: ignore[arg-type]
        execution_service=execution,  # type: ignore[arg-type]
        review_service=review,  # type: ignore[arg-type]
        ui_service=ui,  # type: ignore[arg-type]
    )

    assert orch._plan_service is plan
    assert orch._execution_service is execution
    assert orch._review_service is review
    assert orch._ui_service is ui


def test_orchestrator_uses_injected_plan_execution_review_services(tmp_path) -> None:
    class _PlanSvc:
        def __init__(self) -> None:
            self.payload_calls = 0

        def plan_from_payload(self, payload, *, user_goal, max_tasks):
            self.payload_calls += 1
            assert payload == {"tasks": [{"id": "t1"}]}
            assert user_goal == "goal"
            assert max_tasks == 3
            return "plan-result"

    class _ExecSvc:
        def __init__(self) -> None:
            self.parse_calls = 0

        def parse_review_result(self, text, *, logger=None):
            _ = logger
            self.parse_calls += 1
            assert text == "review-json"
            return "review-result"

    class _ReviewSvc:
        def __init__(self) -> None:
            self.snapshot_calls = 0

        def snapshot_workdir(self, workdir, *, max_files, hash_max_bytes):
            self.snapshot_calls += 1
            assert max_files == 10
            assert hash_max_bytes == 20
            return {"wd": workdir}

    plan = _PlanSvc()
    execution = _ExecSvc()
    review = _ReviewSvc()

    orch = ManagerOrchestrator(
        _make_config(tmp_path),
        plan_service=plan,  # type: ignore[arg-type]
        execution_service=execution,  # type: ignore[arg-type]
        review_service=review,  # type: ignore[arg-type]
    )

    parsed = orch._payload_to_plan({"tasks": [{"id": "t1"}]}, "goal", 3)
    review_parsed = orch._try_parse_review("review-json")
    snap = orch._snapshot_workdir("workdir-a", max_files=10, hash_max_bytes=20)

    assert parsed == "plan-result"
    assert review_parsed == "review-result"
    assert snap == {"wd": "workdir-a"}
    assert plan.payload_calls == 1
    assert execution.parse_calls == 1
    assert review.snapshot_calls == 1


def test_orchestrator_notify_plan_uses_injected_ui_service_and_isolated_between_intents(tmp_path) -> None:
    class _UiSvc:
        def __init__(self) -> None:
            self.goals = []

        def format_plan_notification(self, plan: ProjectPlan) -> str:
            self.goals.append(plan.project_goal)
            return f"notify:{plan.project_goal}"

    async def _run() -> None:
        ui = _UiSvc()
        orch = ManagerOrchestrator(_make_config(tmp_path), ui_service=ui)  # type: ignore[arg-type]
        bot = _FakeBot()
        session = SimpleNamespace(id="s1")
        dest = {"chat_id": 123, "kind": "telegram"}

        plan_a = ProjectPlan(
            project_goal="intent-a",
            tasks=[DevTask(id="a1", title="A1", description="", acceptance_criteria=["ok"])],
            analysis=ProjectAnalysis(current_state="", already_done=[], remaining_work=[]),
            status="active",
        )
        plan_b = ProjectPlan(
            project_goal="intent-b",
            tasks=[DevTask(id="b1", title="B1", description="", acceptance_criteria=["ok"])],
            analysis=ProjectAnalysis(current_state="", already_done=[], remaining_work=[]),
            status="active",
        )

        await orch._notify_plan(session, plan_a, bot, context=None, dest=dest)
        await orch._notify_plan(session, plan_b, bot, context=None, dest=dest)

        assert ui.goals == ["intent-a", "intent-b"]
        assert bot.messages == [(123, "notify:intent-a"), (123, "notify:intent-b")]
        assert bot.outputs == []

    asyncio.run(_run())
