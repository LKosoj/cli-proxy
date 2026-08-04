from __future__ import annotations

import asyncio
from collections import deque
from types import SimpleNamespace

import pytest

import bot as bot_module
import app.services.lifecycle_service as lifecycle_module
from app.services.cli_backends.tmux_backend import TmuxExecutionBackend, TmuxRecoveryRequest
from app.services.lifecycle_service import build_post_init
from bot import BotApp
from sessions.session_run_service import SessionRunService


def _run_service(events: list[tuple]) -> SessionRunService:
    async def _send_output(_session, dest, output, _context):
        events.append(("output", str(output), dict(dest)))

    bot_app = SimpleNamespace(
        _shutdown_in_progress=False,
        _last_delivery_error=None,
        config=None,
        send_output=_send_output,
        _send_message=lambda *_args, **_kwargs: asyncio.sleep(0),
    )
    return SessionRunService(
        bot_app=bot_app,
        persist_sessions=lambda: None,
        persist_session=lambda _chat_id, _session_id: True,
        mode_tasks_list=lambda **_kwargs: [],
        mode_tasks_create=lambda **_kwargs: None,
        log_cli_dialog=lambda *_args, **_kwargs: None,
        reset_session_fields_like_sessions_reset=lambda *_args, **_kwargs: None,
    )


def _recovery_session() -> SimpleNamespace:
    async def _forbidden_run_prompt(*_args, **_kwargs):
        raise AssertionError("recovery must not submit the original prompt again")

    async def _recover(request):
        assert request.request_id == "recover-codex"
        return "recovered codex answer"

    return SimpleNamespace(
        id="s1",
        chat_id=42,
        name="codex@repo",
        workdir="/tmp/repo",
        tool=SimpleNamespace(name="codex"),
        config=None,
        cli=SimpleNamespace(active_cli="codex", pending_switch_notice=None),
        modes=SimpleNamespace(active_mode=None, analyst_mode="spec"),
        orchestrator=SimpleNamespace(enabled=False, pending_input=None, last_mode_output=None, last_mode_id=None),
        conversation_scope=None,
        run_lock=asyncio.Lock(),
        send_lock=asyncio.Lock(),
        queue=deque(
            [
                {
                    "text": "queued next",
                    "dest": {"kind": "telegram", "chat_id": 42, "message_thread_id": 9},
                }
            ]
        ),
        busy=False,
        started_at=0.0,
        last_output_ts=0.0,
        last_tick_ts=None,
        last_tick_value=None,
        last_assistant_text_ts=None,
        last_assistant_text_value=None,
        assistant_preview_message_id=None,
        assistant_preview_last_value=None,
        tick_seen=0,
        headless_forced_stop=None,
        state_summary=None,
        state_updated_at=None,
        last_tmux_request_id=None,
        run_prompt=_forbidden_run_prompt,
        recover_tmux_request=_recover,
    )


@pytest.mark.asyncio
async def test_recovered_tmux_result_is_delivered_before_queue_continues(monkeypatch):
    events: list[tuple] = []
    service = _run_service(events)
    session = _recovery_session()
    recovery = TmuxRecoveryRequest(
        request_id="recover-codex",
        started_at=10.0,
        offset=0,
        prompt="original request",
        dest={"kind": "telegram", "chat_id": 42, "message_thread_id": 9},
    )
    delivered: list[str] = []

    monkeypatch.setattr(
        TmuxExecutionBackend,
        "mark_request_delivered",
        lambda _session, request_id: delivered.append(str(request_id)) or True,
    )

    def _start_prompt_task(_session, prompt, dest, _context, *, task_name="run_prompt"):
        events.append(("queue", str(prompt), dict(dest), str(task_name)))
        return True

    service.start_prompt_task = _start_prompt_task

    await service.run_prompt(
        session,
        recovery.prompt,
        recovery.dest,
        context=object(),
        recovery_request=recovery,
    )

    assert events == [
        ("output", "recovered codex answer", recovery.dest),
        (
            "queue",
            "queued next",
            {**recovery.dest, "user_id": None},
            "run_prompt.queue_next",
        ),
    ]
    assert delivered == ["recover-codex"]
    assert list(session.queue) == []


