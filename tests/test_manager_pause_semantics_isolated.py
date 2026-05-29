import asyncio
import types
from unittest.mock import AsyncMock

from modes.manager.mode import ManagerMode
from modes.sdk import (
    CallbackModel,
    DictStateService,
    MessagingService,
    ModePipelineService,
    SessionControlService,
    TaskService,
)
from modes.sdk.planning import load_plan, save_plan
from modes.sdk.runtime.contracts import DevTask, ProjectPlan
from session import session_runtime_uid


class _FakeRuntime:
    def pause(self, session) -> None:
        plan = load_plan(session.workdir)
        assert plan is not None
        plan.set_status("paused")
        save_plan(session.workdir, plan)


class _FakeBotApp:
    def __init__(self) -> None:
        self.config = types.SimpleNamespace(defaults=types.SimpleNamespace())
        self.manager_resume_pending = {}
        self.manager = types.SimpleNamespace(_persist_sessions=lambda: None)

    async def _send_message(self, _context, *, chat_id: int, text: str, **_kwargs):
        return None

    async def _edit_message(self, _context, *, chat_id: int, message_id: int, text: str, **_kwargs):
        return True


def _build_mode(bot_app: _FakeBotApp, *, runtime) -> ManagerMode:
    mode = ManagerMode()

    async def _run_mode_pipeline(_session, _prompt, _dest, _context, _mode_id):
        return None

    mode.initialize(
        config=bot_app.config,
        services={
            "tasks": TaskService(),
            "dialogs": types.SimpleNamespace(
                is_active=(lambda **_k: False),
                start=(lambda **_k: None),
                end=(lambda **_k: None),
            ),
            "session_control": SessionControlService(
                persist_sessions=bot_app.manager._persist_sessions,
                cancel_mode_tasks=(lambda _sid, _mid, _timeout: asyncio.sleep(0, result=0)),
                cancel_session_tasks=(lambda _sid, _timeout: asyncio.sleep(0, result=0)),
            ),
            "manager_pending": DictStateService(bot_app.manager_resume_pending),
            "pipeline": ModePipelineService(run_mode_pipeline_fn=_run_mode_pipeline),
            "messaging_factory": (
                lambda ctx: MessagingService(
                    send_message=bot_app._send_message,
                    edit_message=bot_app._edit_message,
                    transport_context=ctx,
                )
            ),
            "runtime_by_capability": (lambda cap: runtime if cap == "manager_control" else None),
        },
    )
    return mode


def test_manager_pause_isolated_calls_cancel_and_sets_paused(tmp_path) -> None:
    async def _run() -> None:
        bot_app = _FakeBotApp()
        runtime = _FakeRuntime()
        mode = _build_mode(bot_app, runtime=runtime)

        plan = ProjectPlan(
            project_goal="g",
            tasks=[DevTask(id="t1", title="t1", description="", acceptance_criteria=["ok"], status="in_progress")],
            status="active",
        )
        save_plan(str(tmp_path), plan)

        session = types.SimpleNamespace(
            id="s1",
            workdir=str(tmp_path),
            busy=False,
            run_lock=asyncio.Lock(),
            active_mode="manager",
            manager_quiet_mode=False,
            queue=[],
        )
        query = types.SimpleNamespace(message=types.SimpleNamespace(chat_id=123, message_id=10))

        cancel_mock = AsyncMock(return_value=1)
        rerender_mock = AsyncMock(return_value=None)
        mode._cancel_mode_tasks = cancel_mock
        mode._rerender_menu = rerender_mock

        await mode.handle_callback(
            CallbackModel(action="pause", chat_id=123, user_id=None, payload={}),
            {
                "bot_app": bot_app,
                "session": session,
                "chat_id": 123,
                "context": object(),
                "query": query,
            },
        )

        cancel_mock.assert_awaited_once_with(
            bot_app=bot_app,
            session_id=session_runtime_uid(session),
            mode_id="manager",
            timeout_s=0.5,
        )
        rerender_mock.assert_awaited_once()
        updated = load_plan(str(tmp_path))
        assert updated is not None
        assert updated.status == "paused"

    asyncio.run(_run())
