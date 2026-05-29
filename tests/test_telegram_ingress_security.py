from __future__ import annotations

import asyncio
from collections import deque
from types import SimpleNamespace

import pytest
from telegram.ext import CallbackQueryHandler, CommandHandler

from app.security.interfaces import AuthDecision
from app.services.access_policy_service import AccessPolicyService
from app.services.telegram_ui_scope import TelegramUiKey
from app.services.ui_state_models import ChatUiState
from agent.telegram_wiring import install_plugin_handlers
from bot import BotApp
from sessions.conversation_scope import ConversationScope
from tg.callbacks import CallbackHandler
from tg.handlers import PendingInput
from tg.message_processor import MessageProcessor
from tg.wiring import register_handlers


class _FakeApplication:
    def __init__(self) -> None:
        self.bot_data: dict[str, object] = {}
        self.handlers: list[tuple[object, int]] = []

    def add_handler(self, handler, group: int = 0) -> None:
        self.handlers.append((handler, int(group)))


class _FakeQuery:
    def __init__(self, data: str, *, chat_id: int = 1) -> None:
        self.data = data
        self.message = SimpleNamespace(chat_id=chat_id, message_id=10)

    async def answer(self) -> None:
        return None


class _SessionUi:
    async def handle_pending_message(self, _chat_id, _text, _context, *, message_thread_id=None) -> bool:
        _ = message_thread_id
        return False


class _Metrics:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def inc(self, name: str) -> None:
        self.calls.append(str(name))


def _text_message(text: str) -> SimpleNamespace:
    return SimpleNamespace(
        text=text,
        document=None,
        photo=None,
        video=None,
        audio=None,
        voice=None,
        sticker=None,
        animation=None,
        video_note=None,
    )


def _find_handler(app: _FakeApplication, handler_type, predicate) -> object:
    for handler, _group in app.handlers:
        if isinstance(handler, handler_type) and predicate(handler):
            return handler
    raise AssertionError(f"handler not found: {handler_type.__name__}")


def test_access_policy_service_delegates_to_security_facade() -> None:
    calls: list[tuple[int, str, bool]] = []
    sent: list[dict[str, object]] = []

    async def _send_message(_context, *, chat_id: int, text: str, **_kwargs):
        sent.append({"chat_id": int(chat_id), "text": str(text)})

    def _authorize(chat_id: int, *, scope: str = "generic", require_admin: bool = False) -> AuthDecision:
        calls.append((int(chat_id), str(scope), bool(require_admin)))
        if require_admin:
            return AuthDecision(
                chat_id=int(chat_id),
                allowed=False,
                scope=str(scope),
                is_admin=False,
                is_user=True,
                reason="admin_required",
            )
        return AuthDecision(
            chat_id=int(chat_id),
            allowed=True,
            scope=str(scope),
            is_admin=False,
            is_user=True,
            reason="",
        )

    bot_app = SimpleNamespace(
        security=SimpleNamespace(authorize=_authorize),
        _send_message=_send_message,
        config=SimpleNamespace(telegram=SimpleNamespace(whitelist_chat_ids=[7], user_modes={7: "all"})),
        mode_registry_service=SimpleNamespace(list_modes=lambda: [("agent", "Agent"), ("manager", "Manager")]),
    )
    service = AccessPolicyService(bot_app)

    assert service.is_allowed(7) is True
    assert service.is_admin(7, scope="files") is False
    assert asyncio.run(service.ensure_allowed(7, context=object())) is True
    assert set(service.allowed_mode_ids_for_chat(7)) == {"agent", "manager", "direct_cli", "orchestrator"}
    assert sent == []
    assert calls == [
        (7, "generic", False),
        (7, "files", True),
        (7, "generic", False),
        (7, "generic", False),
    ]


