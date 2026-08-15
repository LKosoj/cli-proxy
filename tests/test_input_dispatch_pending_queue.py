from __future__ import annotations

import asyncio
from collections import deque
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services.input_dispatch_models import PendingInput, PendingInputDecision
from app.services.input_dispatch_service import InputDispatchService
from tg.pending_input_ui import build_pending_input_reply_markup


class _RecordingInputTransport:
    def __init__(self, sent: list[str] | None = None, cleared: list[dict] | None = None) -> None:
        self.sent = sent if sent is not None else []
        self.cleared = cleared

    async def send_decision(self, _context, decision, *, dest, fallback_chat_id):
        _ = dest, fallback_chat_id
        self.sent.append(str(decision.text or ""))
        return SimpleNamespace(message_id=len(self.sent))

    async def send_text(self, _context, *, text: str, dest, fallback_chat_id, md2: bool = True):
        _ = dest, fallback_chat_id, md2
        self.sent.append(str(text or ""))
        return SimpleNamespace(message_id=len(self.sent))

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


def test_pending_input_decision_is_transport_neutral_domain_model() -> None:
    decision = PendingInputDecision(
        action="ask_take_in_work",
        text="Сообщение получено. Взять в работу?",
        payload={"session_uid": "forum:-100777000111:101"},
    )

    assert decision.action == "ask_take_in_work"
    assert decision.text == "Сообщение получено. Взять в работу?"
    assert decision.payload == {"session_uid": "forum:-100777000111:101"}

    source_path = Path(__file__).parents[1] / "app" / "services" / "input_dispatch_models.py"
    source = source_path.read_text(encoding="utf-8")
    assert "telegram" not in source.lower()
    assert "tg.handlers" not in source

    service_source_path = Path(__file__).parents[1] / "app" / "services" / "input_dispatch_service.py"
    service_source = service_source_path.read_text(encoding="utf-8")
    assert "from telegram" not in service_source
    assert "import telegram" not in service_source
    assert "from tg.handlers import PendingInput" not in service_source
    assert "self.bot_app._send_message" not in service_source
    assert "self.bot_app._edit_message" not in service_source
    assert "self.bot_app._clear_message_reply_markup" not in service_source
    assert "self.bot_app._send_document" not in service_source
    assert "_telegram_reply_kwargs" not in service_source


def test_telegram_pending_input_adapter_preserves_callback_data_contract() -> None:
    confirm_markup = build_pending_input_reply_markup(
        PendingInputDecision(action=InputDispatchService.PENDING_ACTION_CONFIRM, text="", payload={})
    )
    queue_markup = build_pending_input_reply_markup(
        PendingInputDecision(action=InputDispatchService.PENDING_ACTION_QUEUE_CHOICE, text="", payload={})
    )
    queue_confirm_markup = build_pending_input_reply_markup(
        PendingInputDecision(action=InputDispatchService.PENDING_ACTION_QUEUE_CONFIRM, text="", payload={})
    )
    tmux_queue_markup = build_pending_input_reply_markup(
        PendingInputDecision(action=InputDispatchService.PENDING_ACTION_TMUX_QUEUE_CHOICE, text="", payload={})
    )
    tmux_queue_confirm_markup = build_pending_input_reply_markup(
        PendingInputDecision(action=InputDispatchService.PENDING_ACTION_TMUX_QUEUE_CONFIRM, text="", payload={})
    )
    assert confirm_markup.inline_keyboard[0][0].callback_data == "take_pending_input"
    assert confirm_markup.inline_keyboard[1][0].callback_data == "discard_input"
    assert queue_markup.inline_keyboard[0][0].callback_data == "queue_append_pending"
    assert queue_markup.inline_keyboard[0][1].callback_data == "queue_input"
    assert queue_confirm_markup.inline_keyboard[0][0].callback_data == "queue_input"
    assert tmux_queue_markup.inline_keyboard[0][0].callback_data == "send_current_tmux"
    assert tmux_queue_markup.inline_keyboard[1][0].callback_data == "queue_append_pending"
    assert tmux_queue_markup.inline_keyboard[1][1].callback_data == "queue_input"
    assert tmux_queue_confirm_markup.inline_keyboard[0][0].callback_data == "send_current_tmux"
    assert tmux_queue_confirm_markup.inline_keyboard[1][0].callback_data == "queue_input"


