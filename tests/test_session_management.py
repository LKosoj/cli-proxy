import asyncio
import logging
import time
import types
from collections import deque

import pytest

from modes.sdk.session_busy import is_session_busy
from sessions.conversation_scope import ConversationScope
from sessions.queue_item import SessionQueueItem
from sessions.session_output_service import SessionOutputService
from sessions.session_run_service import SessionRunService
from session import SessionManager


class _ModeStub:
    def __init__(self, mode_id: str) -> None:
        self.mode_id = str(mode_id)
        self.calls: list[str] = []

    def pre_run_reset_mode_id(self):
        if self.mode_id == "analyst":
            return self.mode_id
        return None

    def framework_sends_output(self) -> bool:
        return False

    async def run_pipeline(self, *, session, user_text, bot_app, context, dest):
        _ = session, bot_app, context, dest
        self.calls.append(str(user_text))
        return f"{self.mode_id}:{user_text}"


class _ModeRegistryStub:
    def __init__(self, modes: dict[str, _ModeStub]) -> None:
        self._modes = dict(modes or {})

    def get(self, mode_id: str):
        return self._modes.get(str(mode_id or ""))


class _SessionStub(types.SimpleNamespace):
    def is_active_by_tick(self, now=None, window_sec: int = 3) -> bool:
        ts = getattr(self, "last_tick_ts", None)
        if not ts:
            return False
        now_ts = time.time() if now is None else float(now)
        return (now_ts - float(ts)) <= int(window_sec)


def _make_prompt_drain_service(started: list[dict], persisted: list[tuple[int, str]]) -> SessionRunService:
    def _mode_tasks_create(*, session_id: str, mode_id: str, coro, name: str) -> None:
        _ = session_id, mode_id, name
        coro.close()

    async def _send_output(*_args, **_kwargs):
        return None

    bot_app = types.SimpleNamespace(
        config=None,
        send_output=_send_output,
        _send_message=(lambda *_a, **_k: asyncio.sleep(0)),
    )
    service = SessionRunService(
        bot_app=bot_app,
        persist_sessions=(lambda: None),
        persist_session=(lambda chat_id, session_id: persisted.append((int(chat_id), str(session_id))) or True),
        mode_tasks_list=(lambda **_kwargs: []),
        mode_tasks_create=_mode_tasks_create,
        log_cli_dialog=(lambda *_args, **_kwargs: None),
        reset_session_fields_like_sessions_reset=(lambda *_args, **_kwargs: None),
    )

    def _start_prompt_task(session, prompt: str, dest: dict, context, *, task_name: str = "run_prompt") -> bool:
        _ = context
        started.append(
            {
                "session_id": str(getattr(session, "id", "") or ""),
                "text": str(prompt or ""),
                "dest": dict(dest or {}),
                "task_name": str(task_name or ""),
            }
        )
        return True

    service.start_prompt_task = _start_prompt_task
    return service


def _make_prompt_session(tmp_path, queue) -> _SessionStub:
    ran_prompts: list[dict] = []

    async def _run_prompt(prompt: str, **kwargs):
        ran_prompts.append({"prompt": str(prompt or ""), "kwargs": dict(kwargs or {})})
        return "ok"

    return _SessionStub(
        id="s1",
        chat_id=1,
        name="s1",
        workdir=str(tmp_path),
        tool=types.SimpleNamespace(name="dummy"),
        config=None,
        run_lock=asyncio.Lock(),
        send_lock=asyncio.Lock(),
        queue=deque(queue),
        busy=False,
        started_at=0.0,
        last_output_ts=0.0,
        last_tick_ts=None,
        last_tick_value=None,
        tick_seen=0,
        cli=types.SimpleNamespace(active_cli="dummy", pending_switch_notice=None),
        modes=types.SimpleNamespace(active_mode=None, analyst_mode="spec"),
        orchestrator=types.SimpleNamespace(
            enabled=False,
            pending_input=None,
            last_mode_output=None,
            last_mode_id=None,
        ),
        state_summary=None,
        state_updated_at=None,
        run_prompt=_run_prompt,
        ran_prompts=ran_prompts,
    )