@pytest.mark.asyncio
async def test_tg_wiring_command_filter_uses_access_policy_service(monkeypatch) -> None:
    app = _FakeApplication()
    metrics = _Metrics()
    policy_calls: list[int] = []
    command_calls: list[int] = []
    allowed = {"value": False}

    async def _command_handler(update, _context) -> None:
        command_calls.append(int(update.effective_chat.id))

    bot_app = SimpleNamespace(
        access_policy_service=SimpleNamespace(
            is_allowed=lambda chat_id: policy_calls.append(int(chat_id)) or bool(allowed["value"])
        ),
        metrics=metrics,
        on_pre_command=(lambda *_args, **_kwargs: None),
        on_callback=(lambda *_args, **_kwargs: None),
        on_unknown_command=(lambda *_args, **_kwargs: None),
        on_photo=(lambda *_args, **_kwargs: None),
        on_document=(lambda *_args, **_kwargs: None),
        on_message=(lambda *_args, **_kwargs: None),
        is_allowed=(lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("legacy is_allowed used"))),
    )

    monkeypatch.setattr(
        "tg.wiring.build_command_registry",
        lambda _bot_app: [{"name": "files", "handler": _command_handler, "menu": True}],
    )
    monkeypatch.setattr("tg.wiring.install_plugin_handlers", lambda **_kwargs: None)

    register_handlers(app=app, bot_app=bot_app, config=object())
    handler = _find_handler(app, CommandHandler, lambda item: "files" in getattr(item, "commands", ()))
    update = SimpleNamespace(effective_chat=SimpleNamespace(id=7))

    await handler.callback(update, object())
    assert command_calls == []
    assert metrics.calls == []

    allowed["value"] = True
    await handler.callback(update, object())
    assert command_calls == [7]
    assert metrics.calls == ["commands"]
    assert policy_calls == [7, 7]


@pytest.mark.parametrize(
    ("command_name", "expected_allow_outside_topic", "expected_calls"),
    [
        ("tools", True, [7]),
        ("sessions", True, [7]),
        ("files", False, []),
    ],
)
def test_tg_wiring_passes_outside_topic_allowlist_to_authorizer(
    monkeypatch,
    command_name: str,
    expected_allow_outside_topic: bool,
    expected_calls: list[int],
) -> None:
    async def _run() -> None:
        app = _FakeApplication()
        metrics = _Metrics()
        command_calls: list[int] = []
        authorizer_calls: list[bool] = []

        async def _command_handler(update, _context) -> None:
            command_calls.append(int(update.effective_chat.id))

        async def _ensure_telegram_inbound_authorized(_update, _context, **kwargs):
            authorizer_calls.append(bool(kwargs.get("allow_outside_topic", False)))
            if bool(kwargs.get("allow_outside_topic", False)) != expected_allow_outside_topic:
                return None
            if not expected_calls:
                return None
            return SimpleNamespace(reply_chat_id=7, owner_chat_id=7, message_thread_id=None)

        bot_app = SimpleNamespace(
            access_policy_service=SimpleNamespace(is_allowed=lambda _chat_id: True),
            ensure_telegram_inbound_authorized=_ensure_telegram_inbound_authorized,
            metrics=metrics,
            on_pre_command=(lambda *_args, **_kwargs: None),
            on_callback=(lambda *_args, **_kwargs: None),
            on_unknown_command=(lambda *_args, **_kwargs: None),
            on_photo=(lambda *_args, **_kwargs: None),
            on_document=(lambda *_args, **_kwargs: None),
            on_message=(lambda *_args, **_kwargs: None),
        )

        monkeypatch.setattr(
            "tg.wiring.build_command_registry",
            lambda _bot_app: [{"name": command_name, "handler": _command_handler, "menu": True}],
        )
        monkeypatch.setattr("tg.wiring.install_plugin_handlers", lambda **_kwargs: None)

        register_handlers(app=app, bot_app=bot_app, config=object())
        handler = _find_handler(app, CommandHandler, lambda item: command_name in getattr(item, "commands", ()))
        update = SimpleNamespace(effective_chat=SimpleNamespace(id=7))

        await handler.callback(update, object())

        assert command_calls == expected_calls
        assert metrics.calls == (["commands"] if expected_calls else [])
        assert authorizer_calls == [expected_allow_outside_topic]

    asyncio.run(_run())


