"""Tests that BotApp.send_message delegates to BotApp._send_message (M3 layer fix)."""
import asyncio


def test_send_message_delegates_to_private(monkeypatch):
    """send_message must forward all kwargs to _send_message and return its result."""
    from unittest.mock import MagicMock
    import bot as bot_module

    # Build a minimal stub that only has the parts needed to call send_message.
    stub = object.__new__(bot_module.BotApp)

    captured: list = []

    async def fake_send_message(context, **kwargs):
        captured.append((context, kwargs))
        return "sent"

    monkeypatch.setattr(stub, "_send_message", fake_send_message)

    fake_context = MagicMock()
    result = asyncio.run(stub.send_message(fake_context, chat_id=42, text="hello"))

    assert result == "sent"
    assert len(captured) == 1
    ctx, kw = captured[0]
    assert ctx is fake_context
    assert kw == {"chat_id": 42, "text": "hello"}
