import asyncio
import logging
import re
import types
from collections import deque
from io import StringIO

import pytest

from app.security.interfaces import AuthDecision
from app.services.input_dispatch_service import InputDispatchService
from app.services.telegram_ui_scope import TelegramUiKey
from session import session_runtime_uid
from tg import callbacks as tg_callbacks
from tg.callbacks import CallbackHandler
from modes.registry import ModeRegistry
from modes.sdk import BaseMode, DialogService, ModeCallbackRouterService, ModeRegistryService, ToolResult
from modes.sdk.services.tasks import TaskService as ModeTaskService


class _FakeMessage:
    def __init__(self, chat_id: int = 1, message_id: int = 10) -> None:
        self.chat_id = chat_id
        self.message_id = message_id


class _FakeQuery:
    def __init__(self, data: str) -> None:
        self.data = data
        self.message = _FakeMessage()
        self.from_user = types.SimpleNamespace(id=42)

    async def answer(self) -> None:
        return None


class _FakeManager:
    def __init__(self, session) -> None:
        self._session = session

    def active(self, _chat_id: int):
        return self._session

    def get(self, _chat_id: int, session_id: str):
        if str(getattr(self._session, "id", "") or "") == str(session_id or ""):
            return self._session
        return None

    def get_by_uid(self, session_uid: str):
        raw_uid = getattr(getattr(self._session, "conversation_scope", None), "session_uid", None)
        if str(raw_uid or "") == str(session_uid or ""):
            return self._session
        return None


class _FakePendingInputUi:
    def __init__(self, app) -> None:
        self.app = app

    async def send_decision(self, _context, decision, *, dest, fallback_chat_id):
        chat_id = int(dict(dest or {}).get("chat_id", fallback_chat_id))
        self.app.sent.append((chat_id, str(decision.text or "")))
        return types.SimpleNamespace(message_id=len(self.app.sent))

    async def send_text(self, _context, *, text: str, dest, fallback_chat_id, md2: bool = True):
        _ = md2
        chat_id = int(dict(dest or {}).get("chat_id", fallback_chat_id))
        self.app.sent.append((chat_id, str(text or "")))
        return types.SimpleNamespace(message_id=len(self.app.sent))

    async def retire_prompt(self, *_args, **_kwargs):
        return True


class _FakeBotApp:
    def __init__(self, session) -> None:
        self.manager = _FakeManager(session)
        self.config = types.SimpleNamespace(
            telegram=types.SimpleNamespace(user_languages={}),
            defaults=types.SimpleNamespace(default_language="ru"),
        )
        self.mode_registry = ModeRegistry()
        self.mode_registry_service = ModeRegistryService(self.mode_registry)
        self.mode_dialogs = DialogService()
        self.sent = []
        self.ui_state = types.SimpleNamespace(
            context_by_chat={},
            pending_questions={},
            active_ask_question_by_chat={},
            dirs_mode={},
            pending={},
        )
        self.mode_callback_router = ModeCallbackRouterService(
            mode_registry=self.mode_registry_service,
            dialogs=self.mode_dialogs,
            send_message=self._send_message,
            get_session=lambda chat_id: self.manager.active(chat_id),
            get_dirs_mode_token=lambda chat_id, message_thread_id=None: str(
                self.ui_state.dirs_mode.get(TelegramUiKey.from_parts(int(chat_id), message_thread_id), "") or ""
            ),
            clear_dirs_mode_token=lambda chat_id, message_thread_id=None: self.ui_state.dirs_mode.pop(
                TelegramUiKey.from_parts(int(chat_id), message_thread_id),
                None,
            ),
        )
        self._allow_modes = True
        self._shutdown_in_progress = False
        self.handled_user_inputs = []
        self.run_prompt_calls = []
        self.pending_input_ui = _FakePendingInputUi(self)
        self.input_dispatch_service = InputDispatchService(self, pending_input_ui=self.pending_input_ui)
        self.git = types.SimpleNamespace(handle_callback=(lambda *_a, **_k: asyncio.sleep(0, result=False)))
        self.session_ui = types.SimpleNamespace(handle_callback=(lambda *_a, **_k: asyncio.sleep(0, result=False)))
        self.mode_session_control = types.SimpleNamespace(persist=(lambda: None), cancel_session=(lambda **_k: asyncio.sleep(0)))
        self.mode_launch_checks = []
        self.access_policy_service = types.SimpleNamespace(
            ensure_allowed=(lambda _chat_id, _context: asyncio.sleep(0, result=True)),
            is_mode_allowed_for_chat=(lambda _chat_id, _mode_id: bool(self._allow_modes)),
            is_admin=(lambda _chat_id, scope="generic": bool(self._is_admin)),
            callback_admin_scope=(lambda _chat_id, data, **kwargs: self._callback_admin_scope(data, **kwargs)),
            admin_denied_text=(lambda scope="generic": f"denied:{scope}"),
        )
        self.security = types.SimpleNamespace(authorize_mode_launch=self._authorize_mode_launch)
        self._is_admin = True
        self.resolve_telegram_callback_scope = (
            lambda query: (
                int(getattr(getattr(query, "message", None), "chat_id", 0) or 0),
                None,
                int(getattr(getattr(query, "message", None), "chat_id", 0) or 0),
                self.manager.active(int(getattr(getattr(query, "message", None), "chat_id", 0) or 0)),
            )
        )

    @staticmethod
    def telegram_ui_key(chat_id: int, message_thread_id=None) -> TelegramUiKey:
        return TelegramUiKey.from_parts(chat_id, message_thread_id)

    @staticmethod
    def telegram_ui_key_from_query(query):
        return TelegramUiKey.from_query(query)

    def _clear_pending_question(self, question_id: str) -> bool:
        qid = str(question_id or "").strip()
        meta = self.ui_state.pending_questions.pop(qid, None)
        if not isinstance(meta, dict):
            return False
        ui_key = self.telegram_ui_key(
            int(meta.get("chat_id") or 0),
            meta.get("message_thread_id"),
        )
        if str(self.ui_state.active_ask_question_by_chat.get(ui_key) or "") == qid:
            self.ui_state.active_ask_question_by_chat.pop(ui_key, None)
        return True

    def is_admin(self, _chat_id: int) -> bool:
        return bool(self._is_admin)

    async def _authorize_mode_launch(
        self,
        chat_id: int,
        *,
        mode_id: str,
        is_mode_allowed: bool = True,
        action: str = "enable",
        session_id: str = "",
        context=None,
    ) -> AuthDecision:
        self.mode_launch_checks.append(
            {
                "chat_id": int(chat_id),
                "mode_id": str(mode_id),
                "is_mode_allowed": bool(is_mode_allowed),
                "action": str(action),
                "session_id": str(session_id),
                "context": dict(context or {}),
            }
        )
        return AuthDecision(
            chat_id=int(chat_id),
            allowed=bool(is_mode_allowed),
            scope=f"mode.launch.{mode_id}",
            is_admin=bool(self._is_admin),
            is_user=True,
            reason="" if is_mode_allowed else "mode_not_allowed",
        )

    def _callback_admin_scope(self, data: str, *, mode_id: str = "", flow: str = "") -> str:
        if self._is_admin:
            return ""
        token = str(data or "")
        parts = [part.strip().lower() for part in token.split(":") if part.strip()]
        if parts and parts[0] in ("ma", "mode_action") and "promote_skills" in parts:
            return "global_skills"
        if token.startswith("file_") or token.startswith("file_nav:") or token.startswith("file_pick:"):
            return "files"
        if token.startswith("git_") or token.startswith("gitpull_") or token.startswith("git_conflict"):
            return "git"
        if token == "dir_git_clone":
            return "git"
        if token in {"dir_enter", "dir_create"} or token.startswith("dir_create:"):
            return "new_projects"
        if token.startswith("dir_") or token.startswith("dir_pick:") or token.startswith("dir_page:"):
            if not (str(mode_id or "").strip() and str(flow or "").strip()):
                return "new_projects"
        return ""

    async def _send_message(self, _context, *, chat_id: int, text: str, **_kwargs):
        self.sent.append((chat_id, text))
        return True

    async def _edit_message(self, *_a, **_k):
        return True

    async def _handle_user_input(self, session, text, chat_id, context, dest=None):
        _ = context
        self.handled_user_inputs.append(
            {
                "session_id": str(getattr(session, "id", "") or ""),
                "text": str(text or ""),
                "chat_id": int(chat_id),
                "dest": dict(dest or {}),
            }
        )
        return None

    async def run_prompt(self, session, text, dest, context):
        _ = context
        self.run_prompt_calls.append(
            {
                "session_id": str(getattr(session, "id", "") or ""),
                "text": str(text or ""),
                "dest": dict(dest or {}),
            }
        )
        return ""