@pytest.mark.asyncio
async def test_agent_plugin_wiring_uses_access_policy_service(monkeypatch) -> None:
    app = _FakeApplication()
    calls: list[int] = []
    policy_calls: list[int] = []
    allowed = {"value": False}

    async def _plugin_handler(update, _context, **_kwargs):
        calls.append(int(update.effective_chat.id))
        return True

    runtime = SimpleNamespace(
        get_plugin_ui=lambda _profile: {
            "inline_handlers": [{"pattern": r"^plug$", "handler": _plugin_handler}],
            "message_handlers": [],
        }
    )
    bot_app = SimpleNamespace(
        access_policy_service=SimpleNamespace(
            is_allowed=lambda chat_id: policy_calls.append(int(chat_id)) or bool(allowed["value"])
        ),
        get_runtime_by_capability=lambda cap: runtime if str(cap) == "plugin_ui" else None,
        is_allowed=(lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("legacy is_allowed used"))),
    )

    monkeypatch.setattr("agent.telegram_wiring.get_tool_registry", lambda _config: object())
    monkeypatch.setattr("agent.telegram_wiring.build_default_profile", lambda _config, _registry: object())

    install_plugin_handlers(app, bot_app, config=object(), core_command_names=set())
    handler = _find_handler(app, CallbackQueryHandler, lambda _item: True)
    update = SimpleNamespace(effective_chat=SimpleNamespace(id=9))

    await handler.callback(update, object())
    assert calls == []

    allowed["value"] = True
    await handler.callback(update, object())
    assert calls == [9]
    assert policy_calls == [9, 9]


@pytest.mark.asyncio
async def test_bot_pre_command_uses_access_policy_service_instead_of_legacy_ensure_allowed() -> None:
    allow_calls: list[int] = []
    stop_calls: list[int] = []

    async def _ensure_allowed(chat_id: int, _context) -> bool:
        allow_calls.append(int(chat_id))
        return False

    fake_app = SimpleNamespace(
        _stop_files_rename_wait=lambda chat_id: stop_calls.append(int(chat_id)),
        access_policy_service=SimpleNamespace(ensure_allowed=_ensure_allowed),
        ensure_allowed=(lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("legacy ensure_allowed used"))),
    )
    update = SimpleNamespace(
        effective_chat=SimpleNamespace(id=11),
        message=SimpleNamespace(text="/files"),
    )

    await BotApp.on_pre_command(fake_app, update, context=object())

    assert allow_calls == [11]
    assert stop_calls == []


@pytest.mark.asyncio
async def test_bot_pre_command_passes_outside_topic_allowlist_to_authorizer() -> None:
    authorizer_calls: list[bool] = []

    async def _ensure_telegram_inbound_authorized(_update, _context, **kwargs):
        authorizer_calls.append(bool(kwargs.get("allow_outside_topic", False)))
        return None

    fake_app = SimpleNamespace(
        _stop_files_rename_wait=(lambda *_args, **_kwargs: None),
        ensure_telegram_inbound_authorized=_ensure_telegram_inbound_authorized,
        access_policy_service=SimpleNamespace(
            ensure_allowed=(
                lambda *_args, **_kwargs: (_ for _ in ()).throw(
                    AssertionError("unexpected legacy ensure_allowed")
                )
            )
        ),
    )
    update = SimpleNamespace(
        effective_chat=SimpleNamespace(id=11),
        message=SimpleNamespace(text="/sessions"),
    )

    await BotApp.on_pre_command(fake_app, update, context=object())

    assert authorizer_calls == [True]


@pytest.mark.asyncio
async def test_callback_handler_uses_access_policy_service_admin_scope_gate() -> None:
    edited: list[str] = []

    policy = SimpleNamespace(
        ensure_allowed=(lambda _chat_id, _context: asyncio.sleep(0, result=True)),
        callback_admin_scope=(lambda _chat_id, _data, **_kwargs: "files"),
        admin_denied_text=(lambda scope="generic": f"denied:{scope}"),
    )
    bot_app = SimpleNamespace(
        access_policy_service=policy,
        context_by_chat={},
        mode_callback_router=SimpleNamespace(
            resolve_dirs_mode_plugin=lambda _chat_id, _message_thread_id=None: ("", None, None, None)
        ),
        mode_dialogs=None,
        mode_registry_service=SimpleNamespace(),
        _send_message=(lambda *_args, **_kwargs: asyncio.sleep(0)),
        telegram_ui_key=(lambda chat_id, message_thread_id=None: TelegramUiKey.from_parts(chat_id, message_thread_id)),
        telegram_ui_key_from_query=(lambda query: TelegramUiKey.from_query(query)),
    )
    handler = CallbackHandler(bot_app)

    async def _edit_msg(_context, _query, text, *, reply_markup=None, md2=True):
        del reply_markup, md2
        edited.append(str(text))
        return True

    handler._edit_msg = _edit_msg
    update = SimpleNamespace(callback_query=_FakeQuery("file_nav:cancel", chat_id=15))

    await handler.handle_callback(update, context=object())

    assert edited == ["denied:files"]


