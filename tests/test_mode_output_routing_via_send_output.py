import asyncio
import types

import pytest

from modes.registry import ModeRegistry
from modes.sdk import BaseMode, ModeCallbackRouterService, ModeInputRoutingService, ModeRegistryService, ToolResult
from sessions.conversation_scope import ConversationScope


def test_mode_input_result_goes_via_send_output() -> None:
    class EchoMode(BaseMode):
        mode_id = "echo"

        async def handle_input(self, _message, _ctx):
            return ToolResult.ok("RESULT_FROM_MODE")

        async def handle_callback(self, _callback, _ctx):
            return ToolResult.ok()

    registry = ModeRegistry()
    registry.register(EchoMode())
    service = ModeInputRoutingService(mode_registry=ModeRegistryService(registry))

    sent_output = []
    sent_messages = []

    async def _send_output(session, dest, output, _context, **kwargs):
        sent_output.append(
            {
                "session_id": getattr(session, "id", None),
                "dest": dict(dest),
                "output": output,
                "kwargs": dict(kwargs),
            }
        )

    async def _send_message(_context, **kwargs):
        sent_messages.append(dict(kwargs))

    async def _cli_fallback(_session, _text, _chat_id, _context):
        raise AssertionError("cli fallback must not be called")

    service.send_output = _send_output
    service.send_message = _send_message

    session = types.SimpleNamespace(id="s1", active_mode="echo")
    asyncio.run(
        service.route_mode_or_cli(
            bot_app=types.SimpleNamespace(),
            session=session,
            text="hello",
            chat_id=101,
            context=object(),
            dest={"kind": "telegram", "chat_id": 101},
            user_id=7,
            cli_fallback=_cli_fallback,
        )
    )

    assert len(sent_output) == 1
    assert sent_output[0]["session_id"] == "s1"
    assert sent_output[0]["dest"]["chat_id"] == 101
    assert sent_output[0]["output"] == "RESULT_FROM_MODE"
    assert sent_output[0]["kwargs"]["send_header"] is False
    assert sent_messages == []


def test_mode_callback_result_goes_via_send_output() -> None:
    class EchoMode(BaseMode):
        mode_id = "echo"

        async def handle_input(self, _message, _ctx):
            return ToolResult.ok()

        async def handle_callback(self, callback, _ctx):
            assert callback.action == "do"
            return ToolResult.ok("RESULT_FROM_CALLBACK")

    registry = ModeRegistry()
    registry.register(EchoMode())
    service = ModeCallbackRouterService(mode_registry=ModeRegistryService(registry))

    sent_output = []
    sent_messages = []

    async def _send_output(session, dest, output, _context, **kwargs):
        sent_output.append(
            {
                "session_id": getattr(session, "id", None),
                "dest": dict(dest),
                "output": output,
                "kwargs": dict(kwargs),
            }
        )

    async def _send_message(_context, **kwargs):
        sent_messages.append(dict(kwargs))

    service.send_output = _send_output
    service.send_message = _send_message
    service.get_session = lambda _chat_id: types.SimpleNamespace(id="s2", active_mode="echo")

    bot_app = types.SimpleNamespace(
        access_policy_service=types.SimpleNamespace(is_mode_allowed_for_chat=(lambda _chat_id, _mode_id: True)),
    )

    ok = asyncio.run(
        service.handle_mode_action_callback(
            data="ma:do",
            chat_id=303,
            query=types.SimpleNamespace(
                from_user=types.SimpleNamespace(id=11),
                message=types.SimpleNamespace(message_id=55),
            ),
            context=object(),
            bot_app=bot_app,
        )
    )

    assert ok is True
    assert len(sent_output) == 1
    assert sent_output[0]["session_id"] == "s2"
    assert sent_output[0]["dest"]["chat_id"] == 303
    assert sent_output[0]["output"] == "RESULT_FROM_CALLBACK"
    assert sent_output[0]["kwargs"]["send_header"] is False
    assert sent_messages == []