@pytest.mark.asyncio
async def test_recovered_tmux_send_failure_keeps_pending_queue(monkeypatch):
    events: list[tuple] = []
    service = _run_service(events)
    session = _recovery_session()
    recovery = TmuxRecoveryRequest(
        request_id="recover-codex",
        started_at=10.0,
        offset=0,
        prompt="original request",
        dest={"kind": "telegram", "chat_id": 42, "message_thread_id": 9},
    )

    async def _fail_send(*_args, **_kwargs):
        raise RuntimeError("telegram unavailable")

    service.bot_app.send_output = _fail_send
    marked: list[str] = []
    monkeypatch.setattr(
        TmuxExecutionBackend,
        "mark_request_delivered",
        lambda _session, request_id: marked.append(str(request_id)) or True,
    )
    service.start_prompt_task = lambda *_args, **_kwargs: events.append(("queue",)) or True

    await service.run_prompt(
        session,
        recovery.prompt,
        recovery.dest,
        context=object(),
        recovery_request=recovery,
    )

    assert marked == []
    assert events == []
    assert [item["text"] for item in session.queue] == ["queued next"]


@pytest.mark.asyncio
async def test_observed_tmux_silence_is_not_reported_to_chat(monkeypatch):
    events: list[tuple] = []
    service = _run_service(events)
    session = _recovery_session()

    async def _recover_silent(request):
        assert request.observe is True
        return "  \n"

    session.recover_tmux_request = _recover_silent
    recovery = TmuxRecoveryRequest(
        request_id="observe-codex",
        started_at=10.0,
        offset=0,
        prompt="original request",
        dest={"kind": "telegram", "chat_id": 42, "message_thread_id": 9},
        observe=True,
    )
    delivered: list[str] = []
    monkeypatch.setattr(
        TmuxExecutionBackend,
        "mark_request_delivered",
        lambda _session, request_id: delivered.append(str(request_id)) or True,
    )

    def _start_prompt_task(_session, prompt, dest, _context, *, task_name="run_prompt"):
        events.append(("queue", str(prompt), str(task_name)))
        return True

    service.start_prompt_task = _start_prompt_task

    await service.run_prompt(
        session,
        recovery.prompt,
        recovery.dest,
        context=object(),
        recovery_request=recovery,
    )

    assert [event for event in events if event[0] == "output"] == []
    assert events == [("queue", "queued next", "run_prompt.queue_next")]
    assert delivered == ["observe-codex"]
    assert list(session.queue) == []


@pytest.mark.asyncio
async def test_startup_reconciliation_schedules_all_supported_tmux_clis(monkeypatch):
    events: list[tuple] = []
    service = _run_service(events)
    sessions = {}
    for cli_name in ("claude", "codex", "grok", "qwen"):
        session = _recovery_session()
        session.id = f"s-{cli_name}"
        session.tool.name = cli_name
        session.cli.active_cli = cli_name
        session.busy = False
        sessions[session.id] = session
    service.bot_app.manager = SimpleNamespace(sessions_by_chat={42: sessions})
    service.bot_app.build_telegram_reply_dest = lambda _session, chat_id, **_kwargs: {
        "kind": "telegram",
        "chat_id": int(chat_id),
    }

    monkeypatch.setattr("sessions.session_run_service.get_session_execution_backend", lambda _session: "tmux")

    async def _get_recovery(_backend, session):
        cli_name = session.tool.name
        return TmuxRecoveryRequest(
            request_id=f"recover-{cli_name}",
            started_at=10.0,
            offset=0,
            prompt=f"prompt-{cli_name}",
            dest={"kind": "telegram", "chat_id": 42},
        )

    monkeypatch.setattr(TmuxExecutionBackend, "get_recovery_request", _get_recovery)
    scheduled: list[tuple[str, str]] = []

    def _start_session_task(session, *, coro, name):
        scheduled.append((session.tool.name, str(name)))
        coro.close()
        return True

    service.start_session_task = _start_session_task

    recovered = await service.recover_tmux_sessions(context=object())

    assert recovered == 4
    assert scheduled == [
        ("claude", "tmux_recovery:recover-claude"),
        ("codex", "tmux_recovery:recover-codex"),
        ("grok", "tmux_recovery:recover-grok"),
        ("qwen", "tmux_recovery:recover-qwen"),
    ]
    assert all(session.busy for session in sessions.values())


