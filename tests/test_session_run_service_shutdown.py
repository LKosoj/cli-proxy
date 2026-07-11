import asyncio
import logging
from collections import deque
from types import SimpleNamespace

from app.services.telegram_transport import TelegramEditOutcome
from sessions.session_run_service import SessionRunService


def _build_service(bot_app):
    return SessionRunService(
        bot_app=bot_app,
        persist_sessions=lambda: None,
        mode_tasks_list=lambda **_k: [],
        mode_tasks_create=lambda **_k: None,
        log_cli_dialog=lambda *_a, **_k: None,
        reset_session_fields_like_sessions_reset=lambda *_a, **_k: None,
    )


def test_session_run_service_skips_background_tasks_during_shutdown() -> None:
    async def _run() -> None:
        calls = {"send_output": 0, "handle_user_input": 0}

        async def _send_output(_session, _dest, _output, _context):
            calls["send_output"] += 1

        async def _handle_user_input(*_args, **_kwargs):
            calls["handle_user_input"] += 1

        bot_app = SimpleNamespace(
            _shutdown_in_progress=True,
            send_output=_send_output,
            _handle_user_input=_handle_user_input,
            _send_message=(lambda *_a, **_k: asyncio.sleep(0)),
        )
        service = _build_service(bot_app)
        session = SimpleNamespace(
            id="s1",
            name="s1",
            tool=SimpleNamespace(name="dummy"),
            workdir="/tmp",
            busy=False,
            queue=deque([{"text": "next", "dest": {"kind": "telegram", "chat_id": 1}}]),
            run_lock=asyncio.Lock(),
            started_at=None,
            last_output_ts=None,
            last_tick_ts=None,
            last_tick_value=None,
            tick_seen=0,
            headless_forced_stop=None,
        )

        async def _run_prompt(_prompt, **_kwargs):
            return "ok"

        session.run_prompt = _run_prompt

        await service.run_prompt(
            session=session,
            prompt="go",
            dest={"kind": "telegram", "chat_id": 1},
            context=object(),
        )

        assert calls["send_output"] == 0
        assert calls["handle_user_input"] == 0
        # During shutdown next queued item must stay queued, not dropped.
        assert len(session.queue) == 1
        assert session.busy is False

    asyncio.run(_run())


def test_session_run_service_safe_create_task_no_running_loop() -> None:
    calls = {"closed": 0}

    async def _coro():
        try:
            await asyncio.sleep(0)
        finally:
            calls["closed"] += 1

    bot_app = SimpleNamespace(_shutdown_in_progress=False)
    service = _build_service(bot_app)
    task = service._safe_create_task(_coro(), label="no_loop")

    assert task is None
    # Coroutine object must be closed safely without scheduling.
    assert calls["closed"] == 0


def test_session_run_service_logs_shutdown_close_cleanup_failure(caplog) -> None:
    class _FailingClose:
        def close(self) -> None:
            raise RuntimeError("close denied")

    bot_app = SimpleNamespace(_shutdown_in_progress=True)
    service = _build_service(bot_app)

    caplog.set_level(logging.ERROR, logger="sessions.session_run_service")
    task = service._safe_create_task(_FailingClose(), label="shutdown_close")

    assert task is None
    assert (
        "best_effort_cleanup: failed to close coroutine during shutdown operation=shutdown_close"
        in caplog.text
    )


