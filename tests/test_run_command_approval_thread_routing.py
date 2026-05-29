from __future__ import annotations

import asyncio
import types

import pytest

from agent.tooling import helpers
from bot import BotApp
from sessions.conversation_scope import ConversationScope


@pytest.fixture(autouse=True)
def _reset_pending_commands(monkeypatch):
    helpers._PENDING_COMMANDS.clear()
    helpers._PENDING_COMMAND_WAITERS.clear()
    helpers._PENDING_COMMAND_DECISIONS.clear()
    monkeypatch.setattr(helpers, "_PENDING_STORE_REPO", None, raising=False)
    monkeypatch.setattr(helpers, "_PENDING_STORE_LOADED", True, raising=False)


@pytest.mark.asyncio
async def test_request_command_approval_routes_buttons_to_session_topic() -> None:
    sent_messages: list[dict] = []

    async def _send_message(_context, **kwargs):
        sent_messages.append(dict(kwargs))
        return True

    app = BotApp.__new__(BotApp)
    app.ui_state = types.SimpleNamespace(context_by_chat={-100777000111: object()})
    app.manager = types.SimpleNamespace(
        sessions_by_chat={
            1: {
                "s-thread": types.SimpleNamespace(
                    id="s-thread",
                    chat_id=1,
                    conversation_scope=ConversationScope(
                        chat_id=-100777000111,
                        message_thread_id=202,
                    ),
                )
            }
        }
    )
    app._send_message = _send_message

    cmd_id = helpers._store_pending_command(
        "s-thread",
        -100777000111,
        "rm -rf ./tmp",
        "/repo",
        "Dangerous",
    )

    app._request_command_approval(-100777000111, cmd_id, "rm -rf ./tmp", "Dangerous")
    await asyncio.sleep(0)

    assert len(sent_messages) == 1
    payload = sent_messages[0]
    assert payload["chat_id"] == -100777000111
    assert payload["message_thread_id"] == 202
    assert "Нужное подтверждение: Dangerous" in payload["text"]
    keyboard = payload["reply_markup"]
    buttons = keyboard.inline_keyboard[0]
    assert [button.callback_data for button in buttons] == [
        f"approve_cmd:{cmd_id}",
        f"deny_cmd:{cmd_id}",
    ]