_MODE_CHANGE_CALLBACK_INVENTORY = [
    ("agent", "enable"),
    ("analyst", "disable"),
    ("manager", "enable"),
    ("webmaster", "disable"),
    ("admin", "enable"),
    ("codebase_mapper", "disable"),
]

_DESTRUCTIVE_CALLBACK_INVENTORY = [
    ("agent", "clean_all"),
    ("agent", "project_disconnect"),
    ("webmaster", "reset"),
    ("manager", "reset"),
]

_SHARED_RUN_CALLBACK_INVENTORY = [
    ("agent", "apply_recommendation"),
    ("agent", "recover"),
    ("agent", "resume"),
    ("analyst", "apply_recommendation"),
    ("analyst", "recover"),
    ("analyst", "resume"),
    ("manager", "apply_recommendation"),
    ("manager", "recover"),
    ("manager", "resume"),
    ("webmaster", "apply_recommendation"),
    ("webmaster", "recover"),
    ("webmaster", "resume"),
]


def test_mode_action_routes_to_plugin_handle_callback():
    class EchoMode(BaseMode):
        mode_id = "echo"

        async def handle_input(self, message, ctx):
            return ToolResult.ok()

        async def handle_callback(self, callback, ctx):
            assert callback.action == "do"
            assert callback.payload["x"] == 1
            return ToolResult.ok("OK")

    session = types.SimpleNamespace(id="s1", active_mode="echo")
    app = _FakeBotApp(session)
    app.mode_registry.register(EchoMode())

    handler = CallbackHandler(app)
    update = types.SimpleNamespace(callback_query=_FakeQuery('ma:do:{"x": 1}'))
    asyncio.run(handler.handle_callback(update, context=object()))

    assert app.sent == [(1, "OK")]


def test_mode_action_explicit_session_uid_overrides_scope_session():
    handled = {"session_id": None, "payload": None}

    class EchoMode(BaseMode):
        mode_id = "echo"

        async def handle_input(self, message, ctx):
            return ToolResult.ok()

        async def handle_callback(self, callback, ctx):
            handled["session_id"] = getattr(ctx.get("session"), "id", None)
            handled["payload"] = dict(callback.payload)
            return ToolResult.ok("OK")

    scoped_session = types.SimpleNamespace(
        id="s1",
        active_mode="echo",
        busy=False,
        queue=[],
        interrupt=(lambda: None),
        modes=types.SimpleNamespace(active_mode="echo"),
        conversation_scope=types.SimpleNamespace(session_uid="chat:1"),
    )
    explicit_session = types.SimpleNamespace(
        id="s2",
        active_mode="echo",
        busy=False,
        queue=[],
        interrupt=(lambda: None),
        modes=types.SimpleNamespace(active_mode="echo"),
        conversation_scope=types.SimpleNamespace(session_uid="chat:1"),
    )

    class _TwoSessionManager:
        def __init__(self, active_session, other_session) -> None:
            self._active_session = active_session
            self._other_session = other_session
            self.lookups: list[str] = []

        def active(self, _chat_id: int):
            return self._active_session

        def get_by_uid(self, session_uid: str):
            token = str(session_uid or "")
            self.lookups.append(token)
            if token == session_runtime_uid(self._active_session):
                return self._active_session
            if token == session_runtime_uid(self._other_session):
                return self._other_session
            return None

    app = _FakeBotApp(scoped_session)
    app.mode_registry.register(EchoMode())
    manager = _TwoSessionManager(scoped_session, explicit_session)
    app.manager = manager
    app.mode_callback_router.get_session = lambda chat_id: manager.active(chat_id)

    callback_data = f"ma:echo:status:s={session_runtime_uid(explicit_session)}"
    ok = asyncio.run(
        app.mode_callback_router.handle_mode_action_callback(
            data=callback_data,
            chat_id=1,
            query=_FakeQuery(callback_data),
            context=object(),
            bot_app=app,
        )
    )

    assert ok is True
    assert handled["session_id"] == explicit_session.id
    assert handled["payload"] == {"s": session_runtime_uid(explicit_session)}
    assert manager.lookups == [session_runtime_uid(explicit_session)]
    assert app.sent == [(1, "OK")]


def test_dialog_service_intercepts_mode_action_callbacks():
    called = {"dlg": 0}

    async def _dlg_handler(_cb, ctx):
        called["dlg"] += 1
        assert ctx["dialog"]["step"] == "x"
        return ToolResult.ok("DIALOG")

    class EchoMode(BaseMode):
        mode_id = "echo"

        async def handle_input(self, message, ctx):
            return ToolResult.ok()

        async def handle_callback(self, callback, ctx):
            raise AssertionError("plugin must not be called when dialog is active")

    session = types.SimpleNamespace(id="s1", active_mode="echo")
    app = _FakeBotApp(session)
    app.mode_registry.register(EchoMode())
    app.mode_dialogs.start(chat_id=1, session_id="s1", mode_id="echo", on_callback=_dlg_handler, data={"step": "x"})

    handler = CallbackHandler(app)
    update = types.SimpleNamespace(callback_query=_FakeQuery("ma:next"))
    asyncio.run(handler.handle_callback(update, context=object()))

    assert called["dlg"] == 1
    assert app.sent == [(1, "DIALOG")]


def test_non_mode_action_callbacks_fall_through_to_common_handler():
    # This test only checks that mode router doesn't crash or consume unrelated callbacks.
    # Use "ask:" which is handled by the common callback branch later.
    session = types.SimpleNamespace(id="s1", active_mode="echo")
    app = _FakeBotApp(session)
    app.ui_state.pending_questions = {"q1": {"options": ["A"], "chat_id": 1}}

    handler = CallbackHandler(app)
    update = types.SimpleNamespace(callback_query=_FakeQuery("ask:q1:0"))
    # agent.resolve_question is referenced by ask branch
    app.get_runtime_by_capability = lambda cap: (
        types.SimpleNamespace(resolve_question=lambda *_a, **_k: True)
        if str(cap) == "resolve_question"
        else None
    )

    asyncio.run(handler.handle_callback(update, context=object()))
    # Common branch uses edit message; our fake doesn't store edits, but should not send messages.
    assert app.sent == []


def test_sess_mode_routes_to_registered_mode_menu():
    class EchoMode(BaseMode):
        mode_id = "echo"
        display_name = "Echo"

        async def handle_input(self, message, ctx):
            return ToolResult.ok()

        async def handle_callback(self, callback, ctx):
            return ToolResult.ok("fallback-menu")

        def build_menu(self, session, back_callback="sess_active", back_text="⬅️ Назад"):
            from telegram import InlineKeyboardButton, InlineKeyboardMarkup

            return "Echo menu", InlineKeyboardMarkup([[InlineKeyboardButton(back_text, callback_data=back_callback)]])

    session = types.SimpleNamespace(id="s1", active_mode="echo")
    app = _FakeBotApp(session)
    app.mode_registry.register(EchoMode())
    edited = {"text": None}

    handler = CallbackHandler(app)

    async def _fake_edit_msg(_context, _query, text, *, reply_markup=None, md2=True):
        edited["text"] = text
        return True

    handler._edit_msg = _fake_edit_msg
    update = types.SimpleNamespace(callback_query=_FakeQuery("sess_mode:echo"))
    asyncio.run(handler.handle_callback(update, context=object()))
    assert edited["text"] == "Echo menu"


def test_mode_action_denied_when_mode_not_allowed():
    class EchoMode(BaseMode):
        mode_id = "echo"

        async def handle_input(self, message, ctx):
            return ToolResult.ok()

        async def handle_callback(self, callback, ctx):
            raise AssertionError("must not be called when mode is denied")

    session = types.SimpleNamespace(id="s1", active_mode="echo")
    app = _FakeBotApp(session)
    app.mode_registry.register(EchoMode())
    app._allow_modes = False

    handler = CallbackHandler(app)
    update = types.SimpleNamespace(callback_query=_FakeQuery("ma:echo:enable"))
    asyncio.run(handler.handle_callback(update, context=object()))

    assert app.sent == [(1, "Режим недоступен для вашего пользователя.")]
    assert app.mode_launch_checks == [
        {
            "chat_id": 1,
            "mode_id": "echo",
            "is_mode_allowed": False,
            "action": "enable",
            "session_id": "s1",
            "context": {
                "callback_data": "ma:echo:enable",
                "user_id": 42,
            },
        }
    ]