def test_session_run_service_keeps_assistant_preview_after_unknown_edit_failure(caplog) -> None:
    async def _run() -> None:
        async def _edit_message(*_args, **_kwargs):
            raise RuntimeError("edit denied")

        async def _send_message(_context, *, text, **_kwargs):
            sent_messages.append(text)
            return SimpleNamespace(message_id=43)

        sent_messages: list[str] = []
        bot_app = SimpleNamespace(
            _edit_message=_edit_message,
            _send_message=_send_message,
        )
        service = _build_service(bot_app)
        session = SimpleNamespace(
            id="s-preview",
            send_lock=asyncio.Lock(),
            assistant_preview_message_id=42,
            assistant_preview_last_value="old",
        )

        await service._upsert_telegram_assistant_preview(
            session,
            {"kind": "telegram", "chat_id": 123},
            object(),
            "preview text",
        )

        assert sent_messages == []
        assert session.assistant_preview_message_id == 42
        assert session.assistant_preview_last_value == "old"

    caplog.set_level(logging.DEBUG, logger="sessions.session_run_service")
    asyncio.run(_run())

    assert "assistant preview edit failed; keeping current message session=s-preview" in caplog.text
    assert "chat_id=123" in caplog.text
    assert "message_id=42" in caplog.text


def test_session_run_service_retries_transient_preview_edit_in_place() -> None:
    async def _run() -> None:
        outcomes = iter((TelegramEditOutcome.RETRY, TelegramEditOutcome.UPDATED))
        edit_calls: list[dict[str, object]] = []
        sent_messages: list[str] = []

        async def _edit_message_outcome(_context, **kwargs):
            edit_calls.append(dict(kwargs))
            return next(outcomes)

        async def _send_message(_context, *, text, **_kwargs):
            sent_messages.append(text)
            return SimpleNamespace(message_id=43)

        bot_app = SimpleNamespace(
            _edit_message_outcome=_edit_message_outcome,
            _send_message=_send_message,
        )
        service = _build_service(bot_app)
        session = SimpleNamespace(
            id="s-preview",
            send_lock=asyncio.Lock(),
            assistant_preview_message_id=42,
            assistant_preview_last_value="old",
        )
        dest = {"kind": "telegram", "chat_id": 123}

        await service._upsert_telegram_assistant_preview(session, dest, object(), "preview 1")
        await service._upsert_telegram_assistant_preview(session, dest, object(), "preview 2")

        assert sent_messages == []
        assert [call["message_id"] for call in edit_calls] == [42, 42]
        assert session.assistant_preview_message_id == 42
        assert session.assistant_preview_last_value == "preview 2"

    asyncio.run(_run())


def test_session_run_service_replaces_only_permanently_missing_preview() -> None:
    async def _run() -> None:
        sent_messages: list[dict[str, object]] = []

        async def _edit_message_outcome(*_args, **_kwargs):
            return TelegramEditOutcome.REPLACE

        async def _send_message(_context, *, text, **kwargs):
            sent_messages.append({"text": text, **kwargs})
            return SimpleNamespace(message_id=43)

        bot_app = SimpleNamespace(
            _edit_message_outcome=_edit_message_outcome,
            _send_message=_send_message,
        )
        service = _build_service(bot_app)
        session = SimpleNamespace(
            id="s-preview",
            send_lock=asyncio.Lock(),
            assistant_preview_message_id=42,
            assistant_preview_last_value="old",
        )

        await service._upsert_telegram_assistant_preview(
            session,
            {"kind": "telegram", "chat_id": 123},
            object(),
            "preview text",
        )

        assert sent_messages == [
            {
                "text": "preview text",
                "chat_id": 123,
                "prefer_rich": False,
            }
        ]
        assert session.assistant_preview_message_id == 43
        assert session.assistant_preview_last_value == "preview text"

    asyncio.run(_run())


def test_session_run_service_does_not_repeat_uncertain_preview_send() -> None:
    async def _run() -> None:
        sent_messages: list[dict[str, object]] = []

        async def _send_message(_context, *, text, **kwargs):
            sent_messages.append({"text": text, **kwargs})
            return None

        service = _build_service(SimpleNamespace(_send_message=_send_message))
        session = SimpleNamespace(
            id="s-preview",
            send_lock=asyncio.Lock(),
            assistant_preview_message_id=None,
            assistant_preview_last_value=None,
        )
        dest = {"kind": "telegram", "chat_id": 123}

        await service._upsert_telegram_assistant_preview(session, dest, object(), "preview 1")
        await service._upsert_telegram_assistant_preview(session, dest, object(), "preview 2")

        assert sent_messages == [
            {
                "text": "preview 1",
                "chat_id": 123,
                "prefer_rich": False,
            }
        ]
        assert session.assistant_preview_message_id is None

    asyncio.run(_run())


