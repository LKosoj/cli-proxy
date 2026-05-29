import asyncio
import types

from modes.manager.mode import ManagerMode
from modes.sdk import (
    CallbackModel,
    DictStateService,
    MessagingService,
    ModePipelineService,
    SessionControlService,
    TaskService,
    ToolResult,
)


def _build_mode(bot_app) -> ManagerMode:
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
            "runtime_by_capability": (lambda _cap: None),
        },
    )
    return mode


def test_manager_handle_callback_uses_dispatcher_and_action_method(monkeypatch) -> None:
    async def _run() -> None:
        class _FakeBotApp:
            def __init__(self):
                self.config = types.SimpleNamespace(defaults=types.SimpleNamespace())
                self.manager_resume_pending = {}
                self.manager = types.SimpleNamespace(_persist_sessions=lambda: None)

            async def _send_message(self, _context, *, chat_id: int, text: str, **_kwargs):
                return None

            async def _edit_message(self, _context, *, chat_id: int, message_id: int, text: str, **_kwargs):
                return True

        bot_app = _FakeBotApp()
        mode = _build_mode(bot_app)
        session = types.SimpleNamespace(
            id="s1",
            workdir="/tmp",
            busy=False,
            run_lock=asyncio.Lock(),
            active_mode="manager",
            manager_quiet_mode=False,
            queue=[],
        )

        seen = {"dispatch_action": None, "status_called": 0}

        async def _fake_cb_status(**_kwargs):
            seen["status_called"] += 1
            return ToolResult.ok()

        async def _fake_dispatch(*, action, handlers):
            seen["dispatch_action"] = action
            assert "status" in handlers
            return await handlers[action]()

        monkeypatch.setattr(mode, "_cb_status", _fake_cb_status)
        monkeypatch.setattr(mode, "_dispatch_callback_action", _fake_dispatch)

        result = await mode.handle_callback(
            CallbackModel(action="status", chat_id=123, user_id=99, payload={}),
            {
                "bot_app": bot_app,
                "session": session,
                "chat_id": 123,
                "context": object(),
                "query": None,
            },
        )

        assert result.success is True
        assert seen["dispatch_action"] == "status"
        assert seen["status_called"] == 1

    asyncio.run(_run())