def test_mode_action_enable_routes_through_security_before_plugin():
    calls = []

    class EchoMode(BaseMode):
        mode_id = "echo"

        async def handle_input(self, message, ctx):
            return ToolResult.ok()

        async def handle_callback(self, callback, ctx):
            _ = ctx
            calls.append(str(callback.action or ""))
            return ToolResult.ok("MODE_ENABLED")

    session = types.SimpleNamespace(id="s1", active_mode="echo")
    app = _FakeBotApp(session)
    app.mode_registry.register(EchoMode())

    handler = CallbackHandler(app)
    update = types.SimpleNamespace(callback_query=_FakeQuery("ma:echo:enable"))
    asyncio.run(handler.handle_callback(update, context=object()))

    assert calls == ["enable"]
    assert app.sent == [(1, "MODE_ENABLED")]
    assert app.mode_launch_checks == [
        {
            "chat_id": 1,
            "mode_id": "echo",
            "is_mode_allowed": True,
            "action": "enable",
            "session_id": "s1",
            "context": {
                "callback_data": "ma:echo:enable",
                "user_id": 42,
            },
        }
    ]


def test_mode_action_enable_disable_blocked_when_session_busy():
    class EchoMode(BaseMode):
        mode_id = "echo"

        async def handle_input(self, message, ctx):
            return ToolResult.ok()

        async def handle_callback(self, callback, ctx):
            raise AssertionError("must not be called when session is busy for mode changes")

    session = types.SimpleNamespace(id="s1", active_mode="echo", busy=True, queue=[])
    app = _FakeBotApp(session)
    app.mode_registry.register(EchoMode())

    handler = CallbackHandler(app)
    update = types.SimpleNamespace(callback_query=_FakeQuery("ma:echo:disable"))
    asyncio.run(handler.handle_callback(update, context=object()))
    assert app.sent == [(1, "Сессия занята. Переключение/выключение режима доступно только когда сессия свободна.")]


@pytest.mark.parametrize(("mode_id", "action"), _MODE_CHANGE_CALLBACK_INVENTORY)
def test_mode_action_enable_disable_inventory_blocked_by_all_busy_signals_and_recover(mode_id: str, action: str) -> None:
    calls: list[str] = []

    class EchoMode(BaseMode):
        def __init__(self) -> None:
            super().__init__()
            self.mode_id = mode_id

        async def handle_input(self, message, ctx):
            return ToolResult.ok()

        async def handle_callback(self, callback, ctx):
            _ = ctx
            calls.append(str(callback.action or ""))
            return ToolResult.ok(f"{mode_id}:{action}:OK")

    async def _run() -> None:
        tick_state = {"active": False}
        session = types.SimpleNamespace(
            id="s1",
            active_mode=mode_id,
            busy=False,
            queue=[],
            run_lock=asyncio.Lock(),
            is_active_by_tick=lambda: bool(tick_state["active"]),
        )
        app = _FakeBotApp(session)
        app.mode_registry.register(EchoMode())

        handler = CallbackHandler(app)
        update = types.SimpleNamespace(callback_query=_FakeQuery(f"ma:{mode_id}:{action}"))
        busy_msg = "Сессия занята. Переключение/выключение режима доступно только когда сессия свободна."

        session.busy = True
        await handler.handle_callback(update, context=object())
        assert calls == []
        assert app.sent[-1] == (1, busy_msg)

        session.busy = False
        await handler.handle_callback(update, context=object())
        assert calls == [action]
        assert app.sent[-1] == (1, f"{mode_id}:{action}:OK")

        await session.run_lock.acquire()
        try:
            await handler.handle_callback(update, context=object())
            assert calls == [action]
            assert app.sent[-1] == (1, busy_msg)
        finally:
            session.run_lock.release()

        await handler.handle_callback(update, context=object())
        assert calls == [action, action]
        assert app.sent[-1] == (1, f"{mode_id}:{action}:OK")

        tick_state["active"] = True
        await handler.handle_callback(update, context=object())
        assert calls == [action, action]
        assert app.sent[-1] == (1, busy_msg)

        tick_state["active"] = False
        await handler.handle_callback(update, context=object())
        assert calls == [action, action, action]
        assert app.sent[-1] == (1, f"{mode_id}:{action}:OK")

    asyncio.run(_run())


def test_mode_action_destructive_blocked_when_session_busy_for_agent_webmaster_manager():
    calls = []

    class ProbeMode(BaseMode):
        def __init__(self, mode_id: str) -> None:
            super().__init__()
            self.mode_id = mode_id

        async def handle_input(self, message, ctx):
            return ToolResult.ok()

        async def handle_callback(self, callback, ctx):
            _ = ctx
            calls.append((self.mode_id, str(callback.action or "").strip()))
            return ToolResult.ok("SHOULD_NOT_BE_SENT")

    session = types.SimpleNamespace(id="s1", active_mode="agent", busy=True, queue=[])
    app = _FakeBotApp(session)
    for mode_id in ("agent", "webmaster", "manager"):
        app.mode_registry.register(ProbeMode(mode_id))

    handler = CallbackHandler(app)
    busy_msg = "Сессия занята. Деструктивные действия (reset/clean/disconnect) доступны только когда сессия свободна."
    destructive_callbacks = [
        "ma:agent:clean_all",
        "ma:agent:project_disconnect",
        "ma:webmaster:reset",
        "ma:manager:reset",
    ]
    for data in destructive_callbacks:
        update = types.SimpleNamespace(callback_query=_FakeQuery(data))
        asyncio.run(handler.handle_callback(update, context=object()))
        assert app.sent[-1] == (1, busy_msg)

    assert calls == []
    assert all("SHOULD_NOT_BE_SENT" not in text for _chat_id, text in app.sent)


@pytest.mark.parametrize(("mode_id", "action"), _DESTRUCTIVE_CALLBACK_INVENTORY)
def test_mode_action_destructive_inventory_blocked_by_all_busy_signals_and_recover(mode_id: str, action: str) -> None:
    calls: list[str] = []

    class ProbeMode(BaseMode):
        def __init__(self) -> None:
            super().__init__()
            self.mode_id = mode_id

        async def handle_input(self, message, ctx):
            return ToolResult.ok()

        async def handle_callback(self, callback, ctx):
            _ = ctx
            calls.append(str(callback.action or ""))
            return ToolResult.ok(f"{mode_id}:{action}:OK")

    async def _run() -> None:
        tick_state = {"active": False}
        session = types.SimpleNamespace(
            id="s1",
            active_mode=mode_id,
            busy=False,
            queue=[],
            run_lock=asyncio.Lock(),
            is_active_by_tick=lambda: bool(tick_state["active"]),
        )
        app = _FakeBotApp(session)
        app.mode_registry.register(ProbeMode())

        handler = CallbackHandler(app)
        update = types.SimpleNamespace(callback_query=_FakeQuery(f"ma:{mode_id}:{action}"))
        busy_msg = "Сессия занята. Деструктивные действия (reset/clean/disconnect) доступны только когда сессия свободна."

        session.busy = True
        await handler.handle_callback(update, context=object())
        assert calls == []
        assert app.sent[-1] == (1, busy_msg)

        session.busy = False
        await handler.handle_callback(update, context=object())
        assert calls == [action]
        assert app.sent[-1] == (1, f"{mode_id}:{action}:OK")

        await session.run_lock.acquire()
        try:
            await handler.handle_callback(update, context=object())
            assert calls == [action]
            assert app.sent[-1] == (1, busy_msg)
        finally:
            session.run_lock.release()

        await handler.handle_callback(update, context=object())
        assert calls == [action, action]
        assert app.sent[-1] == (1, f"{mode_id}:{action}:OK")

        tick_state["active"] = True
        await handler.handle_callback(update, context=object())
        assert calls == [action, action]
        assert app.sent[-1] == (1, busy_msg)

        tick_state["active"] = False
        await handler.handle_callback(update, context=object())
        assert calls == [action, action, action]
        assert app.sent[-1] == (1, f"{mode_id}:{action}:OK")

    asyncio.run(_run())