@pytest.mark.asyncio
async def test_tmux_reread_detaches_monitor_without_interrupting_cli(monkeypatch):
    events: list[tuple] = []
    service = _run_service(events)
    session = _recovery_session()
    session._preserve_tmux_on_shutdown = False
    cancelled: list[bool] = []

    async def _cancel_session(*, session_id, timeout_s):
        cancelled.append(bool(getattr(session, "_preserve_tmux_on_shutdown", False)))
        return 1

    cleared: list[str] = []
    service.bot_app.mode_session_control = SimpleNamespace(cancel_session=_cancel_session)
    service.bot_app._clear_pending_questions = lambda **kwargs: cleared.append(kwargs.get("session_id")) or 1
    service.bot_app.build_telegram_reply_dest = lambda _session, chat_id, **_kwargs: {
        "kind": "telegram",
        "chat_id": int(chat_id),
    }
    monkeypatch.setattr("sessions.session_run_service.get_session_execution_backend", lambda _session: "tmux")

    async def _get_recovery(_backend, _session):
        return TmuxRecoveryRequest(
            request_id="recover-codex",
            started_at=10.0,
            offset=0,
            prompt="original request",
            dest={"kind": "telegram", "chat_id": 42},
        )

    monkeypatch.setattr(TmuxExecutionBackend, "get_recovery_request", _get_recovery)
    scheduled: list[str] = []

    def _start_session_task(_session, *, coro, name):
        scheduled.append(str(name))
        coro.close()
        return True

    service.start_session_task = _start_session_task

    outcome = await service.reread_tmux_output(session, context=object())

    assert outcome == "started"
    # Монитор снимается с взведённым флагом сохранения tmux: Ctrl+C в pane не уходит.
    assert cancelled == [True]
    assert session._preserve_tmux_on_shutdown is False
    assert cleared == ["s1"]
    assert scheduled == ["tmux_recovery:recover-codex"]
    assert session.busy is True


@pytest.mark.asyncio
async def test_tmux_reread_observes_live_pane_after_request_was_delivered(monkeypatch):
    events: list[tuple] = []
    service = _run_service(events)
    session = _recovery_session()

    async def _cancel_session(*, session_id, timeout_s):
        return 1

    service.bot_app.mode_session_control = SimpleNamespace(cancel_session=_cancel_session)
    service.bot_app.build_telegram_reply_dest = lambda _session, chat_id, **_kwargs: {
        "kind": "telegram",
        "chat_id": int(chat_id),
    }
    monkeypatch.setattr("sessions.session_run_service.get_session_execution_backend", lambda _session: "tmux")

    async def _no_recovery(_backend, _session):
        return None

    async def _observe(_backend, _session):
        return TmuxRecoveryRequest(
            request_id="observe-codex",
            started_at=10.0,
            offset=4096,
            prompt="",
            dest={"kind": "telegram", "chat_id": 42},
        )

    monkeypatch.setattr(TmuxExecutionBackend, "get_recovery_request", _no_recovery)
    monkeypatch.setattr(TmuxExecutionBackend, "build_observe_request", _observe)
    scheduled: list[str] = []

    def _start_session_task(_session, *, coro, name):
        scheduled.append(str(name))
        coro.close()
        return True

    service.start_session_task = _start_session_task

    outcome = await service.reread_tmux_output(session, context=object())

    assert outcome == "started"
    assert scheduled == ["tmux_recovery:observe-codex"]
    assert session.busy is True


