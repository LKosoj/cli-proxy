from __future__ import annotations

import asyncio
import types

from agent.manager import ManagerOrchestrator
from modes.sdk.runtime.contracts import DevTask, ProjectPlan


class _FakeBot:
    def __init__(self) -> None:
        self.messages = []
        self.outputs = []

    async def _send_message(self, _context, *, chat_id: int, text: str, **_kwargs) -> None:
        self.messages.append((chat_id, text))

    async def send_output(self, _session, _dest, output: str, _context, **kwargs) -> None:
        self.outputs.append((output, kwargs))


def _make_orchestrator() -> ManagerOrchestrator:
    obj = object.__new__(ManagerOrchestrator)
    obj._config = types.SimpleNamespace(defaults=types.SimpleNamespace(manager_auto_resume=True))
    return obj


def test_notify_plan_uses_ui_service_formatter() -> None:
    async def _run() -> None:
        orch = _make_orchestrator()
        plan = ProjectPlan(
            project_goal="goal",
            tasks=[DevTask(id="t1", title="Task", description="", acceptance_criteria=["ok"])],
            status="active",
        )
        session = types.SimpleNamespace(id="s1")
        bot = _FakeBot()
        dest = {"chat_id": 123, "kind": "telegram"}
        calls = {"count": 0}

        class _SpyUI:
            @staticmethod
            def format_plan_notification(_plan):
                calls["count"] += 1
                return "FORMATTED-BY-UI-SERVICE"

        orch._ui_service = _SpyUI()

        await orch._notify_plan(session, plan, bot, context=None, dest=dest)
        assert calls["count"] == 1
        assert bot.messages == [(123, "FORMATTED-BY-UI-SERVICE")]
        assert bot.outputs == []

    asyncio.run(_run())