@pytest.mark.asyncio
async def test_callback_handler_authorizes_group_topic_callbacks_by_owner() -> None:
    edited: list[str] = []
    ensure_calls: list[int] = []
    admin_calls: list[int] = []

    async def _ensure_allowed(chat_id: int, _context) -> bool:
        ensure_calls.append(int(chat_id))
        return True

    policy = SimpleNamespace(
        ensure_allowed=_ensure_allowed,
        callback_admin_scope=lambda chat_id, _data, **_kwargs: admin_calls.append(int(chat_id)) or "files",
        admin_denied_text=(lambda scope="generic": f"denied:{scope}"),
    )
    bot_app = SimpleNamespace(
        access_policy_service=policy,
        context_by_chat={},
        mode_callback_router=SimpleNamespace(
            resolve_dirs_mode_plugin=lambda _chat_id, _message_thread_id=None: ("", None, None, None)
        ),
        mode_dialogs=None,
        mode_registry_service=SimpleNamespace(),
        _send_message=(lambda *_args, **_kwargs: asyncio.sleep(0)),
        telegram_ui_key=(lambda chat_id, message_thread_id=None: TelegramUiKey.from_parts(chat_id, message_thread_id)),
        telegram_ui_key_from_query=(lambda query: TelegramUiKey.from_query(query)),
        resolve_telegram_callback_scope=lambda _query: (-100777000111, 77, 42, None),
    )
    handler = CallbackHandler(bot_app)

    async def _edit_msg(_context, _query, text, *, reply_markup=None, md2=True):
        del reply_markup, md2
        edited.append(str(text))
        return True

    handler._edit_msg = _edit_msg
    query = _FakeQuery("file_nav:cancel", chat_id=-100777000111)
    query.message.message_thread_id = 77
    update = SimpleNamespace(callback_query=query)

    await handler.handle_callback(update, context=object())

    assert ensure_calls == [42]
    assert admin_calls == [42]
    assert edited == ["denied:files"]


@pytest.mark.asyncio
async def test_callback_cancel_current_interrupts_owner_scope_for_group_topic() -> None:
    owner_chat_id = 42
    reply_chat_id = -100777000111
    message_thread_id = 77
    ui_key = TelegramUiKey.from_parts(reply_chat_id, message_thread_id)
    session = SimpleNamespace(id="s1")
    pending = PendingInput(
        session_id="s1",
        session_uid="thread:owner:s1",
        text="pending",
        dest={"kind": "telegram", "chat_id": reply_chat_id, "message_thread_id": message_thread_id},
    )
    ui_state = ChatUiState()
    ui_state.pending[ui_key] = deque([pending])
    interrupt_calls: list[dict[str, object]] = []
    responses: list[str] = []

    async def _interrupt_session_runtime(session_arg, **kwargs):
        interrupt_calls.append({"session": session_arg, **kwargs})
        return SimpleNamespace(status="completed")

    policy = SimpleNamespace(
        ensure_allowed=(lambda chat_id, _context: asyncio.sleep(0, result=int(chat_id) == owner_chat_id)),
        callback_admin_scope=(lambda *_args, **_kwargs: ""),
        admin_denied_text=(lambda scope="generic": f"denied:{scope}"),
    )
    bot_app = SimpleNamespace(
        access_policy_service=policy,
        context_by_chat={},
        ui_state=ui_state,
        manager=SimpleNamespace(get_by_uid=lambda _session_uid: session),
        git=SimpleNamespace(handle_callback=lambda *_args, **_kwargs: asyncio.sleep(0, result=False)),
        session_ui=SimpleNamespace(handle_callback=lambda *_args, **_kwargs: asyncio.sleep(0, result=False)),
        session_management=SimpleNamespace(interrupt_session_runtime=_interrupt_session_runtime),
        mode_callback_router=SimpleNamespace(
            resolve_dirs_mode_plugin=lambda _chat_id, _message_thread_id=None: ("", None, None, None)
        ),
        mode_dialogs=None,
        mode_registry_service=SimpleNamespace(),
        _send_message=(lambda *_args, **_kwargs: asyncio.sleep(0)),
        telegram_ui_key=(lambda chat_id, message_thread_id=None: TelegramUiKey.from_parts(chat_id, message_thread_id)),
        telegram_ui_key_from_query=(lambda query: TelegramUiKey.from_query(query)),
        resolve_telegram_callback_scope=lambda _query: (reply_chat_id, message_thread_id, owner_chat_id, session),
    )
    handler = CallbackHandler(bot_app)

    async def _respond_callback(**kwargs):
        responses.append(str(kwargs["text"]))

    async def _show_next_pending_input(**_kwargs):
        return None

    handler._respond_callback = _respond_callback  # type: ignore[method-assign]
    handler._show_next_pending_input = _show_next_pending_input  # type: ignore[method-assign]
    query = _FakeQuery("cancel_current", chat_id=reply_chat_id)
    query.message.message_thread_id = message_thread_id
    update = SimpleNamespace(callback_query=query)

    await handler.handle_callback(update, context=object())

    assert interrupt_calls[0]["owner_chat_id"] == owner_chat_id
    assert interrupt_calls[0]["reply_chat_id"] == reply_chat_id
    assert interrupt_calls[0]["message_thread_id"] == message_thread_id
    assert responses == ["Текущая генерация прервана. Сессия освобождена. Ввод отброшен."]


