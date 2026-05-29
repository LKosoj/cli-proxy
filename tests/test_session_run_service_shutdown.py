import asyncio
import logging
from collections import deque
from types import SimpleNamespace

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


def test_session_run_service_logs_assistant_preview_edit_fallback(caplog) -> None:
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

        assert sent_messages == ["preview text"]
        assert session.assistant_preview_message_id == 43
        assert session.assistant_preview_last_value == "preview text"

    caplog.set_level(logging.DEBUG, logger="sessions.session_run_service")
    asyncio.run(_run())

    assert "legacy_fallback: assistant preview edit failed session=s-preview" in caplog.text
    assert "chat_id=123" in caplog.text
    assert "message_id=42" in caplog.text


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
