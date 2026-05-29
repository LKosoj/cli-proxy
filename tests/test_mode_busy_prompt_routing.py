import asyncio
import types

from app.services.input_dispatch_service import InputDispatchService
from modes.sdk import BaseMode
from modes.sdk.session_busy import is_session_busy
from modes.sdk.services.messaging import MessagingService


class _DummyMode(BaseMode):
    mode_id = "dummy"

    async def handle_input(self, message, ctx):
        raise NotImplementedError

    async def handle_callback(self, callback, ctx):
        raise NotImplementedError


class _ProbeLock:
    def __init__(self) -> None:
        self._locked = False

    def set_locked(self, value: bool) -> None:
        self._locked = bool(value)

    def locked(self) -> bool:
        return bool(self._locked)


class _RecordingInputTransport:
    def __init__(self, sent: list[dict] | None = None, cleared: list[dict] | None = None) -> None:
        self.sent = sent if sent is not None else []
        self.cleared = cleared

    async def send_decision(self, _context, decision, *, dest, fallback_chat_id):
        self.sent.append(
            {
                "text": str(decision.text or ""),
                "reply_markup": None,
                "kwargs": {"chat_id": fallback_chat_id, **dict(dest or {})},
            }
        )
        return types.SimpleNamespace(message_id=len(self.sent))

    async def send_text(self, _context, *, text: str, dest, fallback_chat_id, md2: bool = True):
        _ = md2
        self.sent.append(
            {
                "text": str(text or ""),
                "reply_markup": None,
                "kwargs": {"chat_id": fallback_chat_id, **dict(dest or {})},
            }
        )
        return types.SimpleNamespace(message_id=len(self.sent))

    async def retire_prompt(self, _context, *, dest, message_id: int, stale_text: str, ui_key):
        _ = stale_text, ui_key
        if self.cleared is None:
            return False
        self.cleared.append(
            {
                "chat_id": int(dict(dest or {}).get("chat_id")),
                "message_id": int(message_id),
                "dest": dict(dest or {}),
            }
        )
        return True


def test_is_session_busy_checks_all_three_signals_with_recovery() -> None:
    session = types.SimpleNamespace(
        busy=False,
        is_active_by_tick=lambda: False,
    )
    run_lock = _ProbeLock()

    assert is_session_busy(session, run_lock) is False

    session.busy = True
    assert is_session_busy(session, run_lock) is True
    session.busy = False
    assert is_session_busy(session, run_lock) is False

    run_lock.set_locked(True)
    assert is_session_busy(session, run_lock) is True
    run_lock.set_locked(False)
    assert is_session_busy(session, run_lock) is False

    session.is_active_by_tick = lambda: True
    assert is_session_busy(session, run_lock) is True
    session.is_active_by_tick = lambda: False
    assert is_session_busy(session, run_lock) is False


def test_enqueue_if_busy_uses_pending_prompt_dispatcher() -> None:
    mode = _DummyMode()
    session = types.SimpleNamespace(
        busy=True,
        run_lock=asyncio.Lock(),
        queue=[],
        id="s1",
    )
    calls = []

    class _Dispatcher:
        async def handle_cli_input(
            self,
            _session,
            text,
            chat_id,
            context,
            *,
            dest=None,
            enforce_direct_cli_policy=True,
        ):
            calls.append(
                {
                    "session_id": getattr(_session, "id", ""),
                    "text": text,
                    "chat_id": chat_id,
                    "context": context,
                    "dest": dict(dest or {}),
                    "enforce_direct_cli_policy": bool(enforce_direct_cli_policy),
                }
            )

    bot_app = types.SimpleNamespace(input_dispatch_service=_Dispatcher())
    ms = MessagingService(transport_context=object())

    result = asyncio.run(
        mode._enqueue_if_busy(
            session=session,
            bot_app=bot_app,
            ms=ms,
            chat_id=101,
            text="hello",
            dest={"kind": "telegram", "chat_id": 101},
        )
    )

    assert result is True
    assert calls == [
        {
            "session_id": "s1",
            "text": "hello",
            "chat_id": 101,
            "context": ms.transport_context,
            "dest": {"kind": "telegram", "chat_id": 101},
            "enforce_direct_cli_policy": False,
        }
    ]
    assert session.queue == []