def test_append_queue_item_normalizes_payload_and_preserves_metadata() -> None:
    session = SimpleNamespace(queue=[])
    pending = PendingInput(
        session_id="s1",
        text="caption",
        dest={"kind": "telegram", "chat_id": 1, "image_paths": ["/tmp/a.png", "/tmp/b.png"]},
        image_paths=["/tmp/a.png", "/tmp/b.png"],
    )
    item = InputDispatchService.queue_item_from_pending(pending)
    item["attachments"] = ["/tmp/source.txt"]

    assert InputDispatchService.append_queue_item(session, item)

    assert session.queue == [
        {
            "text": "caption",
            "dest": {"kind": "telegram", "chat_id": 1, "image_paths": ["/tmp/a.png", "/tmp/b.png"]},
            "image_paths": ["/tmp/a.png", "/tmp/b.png"],
            "attachments": ["/tmp/source.txt"],
        }
    ]
    assert session.queue[0] is not item
    assert session.queue[0]["dest"] is not item["dest"]
    assert session.queue[0]["image_paths"] is not item["image_paths"]
    assert session.queue[0]["attachments"] is not item["attachments"]


@pytest.mark.asyncio
async def test_stage_user_input_sends_pending_input_decision_through_adapter() -> None:
    decisions: list[dict] = []

    class _Adapter:
        async def send_decision(self, _context, decision, *, dest, fallback_chat_id):
            decisions.append(
                {
                    "action": decision.action,
                    "text": decision.text,
                    "payload": dict(decision.payload),
                    "dest": dict(dest or {}),
                    "fallback_chat_id": fallback_chat_id,
                }
            )
            return SimpleNamespace(message_id=1)

    async def _send_message(*_args, **_kwargs):
        raise AssertionError("pending prompts must go through PendingInputDecision adapter")

    bot_app = SimpleNamespace(
        _shutdown_in_progress=False,
        ui_state=SimpleNamespace(pending={}),
        _send_message=_send_message,
    )
    service = InputDispatchService(bot_app, pending_input_ui=_Adapter())
    session = SimpleNamespace(
        id="s1",
        busy=False,
        queue=[],
        is_active_by_tick=lambda: False,
        run_lock=asyncio.Lock(),
    )

    await service.stage_user_input(session, "hello", chat_id=1, context=object())

    assert decisions == [
        {
            "action": InputDispatchService.PENDING_ACTION_CONFIRM,
            "text": InputDispatchService.take_in_work_prompt_text(),
            "payload": {
                "pending_action": InputDispatchService.PENDING_ACTION_CONFIRM,
                "session_id": "s1",
                "session_uid": "desktop:s1",
                "dest": {"kind": "telegram", "chat_id": 1},
            },
            "dest": {"kind": "telegram", "chat_id": 1},
            "fallback_chat_id": 1,
        }
    ]


@pytest.mark.asyncio
async def test_pending_input_decision_has_no_legacy_bot_app_send_fallback() -> None:
    calls: list[dict] = []

    async def _send_message(*_args, **kwargs):
        calls.append(dict(kwargs))
        return SimpleNamespace(message_id=1)

    bot_app = SimpleNamespace(_send_message=_send_message)
    service = InputDispatchService(bot_app)

    with pytest.raises(RuntimeError, match="send_decision"):
        await service.send_pending_input_decision(
            context=object(),
            decision=PendingInputDecision(action=InputDispatchService.PENDING_ACTION_CONFIRM, text="hello"),
            dest={"kind": "telegram", "chat_id": 1},
            chat_id=1,
        )

    assert calls == []


