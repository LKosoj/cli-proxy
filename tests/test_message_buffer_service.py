from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from app.services.message_buffer_service import MessageBufferService


@pytest.mark.asyncio
async def test_buffer_or_send_debounces_all_chunks_into_single_payload() -> None:
    payloads: list[str] = []

    async def _stage_user_input(_session, text, _chat_id, _context, *, dest=None, image_path=None, image_paths=None):
        _ = dest
        _ = image_path, image_paths
        payloads.append(str(text or ""))

    bot_app = SimpleNamespace(
        message_buffer_user_id={},
        message_buffer={},
        buffer_tasks={},
        _stage_user_input=_stage_user_input,
    )
    service = MessageBufferService(bot_app)
    session = SimpleNamespace(id="s1")
    context = object()

    await service.buffer_or_send(session, "part-1", chat_id=7, context=context, user_id=101)
    await service.buffer_or_send(session, "part-2", chat_id=7, context=context, user_id=101)
    await service.buffer_or_send(session, "tail", chat_id=7, context=context, user_id=101)

    assert payloads == []
    assert bot_app.message_buffer.get(7) == ["part-1", "part-2", "tail"]

    await service.flush_buffer(7, session, context)
    await asyncio.sleep(0)

    assert payloads == ["part-1\n\npart-2\n\ntail"]
    assert bot_app.message_buffer.get(7) == []
    assert 7 not in bot_app.buffer_tasks


@pytest.mark.asyncio
async def test_buffer_or_send_preserves_direct_messages_topic_id() -> None:
    delivered: list[dict | None] = []

    async def _stage_user_input(_session, _text, _chat_id, _context, *, dest=None, image_path=None, image_paths=None):
        _ = image_path, image_paths
        delivered.append(dest)

    def _build_dest(_session, chat_id, *, user_id=None, direct_messages_topic_id=None):
        return {
            "kind": "telegram",
            "chat_id": chat_id,
            "user_id": user_id,
            "direct_messages_topic_id": direct_messages_topic_id,
        }

    bot_app = SimpleNamespace(
        message_buffer_user_id={},
        message_buffer_direct_messages_topic_id={},
        message_buffer={},
        buffer_tasks={},
        build_telegram_reply_dest=_build_dest,
        _stage_user_input=_stage_user_input,
    )
    service = MessageBufferService(bot_app)
    session = SimpleNamespace(id="s1")
    context = object()

    await service.buffer_or_send(
        session,
        "hello",
        chat_id=7,
        context=context,
        user_id=101,
        direct_messages_topic_id=888,
    )
    await service.flush_buffer(7, session, context)

    assert delivered == [
        {
            "kind": "telegram",
            "chat_id": 7,
            "user_id": 101,
            "direct_messages_topic_id": 888,
        }
    ]
    assert bot_app.message_buffer_direct_messages_topic_id == {}


@pytest.mark.asyncio
async def test_schedule_flush_resets_timer_when_new_chunk_arrives() -> None:
    async def _stage_user_input(_session, _text, _chat_id, _context, *, dest=None, image_path=None, image_paths=None):
        _ = dest, image_path, image_paths
        return None

    bot_app = SimpleNamespace(
        message_buffer_user_id={},
        message_buffer={},
        buffer_tasks={},
        _stage_user_input=_stage_user_input,
    )
    service = MessageBufferService(bot_app)
    session = SimpleNamespace(id="s1")
    context = object()

    await service.buffer_or_send(session, "first", chat_id=9, context=context, user_id=501)
    first_task = bot_app.buffer_tasks[9]

    await service.buffer_or_send(session, "second", chat_id=9, context=context, user_id=501)
    second_task = bot_app.buffer_tasks[9]

    assert second_task is not first_task

    await asyncio.sleep(0)
    assert first_task.done()

    await service.flush_buffer(9, session, context)
    await asyncio.sleep(0)

    assert 9 not in bot_app.buffer_tasks


@pytest.mark.asyncio
async def test_flush_after_delay_does_not_cancel_its_own_delivery(monkeypatch) -> None:
    delivered: list[str] = []
    done = asyncio.Event()
    real_sleep = asyncio.sleep

    async def _fast_sleep(_delay):
        await real_sleep(0)

    async def _stage_user_input(_session, text, _chat_id, _context, *, dest=None, image_path=None, image_paths=None):
        _ = dest
        _ = image_path, image_paths
        delivered.append(str(text or ""))
        await real_sleep(0)
        delivered.append("done")
        done.set()

    monkeypatch.setattr("app.services.message_buffer_service.asyncio.sleep", _fast_sleep)

    bot_app = SimpleNamespace(
        message_buffer_user_id={},
        message_buffer={},
        buffer_tasks={},
        _stage_user_input=_stage_user_input,
    )
    service = MessageBufferService(bot_app)
    session = SimpleNamespace(id="s1")
    context = object()

    await service.buffer_or_send(session, "hello", chat_id=3, context=context, user_id=11)
    await asyncio.wait_for(done.wait(), timeout=1.0)

    assert delivered == ["hello", "done"]
    assert bot_app.message_buffer.get(3) == []
    assert 3 not in bot_app.buffer_tasks