def test_mode_action_non_destructive_still_routes_when_session_busy():
    calls = {"count": 0}

    class EchoMode(BaseMode):
        mode_id = "manager"

        async def handle_input(self, message, ctx):
            return ToolResult.ok()

        async def handle_callback(self, callback, ctx):
            _ = ctx
            calls["count"] += 1
            assert callback.action == "status"
            return ToolResult.ok("STATUS_OK")

    session = types.SimpleNamespace(id="s1", active_mode="manager", busy=True, queue=[])
    app = _FakeBotApp(session)
    app.mode_registry.register(EchoMode())

    handler = CallbackHandler(app)
    update = types.SimpleNamespace(callback_query=_FakeQuery("ma:manager:status"))
    asyncio.run(handler.handle_callback(update, context=object()))

    assert calls["count"] == 1
    assert app.sent[-1] == (1, "STATUS_OK")


@pytest.mark.parametrize("signal_name", ["busy", "run_lock", "tick"])
def test_take_pending_input_requeues_while_running_and_recovers_after_signal_clears(signal_name: str) -> None:
    async def _run() -> None:
        tick_state = {"active": False}
        session = types.SimpleNamespace(
            id="s1",
            active_mode="agent",
            busy=False,
            queue=[],
            run_lock=asyncio.Lock(),
            is_active_by_tick=lambda: bool(tick_state["active"]),
            conversation_scope=types.SimpleNamespace(session_uid="forum:-100777000111:101"),
        )
        app = _FakeBotApp(session)
        dispatch = app.input_dispatch_service
        pending = dispatch._build_pending_input(
            session=session,
            text="pending work",
            chat_id=1,
            dest={"kind": "telegram", "chat_id": 1},
            image_path=None,
            image_paths=None,
            action=InputDispatchService.PENDING_ACTION_CONFIRM,
        )
        ui_key = TelegramUiKey.from_parts(1)
        app.ui_state.pending[ui_key] = deque([pending])
        handler = CallbackHandler(app)
        edited: list[str] = []

        async def _fake_edit_msg(_context, _query, text, *, reply_markup=None, md2=True):
            _ = reply_markup, md2
            edited.append(str(text or ""))
            return True

        handler._edit_msg = _fake_edit_msg
        update = types.SimpleNamespace(callback_query=_FakeQuery("take_pending_input"))

        if signal_name == "busy":
            session.busy = True
        elif signal_name == "run_lock":
            await session.run_lock.acquire()
        elif signal_name == "tick":
            tick_state["active"] = True

        await handler.handle_callback(update, context=object())

        assert app.handled_user_inputs == []
        assert edited[-1] == "Сессия занята. Переношу ввод в очередь."
        assert InputDispatchService.pending_head(app.ui_state.pending, ui_key) is not None
        assert app.sent[-1] == (1, InputDispatchService.queue_confirm_prompt_text())

        if signal_name == "busy":
            session.busy = False
        elif signal_name == "run_lock":
            session.run_lock.release()
        elif signal_name == "tick":
            tick_state["active"] = False

        await handler.handle_callback(update, context=object())

        assert edited[-1] == "Взято в работу."
        assert app.handled_user_inputs == [
            {
                "session_id": "s1",
                "text": "pending work",
                "chat_id": 1,
                "dest": {"kind": "telegram", "chat_id": 1},
            }
        ]
        assert InputDispatchService.pending_head(app.ui_state.pending, ui_key) is None

    asyncio.run(_run())


def test_mode_action_doctor_routes_through_shared_run_operations_service():
    class ProbeMode(BaseMode):
        mode_id = "analyst"

        async def handle_input(self, message, ctx):
            return ToolResult.ok()

        async def handle_callback(self, callback, ctx):
            raise AssertionError("plugin must not be called for shared run doctor action")

    calls = []

    class _RunOps:
        async def doctor_run(self, *, session, mode_id=None, context=None, dest=None):
            _ = context, dest
            calls.append(("doctor", str(getattr(session, "id", "")), str(mode_id or "")))
            return types.SimpleNamespace(message="DOCTOR_OK")

    session = types.SimpleNamespace(id="s1", active_mode="analyst", busy=False, queue=[])
    app = _FakeBotApp(session)
    app.mode_registry.register(ProbeMode())
    app.mode_run_operations = _RunOps()

    handler = CallbackHandler(app)
    update = types.SimpleNamespace(callback_query=_FakeQuery("ma:analyst:doctor"))
    asyncio.run(handler.handle_callback(update, context=object()))

    assert calls == [("doctor", "s1", "analyst")]
    assert app.sent[-1] == (1, "DOCTOR_OK")


def test_mode_action_recover_and_resume_blocked_by_all_busy_signals_and_recover():
    class ProbeMode(BaseMode):
        mode_id = "analyst"

        async def handle_input(self, message, ctx):
            return ToolResult.ok()

        async def handle_callback(self, callback, ctx):
            raise AssertionError("plugin must not be called for shared run operations")

    calls = []
    tick_state = {"active": False}

    class _RunOps:
        async def recover_run(self, *, session, mode_id=None, context=None, dest=None):
            _ = context, dest
            calls.append(("recover", str(getattr(session, "id", "")), str(mode_id or "")))
            return types.SimpleNamespace(message="RECOVER_OK")

        async def resume_run(self, *, session, mode_id=None, context=None, dest=None):
            _ = context, dest
            calls.append(("resume", str(getattr(session, "id", "")), str(mode_id or "")))
            return types.SimpleNamespace(message="RESUME_OK")

        async def apply_recommendation_run(self, *, session, mode_id=None, context=None, dest=None):
            _ = context, dest
            calls.append(("apply_recommendation", str(getattr(session, "id", "")), str(mode_id or "")))
            return types.SimpleNamespace(message=f"{mode_id}:apply_recommendation:OK")

    session = types.SimpleNamespace(
        id="s1",
        active_mode="analyst",
        busy=False,
        queue=[],
        run_lock=asyncio.Lock(),
        is_active_by_tick=lambda: bool(tick_state["active"]),
    )
    app = _FakeBotApp(session)
    app.mode_registry.register(ProbeMode())
    app.mode_run_operations = _RunOps()
    handler = CallbackHandler(app)

    for action, ok_text in (("recover", "RECOVER_OK"), ("resume", "RESUME_OK")):
        session.busy = True
        update = types.SimpleNamespace(callback_query=_FakeQuery(f"ma:analyst:{action}"))
        asyncio.run(handler.handle_callback(update, context=object()))
        assert calls.count((action, "s1", "analyst")) == 0
        assert "Сессия занята" in app.sent[-1][1]

        session.busy = False
        asyncio.run(handler.handle_callback(update, context=object()))
        assert calls.count((action, "s1", "analyst")) == 1
        assert app.sent[-1] == (1, ok_text)

        asyncio.run(session.run_lock.acquire())
        try:
            asyncio.run(handler.handle_callback(update, context=object()))
            assert calls.count((action, "s1", "analyst")) == 1
            assert "Сессия занята" in app.sent[-1][1]
        finally:
            session.run_lock.release()

        asyncio.run(handler.handle_callback(update, context=object()))
        assert calls.count((action, "s1", "analyst")) == 2
        assert app.sent[-1] == (1, ok_text)

        tick_state["active"] = True
        asyncio.run(handler.handle_callback(update, context=object()))
        assert calls.count((action, "s1", "analyst")) == 2
        assert "Сессия занята" in app.sent[-1][1]

        tick_state["active"] = False
        asyncio.run(handler.handle_callback(update, context=object()))
        assert calls.count((action, "s1", "analyst")) == 3
        assert app.sent[-1] == (1, ok_text)