@pytest.mark.asyncio
async def test_handle_cli_input_busy_without_queue_requires_queue_confirmation() -> None:
    sent: list[str] = []
    metrics = {"queued": 0}

    async def _send_message(_context, *, chat_id: int, text: str, **_kwargs):
        _ = chat_id
        sent.append(str(text or ""))
        return True

    bot_app = SimpleNamespace(
        _shutdown_in_progress=False,
        run_prompt=(lambda *_a, **_k: asyncio.sleep(0)),
        ui_state=SimpleNamespace(pending={}),
        metrics=SimpleNamespace(inc=lambda name: metrics.__setitem__(str(name), int(metrics.get(str(name), 0)) + 1)),
        _send_message=_send_message,
    )
    service = InputDispatchService(bot_app, pending_input_ui=_RecordingInputTransport(sent))
    session = SimpleNamespace(
        id="s1",
        busy=True,
        queue=[],
        is_active_by_tick=lambda: False,
        run_lock=asyncio.Lock(),
    )

    for idx in range(1, 6):
        await service.handle_cli_input(session, f"m{idx}", chat_id=1, context=object())

    pending_queue = bot_app.ui_state.pending.get(InputDispatchService._pending_ui_key(None, 1))
    assert isinstance(pending_queue, deque)
    assert session.queue == []
    assert pending_queue[0].text == "m1\n\nm2\n\nm3\n\nm4\n\nm5"
    assert int(metrics.get("queued", 0)) == 5
    assert all(msg == InputDispatchService.queue_confirm_prompt_text() for msg in sent)


@pytest.mark.asyncio
async def test_handle_cli_input_busy_without_queue_still_requires_confirmation_when_confirmation_disabled() -> None:
    sent: list[str] = []
    metrics = {"queued": 0}

    async def _send_message(_context, *, chat_id: int, text: str, **_kwargs):
        _ = chat_id
        sent.append(str(text or ""))
        return True

    bot_app = SimpleNamespace(
        _shutdown_in_progress=False,
        run_prompt=(lambda *_a, **_k: asyncio.sleep(0)),
        ui_state=SimpleNamespace(pending={}),
        metrics=SimpleNamespace(inc=lambda name: metrics.__setitem__(str(name), int(metrics.get(str(name), 0)) + 1)),
        _send_message=_send_message,
        config=SimpleNamespace(defaults=SimpleNamespace(pending_input_confirmation_enabled=False)),
    )
    service = InputDispatchService(bot_app, pending_input_ui=_RecordingInputTransport(sent))
    session = SimpleNamespace(
        id="s1",
        busy=True,
        queue=[],
        is_active_by_tick=lambda: False,
        run_lock=asyncio.Lock(),
    )

    await service.handle_cli_input(session, "m1", chat_id=1, context=object())

    pending_queue = bot_app.ui_state.pending.get(InputDispatchService._pending_ui_key(None, 1))
    assert isinstance(pending_queue, deque)
    assert pending_queue[0].text == "m1"
    assert session.queue == []
    assert int(metrics.get("queued", 0)) == 1
    assert sent == [InputDispatchService.queue_confirm_prompt_text()]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("queue", "expected_action"),
    [
        ([], InputDispatchService.PENDING_ACTION_TMUX_QUEUE_CONFIRM),
        (
            [{"text": "queued", "dest": {"kind": "telegram", "chat_id": 1}}],
            InputDispatchService.PENDING_ACTION_TMUX_QUEUE_CHOICE,
        ),
    ],
)
async def test_handle_cli_input_busy_active_tmux_offers_direct_send(
    monkeypatch,
    queue,
    expected_action: str,
) -> None:
    sent: list[str] = []
    bot_app = SimpleNamespace(
        _shutdown_in_progress=False,
        ui_state=SimpleNamespace(pending={}),
        metrics=SimpleNamespace(inc=lambda *_a, **_k: None),
    )
    service = InputDispatchService(bot_app, pending_input_ui=_RecordingInputTransport(sent))
    session = SimpleNamespace(
        id="s1",
        busy=True,
        queue=list(queue),
        is_active_by_tick=lambda: False,
        run_lock=asyncio.Lock(),
    )

    async def _active_tmux(_session, _pending) -> bool:
        return True

    monkeypatch.setattr(service, "can_send_pending_to_active_tmux", _active_tmux)

    await service.handle_cli_input(session, "steer", chat_id=1, context=object())

    pending = InputDispatchService.pending_head(
        bot_app.ui_state.pending,
        InputDispatchService._pending_ui_key(None, 1),
    )
    assert pending is not None
    assert pending.action == expected_action
    assert sent == [InputDispatchService.tmux_busy_prompt_text()]