@pytest.mark.asyncio
async def test_message_processor_uses_access_policy_service_for_admin_only_path_input(tmp_path) -> None:
    sent: list[str] = []
    ui_key = TelegramUiKey.from_parts(21, None)
    policy = SimpleNamespace(
        ensure_allowed=(lambda _chat_id, _context: asyncio.sleep(0, result=True)),
        can_input_project_path=(lambda _chat_id, **_kwargs: False),
        admin_denied_text=(lambda scope="generic": f"denied:{scope}"),
    )

    async def _send_message(_context, *, chat_id: int, text: str, **_kwargs):
        assert int(chat_id) == 21
        sent.append(str(text))

    ui_state = ChatUiState()
    ui_state.pending_dir_input[ui_key] = True
    ui_state.pending_new_tool[ui_key] = "dummy"
    ui_state.dirs_mode[ui_key] = "new_session"
    bot_app = SimpleNamespace(
        access_policy_service=policy,
        ui_state=ui_state,
        metrics=_Metrics(),
        session_ui=_SessionUi(),
        _resolve_pending_custom_answer=lambda *_args, **_kwargs: False,
        _send_message=_send_message,
        config=SimpleNamespace(defaults=SimpleNamespace(workdir=str(tmp_path))),
        telegram_ui_key=(lambda chat_id, message_thread_id=None: TelegramUiKey.from_parts(chat_id, message_thread_id)),
    )
    processor = MessageProcessor(bot_app)
    update = SimpleNamespace(
        effective_chat=SimpleNamespace(id=21),
        effective_user=SimpleNamespace(id=2100),
        message=_text_message("/tmp/project"),
    )

    await processor.process_message(update, context=object())

    assert sent == ["denied:new_projects"]
    assert bot_app.ui_state.pending_new_tool == {}
    assert bot_app.ui_state.dirs_mode == {}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("owner_chat_id", "ui_chat_id", "message_thread_id"),
    [
        (42, -100777000111, 101),
        (21, 21, None),
    ],
)
async def test_message_processor_routes_pending_session_creation_by_owner_and_ui_scope(
    tmp_path,
    owner_chat_id: int,
    ui_chat_id: int,
    message_thread_id: int | None,
) -> None:
    create_calls: list[dict[str, object]] = []
    sent: list[dict[str, object]] = []
    ui_key = TelegramUiKey.from_parts(ui_chat_id, message_thread_id)
    ui_state = ChatUiState()
    ui_state.pending_dir_input[ui_key] = True
    ui_state.pending_new_tool[ui_key] = "dummy"
    ui_state.dirs_root[ui_key] = str(tmp_path)
    route = SimpleNamespace(
        owner_chat_id=owner_chat_id,
        reply_chat_id=ui_chat_id,
        message_thread_id=message_thread_id,
        session_uid=None,
        session=None,
        reply_kwargs=lambda: ui_key.reply_kwargs(),
    )

    async def _ensure_telegram_inbound_authorized(_update, _context):
        return route

    async def _create_from_pending_tool(
        owner_chat_id: int,
        path: str,
        *,
        root=None,
        clear_dirs_mode: bool = False,
        bot=None,
        message_thread_id=None,
        ui_chat_id=None,
    ):
        create_calls.append(
            {
                "owner_chat_id": int(owner_chat_id),
                "path": str(path),
                "root": root,
                "clear_dirs_mode": bool(clear_dirs_mode),
                "bot": bot,
                "message_thread_id": message_thread_id,
                "ui_chat_id": ui_chat_id,
            }
        )
        popped = ui_state.pending_new_tool.pop(
            TelegramUiKey.from_parts(ui_chat_id, message_thread_id),
            None,
        )
        assert popped == "dummy"
        scope = ConversationScope.from_parts(
            ui_chat_id,
            909 if int(ui_chat_id) < 0 else None,
        )
        return SimpleNamespace(
            id="s-created",
            chat_id=int(owner_chat_id),
            conversation_scope=scope,
        ), None

    async def _send_message(_context, *, chat_id: int, text: str, message_thread_id=None, **_kwargs):
        sent.append(
            {
                "chat_id": int(chat_id),
                "text": str(text),
                "message_thread_id": message_thread_id,
            }
        )

    policy = SimpleNamespace(
        can_input_project_path=lambda _chat_id, **_kwargs: True,
    )
    bot_app = SimpleNamespace(
        ensure_telegram_inbound_authorized=_ensure_telegram_inbound_authorized,
        access_policy_service=policy,
        ui_state=ui_state,
        metrics=_Metrics(),
        session_ui=_SessionUi(),
        _resolve_pending_custom_answer=lambda *_args, **_kwargs: False,
        _plugin_awaiting_input=lambda _chat_id: False,
        _send_message=_send_message,
        session_creation_service=SimpleNamespace(create_from_pending_tool=_create_from_pending_tool),
        handlers=SimpleNamespace(
            build_sessions_active_overview=lambda owner_chat_id, session=None: (
                f"overview:{owner_chat_id}:{getattr(session, 'id', '')}",
                None,
            )
        ),
        config=SimpleNamespace(defaults=SimpleNamespace(workdir=str(tmp_path))),
        is_within_root=lambda path, root: True,
        telegram_ui_key=(lambda chat_id, message_thread_id=None: TelegramUiKey.from_parts(chat_id, message_thread_id)),
        build_telegram_reply_dest=lambda session, chat_id, user_id=None: {
            "kind": "telegram",
            "chat_id": int(chat_id),
            "message_thread_id": getattr(getattr(session, "conversation_scope", None), "message_thread_id", None),
        },
    )
    processor = MessageProcessor(bot_app)
    message = _text_message(str(tmp_path))
    message.message_thread_id = message_thread_id
    update = SimpleNamespace(
        effective_chat=SimpleNamespace(id=ui_chat_id),
        effective_user=SimpleNamespace(id=owner_chat_id),
        message=message,
    )
    context = SimpleNamespace(bot=object())

    await processor.process_message(update, context=context)

    assert create_calls == [
        {
            "owner_chat_id": owner_chat_id,
            "path": str(tmp_path),
            "root": None,
            "clear_dirs_mode": False,
            "bot": context.bot,
            "message_thread_id": message_thread_id,
            "ui_chat_id": ui_chat_id,
        }
    ]
    if int(ui_chat_id) < 0:
        assert sent == [
            {
                "chat_id": ui_chat_id,
                "text": "Сессия s-created создана. Продолжайте в новом topic.",
                "message_thread_id": message_thread_id,
            },
            {
                "chat_id": ui_chat_id,
                "text": f"overview:{owner_chat_id}:s-created",
                "message_thread_id": 909,
            },
        ]
    else:
        assert sent == [
            {
                "chat_id": ui_chat_id,
                "text": "Сессия s-created создана и выбрана.",
                "message_thread_id": None,
            }
        ]
    assert bot_app.ui_state.context_by_chat[ui_chat_id] is context