@pytest.mark.parametrize(("mode_id", "action"), _SHARED_RUN_CALLBACK_INVENTORY)
def test_mode_action_shared_run_inventory_blocked_by_all_busy_signals_and_recover(mode_id: str, action: str) -> None:
    calls: list[tuple[str, str, str]] = []
    tick_state = {"active": False}

    class ProbeMode(BaseMode):
        def __init__(self) -> None:
            super().__init__()
            self.mode_id = mode_id

        async def handle_input(self, message, ctx):
            return ToolResult.ok()

        async def handle_callback(self, callback, ctx):
            raise AssertionError("plugin must not be called for shared run operations")

    class _RunOps:
        async def recover_run(self, *, session, mode_id=None, context=None, dest=None):
            _ = context, dest
            calls.append(("recover", str(getattr(session, "id", "")), str(mode_id or "")))
            return types.SimpleNamespace(message=f"{mode_id}:recover:OK")

        async def resume_run(self, *, session, mode_id=None, context=None, dest=None):
            _ = context, dest
            calls.append(("resume", str(getattr(session, "id", "")), str(mode_id or "")))
            return types.SimpleNamespace(message=f"{mode_id}:resume:OK")

        async def apply_recommendation_run(self, *, session, mode_id=None, context=None, dest=None):
            _ = context, dest
            calls.append(("apply_recommendation", str(getattr(session, "id", "")), str(mode_id or "")))
            return types.SimpleNamespace(message=f"{mode_id}:apply_recommendation:OK")

    async def _run() -> None:
        session = types.SimpleNamespace(
            id="s1",
            active_mode=mode_id,
            busy=False,
            queue=[],
            run_lock=asyncio.Lock(),
            is_active_by_tick=lambda: bool(tick_state["active"]),
        )
        app = _FakeBotApp(session)
        app.mode_registry.register(ProbeMode())
        app.mode_run_operations = _RunOps()
        handler = CallbackHandler(app)
        update = types.SimpleNamespace(callback_query=_FakeQuery(f"ma:{mode_id}:{action}"))
        ok_text = f"{mode_id}:{action}:OK"

        session.busy = True
        await handler.handle_callback(update, context=object())
        assert calls.count((action, "s1", mode_id)) == 0
        assert "Сессия занята" in app.sent[-1][1]

        session.busy = False
        await handler.handle_callback(update, context=object())
        assert calls.count((action, "s1", mode_id)) == 1
        assert app.sent[-1] == (1, ok_text)

        await session.run_lock.acquire()
        try:
            await handler.handle_callback(update, context=object())
            assert calls.count((action, "s1", mode_id)) == 1
            assert "Сессия занята" in app.sent[-1][1]
        finally:
            session.run_lock.release()

        await handler.handle_callback(update, context=object())
        assert calls.count((action, "s1", mode_id)) == 2
        assert app.sent[-1] == (1, ok_text)

        tick_state["active"] = True
        await handler.handle_callback(update, context=object())
        assert calls.count((action, "s1", mode_id)) == 2
        assert "Сессия занята" in app.sent[-1][1]

        tick_state["active"] = False
        await handler.handle_callback(update, context=object())
        assert calls.count((action, "s1", mode_id)) == 3
        assert app.sent[-1] == (1, ok_text)

    asyncio.run(_run())


def test_mode_action_promote_skills_routes_through_shared_skill_runtime_service():
    class ProbeMode(BaseMode):
        mode_id = "agent"

        async def handle_input(self, message, ctx):
            return ToolResult.ok()

        async def handle_callback(self, callback, ctx):
            raise AssertionError("plugin must not be called for shared skill promotion")

    calls = []

    class _SkillRuntime:
        def promote_run_skills(
            self,
            *,
            session,
            run_artifact_store,
            mode_id=None,
            actor_chat_id=None,
            access_policy=None,
            context=None,
            dest=None,
        ):
            _ = context, dest
            calls.append(
                (
                    str(getattr(session, "id", "")),
                    str(mode_id or ""),
                    int(actor_chat_id or 0),
                    bool(run_artifact_store is app.mode_run_artifacts),
                    bool(access_policy is app.access_policy_service),
                )
            )
            return types.SimpleNamespace(message="PROMOTE_OK")

    class _BotAppSkillRuntime:
        def promote_run_skills(self, **_kwargs):
            raise AssertionError("shared skill promotion must use SDK skill_runtime")

    session = types.SimpleNamespace(id="s1", active_mode="agent", busy=False, queue=[])
    app = _FakeBotApp(session)
    app.mode_run_artifacts = object()
    mode = ProbeMode()
    mode.initialize(
        services={
            "skill_runtime": _SkillRuntime(),
            "run_artifacts": app.mode_run_artifacts,
        }
    )
    app.mode_registry.register(mode)
    app.mode_skill_runtime = _BotAppSkillRuntime()

    handler = CallbackHandler(app)
    update = types.SimpleNamespace(callback_query=_FakeQuery("ma:agent:promote_skills"))
    asyncio.run(handler.handle_callback(update, context=object()))

    assert calls == [("s1", "agent", 1, True, True)]
    assert app.sent[-1] == (1, "PROMOTE_OK")


def test_mode_action_promote_skills_denied_for_non_admin():
    class ProbeMode(BaseMode):
        mode_id = "agent"

        async def handle_input(self, message, ctx):
            return ToolResult.ok()

        async def handle_callback(self, callback, ctx):
            raise AssertionError("plugin must not be called for denied shared skill promotion")

    calls = []

    class _SkillRuntime:
        def promote_run_skills(self, **kwargs):
            calls.append(kwargs)
            return types.SimpleNamespace(message="PROMOTE_OK")

    session = types.SimpleNamespace(id="s1", active_mode="agent", busy=False, queue=[])
    app = _FakeBotApp(session)
    app._is_admin = False
    app.mode_registry.register(ProbeMode())
    app.mode_skill_runtime = _SkillRuntime()
    app.mode_run_artifacts = object()

    handler = CallbackHandler(app)
    denied = {"text": None}

    async def _fake_edit_msg(_context, _query, text, *, reply_markup=None, md2=True):
        _ = reply_markup, md2
        denied["text"] = text
        return True

    handler._edit_msg = _fake_edit_msg
    update = types.SimpleNamespace(callback_query=_FakeQuery("ma:agent:promote_skills"))
    asyncio.run(handler.handle_callback(update, context=object()))

    assert calls == []
    assert denied["text"] == "denied:global_skills"


def test_mode_action_audit_logs_entry_with_required_fields():
    class EchoMode(BaseMode):
        mode_id = "echo"

        async def handle_input(self, message, ctx):
            return ToolResult.ok()

        async def handle_callback(self, callback, ctx):
            return ToolResult.ok("OK")

    session = types.SimpleNamespace(id="s1", active_mode="echo", busy=False, queue=[])
    app = _FakeBotApp(session)
    app.mode_registry.register(EchoMode())
    handler = CallbackHandler(app)

    logger = logging.getLogger("mode.audit.mode_callbacks")
    original_handlers = list(logger.handlers)
    original_level = logger.level
    original_propagate = logger.propagate
    stream = StringIO()
    capture = logging.StreamHandler(stream)
    capture.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.handlers = [capture]
    logger.setLevel(logging.INFO)
    logger.propagate = False
    try:
        update = types.SimpleNamespace(callback_query=_FakeQuery("ma:echo:status"))
        asyncio.run(handler.handle_callback(update, context=object()))
    finally:
        logger.handlers = original_handlers
        logger.setLevel(original_level)
        logger.propagate = original_propagate

    out = stream.getvalue()
    assert "INFO" in out
    assert "session_id=s1" in out
    assert "mode=echo" in out
    assert "action=status" in out
    assert "chat_id=1" in out
    assert "timestamp=" in out
    assert re.search(r"\d{4}-\d{2}-\d{2}", out)


def test_file_callbacks_denied_for_non_admin():
    session = types.SimpleNamespace(id="s1", active_mode="echo")
    app = _FakeBotApp(session)
    app._is_admin = False

    edited = {"text": None}
    handler = CallbackHandler(app)

    async def _fake_edit_msg(_context, _query, text, *, reply_markup=None, md2=True):
        edited["text"] = text
        return True

    handler._edit_msg = _fake_edit_msg
    update = types.SimpleNamespace(callback_query=_FakeQuery("file_pick:abc"))
    asyncio.run(handler.handle_callback(update, context=object()))

    assert edited["text"] == "denied:files"


def test_dirs_callbacks_denied_for_non_admin_without_mode_flow():
    session = types.SimpleNamespace(id="s1", active_mode="echo")
    app = _FakeBotApp(session)
    app._is_admin = False

    edited = {"text": None}
    handler = CallbackHandler(app)

    async def _fake_edit_msg(_context, _query, text, *, reply_markup=None, md2=True):
        edited["text"] = text
        return True

    handler._edit_msg = _fake_edit_msg
    update = types.SimpleNamespace(callback_query=_FakeQuery("dir_pick:/tmp"))
    asyncio.run(handler.handle_callback(update, context=object()))

    assert edited["text"] == "denied:new_projects"


def test_new_tool_callback_is_not_role_gated_for_non_admin():
    session = types.SimpleNamespace(id="s1", active_mode="echo")
    app = _FakeBotApp(session)
    app._is_admin = False

    assert app._callback_admin_scope("new_tool:qwen") == ""