def test_session_run_service_keeps_direct_messages_topic_on_preview_fallback() -> None:
    async def _run() -> None:
        sent_messages: list[dict[str, object]] = []
        rich_drafts: list[dict[str, object]] = []

        async def _send_message(_context, *, text, **kwargs):
            sent_messages.append({"text": text, **kwargs})
            return SimpleNamespace(message_id=43)

        async def _send_rich_message_draft(_context, **kwargs):
            rich_drafts.append(dict(kwargs))
            return True

        bot_app = SimpleNamespace(
            _send_message=_send_message,
            _send_rich_message_draft=_send_rich_message_draft,
        )
        service = _build_service(bot_app)
        session = SimpleNamespace(
            id="s-preview",
            send_lock=asyncio.Lock(),
            assistant_preview_message_id=None,
            assistant_preview_last_value=None,
        )

        await service._upsert_telegram_assistant_preview(
            session,
            {
                "kind": "telegram",
                "chat_id": 123,
                "direct_messages_topic_id": 777,
            },
            object(),
            "⏳ preview text",
        )

        assert rich_drafts == []
        assert sent_messages == [
            {
                "chat_id": 123,
                "direct_messages_topic_id": 777,
                "text": "⏳ preview text",
                "prefer_rich": False,
            }
        ]

    asyncio.run(_run())


def test_session_run_service_uses_legacy_send_and_edit_for_preview() -> None:
    async def _run() -> None:
        sent_messages: list[dict[str, object]] = []
        edited_messages: list[dict[str, object]] = []
        rich_drafts: list[dict[str, object]] = []

        async def _send_message(_context, *, text, **kwargs):
            sent_messages.append({"text": text, **kwargs})
            return SimpleNamespace(message_id=43)

        async def _edit_message(_context, **kwargs):
            edited_messages.append(dict(kwargs))
            return True

        async def _send_rich_message_draft(_context, **kwargs):
            rich_drafts.append(dict(kwargs))
            return True

        bot_app = SimpleNamespace(
            _send_message=_send_message,
            _edit_message=_edit_message,
            _send_rich_message_draft=_send_rich_message_draft,
        )
        service = _build_service(bot_app)
        session = SimpleNamespace(
            id="s-preview",
            send_lock=asyncio.Lock(),
            assistant_preview_message_id=None,
            assistant_preview_last_value=None,
            started_at=10.0,
        )
        dest = {"kind": "telegram", "chat_id": 123}

        await service._upsert_telegram_assistant_preview(session, dest, object(), "⏳ draft text")
        await service._upsert_telegram_assistant_preview(session, dest, object(), "⏳ updated draft")

        assert rich_drafts == []
        assert sent_messages == [
            {
                "text": "⏳ draft text",
                "chat_id": 123,
                "prefer_rich": False,
            }
        ]
        assert edited_messages == [
            {
                "chat_id": 123,
                "message_id": 43,
                "text": "⏳ updated draft",
                "md2": True,
                "prefer_rich": False,
            }
        ]

    asyncio.run(_run())


def test_session_run_service_logs_framework_output_policy_fallback(caplog) -> None:
    class _Mode:
        def framework_sends_output(self) -> bool:
            raise RuntimeError("policy denied")

    bot_app = SimpleNamespace(_shutdown_in_progress=False)
    service = _build_service(bot_app)
    session = SimpleNamespace(id="s-policy")

    caplog.set_level(logging.DEBUG, logger="sessions.session_run_service")
    assert service._mode_framework_sends_output(_Mode(), session=session, mode_id="agent") is True

    assert "legacy_fallback: failed to resolve framework output policy session=s-policy" in caplog.text
    assert "mode=agent" in caplog.text