def test_stage_user_input_retires_previous_prompt_by_clearing_reply_markup() -> None:
    session = types.SimpleNamespace(
        busy=False,
        run_lock=_ProbeLock(),
        queue=[],
        id="s1",
        is_active_by_tick=lambda: False,
    )
    sent: list[dict] = []
    cleared: list[dict] = []

    class _BotApp:
        def __init__(self) -> None:
            self.ui_state = types.SimpleNamespace(pending={}, pending_prompt_messages={})

        async def _send_message(self, _context, *, text: str, reply_markup=None, **kwargs):
            sent.append(
                {
                    "text": str(text),
                    "reply_markup": reply_markup,
                    "kwargs": dict(kwargs),
                }
            )
            return types.SimpleNamespace(message_id=len(sent))

        async def _clear_message_reply_markup(self, _context, *, chat_id: int, message_id: int, dest=None):
            cleared.append(
                {
                    "chat_id": int(chat_id),
                    "message_id": int(message_id),
                    "dest": dict(dest or {}),
                }
            )
            return True

        async def _edit_message(self, *_args, **_kwargs):
            raise AssertionError("stale text fallback should not be used when clear helper succeeds")

    bot_app = _BotApp()
    service = InputDispatchService(bot_app, pending_input_ui=_RecordingInputTransport(sent, cleared))
    dest = {"kind": "telegram", "chat_id": 101}

    asyncio.run(service.stage_user_input(session, "first", 101, object(), dest=dest))
    asyncio.run(service.stage_user_input(session, "second", 101, object(), dest=dest))

    assert len(sent) == 2
    assert all(item["text"] == InputDispatchService.take_in_work_prompt_text() for item in sent)
    assert cleared == [{"chat_id": 101, "message_id": 1, "dest": dest}]
    pending = InputDispatchService.pending_head(bot_app.ui_state.pending, service._pending_ui_key(dest, 101))
    assert pending is not None
    assert pending.text == "first\n\nsecond"


def test_enqueue_if_busy_fallback_adds_queue_when_dispatcher_missing() -> None:
    mode = _DummyMode()
    persisted = {"ok": False}
    mode.initialize(services={"session_control": types.SimpleNamespace(persist=lambda: persisted.__setitem__("ok", True))})
    session = types.SimpleNamespace(
        busy=True,
        run_lock=asyncio.Lock(),
        queue=[],
        id="s1",
    )
    sent = []

    async def _send_message(_context, *, chat_id: int, text: str, md2: bool = True, **_kwargs):
        sent.append((chat_id, text, md2))
        return True

    ms = MessagingService(send_message=_send_message, transport_context=object())
    bot_app = types.SimpleNamespace()

    result = asyncio.run(
        mode._enqueue_if_busy(
            session=session,
            bot_app=bot_app,
            ms=ms,
            chat_id=101,
            text="hello",
            dest={"kind": "telegram", "chat_id": 101},
        )
    )

    assert result is True
    assert session.queue == [{"text": "hello", "dest": {"kind": "telegram", "chat_id": 101}}]
    assert persisted["ok"] is True
    assert sent == [(101, "Сессия занята. Добавил запрос в очередь.", True)]


def test_stage_user_input_routes_immediately_when_confirmation_disabled() -> None:
    session = types.SimpleNamespace(
        busy=False,
        run_lock=_ProbeLock(),
        queue=[],
        id="s1",
        is_active_by_tick=lambda: False,
    )
    routed: list[dict] = []

    class _BotApp:
        def __init__(self) -> None:
            self.ui_state = types.SimpleNamespace(pending={}, pending_prompt_messages={})
            self.config = types.SimpleNamespace(
                defaults=types.SimpleNamespace(pending_input_confirmation_enabled=False)
            )

        async def _send_message(self, *_args, **_kwargs):
            raise AssertionError("pending confirmation must be bypassed when feature is disabled")

    bot_app = _BotApp()
    service = InputDispatchService(bot_app)
    transport_context = object()

    async def _fake_handle_user_input_impl(*, session, text, chat_id, context, dest, allow_orchestration):
        routed.append(
            {
                "session_id": getattr(session, "id", ""),
                "text": str(text or ""),
                "chat_id": chat_id,
                "context": context,
                "dest": dict(dest or {}),
                "allow_orchestration": bool(allow_orchestration),
            }
        )

    service._handle_user_input_impl = _fake_handle_user_input_impl  # type: ignore[method-assign]
    dest = {"kind": "telegram", "chat_id": 101}

    asyncio.run(service.stage_user_input(session, "hello", 101, transport_context, dest=dest))

    assert InputDispatchService.pending_head(bot_app.ui_state.pending, service._pending_ui_key(dest, 101)) is None
    assert routed == [
        {
            "session_id": "s1",
            "text": "hello",
            "chat_id": 101,
            "context": transport_context,
            "dest": dest,
            "allow_orchestration": True,
        }
    ]


def test_stage_user_input_busy_requires_entry_confirmation_before_queue_choice() -> None:
    session = types.SimpleNamespace(
        busy=True,
        run_lock=_ProbeLock(),
        queue=[],
        id="s1",
        is_active_by_tick=lambda: False,
    )
    sent: list[dict] = []

    class _BotApp:
        def __init__(self) -> None:
            self.ui_state = types.SimpleNamespace(pending={}, pending_prompt_messages={})

        async def _send_message(self, _context, *, text: str, reply_markup=None, **kwargs):
            sent.append(
                {
                    "text": str(text),
                    "reply_markup": reply_markup,
                    "kwargs": dict(kwargs),
                }
            )
            return types.SimpleNamespace(message_id=len(sent))

    bot_app = _BotApp()
    service = InputDispatchService(bot_app, pending_input_ui=_RecordingInputTransport(sent))
    dest = {"kind": "telegram", "chat_id": 101}

    asyncio.run(service.stage_user_input(session, "hello", 101, object(), dest=dest))

    pending = InputDispatchService.pending_head(bot_app.ui_state.pending, service._pending_ui_key(dest, 101))
    assert pending is not None
    assert pending.text == "hello"
    assert pending.action == InputDispatchService.PENDING_ACTION_CONFIRM
    assert session.queue == []
    assert [item["text"] for item in sent] == [InputDispatchService.take_in_work_prompt_text()]