def _make_persistable_session(queue) -> _SessionStub:
    return _SessionStub(
        id="s1",
        chat_id=1,
        name="s1",
        workdir="/tmp/work",
        tool=types.SimpleNamespace(name="dummy"),
        conversation_scope=ConversationScope.from_parts(1),
        queue=deque(queue),
        cli=types.SimpleNamespace(
            active_cli="dummy",
            resume_tokens={},
            cli_work_type=None,
            auto_commands_ran=False,
        ),
        git=types.SimpleNamespace(
            busy=False,
            conflict=False,
            conflict_files=[],
            conflict_kind=None,
        ),
        modes=types.SimpleNamespace(
            active_mode=None,
            analyst_mode="spec",
            analyst_template_id="default",
            manager_quiet_mode=False,
            agent_memory={},
            ssh_remote_enabled=False,
            remote_control_enabled=False,
            remote_control_host_alias=None,
        ),
        orchestrator=types.SimpleNamespace(
            enabled=False,
            pending_input=None,
            last_mode_output=None,
            last_mode_id=None,
        ),
        state_summary=None,
        state_updated_at=None,
        project_root=None,
    )


async def _wait_until_three_flags_released(session: _SessionStub, *, timeout_s: float = 6.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + float(timeout_s)
    while loop.time() < deadline:
        run_lock = getattr(session, "run_lock", None)
        is_active_by_tick = getattr(session, "is_active_by_tick", None)
        tick_active = False
        if callable(is_active_by_tick):
            try:
                last_tick_ts = getattr(session, "last_tick_ts", None)
                if last_tick_ts is not None:
                    tick_active = bool(is_active_by_tick(now=float(last_tick_ts) + 4.0))
                else:
                    tick_active = bool(is_active_by_tick())
            except TypeError:
                tick_active = bool(is_active_by_tick())
        probe_session = types.SimpleNamespace(
            busy=bool(getattr(session, "busy", False)),
            is_active_by_tick=(lambda: bool(tick_active)),
        )
        released = (
            not bool(getattr(session, "busy", False))
            and not bool(run_lock and run_lock.locked())
            and not tick_active
            and not is_session_busy(probe_session, run_lock)
        )
        if released:
            return
        await asyncio.sleep(0.01)
    raise AssertionError("timeout waiting for busy/run_lock/tick flags release")


@pytest.mark.asyncio
async def test_run_prompt_drain_normalizes_legacy_string_queue_with_fallback_dest(tmp_path) -> None:
    started: list[dict] = []
    persisted: list[tuple[int, str]] = []
    service = _make_prompt_drain_service(started, persisted)
    restored_queue = SessionManager._normalize_queue_items_for_persistence(["legacy queued"])
    session = _make_prompt_session(tmp_path, restored_queue)

    await service.run_prompt(
        session,
        "current",
        {"kind": "telegram", "chat_id": 101, "message_thread_id": 7},
        context=None,
    )

    assert started == [
        {
            "session_id": "s1",
            "text": "legacy queued",
            "dest": {"kind": "telegram", "chat_id": 101, "user_id": None, "message_thread_id": 7},
            "task_name": "run_prompt.queue_next",
        }
    ]
    assert list(session.queue) == []
    assert persisted == [(1, "s1")]


def test_session_queue_item_preserves_nested_dest_after_persist_restore_boundary() -> None:
    dataclass_dest = {"kind": "telegram", "chat_id": 11, "nested": {"thread": {"id": 5}}}
    dict_dest = {"kind": "desktop", "session_uid": "desktop:s2", "nested": {"panel": "main"}}
    session = _make_persistable_session(
        [
            SessionQueueItem(text="from dataclass", dest=dataclass_dest, created_at=10.5),
            {"text": "from dict", "dest": dict_dest},
        ]
    )

    payload = SessionManager._serialize_session_payload(session)
    restored_queue = SessionManager._normalize_queue_items_for_persistence(payload["queue"])

    assert restored_queue == [
        {
            "text": "from dataclass",
            "dest": {"kind": "telegram", "chat_id": 11, "nested": {"thread": {"id": 5}}},
            "created_at": 10.5,
        },
        {
            "text": "from dict",
            "dest": {"kind": "desktop", "session_uid": "desktop:s2", "nested": {"panel": "main"}},
        },
    ]


@pytest.mark.asyncio
async def test_run_prompt_drains_two_queued_inputs_with_independent_dest(tmp_path) -> None:
    started: list[dict] = []
    persisted: list[tuple[int, str]] = []
    service = _make_prompt_drain_service(started, persisted)
    session = _make_prompt_session(
        tmp_path,
        [
            {"text": "first queued", "dest": {"kind": "telegram", "chat_id": 201, "nested": {"run": "first"}}},
            {"text": "second queued", "dest": {"kind": "telegram", "chat_id": 202, "nested": {"run": "second"}}},
        ],
    )

    await service.run_prompt(session, "current", {"kind": "telegram", "chat_id": 999}, context=None)
    await service.run_prompt(session, "after first", started[0]["dest"], context=None)

    assert [(item["text"], item["dest"]) for item in started] == [
        ("first queued", {"kind": "telegram", "chat_id": 201, "nested": {"run": "first"}, "user_id": None}),
        ("second queued", {"kind": "telegram", "chat_id": 202, "nested": {"run": "second"}, "user_id": None}),
    ]
    assert list(session.queue) == []
    assert persisted == [(1, "s1"), (1, "s1")]


@pytest.mark.asyncio
async def test_run_scoped_state_isolated_between_analyst_and_agent_runs(tmp_path) -> None:
    analyst_mode = _ModeStub("analyst")
    agent_mode = _ModeStub("agent")
    reset_calls: list[str] = []
    sent_outputs: list[str] = []

    async def _send_output(_session, _dest, output, _context, **_kwargs):
        sent_outputs.append(str(output or ""))

    bot_app = types.SimpleNamespace(
        mode_registry=_ModeRegistryStub({"analyst": analyst_mode, "agent": agent_mode}),
        config=types.SimpleNamespace(defaults=types.SimpleNamespace(summary_max_chars=2000)),
        send_output=_send_output,
        _send_message=(lambda *_a, **_k: asyncio.sleep(0)),
    )
    service = SessionRunService(
        bot_app=bot_app,
        persist_sessions=(lambda: None),
        mode_tasks_list=(lambda **_kwargs: []),
        mode_tasks_create=(lambda **_kwargs: None),
        log_cli_dialog=(lambda *_args, **_kwargs: None),
        reset_session_fields_like_sessions_reset=(
            lambda _session, *, preserve_mode_id=None: reset_calls.append(str(preserve_mode_id or ""))
        ),
    )

    session = _SessionStub(
        id="s1",
        chat_id=1,
        name="s1",
        workdir=str(tmp_path),
        tool=types.SimpleNamespace(name="dummy"),
        config=types.SimpleNamespace(defaults=types.SimpleNamespace(state_path=str(tmp_path / "state.json"))),
        run_lock=asyncio.Lock(),
        queue=deque(),
        busy=False,
        started_at=0.0,
        last_output_ts=0.0,
        last_tick_ts=123.0,
        last_tick_value="stale tick before first run",
        tick_seen=7,
        runtime_progress_events=[{"mode_id": "stale", "phase": "old"}],
        runtime_progress_last_event={"mode_id": "stale", "phase": "old"},
        _runtime_progress_last_sig="stale",
        _runtime_progress_last_ts=123.0,
        modes=types.SimpleNamespace(active_mode=None, analyst_mode="spec"),
        orchestrator=types.SimpleNamespace(
            enabled=False,
            pending_input=None,
            last_mode_output=None,
            last_mode_id=None,
        ),
        state_summary=None,
        state_updated_at=None,
    )

    await service.run_mode_pipeline(session, "intent:analyst", {"chat_id": 1}, context=None, mode_id="analyst")

    assert analyst_mode.calls == ["intent:analyst"]
    assert session.orchestrator.last_mode_id == "analyst"
    first_output = str(session.orchestrator.last_mode_output or "")
    assert first_output == "analyst:intent:analyst"
    assert session.busy is False
    assert session.run_lock.locked() is False
    assert session.tick_seen == len(session.runtime_progress_events)
    assert isinstance(session.runtime_progress_events, list)
    assert len(session.runtime_progress_events) >= 2
    assert {str(item.get("mode_id") or "") for item in session.runtime_progress_events} == {"analyst"}
    assert {str(item.get("phase") or "") for item in session.runtime_progress_events} >= {"start", "final"}
    assert session.runtime_progress_last_event.get("mode_id") == "analyst"
    await _wait_until_three_flags_released(session)
    assert session.is_active_by_tick(now=float(session.last_tick_ts) + 4.0) is False
    probe_first = types.SimpleNamespace(
        busy=bool(getattr(session, "busy", False)),
        is_active_by_tick=(lambda: bool(session.is_active_by_tick(now=float(session.last_tick_ts) + 4.0))),
    )
    assert is_session_busy(probe_first, getattr(session, "run_lock", None)) is False

    await service.run_mode_pipeline(session, "intent:agent", {"chat_id": 1}, context=None, mode_id="agent")

    assert agent_mode.calls == ["intent:agent"]
    assert session.orchestrator.last_mode_id == "agent"
    second_output = str(session.orchestrator.last_mode_output or "")
    assert second_output == "agent:intent:agent"
    assert second_output != first_output
    assert session.busy is False
    assert session.run_lock.locked() is False
    assert session.tick_seen == len(session.runtime_progress_events)
    assert isinstance(session.runtime_progress_events, list)
    assert len(session.runtime_progress_events) >= 2
    assert {str(item.get("mode_id") or "") for item in session.runtime_progress_events} == {"agent"}
    assert {str(item.get("phase") or "") for item in session.runtime_progress_events} >= {"start", "final"}
    assert session.runtime_progress_last_event.get("mode_id") == "agent"
    await _wait_until_three_flags_released(session)
    assert session.is_active_by_tick(now=float(session.last_tick_ts) + 4.0) is False
    probe_second = types.SimpleNamespace(
        busy=bool(getattr(session, "busy", False)),
        is_active_by_tick=(lambda: bool(session.is_active_by_tick(now=float(session.last_tick_ts) + 4.0))),
    )
    assert is_session_busy(probe_second, getattr(session, "run_lock", None)) is False

    assert reset_calls == ["analyst"]
    assert sent_outputs == []


def test_start_mode_task_uses_session_runtime_uid_for_mode_task_groups() -> None:
    listed: list[tuple[str, str]] = []
    created: list[tuple[str, str, str]] = []
    task_groups: dict[tuple[str, str], list[str]] = {}

    def _mode_tasks_list(*, session_id: str, mode_id: str):
        key = (str(session_id), str(mode_id))
        listed.append(key)
        return list(task_groups.get(key, []))

    def _mode_tasks_create(*, session_id: str, mode_id: str, coro, name: str):
        key = (str(session_id), str(mode_id))
        created.append((key[0], key[1], str(name)))
        task_groups.setdefault(key, []).append(str(name))
        coro.close()

    bot_app = types.SimpleNamespace(
        mode_registry=_ModeRegistryStub({}),
        config=types.SimpleNamespace(defaults=types.SimpleNamespace(summary_max_chars=2000)),
    )
    service = SessionRunService(
        bot_app=bot_app,
        persist_sessions=(lambda: None),
        mode_tasks_list=_mode_tasks_list,
        mode_tasks_create=_mode_tasks_create,
        log_cli_dialog=(lambda *_args, **_kwargs: None),
        reset_session_fields_like_sessions_reset=(lambda *_args, **_kwargs: None),
    )

    session_one = _SessionStub(
        id="shared-raw-id",
        chat_id=1,
        conversation_scope=ConversationScope.from_parts(1, 101),
        modes=types.SimpleNamespace(active_mode="analyst"),
    )
    session_two = _SessionStub(
        id="shared-raw-id",
        chat_id=1,
        conversation_scope=ConversationScope.from_parts(1, 202),
        modes=types.SimpleNamespace(active_mode="analyst"),
    )

    service.start_mode_task(session_one, "prompt-a", {"chat_id": 1}, context=None, mode_id="analyst")
    service.start_mode_task(session_two, "prompt-b", {"chat_id": 1}, context=None, mode_id="analyst")

    assert listed == [
        ("thread:1:101", "analyst"),
        ("thread:1:202", "analyst"),
    ]
    assert created == [
        ("thread:1:101", "analyst", "run_analyst"),
        ("thread:1:202", "analyst", "run_analyst"),
    ]


def _make_output_service(bot_app, *, make_html_file_fn=None) -> SessionOutputService:
    return SessionOutputService(
        bot_app=bot_app,
        persist_sessions=lambda: None,
        html_process_pool=None,
        summarize_fn=lambda *_args, **_kwargs: (None, None),
        ansi_to_html_fn=lambda text: str(text),
        make_html_file_fn=make_html_file_fn,
    )


def test_send_output_logs_last_delivery_error_fallback_without_raising(caplog) -> None:
    class _BotApp:
        config = types.SimpleNamespace(defaults=types.SimpleNamespace(summary_max_chars=2000))

        def __setattr__(self, name, value):
            if name == "_last_delivery_error":
                raise RuntimeError("read-only delivery error")
            super().__setattr__(name, value)

    bot_app = _BotApp()
    service = _make_output_service(bot_app)
    session = types.SimpleNamespace(id="s-empty")

    async def _run() -> None:
        await service.send_output(session, {"kind": "telegram", "chat_id": 1}, "", context=None)

    caplog.set_level(logging.DEBUG, logger="bot.send_output")
    asyncio.run(_run())

    assert "legacy_fallback: failed to store last delivery error session=s-empty" in caplog.text
    assert "[send_output] refused to send: empty output session=s-empty" in caplog.text


def test_send_output_logs_html_cleanup_failure_without_raising(tmp_path, monkeypatch, caplog) -> None:
    html_path = tmp_path / "out.html"

    def _make_html_file(_html_text, _prefix):
        html_path.write_text("<pre>output</pre>", encoding="utf-8")
        return str(html_path)

    sent_documents: list[str] = []
    persisted: list[bool] = []

    async def _send_message(*_args, **_kwargs):
        return True

    async def _send_document(_context, *, document, **_kwargs):
        sent_documents.append(document.name)
        return True

    bot_app = types.SimpleNamespace(
        config=types.SimpleNamespace(
            defaults=types.SimpleNamespace(
                html_filename_prefix="test-output",
                summary_max_chars=2000,
            )
        ),
        metrics=types.SimpleNamespace(observe_output=lambda _value: None),
        _send_message=_send_message,
        _send_document=_send_document,
    )
    service = SessionOutputService(
        bot_app=bot_app,
        persist_sessions=lambda: persisted.append(True),
        html_process_pool=None,
        ansi_to_html_fn=lambda text: str(text),
        make_html_file_fn=_make_html_file,
    )
    session = types.SimpleNamespace(
        id="s-html",
        name="html",
        tool=types.SimpleNamespace(name="dummy"),
        workdir=str(tmp_path),
        queue=deque(),
        resume_token=None,
        send_lock=asyncio.Lock(),
        state_summary=None,
        state_updated_at=None,
    )

    def _remove_raises(path):
        assert str(path) == str(html_path)
        raise RuntimeError("remove denied")

    async def _run() -> None:
        await service.send_output(
            session,
            {"kind": "telegram", "chat_id": 1},
            "hello",
            context=None,
            force_html=True,
            send_summary=False,
        )

    monkeypatch.setattr("sessions.session_output_service.os.remove", _remove_raises)
    caplog.set_level(logging.DEBUG, logger="bot.send_output")
    asyncio.run(_run())

    assert sent_documents == [str(html_path)]
    assert persisted == [True]
    assert "best_effort_cleanup: failed to remove generated html file session=s-html" in caplog.text