@pytest.mark.asyncio
async def test_active_tmux_choice_follows_pane_availability(monkeypatch) -> None:
    from app.services.cli_backends import TmuxExecutionBackend

    sent: list[str] = []
    bot_app = SimpleNamespace(
        _shutdown_in_progress=False,
        ui_state=SimpleNamespace(pending={}),
        metrics=SimpleNamespace(inc=lambda *_a, **_k: None),
    )
    service = InputDispatchService(bot_app, pending_input_ui=_RecordingInputTransport(sent))
    session = SimpleNamespace(
        id="s1",
        busy=True,
        queue=[],
        is_active_by_tick=lambda: False,
        run_lock=asyncio.Lock(),
    )

    # У бота своего запроса нет, но pane печатает: дописывать в него можно.
    async def _can_accept(_backend, _session) -> bool:
        return True

    monkeypatch.setattr(TmuxExecutionBackend, "can_accept_input", _can_accept)

    await service.handle_cli_input(session, "steer", chat_id=1, context=object())

    pending = InputDispatchService.pending_head(
        bot_app.ui_state.pending,
        InputDispatchService._pending_ui_key(None, 1),
    )
    assert pending is not None
    assert pending.action == InputDispatchService.PENDING_ACTION_TMUX_QUEUE_CONFIRM
    assert sent == [InputDispatchService.tmux_busy_prompt_text()]


@pytest.mark.asyncio
async def test_active_tmux_choice_is_offered_for_attachments(monkeypatch) -> None:
    from app.services.cli_backends import TmuxExecutionBackend

    sent: list[str] = []
    bot_app = SimpleNamespace(
        _shutdown_in_progress=False,
        ui_state=SimpleNamespace(pending={}),
        metrics=SimpleNamespace(inc=lambda *_a, **_k: None),
    )
    service = InputDispatchService(bot_app, pending_input_ui=_RecordingInputTransport(sent))
    session = SimpleNamespace(
        id="s1",
        busy=True,
        queue=[],
        workdir="/work",
        is_active_by_tick=lambda: False,
        run_lock=asyncio.Lock(),
    )

    async def _can_accept(_backend, _session) -> bool:
        return True

    monkeypatch.setattr(TmuxExecutionBackend, "can_accept_input", _can_accept)

    await service.handle_cli_input(
        session,
        "caption",
        chat_id=1,
        context=object(),
        image_paths=["/work/.attachments/image.png"],
    )

    pending = InputDispatchService.pending_head(
        bot_app.ui_state.pending,
        InputDispatchService._pending_ui_key(None, 1),
    )
    assert pending is not None
    assert pending.action == InputDispatchService.PENDING_ACTION_TMUX_QUEUE_CONFIRM
    assert sent == [InputDispatchService.tmux_busy_prompt_text()]


@pytest.mark.asyncio
async def test_pending_attachments_are_sent_to_tmux_as_file_refs(monkeypatch) -> None:
    from app.services.cli_backends import TmuxExecutionBackend

    sent_prompts: list[str] = []

    async def _send_input(_backend, _session, text: str) -> None:
        sent_prompts.append(text)

    monkeypatch.setattr(TmuxExecutionBackend, "send_input", _send_input)

    session = SimpleNamespace(id="s1", workdir="/work")
    pending = PendingInput(
        session_id="s1",
        text="посмотри скрин",
        dest={"kind": "telegram", "chat_id": 1},
        image_paths=["/work/.attachments/shot.png", "/tmp/outside.log"],
    )

    await InputDispatchService.send_pending_to_active_tmux(session, pending)

    # Файлы уже сохранены на диск, в панель уходит одна строка со ссылками:
    # внутри workdir — относительные, снаружи — абсолютные.
    assert sent_prompts == ["посмотри скрин @.attachments/shot.png @/tmp/outside.log"]