def test_mode_callback_result_preserves_thread_dest_via_session_scope() -> None:
    class EchoMode(BaseMode):
        mode_id = "echo"

        async def handle_input(self, _message, _ctx):
            return ToolResult.ok()

        async def handle_callback(self, callback, _ctx):
            assert callback.action == "do"
            return ToolResult.ok("THREAD_RESULT")

    registry = ModeRegistry()
    registry.register(EchoMode())
    service = ModeCallbackRouterService(mode_registry=ModeRegistryService(registry))

    sent_output = []

    async def _send_output(session, dest, output, _context, **kwargs):
        sent_output.append(
            {
                "session_id": getattr(session, "id", None),
                "dest": dict(dest),
                "output": output,
                "kwargs": dict(kwargs),
            }
        )

    service.send_output = _send_output
    service.send_message = None
    service.get_session = lambda _chat_id: types.SimpleNamespace(
        id="s-thread",
        active_mode="echo",
        conversation_scope=ConversationScope(chat_id=-100777000111, message_thread_id=202),
    )

    def _build_reply_dest(session, chat_id: int, *, user_id=None):
        dest = {"kind": "telegram", "chat_id": int(chat_id)}
        if user_id is not None:
            dest["user_id"] = int(user_id)
        scope = getattr(session, "conversation_scope", None)
        if scope is not None and getattr(scope, "message_thread_id", None) is not None:
            dest["message_thread_id"] = int(scope.message_thread_id)
        return dest

    def _build_transport_context(context, *, session, chat_id, dest=None, user_id=None, message_thread_id=None, require_thread_id=None):
        return types.SimpleNamespace(
            raw_context=context,
            bot=getattr(context, "bot", None),
            chat_id=int(chat_id),
            message_thread_id=(
                int(message_thread_id)
                if message_thread_id is not None
                else int((dest or {}).get("message_thread_id") or 0) or None
            ),
            require_thread_id=bool(require_thread_id),
            session_uid=getattr(getattr(session, "conversation_scope", None), "session_uid", None),
        )

    bot_app = types.SimpleNamespace(
        access_policy_service=types.SimpleNamespace(is_mode_allowed_for_chat=(lambda _chat_id, _mode_id: True)),
        build_telegram_reply_dest=_build_reply_dest,
        build_telegram_transport_context=_build_transport_context,
    )

    ok = asyncio.run(
        service.handle_mode_action_callback(
            data="ma:do",
            chat_id=-100777000111,
            query=types.SimpleNamespace(
                from_user=types.SimpleNamespace(id=11),
                message=types.SimpleNamespace(message_id=55, message_thread_id=202),
            ),
            context=types.SimpleNamespace(bot=object()),
            bot_app=bot_app,
        )
    )

    assert ok is True
    assert len(sent_output) == 1
    assert sent_output[0]["session_id"] == "s-thread"
    assert sent_output[0]["dest"]["chat_id"] == -100777000111
    assert sent_output[0]["dest"]["message_thread_id"] == 202
    assert sent_output[0]["output"] == "THREAD_RESULT"
    assert sent_output[0]["kwargs"]["send_header"] is False


def test_mode_handle_input_error_is_explicit_and_not_fallback_to_cli(caplog) -> None:
    class FailingMode(BaseMode):
        mode_id = "broken"

        async def handle_input(self, _message, _ctx):
            raise RuntimeError("mode exploded")

        async def handle_callback(self, _callback, _ctx):
            return ToolResult.ok()

    registry = ModeRegistry()
    registry.register(FailingMode())
    service = ModeInputRoutingService(mode_registry=ModeRegistryService(registry))

    called = {"cli_fallback": 0}

    async def _cli_fallback(_session, _text, _chat_id, _context):
        called["cli_fallback"] += 1
        return None

    session = types.SimpleNamespace(id="s-fail", active_mode="broken")
    caplog.set_level("ERROR")

    with pytest.raises(RuntimeError, match="mode exploded"):
        asyncio.run(
            service.route_mode_or_cli(
                bot_app=types.SimpleNamespace(),
                session=session,
                text="hello",
                chat_id=101,
                context=object(),
                dest={"kind": "telegram", "chat_id": 101},
                user_id=7,
                cli_fallback=_cli_fallback,
            )
        )

    assert called["cli_fallback"] == 0
    records = [rec for rec in caplog.records if "mode handle_input failed mode=broken" in rec.getMessage()]
    assert records
    assert records[0].exc_info is not None
