import asyncio
import types

import agent.manager as manager_mod
from modes.sdk.runtime.contracts import DevTask, ProjectPlan
from agent.manager import ManagerOrchestrator


class _FakeBot:
    def __init__(self) -> None:
        self.messages = []
        self.outputs = []

    async def _send_message(self, _context, *, chat_id: int, text: str, **_kwargs) -> None:
        self.messages.append((chat_id, text, _kwargs))

    async def send_output(self, _session, _dest, output: str, _context, **kwargs) -> None:
        self.outputs.append((output, kwargs))


def _make_orchestrator() -> ManagerOrchestrator:
    obj = object.__new__(ManagerOrchestrator)
    obj._config = types.SimpleNamespace(
        defaults=types.SimpleNamespace(
            manager_auto_resume=True,
        )
    )
    return obj


def test_manager_final_report_uses_unified_send_output(monkeypatch, tmp_path) -> None:
    async def _run() -> None:
        orch = _make_orchestrator()
        plan = ProjectPlan(
            project_goal="goal",
            tasks=[DevTask(id="t1", title="Task", description="", acceptance_criteria=["ok"])],
            status="completed",
        )

        async def _start_new_plan(_session, _user_text, _bot, _context, _dest):
            return plan

        async def _notify_plan(_session, _plan, _bot, _context, _dest):
            return None

        async def _run_loop(_session, _plan, _bot, _context, _dest):
            return None

        async def _compose_final_report(_plan, workdir=""):
            return "R" * 5000

        async def _final_audit(*_args, **_kwargs):
            return {
                "passed": True,
                "summary_text": "Финальный шаг проверки исходного ТЗ:\n- итог: PASS",
                "result": {"status": "PASS", "fixes_applied": [], "remaining_gaps": []},
            }

        orch._start_new_plan = _start_new_plan
        orch._notify_plan = _notify_plan
        orch._run_loop = _run_loop
        orch._compose_final_report = _compose_final_report
        orch._run_final_spec_audit_and_close_gaps = _final_audit

        monkeypatch.setattr(manager_mod, "load_plan", lambda _w, **_kwargs: None)
        monkeypatch.setattr(manager_mod, "save_plan", lambda _w, _p, **_kwargs: None)
        monkeypatch.setattr(manager_mod, "archive_plan", lambda _w, _s, **_kwargs: None)

        bot = _FakeBot()
        session = types.SimpleNamespace(workdir=str(tmp_path))
        dest = {"chat_id": 123, "kind": "telegram"}

        result = await orch.run(session, "goal", bot, context=None, dest=dest)

        assert bot.messages
        assert "✅ Готово. Результат ниже." in bot.messages[0][1]
        assert len(bot.outputs) == 1
        output, kwargs = bot.outputs[0]
        assert "Финальный шаг проверки исходного ТЗ" in output
        assert len(output) > 5000
        assert kwargs.get("send_header") is False
        assert result == output
        assert result == plan.completion_report

    asyncio.run(_run())


def test_manager_notify_plan_long_sends_file_without_summary(tmp_path) -> None:
    async def _run() -> None:
        orch = _make_orchestrator()
        tasks = [
            DevTask(
                id=f"t{i}",
                title=f"Очень длинная задача номер {i} с подробным описанием для проверки лимитов Telegram",
                description="",
                acceptance_criteria=["ok"],
            )
            for i in range(1, 130)
        ]
        plan = ProjectPlan(
            project_goal="goal",
            tasks=tasks,
            status="active",
        )
        session = types.SimpleNamespace(id="s1", name="dummy", tool=types.SimpleNamespace(name="dummy"))
        bot = _FakeBot()
        dest = {"chat_id": 123, "kind": "telegram"}

        await orch._notify_plan(session, plan, bot, context=None, dest=dest)

        assert bot.messages
        assert "План длинный, отправил его файлом" in bot.messages[0][1]
        assert len(bot.outputs) == 1
        output, kwargs = bot.outputs[0]
        assert output.startswith("📋 План:")
        assert kwargs.get("send_header") is False
        assert kwargs.get("force_html") is True
        assert kwargs.get("send_summary") is False

    asyncio.run(_run())


def test_manager_notify_plan_short_sends_plain_message(tmp_path) -> None:
    async def _run() -> None:
        orch = _make_orchestrator()
        plan = ProjectPlan(
            project_goal="goal",
            tasks=[DevTask(id="t1", title="Task", description="", acceptance_criteria=["ok"])],
            status="active",
        )
        session = types.SimpleNamespace(id="s1", name="dummy", tool=types.SimpleNamespace(name="dummy"))
        bot = _FakeBot()
        dest = {"chat_id": 123, "kind": "telegram"}

        await orch._notify_plan(session, plan, bot, context=None, dest=dest)

        assert len(bot.messages) == 1
        assert "📋 План: goal" in bot.messages[0][1]
        assert bot.outputs == []

    asyncio.run(_run())


def test_manager_failed_shows_retry_buttons_even_if_report_send_fails(monkeypatch, tmp_path) -> None:
    async def _run() -> None:
        orch = _make_orchestrator()
        plan = ProjectPlan(
            project_goal="goal",
            tasks=[DevTask(id="t1", title="Task", description="", acceptance_criteria=["ok"])],
            status="active",
        )

        async def _start_new_plan(_session, _user_text, _bot, _context, _dest):
            return plan

        async def _notify_plan(_session, _plan, _bot, _context, _dest):
            return None

        async def _run_loop(_session, _plan, _bot, _context, _dest):
            _plan.status = "failed"
            return None

        async def _compose_final_report(_plan, workdir=""):
            return "failed report"

        async def _final_audit(*_args, **_kwargs):
            return {
                "passed": True,
                "summary_text": "Финальный шаг проверки исходного ТЗ:\n- итог: PASS",
                "result": {"status": "PASS", "fixes_applied": [], "remaining_gaps": []},
            }

        orch._start_new_plan = _start_new_plan
        orch._notify_plan = _notify_plan
        orch._run_loop = _run_loop
        orch._compose_final_report = _compose_final_report
        orch._run_final_spec_audit_and_close_gaps = _final_audit

        monkeypatch.setattr(manager_mod, "load_plan", lambda _w, **_kwargs: None)
        monkeypatch.setattr(manager_mod, "save_plan", lambda _w, _p, **_kwargs: None)
        monkeypatch.setattr(manager_mod, "archive_plan", lambda _w, _s, **_kwargs: None)

        bot = _FakeBot()

        async def _broken_send_output(*_args, **_kwargs):
            raise RuntimeError("send failed")

        bot.send_output = _broken_send_output

        session = types.SimpleNamespace(workdir=str(tmp_path))
        dest = {"chat_id": 123, "kind": "telegram"}

        await orch.run(session, "goal", bot, context=None, dest=dest)

        assert any("❌ План провален" in m[1] for m in bot.messages)
        action_msgs = [m for m in bot.messages if "Что сделать с проваленным планом?" in m[1]]
        assert action_msgs
        reply_markup = action_msgs[-1][2].get("reply_markup")
        assert reply_markup is not None
        callbacks = [b.callback_data for row in reply_markup.inline_keyboard for b in row]
        assert "ma:manager:failed_retry" in callbacks
        assert "ma:manager:failed_archive" in callbacks

    asyncio.run(_run())