def test_sess_mode_opens_menu_even_when_session_busy():
    class EchoMode(BaseMode):
        mode_id = "echo"
        display_name = "Echo"

        async def handle_input(self, message, ctx):
            return ToolResult.ok()

        async def handle_callback(self, callback, ctx):
            return ToolResult.ok("fallback-menu")

        def build_menu(self, session, back_callback="sess_active", back_text="⬅️ Назад"):
            from telegram import InlineKeyboardButton, InlineKeyboardMarkup

            return "Echo menu", InlineKeyboardMarkup([[InlineKeyboardButton(back_text, callback_data=back_callback)]])

    session = types.SimpleNamespace(id="s1", active_mode="echo", busy=True, queue=[])
    app = _FakeBotApp(session)
    app.mode_registry.register(EchoMode())
    edited = {"text": None}

    handler = CallbackHandler(app)

    async def _fake_edit_msg(_context, _query, text, *, reply_markup=None, md2=True):
        edited["text"] = text
        return True

    handler._edit_msg = _fake_edit_msg
    update = types.SimpleNamespace(callback_query=_FakeQuery("sess_mode:echo"))
    asyncio.run(handler.handle_callback(update, context=object()))

    assert edited["text"] == "Echo menu"


def test_irrelevant_callback_keeps_pending_input_intact():
    session = types.SimpleNamespace(
        id="s1",
        active_mode="echo",
        busy=True,
        queue=[],
        interrupt=(lambda: None),
        conversation_scope=types.SimpleNamespace(session_uid="forum:-100777000111:101"),
    )
    app = _FakeBotApp(session)
    app.ui_state.pending[TelegramUiKey.from_parts(1)] = types.SimpleNamespace(
        session_id="s1",
        text="hello",
        dest={"kind": "telegram", "chat_id": 1},
        session_uid=session_runtime_uid(session),
    )

    handler = CallbackHandler(app)
    update = types.SimpleNamespace(callback_query=_FakeQuery("unknown_callback"))
    asyncio.run(handler.handle_callback(update, context=object()))

    assert TelegramUiKey.from_parts(1) in app.ui_state.pending


def test_orch_transition_stale_apply_keeps_live_pending_input():
    apply_calls = []
    dispatch_calls = []
    persist_calls = []
    pending_input = {
        "text": "handoff",
        "dest": {"kind": "telegram", "chat_id": 1},
        "target_mode_id": "manager",
        "disable_orchestrator_on_cancel": False,
    }
    session = types.SimpleNamespace(
        id="legacy-raw",
        active_mode="analyst",
        busy=False,
        queue=[],
        interrupt=(lambda: None),
        modes=types.SimpleNamespace(active_mode="analyst"),
        orchestrator=types.SimpleNamespace(
            enabled=True,
            pending_input=dict(pending_input),
            last_mode_output=None,
            last_mode_id=None,
        ),
        conversation_scope=types.SimpleNamespace(session_uid="forum:-100777000111:101"),
    )
    app = _FakeBotApp(session)

    def _apply_mode(*, session, target_mode_id: str) -> None:
        apply_calls.append((str(getattr(session, "id", "") or ""), str(target_mode_id or "")))
        session.modes.active_mode = str(target_mode_id or "")

    async def _handle_user_input_no_orchestration(session, text, chat_id, context, *, dest=None):
        _ = context
        dispatch_calls.append(
            {
                "session_id": str(getattr(session, "id", "") or ""),
                "text": str(text or ""),
                "chat_id": int(chat_id),
                "dest": dict(dest or {}),
            }
        )
        return None

    app.advanced_orchestrator_service = types.SimpleNamespace(apply_mode=_apply_mode)
    app.input_dispatch_service = types.SimpleNamespace(
        handle_user_input_no_orchestration=_handle_user_input_no_orchestration,
    )
    app.mode_session_control = types.SimpleNamespace(
        persist=(lambda: persist_calls.append("persist")),
        cancel_session=(lambda **_k: asyncio.sleep(0)),
    )
    handler = CallbackHandler(app)

    edited = {"text": None}

    async def _fake_edit_msg(_context, _query, text, *, reply_markup=None, md2=True):
        _ = reply_markup
        _ = md2
        edited["text"] = str(text or "")
        return True

    handler._edit_msg = _fake_edit_msg
    update = types.SimpleNamespace(
        callback_query=_FakeQuery(f"orch_transition:apply:{session_runtime_uid(session)}:analyst")
    )
    asyncio.run(handler.handle_callback(update, context=object()))

    assert edited["text"] == "Переход устарел. Отправьте сообщение снова."
    assert session.modes.active_mode == "analyst"
    assert session.orchestrator.pending_input == pending_input
    assert apply_calls == []
    assert dispatch_calls == []
    assert persist_calls == []


def test_orch_transition_apply_validates_target_mode_before_clearing_pending_input(monkeypatch):
    clear_calls = []
    pending_input = {
        "text": "handoff",
        "dest": {"kind": "telegram", "chat_id": 1},
        "target_mode_id": "manager",
        "disable_orchestrator_on_cancel": False,
    }
    session = types.SimpleNamespace(
        id="legacy-raw",
        active_mode="analyst",
        busy=False,
        queue=[],
        interrupt=(lambda: None),
        modes=types.SimpleNamespace(active_mode="analyst"),
        orchestrator=types.SimpleNamespace(
            enabled=True,
            pending_input=dict(pending_input),
            last_mode_output=None,
            last_mode_id=None,
        ),
        conversation_scope=types.SimpleNamespace(session_uid="forum:-100777000111:101"),
    )
    app = _FakeBotApp(session)
    handler = CallbackHandler(app)
    edited = {"text": None}
    original_set_pending = tg_callbacks.set_orchestrator_pending_input

    def _record_set_pending(target_session, value):
        clear_calls.append((target_session, value))
        return original_set_pending(target_session, value)

    async def _fake_edit_msg(_context, _query, text, *, reply_markup=None, md2=True):
        _ = reply_markup
        _ = md2
        edited["text"] = str(text or "")
        return True

    monkeypatch.setattr(tg_callbacks, "set_orchestrator_pending_input", _record_set_pending)
    handler._edit_msg = _fake_edit_msg
    update = types.SimpleNamespace(
        callback_query=_FakeQuery(f"orch_transition:apply:{session_runtime_uid(session)}:analyst")
    )
    asyncio.run(handler.handle_callback(update, context=object()))

    assert edited["text"] == "Переход устарел. Отправьте сообщение снова."
    assert clear_calls == []
    assert session.orchestrator.pending_input == pending_input


def test_orch_transition_cancel_validates_action_before_clearing_pending_input(monkeypatch):
    clear_calls = []
    pending_input = {
        "text": "handoff",
        "dest": {"kind": "telegram", "chat_id": 1},
        "target_mode_id": "manager",
        "disable_orchestrator_on_cancel": False,
    }
    session = types.SimpleNamespace(
        id="legacy-raw",
        active_mode="analyst",
        busy=False,
        queue=[],
        interrupt=(lambda: None),
        modes=types.SimpleNamespace(active_mode="analyst"),
        orchestrator=types.SimpleNamespace(
            enabled=True,
            pending_input=dict(pending_input),
            last_mode_output=None,
            last_mode_id=None,
        ),
        conversation_scope=types.SimpleNamespace(session_uid="forum:-100777000111:101"),
    )
    app = _FakeBotApp(session)
    handler = CallbackHandler(app)
    edited = {"text": None}
    original_set_pending = tg_callbacks.set_orchestrator_pending_input

    def _record_set_pending(target_session, value):
        clear_calls.append((target_session, value))
        return original_set_pending(target_session, value)

    async def _fake_edit_msg(_context, _query, text, *, reply_markup=None, md2=True):
        _ = reply_markup
        _ = md2
        edited["text"] = str(text or "")
        return True

    monkeypatch.setattr(tg_callbacks, "set_orchestrator_pending_input", _record_set_pending)
    handler._edit_msg = _fake_edit_msg
    update = types.SimpleNamespace(
        callback_query=_FakeQuery(f"orch_transition:noop:{session_runtime_uid(session)}")
    )
    asyncio.run(handler.handle_callback(update, context=object()))

    assert edited["text"] == "Неизвестное действие оркестратора."
    assert clear_calls == []
    assert session.orchestrator.pending_input == pending_input


