import asyncio
import contextlib
from types import SimpleNamespace

import pytest

from bot import BotApp
from config import AppConfig, DefaultsConfig, MCPConfig, TelegramConfig, ToolConfig
from session import session_runtime_uid


def _build_app(
    tmp_path,
    *,
    dummy_enabled: bool = True,
    include_backup: bool = False,
    backup_enabled: bool = True,
) -> BotApp:
    tools = {
        "dummy": ToolConfig(
            name="dummy",
            mode="headless",
            cmd=["bash", "-lc", "cat"],
            enabled=dummy_enabled,
        )
    }
    if include_backup:
        tools["backup"] = ToolConfig(
            name="backup",
            mode="headless",
            cmd=["bash", "-lc", "cat"],
            enabled=backup_enabled,
        )
    cfg = AppConfig(
        telegram=TelegramConfig(token="", whitelist_chat_ids=[1, 2], admlist_chat_ids=[1, 2]),
        tools=tools,
        defaults=DefaultsConfig(
            workdir=str(tmp_path),
            state_path=str(tmp_path / "state.json"),
            toolhelp_path=str(tmp_path / "toolhelp.json"),
            log_path=str(tmp_path / "bot.log"),
            openai_api_key="k",
            openai_model="m",
        ),
        mcp=MCPConfig(enabled=False),
        mcp_clients=[],
        presets=[],
        path=str(tmp_path / "config.yaml"),
    )
    return BotApp(cfg)


def test_run_prompt_raw_executes_selected_session(tmp_path):
    async def _run() -> None:
        app = _build_app(tmp_path)
        session = app.manager.create(1, "dummy", str(tmp_path))

        called = {"count": 0, "prompt": ""}

        async def _fake_run_prompt(prompt: str, **_kwargs):
            called["count"] += 1
            called["prompt"] = str(prompt)
            return f"OUT:{prompt}"

        session.run_prompt = _fake_run_prompt

        out = await app.run_prompt_raw("hello", session_id=session.id)
        assert out == "OUT:hello"
        assert called["count"] == 1
        assert called["prompt"] == "hello"

    asyncio.run(_run())


def test_run_prompt_raw_requires_session_id_when_multiple_active(tmp_path):
    async def _run() -> None:
        app = _build_app(tmp_path)
        app.manager.create(1, "dummy", str(tmp_path))
        app.manager.create(2, "dummy", str(tmp_path))
        try:
            await app.run_prompt_raw("hello")
            assert False, "expected RuntimeError"
        except RuntimeError as e:
            assert "multiple_sessions_require_session_id" in str(e)

    asyncio.run(_run())


def test_run_prompt_raw_scoped_by_chat_id(tmp_path):
    async def _run() -> None:
        app = _build_app(tmp_path)
        s1 = app.manager.create(1, "dummy", str(tmp_path))
        app.manager.create(2, "dummy", str(tmp_path))

        called = {"count": 0}

        async def _fake_run_prompt(prompt: str, **_kwargs):
            called["count"] += 1
            return f"OUT:{prompt}"

        s1.run_prompt = _fake_run_prompt

        out = await app.run_prompt_raw("hello", session_id=s1.id, chat_id=1)
        assert out == "OUT:hello"
        assert called["count"] == 1

        try:
            await app.run_prompt_raw("hello", session_id=s1.id, chat_id=3)
            assert False, "expected RuntimeError"
        except RuntimeError as e:
            assert "session_not_found" in str(e)

    asyncio.run(_run())


@pytest.mark.asyncio
async def test_direct_prompt_task_is_cancelled_by_session_scoped_cancel(tmp_path):
    app = _build_app(tmp_path)
    session = app.manager.create(1, "dummy", str(tmp_path))
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def _fake_run_prompt(prompt: str, **_kwargs):
        _ = prompt
        started.set()
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    session.run_prompt = _fake_run_prompt
    try:
        await app.input_dispatch_service.handle_cli_input(session, "hello", chat_id=1, context=object())
        await asyncio.wait_for(started.wait(), timeout=1.0)

        cancelled_count = await app.mode_session_control.cancel_session(
            session_id=session_runtime_uid(session),
            timeout_s=0.5,
        )

        await asyncio.wait_for(cancelled.wait(), timeout=1.0)
        assert cancelled_count == 1
        assert app.mode_tasks.list(session_uid=session_runtime_uid(session), mode_id="__session__") == []
    finally:
        app.shutdown_html_process_pool()


@pytest.mark.asyncio
async def test_run_prompt_raw_is_cancelled_by_session_scoped_cancel(tmp_path):
    app = _build_app(tmp_path)
    session = app.manager.create(1, "dummy", str(tmp_path))
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def _fake_run_prompt(prompt: str, **_kwargs):
        _ = prompt
        started.set()
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    session.run_prompt = _fake_run_prompt
    task = asyncio.create_task(app.run_prompt_raw("hello", session_id=session.id))
    try:
        await asyncio.wait_for(started.wait(), timeout=1.0)

        cancelled_count = await app.mode_session_control.cancel_session(
            session_id=session_runtime_uid(session),
            timeout_s=0.5,
        )

        assert cancelled_count == 1
        with pytest.raises(asyncio.CancelledError):
            await task
        assert cancelled.is_set()
        assert app.mode_tasks.list(session_uid=session_runtime_uid(session), mode_id="__session__") == []
    finally:
        if not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        app.shutdown_html_process_pool()