@pytest.mark.asyncio
async def test_attachment_only_pending_is_offered_to_tmux(monkeypatch) -> None:
    from app.services.cli_backends import TmuxExecutionBackend

    probed: list[str] = []

    async def _can_accept(_backend, _session) -> bool:
        probed.append("probe")
        return True

    monkeypatch.setattr(TmuxExecutionBackend, "can_accept_input", _can_accept)

    session = SimpleNamespace(id="s1", workdir="/work")
    pending = PendingInput(
        session_id="s1",
        text="",
        dest={"kind": "telegram", "chat_id": 1},
        image_paths=["/work/.attachments/shot.png"],
    )

    assert await InputDispatchService.can_send_pending_to_active_tmux(session, pending) is True
    assert InputDispatchService.tmux_text_for_pending(session, pending) == "@.attachments/shot.png"
    assert probed == ["probe"]


@pytest.mark.asyncio
async def test_empty_pending_is_not_offered_to_tmux(monkeypatch) -> None:
    from app.services.cli_backends import TmuxExecutionBackend

    async def _can_accept(_backend, _session) -> bool:
        raise AssertionError("tmux must not be probed when there is nothing to send")

    monkeypatch.setattr(TmuxExecutionBackend, "can_accept_input", _can_accept)

    session = SimpleNamespace(id="s1", workdir="/work")
    pending = PendingInput(session_id="s1", text="   ", dest={"kind": "telegram", "chat_id": 1})

    assert await InputDispatchService.can_send_pending_to_active_tmux(session, pending) is False


@pytest.mark.asyncio
async def test_stage_user_input_busy_requires_entry_confirmation_even_when_session_is_busy() -> None:
    sent: list[str] = []

    async def _send_message(_context, *, chat_id: int, text: str, **_kwargs):
        _ = chat_id
        sent.append(str(text or ""))
        return True

    bot_app = SimpleNamespace(
        _shutdown_in_progress=False,
        ui_state=SimpleNamespace(pending={}),
        _send_message=_send_message,
    )
    service = InputDispatchService(bot_app, pending_input_ui=_RecordingInputTransport(sent))
    session = SimpleNamespace(
        id="s1",
        busy=True,
        queue=[],
        is_active_by_tick=lambda: False,
        run_lock=asyncio.Lock(),
    )

    await service.stage_user_input(session, "m1", chat_id=1, context=object())

    pending_queue = bot_app.ui_state.pending.get(InputDispatchService._pending_ui_key(None, 1))
    assert isinstance(pending_queue, deque)
    assert pending_queue[0].text == "m1"
    assert pending_queue[0].action == InputDispatchService.PENDING_ACTION_CONFIRM
    assert session.queue == []
    assert sent == [InputDispatchService.take_in_work_prompt_text()]


@pytest.mark.asyncio
async def test_stage_user_input_preserves_other_session_pending_in_same_thread() -> None:
    sent: list[str] = []

    async def _send_message(_context, *, chat_id: int, text: str, **_kwargs):
        del chat_id
        sent.append(str(text or ""))
        return True

    bot_app = SimpleNamespace(
        _shutdown_in_progress=False,
        ui_state=SimpleNamespace(pending={}),
        _send_message=_send_message,
    )
    service = InputDispatchService(bot_app, pending_input_ui=_RecordingInputTransport(sent))
    session_a = SimpleNamespace(id="s1", busy=False, queue=[], is_active_by_tick=lambda: False, run_lock=asyncio.Lock())
    session_b = SimpleNamespace(id="s2", busy=False, queue=[], is_active_by_tick=lambda: False, run_lock=asyncio.Lock())

    await service.stage_user_input(
        session_a,
        "first",
        chat_id=101,
        context=object(),
        dest={"kind": "telegram", "chat_id": 101, "session_uid": "thread:101:1"},
    )
    await service.stage_user_input(
        session_b,
        "second",
        chat_id=101,
        context=object(),
        dest={"kind": "telegram", "chat_id": 101, "session_uid": "thread:101:2"},
    )

    pending_queue = bot_app.ui_state.pending.get(InputDispatchService._pending_ui_key(None, 101))
    assert isinstance(pending_queue, deque)
    assert [(item.session_uid, item.text) for item in pending_queue] == [
        ("desktop:s1", "first"),
        ("desktop:s2", "second"),
    ]
    assert sent == [InputDispatchService.take_in_work_prompt_text(), InputDispatchService.take_in_work_prompt_text()]


