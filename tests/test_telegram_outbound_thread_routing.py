from __future__ import annotations

import asyncio
import types

from modes.registry import ModeRegistry
from modes.sdk import BaseMode, DialogService, ModeRegistryService, ToolResult
from sessions.conversation_scope import ConversationScope
from tg.callbacks import CallbackHandler
from tg.handlers import BotHandlers


def test_callback_fallback_send_message_preserves_message_thread_id() -> None:
    sent = []

    async def _send_message(_context, **kwargs):
        sent.append(dict(kwargs))
        return True

    handler = CallbackHandler(types.SimpleNamespace(_send_message=_send_message))

    async def _fake_edit_msg(_context, _query, _text, *, reply_markup=None, md2=True):
        _ = reply_markup
        _ = md2
        return False

    handler._edit_msg = _fake_edit_msg

    asyncio.run(
        handler._respond_callback(
            context=object(),
            query=types.SimpleNamespace(
                message=types.SimpleNamespace(chat_id=-100777000111, message_id=10, message_thread_id=202)
            ),
            chat_id=-100777000111,
            text="fallback",
        )
    )

    assert sent == [
        {
            "chat_id": -100777000111,
            "text": "fallback",
            "reply_markup": None,
            "md2": True,
            "message_thread_id": 202,
        }
    ]


def test_session_mode_callback_send_output_preserves_message_thread_id() -> None:
    class EchoMode(BaseMode):
        mode_id = "echo"

        async def handle_input(self, _message, _ctx):
            return ToolResult.ok()

        async def handle_callback(self, _callback, _ctx):
            return ToolResult.ok("THREAD_OUTPUT")

    sent_output = []

    async def _send_message(_context, **kwargs):
        return kwargs

    async def _send_output(session, dest, output, _context, **kwargs):
        sent_output.append(
            {
                "session_id": getattr(session, "id", None),
                "dest": dict(dest),
                "output": output,
                "kwargs": dict(kwargs),
            }
        )

    registry = ModeRegistry()
    registry.register(EchoMode())
    session = types.SimpleNamespace(
        id="s-thread",
        conversation_scope=ConversationScope(chat_id=-100777000111, message_thread_id=202),
    )

    def _build_reply_dest(current_session, chat_id: int, *, user_id=None):
        dest = {"kind": "telegram", "chat_id": int(chat_id)}
        if user_id is not None:
            dest["user_id"] = int(user_id)
        scope = getattr(current_session, "conversation_scope", None)
        if scope is not None and getattr(scope, "message_thread_id", None) is not None:
            dest["message_thread_id"] = int(scope.message_thread_id)
        return dest

    bot_app = types.SimpleNamespace(
        _send_message=_send_message,
        _edit_message=(lambda *_args, **_kwargs: asyncio.sleep(0, result=True)),
        send_output=_send_output,
        config=types.SimpleNamespace(
            telegram=types.SimpleNamespace(user_languages={}),
            defaults=types.SimpleNamespace(default_language="ru"),
        ),
        manager=types.SimpleNamespace(active=(lambda _chat_id: session)),
        mode_registry=registry,
        mode_registry_service=ModeRegistryService(registry),
        mode_dialogs=DialogService(),
        context_by_chat={},
        dirs_mode={},
        input_dispatch_service=types.SimpleNamespace(),
        git=types.SimpleNamespace(handle_callback=(lambda *_a, **_k: asyncio.sleep(0, result=False))),
        session_ui=types.SimpleNamespace(handle_callback=(lambda *_a, **_k: asyncio.sleep(0, result=False))),
        mode_session_control=types.SimpleNamespace(persist=(lambda: None), cancel_session=(lambda **_k: asyncio.sleep(0))),
        access_policy_service=types.SimpleNamespace(
            ensure_allowed=(lambda _chat_id, _context: asyncio.sleep(0, result=True)),
            is_mode_allowed_for_chat=(lambda _chat_id, _mode_id: True),
            is_admin=(lambda _chat_id, scope="generic": True),
            callback_admin_scope=(lambda _chat_id, data, **kwargs: ""),
            admin_denied_text=(lambda scope="generic": f"denied:{scope}"),
        ),
        security=types.SimpleNamespace(authorize_mode_launch=(lambda *a, **k: asyncio.sleep(0))),
        build_telegram_reply_dest=_build_reply_dest,
        resolve_telegram_callback_scope=(
            lambda query: (
                int(getattr(getattr(query, "message", None), "chat_id", -100777000111)),
                int(getattr(getattr(query, "message", None), "message_thread_id", 202)),
                -100777000111,
                session,
            )
        ),
        build_telegram_transport_context=(
            lambda context, **kwargs: types.SimpleNamespace(raw_context=context, bot=None, meta=kwargs)
        ),
    )
    handler = CallbackHandler(bot_app)

    async def _run() -> None:
        ok = await handler._cb_sess_mode(
            data="sess_mode:echo",
            chat_id=-100777000111,
            query=types.SimpleNamespace(
                from_user=types.SimpleNamespace(id=11),
                message=types.SimpleNamespace(chat_id=-100777000111, message_id=10, message_thread_id=202),
            ),
            context=object(),
        )
        assert ok is True

    asyncio.run(_run())

    assert sent_output == [
        {
            "session_id": "s-thread",
            "dest": {
                "kind": "telegram",
                "chat_id": -100777000111,
                "user_id": 11,
                "message_thread_id": 202,
            },
            "output": "THREAD_OUTPUT",
            "kwargs": {"send_header": False},
        }
    ]


def test_reply_kwargs_prefers_created_session_topic_over_origin_route() -> None:
    handler = BotHandlers(
        types.SimpleNamespace(
            build_telegram_reply_dest=lambda session, chat_id, user_id=None: {
                "kind": "telegram",
                "chat_id": int(chat_id),
                "message_thread_id": int(getattr(getattr(session, "conversation_scope", None), "message_thread_id", 0) or 0),
            },
            resolve_telegram_inbound_route=lambda _update: types.SimpleNamespace(
                reply_kwargs=lambda: {
                    "chat_id": -100777000111,
                    "message_thread_id": 101,
                }
            ),
        )
    )
    session = types.SimpleNamespace(
        conversation_scope=ConversationScope(chat_id=-100777000111, message_thread_id=202),
    )
    update = types.SimpleNamespace(
        effective_chat=types.SimpleNamespace(id=-100777000111),
    )

    assert handler._reply_kwargs(update, session) == {
        "chat_id": -100777000111,
        "message_thread_id": 202,
    }