def test_orch_transition_cancel_with_disable_orchestrator_preserves_existing_semantics():
    dispatch_calls = []
    persist_calls = []
    pending_input = {
        "text": "handoff",
        "dest": {"kind": "telegram", "chat_id": 1},
        "target_mode_id": "manager",
        "disable_orchestrator_on_cancel": True,
    }
    session = types.SimpleNamespace(
        id="legacy-raw",
        active_mode="analyst",
        busy=False,
        queue=[],
        interrupt=(lambda: None),
        modes=types.SimpleNamespace(active_mode="analyst"),
        orchestrator=types.SimpleNamespace(
            enabled=True,
            pending_input=dict(pending_input),
            last_mode_output=None,
            last_mode_id=None,
        ),
        conversation_scope=types.SimpleNamespace(session_uid="forum:-100777000111:101"),
    )
    app = _FakeBotApp(session)

    async def _handle_user_input_no_orchestration(session, text, chat_id, context, *, dest=None):
        _ = session, text, chat_id, context, dest
        dispatch_calls.append("dispatched")
        return None

    app.input_dispatch_service = types.SimpleNamespace(
        handle_user_input_no_orchestration=_handle_user_input_no_orchestration,
    )
    app.mode_session_control = types.SimpleNamespace(
        persist=(lambda: persist_calls.append("persist")),
        cancel_session=(lambda **_k: asyncio.sleep(0)),
    )
    handler = CallbackHandler(app)
    edited = {"text": None}

    async def _fake_edit_msg(_context, _query, text, *, reply_markup=None, md2=True):
        _ = reply_markup
        _ = md2
        edited["text"] = str(text or "")
        return True

    handler._edit_msg = _fake_edit_msg
    update = types.SimpleNamespace(
        callback_query=_FakeQuery(f"orch_transition:cancel:{session_runtime_uid(session)}")
    )
    asyncio.run(handler.handle_callback(update, context=object()))

    assert edited["text"] == "Процесс остановлен пользователем. Продвинутый оркестратор выключен."
    assert session.orchestrator.pending_input is None
    assert session.orchestrator.enabled is False
    assert persist_calls == ["persist"]
    assert dispatch_calls == []


def test_queue_input_callback_consumes_pending_head_only():
    session = types.SimpleNamespace(
        id="s1",
        active_mode="echo",
        busy=True,
        queue=[],
        interrupt=(lambda: None),
        conversation_scope=types.SimpleNamespace(session_uid="forum:-100777000111:101"),
    )
    app = _FakeBotApp(session)
    app.ui_state.pending[TelegramUiKey.from_parts(1)] = deque(
        [
            types.SimpleNamespace(
                session_id="s1",
                text="first",
                dest={"kind": "telegram", "chat_id": 1},
                session_uid=session_runtime_uid(session),
                image_path=None,
                image_paths=None,
            ),
            types.SimpleNamespace(
                session_id="s1",
                text="second",
                dest={"kind": "telegram", "chat_id": 1},
                session_uid=session_runtime_uid(session),
                image_path=None,
                image_paths=None,
            ),
        ]
    )
    handler = CallbackHandler(app)

    edited = {"text": None}

    async def _fake_edit_msg(_context, _query, text, *, reply_markup=None, md2=True):
        _ = reply_markup
        _ = md2
        edited["text"] = str(text or "")
        return True

    handler._edit_msg = _fake_edit_msg
    update = types.SimpleNamespace(callback_query=_FakeQuery("queue_input"))
    asyncio.run(handler.handle_callback(update, context=object()))

    assert edited["text"] == "Ввод поставлен в очередь."
    assert list(session.queue) == [{"text": "first", "dest": {"kind": "telegram", "chat_id": 1}}]
    ui_key = TelegramUiKey.from_parts(1)
    assert ui_key in app.ui_state.pending
    rest = app.ui_state.pending[ui_key]
    assert isinstance(rest, deque)
    assert len(rest) == 1
    assert rest[0].text == "second"
    assert app.sent[-1] == (1, "В очереди уже есть сообщение. Что сделать с новым вводом?")


def test_queue_input_callback_enqueues_before_dispatch_when_session_is_idle():
    session = types.SimpleNamespace(
        id="s1",
        active_mode="manager",
        busy=False,
        queue=[],
        interrupt=(lambda: None),
        run_lock=asyncio.Lock(),
        is_active_by_tick=(lambda: False),
        conversation_scope=types.SimpleNamespace(session_uid="forum:-100777000111:101"),
    )
    app = _FakeBotApp(session)
    app.ui_state.pending[TelegramUiKey.from_parts(1)] = deque(
        [
            types.SimpleNamespace(
                session_id="s1",
                text="first",
                dest={"kind": "telegram", "chat_id": 1},
                session_uid=session_runtime_uid(session),
                image_path=None,
                image_paths=None,
            )
        ]
    )
    handler = CallbackHandler(app)

    edited = {"text": None}

    async def _fake_edit_msg(_context, _query, text, *, reply_markup=None, md2=True):
        _ = reply_markup
        _ = md2
        edited["text"] = str(text or "")
        return True

    handler._edit_msg = _fake_edit_msg
    update = types.SimpleNamespace(callback_query=_FakeQuery("queue_input"))
    asyncio.run(handler.handle_callback(update, context=object()))

    assert edited["text"] == "Ввод поставлен в очередь."
    assert session.queue == []
    assert TelegramUiKey.from_parts(1) not in app.ui_state.pending
    assert app.handled_user_inputs == [
        {
            "session_id": "s1",
            "text": "first",
            "chat_id": 1,
            "dest": {"kind": "telegram", "chat_id": 1},
        }
    ]


def test_queue_input_callback_purges_stale_pending_before_live_item():
    session = types.SimpleNamespace(
        id="s1",
        active_mode="echo",
        busy=True,
        queue=[],
        interrupt=(lambda: None),
        conversation_scope=types.SimpleNamespace(session_uid="forum:-100777000111:101"),
    )
    app = _FakeBotApp(session)
    app.ui_state.pending[TelegramUiKey.from_parts(1)] = deque(
        [
            types.SimpleNamespace(
                session_id="stale-session",
                text="stale",
                dest={"kind": "telegram", "chat_id": 1},
                session_uid="forum:-100777000111:stale",
                image_path=None,
                image_paths=None,
            ),
            types.SimpleNamespace(
                session_id="s1",
                text="live",
                dest={"kind": "telegram", "chat_id": 1},
                session_uid=session_runtime_uid(session),
                image_path=None,
                image_paths=None,
            ),
        ]
    )
    handler = CallbackHandler(app)

    edited = {"text": None}

    async def _fake_edit_msg(_context, _query, text, *, reply_markup=None, md2=True):
        _ = reply_markup
        _ = md2
        edited["text"] = str(text or "")
        return True

    handler._edit_msg = _fake_edit_msg
    update = types.SimpleNamespace(callback_query=_FakeQuery("queue_input"))
    asyncio.run(handler.handle_callback(update, context=object()))

    assert edited["text"] == "Ввод поставлен в очередь."
    assert list(session.queue) == [{"text": "live", "dest": {"kind": "telegram", "chat_id": 1}}]
    assert TelegramUiKey.from_parts(1) not in app.ui_state.pending


def test_queue_input_callback_purges_all_stale_pending_when_session_is_missing():
    session = types.SimpleNamespace(
        id="s1",
        active_mode="echo",
        busy=True,
        queue=[],
        interrupt=(lambda: None),
        conversation_scope=types.SimpleNamespace(session_uid="forum:-100777000111:101"),
    )
    app = _FakeBotApp(session)
    app.ui_state.pending[TelegramUiKey.from_parts(1)] = deque(
        [
            types.SimpleNamespace(
                session_id="stale-1",
                text="first",
                dest={"kind": "telegram", "chat_id": 1},
                session_uid="forum:-100777000111:stale-1",
                image_path=None,
                image_paths=None,
            ),
            types.SimpleNamespace(
                session_id="stale-2",
                text="second",
                dest={"kind": "telegram", "chat_id": 1},
                session_uid="forum:-100777000111:stale-2",
                image_path=None,
                image_paths=None,
            ),
        ]
    )
    handler = CallbackHandler(app)

    edited = {"text": None}

    async def _fake_edit_msg(_context, _query, text, *, reply_markup=None, md2=True):
        _ = reply_markup
        _ = md2
        edited["text"] = str(text or "")
        return True

    handler._edit_msg = _fake_edit_msg
    update = types.SimpleNamespace(callback_query=_FakeQuery("queue_input"))
    asyncio.run(handler.handle_callback(update, context=object()))

    assert edited["text"] == "Сессия уже закрыта."
    assert session.queue == []
    assert TelegramUiKey.from_parts(1) not in app.ui_state.pending