@pytest.mark.asyncio
async def test_handle_cli_input_busy_with_existing_queue_merges_pending_choice() -> None:
    sent: list[str] = []

    async def _send_message(_context, *, chat_id: int, text: str, **_kwargs):
        _ = chat_id
        sent.append(str(text or ""))
        return True

    bot_app = SimpleNamespace(
        _shutdown_in_progress=False,
        run_prompt=(lambda *_a, **_k: asyncio.sleep(0)),
        ui_state=SimpleNamespace(pending={}),
        metrics=SimpleNamespace(inc=lambda *_a, **_k: None),
        _send_message=_send_message,
    )
    service = InputDispatchService(bot_app, pending_input_ui=_RecordingInputTransport(sent))
    session = SimpleNamespace(
        id="s1",
        busy=True,
        queue=[{"text": "queued", "dest": {"kind": "telegram", "chat_id": 1}}],
        is_active_by_tick=lambda: False,
        run_lock=asyncio.Lock(),
    )

    for idx in range(1, 7):
        await service.handle_cli_input(session, f"m{idx}", chat_id=1, context=object())

    pending_queue = bot_app.ui_state.pending.get(InputDispatchService._pending_ui_key(None, 1))
    assert isinstance(pending_queue, deque)
    assert len(pending_queue) == 1
    assert pending_queue[0].text == "m1\n\nm2\n\nm3\n\nm4\n\nm5\n\nm6"
    assert all("В очереди уже есть сообщение. Что сделать с новым вводом?" in msg for msg in sent)


@pytest.mark.asyncio
async def test_handle_cli_input_treats_non_empty_session_queue_as_busy() -> None:
    sent: list[str] = []
    metrics = {"queued": 0}

    async def _send_message(_context, *, chat_id: int, text: str, **_kwargs):
        _ = chat_id
        sent.append(str(text or ""))
        return True

    bot_app = SimpleNamespace(
        _shutdown_in_progress=False,
        run_prompt=(lambda *_a, **_k: asyncio.sleep(0)),
        ui_state=SimpleNamespace(pending={}),
        metrics=SimpleNamespace(inc=lambda name: metrics.__setitem__(str(name), int(metrics.get(str(name), 0)) + 1)),
        _send_message=_send_message,
        mode_tasks=SimpleNamespace(list=lambda **_kwargs: []),
    )
    service = InputDispatchService(bot_app, pending_input_ui=_RecordingInputTransport(sent))
    session = SimpleNamespace(
        id="s1",
        busy=False,
        queue=[{"text": "queued", "dest": {"kind": "telegram", "chat_id": 1}}],
        active_mode="manager",
        is_active_by_tick=lambda: False,
        run_lock=asyncio.Lock(),
    )

    await service.handle_cli_input(session, "m1", chat_id=1, context=object())

    pending_queue = bot_app.ui_state.pending.get(InputDispatchService._pending_ui_key(None, 1))
    assert isinstance(pending_queue, deque)
    assert [item.text for item in pending_queue] == ["m1"]
    assert int(metrics.get("queued", 0)) == 1
    assert any("В очереди уже есть сообщение. Что сделать с новым вводом?" in msg for msg in sent)