@pytest.mark.asyncio
async def test_tmux_reread_without_live_pane_does_not_cancel_tasks(monkeypatch):
    events: list[tuple] = []
    service = _run_service(events)
    session = _recovery_session()
    cancelled: list[str] = []

    async def _cancel_session(*, session_id, timeout_s):
        cancelled.append(str(session_id))
        return 1

    service.bot_app.mode_session_control = SimpleNamespace(cancel_session=_cancel_session)
    monkeypatch.setattr("sessions.session_run_service.get_session_execution_backend", lambda _session: "tmux")

    async def _no_recovery(_backend, _session):
        return None

    monkeypatch.setattr(TmuxExecutionBackend, "get_recovery_request", _no_recovery)
    monkeypatch.setattr(TmuxExecutionBackend, "build_observe_request", _no_recovery)

    assert await service.reread_tmux_output(session, context=object()) == "no_request"
    assert cancelled == []


@pytest.mark.asyncio
async def test_tmux_reread_rejects_non_tmux_session(monkeypatch):
    service = _run_service([])
    monkeypatch.setattr("sessions.session_run_service.get_session_execution_backend", lambda _session: "headless")

    assert await service.reread_tmux_output(_recovery_session(), context=object()) == "not_tmux"


@pytest.mark.asyncio
async def test_bot_shutdown_marks_sessions_for_tmux_preservation(monkeypatch):
    events: list[tuple] = []

    class FakeSession:
        def __init__(self):
            self.id = "s1"
            self._preserve_tmux_on_shutdown = False

        def interrupt(self):
            events.append(("interrupt", self._preserve_tmux_on_shutdown))

        def close(self, *, preserve_tmux=False):
            events.append(("close", bool(preserve_tmux)))

    async def _cancel_session(**_kwargs):
        return 0

    monkeypatch.setattr(bot_module, "Session", FakeSession)
    app = BotApp.__new__(BotApp)
    app._shutdown_in_progress = False
    app.manager = SimpleNamespace(sessions_by_chat={42: {"s1": FakeSession()}})
    app.mode_tasks = SimpleNamespace(cancel_session=_cancel_session)
    app.notification_queue_service = None
    app.system_event_bus = None
    app._tool_registry = None

    await app.shutdown_runtime()

    assert app._shutdown_in_progress is True
    assert events == [("interrupt", True), ("close", True)]


@pytest.mark.asyncio
async def test_post_init_starts_tmux_reconciliation_after_notification_queue(monkeypatch):
    events: list[str] = []

    class _CapabilityChecker:
        def __init__(self, _config):
            pass

        async def ensure_supported(self, _bot):
            events.append("capabilities")

    async def _mark(name):
        events.append(name)

    monkeypatch.setattr(lifecycle_module, "ThreadModeCapabilityChecker", _CapabilityChecker)
    bot_app = SimpleNamespace(
        config=object(),
        set_bot_commands=lambda _application: _mark("commands"),
        mcp=SimpleNamespace(start=lambda: _mark("mcp")),
        session_thread_manager=None,
        notification_queue_service=SimpleNamespace(start=lambda: _mark("notifications")),
        session_management=SimpleNamespace(recover_tmux_sessions=lambda _context: _mark("tmux_recovery")),
        scheduler_service=None,
        mode_launch_adapter=None,
        webhook_ingress_service=None,
        miniapp_server=None,
        shared_http_ingress=None,
        handlers=SimpleNamespace(notify_pending_selfupdate=lambda _application: _mark("selfupdate")),
        _task_deadline_checker_task=object(),
    )
    application = SimpleNamespace(bot=object())

    await build_post_init(bot_app)(application)

    assert events.index("notifications") < events.index("tmux_recovery")