def test_queue_input_callback_resolves_live_session_by_session_runtime_uid():
    session = types.SimpleNamespace(
        id="legacy-raw",
        active_mode="echo",
        busy=True,
        queue=[],
        interrupt=(lambda: None),
        conversation_scope=types.SimpleNamespace(session_uid="forum:-100777000111:101"),
    )

    class _StrictUidManager(_FakeManager):
        def __init__(self, scoped_session) -> None:
            super().__init__(scoped_session)
            self.lookups: list[str] = []

        def get_by_uid(self, session_uid: str):
            token = str(session_uid or "")
            self.lookups.append(token)
            return super().get_by_uid(token)

    app = _FakeBotApp(session)
    app.manager = _StrictUidManager(session)
    app.ui_state.pending[TelegramUiKey.from_parts(1)] = deque(
        [
            types.SimpleNamespace(
                session_id="raw-mismatch",
                text="live",
                dest={"kind": "telegram", "chat_id": 1},
                session_uid=session_runtime_uid(session),
                image_path=None,
                image_paths=None,
            ),
        ]
    )
    handler = CallbackHandler(app)

    edited = {"text": None}

    async def _fake_edit_msg(_context, _query, text, *, reply_markup=None, md2=True):
        _ = reply_markup
        _ = md2
        edited["text"] = str(text or "")
        return True

    handler._edit_msg = _fake_edit_msg
    update = types.SimpleNamespace(callback_query=_FakeQuery("queue_input"))
    asyncio.run(handler.handle_callback(update, context=object()))

    assert edited["text"] == "Ввод поставлен в очередь."
    assert app.manager.lookups == [session_runtime_uid(session)]
    assert list(session.queue) == [{"text": "live", "dest": {"kind": "telegram", "chat_id": 1}}]
    assert TelegramUiKey.from_parts(1) not in app.ui_state.pending


def test_queue_kick_cli_run_registers_session_task_for_interrupt_cancellation():
    async def _run() -> None:
        session = types.SimpleNamespace(
            id="legacy-raw",
            active_mode="",
            busy=False,
            queue=deque([{"text": "queued", "dest": {"kind": "telegram", "chat_id": 1}}]),
            interrupt=(lambda: None),
            run_lock=asyncio.Lock(),
            is_active_by_tick=(lambda: False),
            conversation_scope=types.SimpleNamespace(session_uid="forum:-100777000111:101"),
        )
        app = _FakeBotApp(session)
        app.mode_tasks = ModeTaskService()

        async def _cancel_session(*, session_id: str, timeout_s: float) -> None:
            await app.mode_tasks.cancel_session(session_id=session_id, timeout_s=timeout_s)

        app.mode_session_control = types.SimpleNamespace(persist=(lambda: None), cancel_session=_cancel_session)

        started = asyncio.Event()
        started_tasks: list[asyncio.Task] = []

        async def _run_prompt(_session, _text, _dest, _context):
            async with _session.run_lock:
                _session.busy = True
                started.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    _session.busy = False

        app.run_prompt = _run_prompt

        def _start_prompt_task(_session, _text, _dest, _context, *, task_name: str = "run_prompt") -> bool:
            app.mode_tasks.create(
                session_uid=session_runtime_uid(_session),
                mode_id="__session__",
                coro=app.run_prompt(_session, _text, _dest, _context),
                name=task_name,
            )
            return True

        app.session_management = types.SimpleNamespace(start_prompt_task=_start_prompt_task)

        original_safe_create_task = app.input_dispatch_service._safe_create_task

        def _capture_task(coro, *, label: str):
            task = original_safe_create_task(coro, label=label)
            if task is not None:
                started_tasks.append(task)
            return task

        app.input_dispatch_service._safe_create_task = _capture_task
        handler = CallbackHandler(app)

        try:
            await handler._kick_session_queue_if_idle(session=session, chat_id=1, context=object())
            await asyncio.wait_for(started.wait(), timeout=0.5)

            session_uid = session_runtime_uid(session)
            assert app.mode_tasks.list(session_uid=session_uid, mode_id="__session__") == ["callbacks.queue_kick.run_prompt"]
            assert session.busy is True

            await handler._cancel_mode_tasks_session(session_uid)
            await asyncio.sleep(0)

            assert app.mode_tasks.list(session_uid=session_uid, mode_id="__session__") == []
            assert session.busy is False
        finally:
            for task in started_tasks:
                task.cancel()
            await asyncio.gather(*started_tasks, return_exceptions=True)

    asyncio.run(_run())


def test_cancel_current_callback_purges_all_stale_pending_when_session_is_missing():
    interrupt_calls = []
    session = types.SimpleNamespace(
        id="s1",
        active_mode="echo",
        busy=True,
        queue=[],
        interrupt=(lambda: interrupt_calls.append("interrupt")),
        conversation_scope=types.SimpleNamespace(session_uid="forum:-100777000111:101"),
    )
    app = _FakeBotApp(session)
    app.ui_state.pending[TelegramUiKey.from_parts(1)] = deque(
        [
            types.SimpleNamespace(
                session_id="stale-1",
                text="first",
                dest={"kind": "telegram", "chat_id": 1},
                session_uid="forum:-100777000111:stale-1",
                image_path=None,
                image_paths=None,
            ),
            types.SimpleNamespace(
                session_id="stale-2",
                text="second",
                dest={"kind": "telegram", "chat_id": 1},
                session_uid="forum:-100777000111:stale-2",
                image_path=None,
                image_paths=None,
            ),
        ]
    )
    handler = CallbackHandler(app)

    edited = {"text": None}

    async def _fake_edit_msg(_context, _query, text, *, reply_markup=None, md2=True):
        _ = reply_markup
        _ = md2
        edited["text"] = str(text or "")
        return True

    handler._edit_msg = _fake_edit_msg
    update = types.SimpleNamespace(callback_query=_FakeQuery("cancel_current"))
    asyncio.run(handler.handle_callback(update, context=object()))

    assert edited["text"] == "Сессия уже закрыта."
    assert interrupt_calls == []
    assert TelegramUiKey.from_parts(1) not in app.ui_state.pending


def test_cancel_current_callback_cancels_mode_tasks_by_session_runtime_uid():
    interrupt_calls = []
    cancel_calls = []
    session = types.SimpleNamespace(
        id="legacy-raw",
        active_mode="echo",
        busy=True,
        queue=[],
        interrupt=(lambda: interrupt_calls.append("interrupt")),
        conversation_scope=types.SimpleNamespace(session_uid="forum:-100777000111:101"),
    )

    class _StrictUidManager(_FakeManager):
        def __init__(self, scoped_session) -> None:
            super().__init__(scoped_session)
            self.lookups: list[str] = []

        def get_by_uid(self, session_uid: str):
            token = str(session_uid or "")
            self.lookups.append(token)
            return super().get_by_uid(token)

    app = _FakeBotApp(session)
    app.manager = _StrictUidManager(session)

    async def _cancel_session(*, session_id: str, timeout_s: float) -> None:
        cancel_calls.append((str(session_id), float(timeout_s)))

    app.mode_session_control = types.SimpleNamespace(persist=(lambda: None), cancel_session=_cancel_session)
    app.ui_state.pending[TelegramUiKey.from_parts(1)] = deque(
        [
            types.SimpleNamespace(
                session_id="raw-mismatch",
                text="live",
                dest={"kind": "telegram", "chat_id": 1},
                session_uid=session_runtime_uid(session),
                image_path=None,
                image_paths=None,
            ),
        ]
    )
    handler = CallbackHandler(app)

    edited = {"text": None}

    async def _fake_edit_msg(_context, _query, text, *, reply_markup=None, md2=True):
        _ = reply_markup
        _ = md2
        edited["text"] = str(text or "")
        return True

    handler._edit_msg = _fake_edit_msg
    update = types.SimpleNamespace(callback_query=_FakeQuery("cancel_current"))
    asyncio.run(handler.handle_callback(update, context=object()))

    assert edited["text"] == "Текущая генерация прервана. Ввод отброшен."
    assert interrupt_calls == ["interrupt"]
    assert app.manager.lookups == [session_runtime_uid(session)]
    assert cancel_calls == [(session_runtime_uid(session), 0.2)]
    assert TelegramUiKey.from_parts(1) not in app.ui_state.pending