@pytest.mark.asyncio
async def test_handle_cli_input_busy_preserves_image_paths_from_dest() -> None:
    sent: list[str] = []

    async def _send_message(_context, *, chat_id: int, text: str, **_kwargs):
        _ = chat_id
        sent.append(str(text or ""))
        return True

    dest = {"kind": "telegram", "chat_id": 1, "image_paths": ["/tmp/a.png", "/tmp/b.png"]}
    bot_app = SimpleNamespace(
        _shutdown_in_progress=False,
        run_prompt=(lambda *_a, **_k: asyncio.sleep(0)),
        ui_state=SimpleNamespace(pending={}),
        metrics=SimpleNamespace(inc=lambda *_a, **_k: None),
        _send_message=_send_message,
    )
    service = InputDispatchService(bot_app, pending_input_ui=_RecordingInputTransport(sent))
    session = SimpleNamespace(
        id="s1",
        busy=True,
        queue=[],
        is_active_by_tick=lambda: False,
        run_lock=asyncio.Lock(),
    )

    await service.handle_cli_input(session, "caption", chat_id=1, context=object(), dest=dest)

    pending_queue = bot_app.ui_state.pending.get(InputDispatchService._pending_ui_key(dest, 1))
    assert isinstance(pending_queue, deque)
    assert len(session.queue) == 0
    pending = pending_queue[0]
    assert pending.text == "caption"
    assert pending.image_paths == ["/tmp/a.png", "/tmp/b.png"]
    assert pending.dest["image_paths"] == ["/tmp/a.png", "/tmp/b.png"]
    assert sent == [InputDispatchService.queue_confirm_prompt_text()]


@pytest.mark.asyncio
@pytest.mark.parametrize("signal_name", ["busy", "run_lock", "tick"])
async def test_handle_cli_input_running_signals_queue_then_recover_after_signal_clears(signal_name: str) -> None:
    sent: list[str] = []
    started: list[dict] = []
    metrics = {"queued": 0}
    tick_state = {"active": False}
    run_lock = asyncio.Lock()

    async def _send_message(_context, *, chat_id: int, text: str, **_kwargs):
        _ = chat_id
        sent.append(str(text or ""))
        return SimpleNamespace(message_id=len(sent))

    def _start_prompt_task(session, text, dest, context, *, task_name: str):
        _ = context
        started.append(
            {
                "session_id": str(getattr(session, "id", "") or ""),
                "text": str(text or ""),
                "dest": dict(dest or {}),
                "task_name": str(task_name or ""),
            }
        )
        return True

    bot_app = SimpleNamespace(
        _shutdown_in_progress=False,
        ui_state=SimpleNamespace(pending={}),
        metrics=SimpleNamespace(inc=lambda name: metrics.__setitem__(str(name), int(metrics.get(str(name), 0)) + 1)),
        _send_message=_send_message,
        session_management=SimpleNamespace(start_prompt_task=_start_prompt_task),
        mode_tasks=SimpleNamespace(list=lambda **_kwargs: []),
    )
    service = InputDispatchService(bot_app, pending_input_ui=_RecordingInputTransport(sent))
    session = SimpleNamespace(
        id="s1",
        busy=False,
        queue=[],
        active_mode="agent",
        is_active_by_tick=lambda: bool(tick_state["active"]),
        run_lock=run_lock,
    )

    if signal_name == "busy":
        session.busy = True
    elif signal_name == "run_lock":
        await run_lock.acquire()
    elif signal_name == "tick":
        tick_state["active"] = True

    await service.handle_cli_input(
        session,
        f"{signal_name}-queued",
        chat_id=1,
        context=object(),
        dest={"kind": "telegram", "chat_id": 1},
    )

    pending = InputDispatchService.pending_head(
        bot_app.ui_state.pending,
        InputDispatchService._pending_ui_key({"kind": "telegram", "chat_id": 1}, 1),
    )
    assert pending is not None
    assert pending.text == f"{signal_name}-queued"
    assert pending.action == InputDispatchService.PENDING_ACTION_QUEUE_CONFIRM
    assert started == []
    assert sent == [InputDispatchService.queue_confirm_prompt_text()]
    assert int(metrics.get("queued", 0)) == 1

    if signal_name == "busy":
        session.busy = False
    elif signal_name == "run_lock":
        run_lock.release()
    elif signal_name == "tick":
        tick_state["active"] = False

    await service.handle_cli_input(
        session,
        f"{signal_name}-after-clear",
        chat_id=1,
        context=object(),
        dest={"kind": "telegram", "chat_id": 1},
    )

    assert [item["text"] for item in started] == [f"{signal_name}-after-clear"]
    assert started[0]["task_name"] == "input_dispatch.handle_cli_input.run_prompt"