@pytest.mark.asyncio
async def test_direct_run_prompt_notifies_and_uses_fallback_cli_after_restore(tmp_path):
    app = _build_app(tmp_path, dummy_enabled=True, include_backup=True, backup_enabled=True)
    created = app.manager.create(1, "dummy", str(tmp_path))
    session_id = created.id
    app.shutdown_html_process_pool()

    restored = _build_app(tmp_path, dummy_enabled=False, include_backup=True, backup_enabled=True)
    session = restored.manager.get(1, session_id)
    assert session is not None

    sent_messages: list[dict[str, object]] = []

    async def _send_message(_context, **kwargs):
        sent_messages.append(dict(kwargs))
        return SimpleNamespace(message_id=len(sent_messages))

    restored._send_message = _send_message

    async def _fake_run_prompt(prompt: str, **_kwargs):
        return f"OUT:{session.tool.name}:{prompt}"

    session.run_prompt = _fake_run_prompt  # type: ignore[assignment]

    await restored.session_management.run_prompt(
        session,
        "hello",
        {"kind": "telegram", "chat_id": 1, "message_thread_id": 77},
        object(),
    )
    await asyncio.sleep(0)

    assert session.active_cli == "backup"
    assert session.tool.name == "backup"
    switch_message = next(
        msg for msg in sent_messages if "Переключаю на backup" in str(msg.get("text") or "")
    )
    assert switch_message["chat_id"] == 1
    assert switch_message["message_thread_id"] == 77
    restored.shutdown_html_process_pool()


@pytest.mark.asyncio
async def test_run_prompt_telegram_assistant_preview_clears_rich_draft_before_final_output(tmp_path, monkeypatch):
    app = _build_app(tmp_path)
    app.config.defaults.assistant_preview_enabled = True
    session = app.manager.create(1, "dummy", str(tmp_path))

    import sessions.session_run_service as run_service_mod

    preview_messages: list[dict[str, object]] = []
    preview_edits: list[dict[str, object]] = []
    preview_deletes: list[tuple[int, int]] = []
    rich_drafts: list[dict[str, object]] = []
    final_outputs: list[str] = []
    events: list[str] = []
    output_sent = asyncio.Event()

    async def _watch_preview(_session, *, emit_update, stop_event, poll_interval_sec=0.35, **kwargs):
        _ = poll_interval_sec
        assert kwargs["refresh_interval_sec"] <= 25.0
        await emit_update("Черновик ответа")
        await stop_event.wait()

    monkeypatch.setattr(run_service_mod, "watch_session_assistant_preview", _watch_preview)

    async def _send_message(_context, **kwargs):
        preview_messages.append(dict(kwargs))
        return SimpleNamespace(message_id=501)

    async def _edit_message(_context, *, chat_id: int, message_id: int, text: str, **_kwargs):
        preview_edits.append({"chat_id": chat_id, "message_id": message_id, "text": text})
        return True

    async def _delete_message(_context, chat_id: int, message_id: int):
        events.append("delete_preview")
        preview_deletes.append((chat_id, message_id))
        return True

    async def _send_rich_message_draft(_context, *, draft_id: int, rich_message: dict, **kwargs):
        events.append("rich_draft")
        rich_drafts.append({"draft_id": draft_id, "rich_message": rich_message, **kwargs})
        return True

    async def _send_output(_session, _dest, output, _context, **_kwargs):
        events.append("send_output")
        final_outputs.append(str(output))
        output_sent.set()

    app._send_message = _send_message
    app._edit_message = _edit_message
    app._delete_message = _delete_message
    app._send_rich_message_draft = _send_rich_message_draft
    app.send_output = _send_output

    async def _fake_run_prompt(prompt: str, **_kwargs):
        _ = prompt
        session.last_assistant_text_value = "Финальный черновик ответа"
        return "FINAL OUTPUT"

    session.run_prompt = _fake_run_prompt  # type: ignore[assignment]

    await app.session_management.run_prompt(
        session,
        "hello",
        {"kind": "telegram", "chat_id": 1},
        object(),
    )
    await asyncio.wait_for(output_sent.wait(), timeout=1.0)

    assert final_outputs == ["FINAL OUTPUT"]
    assert rich_drafts
    assert rich_drafts[0]["rich_message"]["markdown"].startswith("00:00\n\nЧерновик ответа")
    assert preview_messages == []
    assert preview_edits == []
    assert preview_deletes == []
    assert events == ["rich_draft", "send_output"]
    app.shutdown_html_process_pool()