def test_stage_user_input_busy_still_prompts_queue_choice_when_confirmation_disabled() -> None:
    session = types.SimpleNamespace(
        busy=True,
        run_lock=_ProbeLock(),
        queue=[],
        id="s1",
        is_active_by_tick=lambda: False,
    )
    sent: list[dict] = []

    class _BotApp:
        def __init__(self) -> None:
            self.ui_state = types.SimpleNamespace(pending={}, pending_prompt_messages={})
            self.config = types.SimpleNamespace(
                defaults=types.SimpleNamespace(pending_input_confirmation_enabled=False)
            )

        async def _send_message(self, _context, *, text: str, reply_markup=None, **kwargs):
            sent.append(
                {
                    "text": str(text),
                    "reply_markup": reply_markup,
                    "kwargs": dict(kwargs),
                }
            )
            return types.SimpleNamespace(message_id=len(sent))

    bot_app = _BotApp()
    service = InputDispatchService(bot_app, pending_input_ui=_RecordingInputTransport(sent))
    dest = {"kind": "telegram", "chat_id": 101}

    asyncio.run(service.stage_user_input(session, "hello", 101, object(), dest=dest))

    pending = InputDispatchService.pending_head(bot_app.ui_state.pending, service._pending_ui_key(dest, 101))
    assert pending is not None
    assert pending.text == "hello"
    assert pending.action == InputDispatchService.PENDING_ACTION_QUEUE_CONFIRM
    assert session.queue == []
    assert [item["text"] for item in sent] == [InputDispatchService.queue_confirm_prompt_text()]


def test_enqueue_if_busy_considers_existing_session_queue_as_busy() -> None:
    mode = _DummyMode()
    session = types.SimpleNamespace(
        busy=False,
        run_lock=asyncio.Lock(),
        queue=[{"text": "queued", "dest": {"kind": "telegram", "chat_id": 101}}],
        id="s1",
        active_mode="manager",
        is_active_by_tick=lambda: False,
    )
    calls = []

    class _Dispatcher:
        async def handle_cli_input(
            self,
            _session,
            text,
            chat_id,
            context,
            *,
            dest=None,
            enforce_direct_cli_policy=True,
        ):
            calls.append(
                {
                    "session_id": getattr(_session, "id", ""),
                    "text": text,
                    "chat_id": chat_id,
                    "context": context,
                    "dest": dict(dest or {}),
                    "enforce_direct_cli_policy": bool(enforce_direct_cli_policy),
                }
            )

    bot_app = types.SimpleNamespace(
        input_dispatch_service=_Dispatcher(),
        mode_tasks=types.SimpleNamespace(list=lambda **_kwargs: []),
    )
    ms = MessagingService(transport_context=object())

    result = asyncio.run(
        mode._enqueue_if_busy(
            session=session,
            bot_app=bot_app,
            ms=ms,
            chat_id=101,
            text="hello",
            dest={"kind": "telegram", "chat_id": 101},
        )
    )

    assert result is True
    assert calls == [
        {
            "session_id": "s1",
            "text": "hello",
            "chat_id": 101,
            "context": ms.transport_context,
            "dest": {"kind": "telegram", "chat_id": 101},
            "enforce_direct_cli_policy": False,
        }
    ]


def test_enqueue_if_busy_preserves_image_dest_for_later_mode_processing() -> None:
    mode = _DummyMode()
    session = types.SimpleNamespace(
        busy=True,
        run_lock=asyncio.Lock(),
        queue=[],
        id="s1",
    )
    calls = []

    class _Dispatcher:
        async def handle_cli_input(
            self,
            _session,
            text,
            chat_id,
            context,
            *,
            dest=None,
            enforce_direct_cli_policy=True,
        ):
            calls.append(
                {
                    "session_id": getattr(_session, "id", ""),
                    "text": text,
                    "chat_id": chat_id,
                    "context": context,
                    "dest": dict(dest or {}),
                    "enforce_direct_cli_policy": bool(enforce_direct_cli_policy),
                }
            )

    bot_app = types.SimpleNamespace(input_dispatch_service=_Dispatcher())
    ms = MessagingService(transport_context=object())

    result = asyncio.run(
        mode._enqueue_if_busy(
            session=session,
            bot_app=bot_app,
            ms=ms,
            chat_id=101,
            text="hello",
            dest={"kind": "telegram", "chat_id": 101, "image_paths": ["/tmp/a.png", "/tmp/b.png"]},
        )
    )

    assert result is True
    assert calls == [
        {
            "session_id": "s1",
            "text": "hello",
            "chat_id": 101,
            "context": ms.transport_context,
            "dest": {"kind": "telegram", "chat_id": 101, "image_paths": ["/tmp/a.png", "/tmp/b.png"]},
            "enforce_direct_cli_policy": False,
        }
    ]
