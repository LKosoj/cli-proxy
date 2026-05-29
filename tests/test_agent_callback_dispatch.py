import asyncio
import types

from modes.agent.mode import AgentMode
from modes.sdk import CallbackModel, MessagingService, SessionControlService, TaskService, ToolResult


def _build_mode(bot_app) -> AgentMode:
    mode = AgentMode()
    mode.initialize(
        config=bot_app.config,
        services={
            "tasks": TaskService(),
            "session_control": SessionControlService(
                persist_sessions=(lambda: None),
                cancel_mode_tasks=(lambda _sid, _mid, _timeout: asyncio.sleep(0, result=0)),
                cancel_session_tasks=(lambda _sid, _timeout: asyncio.sleep(0, result=0)),
            ),
            "messaging_factory": (
                lambda ctx: MessagingService(
                    send_message=bot_app._send_message,
                    edit_message=bot_app._edit_message,
                    transport_context=ctx,
                )
            ),
        },
    )
    return mode


def test_agent_handle_callback_uses_dispatcher_and_cb_handler(monkeypatch) -> None:
    async def _run() -> None:
        class _FakeBotApp:
            def __init__(self):
                self.config = types.SimpleNamespace(defaults=types.SimpleNamespace())

            async def _send_message(self, _context, *, chat_id: int, text: str, **_kwargs):
                return None

            async def _edit_message(self, _context, *, chat_id: int, message_id: int, text: str, **_kwargs):
                return True

        bot_app = _FakeBotApp()
        mode = _build_mode(bot_app)
        session = types.SimpleNamespace(
            id="s1",
            active_mode="agent",
            busy=False,
            run_lock=asyncio.Lock(),
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


def test_agent_handle_callback_project_change_alias_dispatches_project_connect(monkeypatch) -> None:
    async def _run() -> None:
        class _FakeBotApp:
            def __init__(self):
                self.config = types.SimpleNamespace(defaults=types.SimpleNamespace())

            async def _send_message(self, _context, *, chat_id: int, text: str, **_kwargs):
                _ = chat_id, text
                return None

            async def _edit_message(self, _context, *, chat_id: int, message_id: int, text: str, **_kwargs):
                _ = chat_id, message_id, text
                return True

        bot_app = _FakeBotApp()
        mode = _build_mode(bot_app)
        session = types.SimpleNamespace(
            id="s1",
            active_mode="agent",
            busy=False,
            run_lock=asyncio.Lock(),
            queue=[],
        )

        seen = {"project_connect_called": 0}

        async def _fake_cb_project_connect(**_kwargs):
            seen["project_connect_called"] += 1
            return ToolResult.ok()

        monkeypatch.setattr(mode, "_cb_project_connect", _fake_cb_project_connect)
        result = await mode.handle_callback(
            CallbackModel(action="project_change", chat_id=123, user_id=99, payload={}),
            {
                "bot_app": bot_app,
                "session": session,
                "chat_id": 123,
                "context": object(),
                "query": None,
            },
        )

        assert result.success is True
        assert seen["project_connect_called"] == 1

    asyncio.run(_run())


def test_agent_handle_callback_unknown_action_returns_fail() -> None:
    async def _run() -> None:
        class _FakeBotApp:
            def __init__(self):
                self.config = types.SimpleNamespace(defaults=types.SimpleNamespace())

            async def _send_message(self, _context, *, chat_id: int, text: str, **_kwargs):
                _ = chat_id, text
                return None

            async def _edit_message(self, _context, *, chat_id: int, message_id: int, text: str, **_kwargs):
                _ = chat_id, message_id, text
                return True

        bot_app = _FakeBotApp()
        mode = _build_mode(bot_app)
        session = types.SimpleNamespace(
            id="s1",
            active_mode="agent",
            busy=False,
            run_lock=asyncio.Lock(),
            queue=[],
        )

        result = await mode.handle_callback(
            CallbackModel(action="unknown_action", chat_id=123, user_id=99, payload={}),
            {
                "bot_app": bot_app,
                "session": session,
                "chat_id": 123,
                "context": object(),
                "query": None,
            },
        )

        assert result.success is False
        assert result.error == "unknown_action"

    asyncio.run(_run())
