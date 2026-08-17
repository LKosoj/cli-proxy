import asyncio
import logging
import time
import types
from pathlib import Path

import pytest

from tg.callbacks import CallbackHandler
from app.services.input_dispatch_service import InputDispatchService
from config import AppConfig, DefaultsConfig, MCPConfig, TelegramConfig, ToolConfig
from bot import BotApp, TelegramInboundRoute
from modes.agent.mode import AgentMode, agent_project_scope_key
from modes.agent.ui import build_agent_menu
from modes.sdk import MessageModel
from modes.sdk.services.callback_data import (
    build_mode_action_callback_data,
    parse_compact_callback_payload,
)
from modes.sdk.services.runtime import AgentRuntimeService
from modes.sdk.session_busy import is_session_busy
from session import session_runtime_uid, session_scoped_key
from sessions.conversation_scope import ConversationScope


class _FakeMessage:
    def __init__(self, chat_id: int = 1, message_id: int = 10, message_thread_id: int | None = None) -> None:
        self.chat_id = chat_id
        self.message_id = message_id
        self.message_thread_id = message_thread_id


class _FakeQuery:
    def __init__(self, data: str, *, chat_id: int = 1, message_id: int = 10, message_thread_id: int | None = None) -> None:
        self.data = data
        self.message = _FakeMessage(chat_id=chat_id, message_id=message_id, message_thread_id=message_thread_id)
        self.from_user = types.SimpleNamespace(id=42)

    async def answer(self) -> None:
        return None


def _build_app(tmp_path) -> BotApp:
    cfg = AppConfig(
        telegram=TelegramConfig(token="", whitelist_chat_ids=[1], admlist_chat_ids=[1]),
        tools={
            "dummy": ToolConfig(
                name="dummy",
                mode="headless",
                cmd=["bash", "-lc", "cat"],
            )
        },
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
    app = BotApp(cfg)
    app._test_selected_session = None

    def _current(chat_id: int):
        selected = getattr(app, "_test_selected_session", None)
        if selected is not None:
            return selected
        sessions = list(app.manager.sessions_for_chat(int(chat_id)).values())
        return sessions[-1] if sessions else None

    def _set_active(chat_id: int, session_id: str) -> bool:
        session = app.manager.get(int(chat_id), str(session_id))
        if session is None:
            return False
        app._test_selected_session = session
        return True

    def _active(chat_id: int):
        return _current(int(chat_id))

    def _resolve_scope_session(*, reply_chat_id: int, message_thread_id=None, owner_chat_id=None):
        _ = message_thread_id
        target_chat_id = owner_chat_id if owner_chat_id is not None else reply_chat_id
        return _current(int(target_chat_id))

    def _resolve_inbound_route(update):
        chat_id = int(getattr(getattr(update, "effective_chat", None), "id", 0) or 0)
        session = _current(chat_id)
        session_uid = None
        if session is not None:
            session_uid = str(
                getattr(getattr(session, "conversation_scope", None), "session_uid", "")
                or f"{chat_id}:{getattr(session, 'id', '')}"
            )
        return TelegramInboundRoute(
            owner_chat_id=chat_id,
            reply_chat_id=chat_id,
            message_thread_id=None,
            session_uid=session_uid,
            session=session,
        )

    def _resolve_callback_scope(query):
        chat_id = int(getattr(getattr(query, "message", None), "chat_id", 0) or 0)
        return chat_id, None, chat_id, _current(chat_id)

    app.manager.active = _active  # type: ignore[attr-defined]
    app.manager.set_active = _set_active  # type: ignore[attr-defined]
    app.resolve_telegram_scope_session = _resolve_scope_session  # type: ignore[method-assign]
    app.resolve_telegram_inbound_route = _resolve_inbound_route  # type: ignore[method-assign]
    app.resolve_telegram_callback_scope = _resolve_callback_scope  # type: ignore[method-assign]
    return app


def _project_callback(action: str, session, *, idx: int | None = None) -> str:
    payload = f"sk={session_scoped_key(session) or session.id}"
    if idx is not None:
        payload = f"{payload}|idx={int(idx)}"
    return f"ma:agent:{action}:{payload}"


def test_agent_mode_plugin_is_loaded(tmp_path):
    app = _build_app(tmp_path)
    assert app.mode_registry.get("agent") is not None


def test_agent_mode_state_reads_go_through_sdk_services():
    source = (
        Path(__file__).resolve().parents[1] / "modes" / "agent" / "mode.py"
    ).read_text(encoding="utf-8")

    assert "bot_app.ui_state" not in source
    assert 'getattr(bot_app, "ui_state"' not in source
    assert 'getattr(getattr(bot_app, "ui_state"' not in source
    assert "bot_app.manager" not in source
    assert 'getattr(bot_app, "manager"' not in source


def test_agent_run_lifecycle_writes_go_through_lifecycle_facade():
    source = (
        Path(__file__).resolve().parents[1] / "modes" / "agent" / "mode.py"
    ).read_text(encoding="utf-8")

    forbidden_store_writes = (
        "artifact_store.start_run(",
        "artifact_store.save_state(",
        "artifact_store.mark_finished(",
        "artifact_store.append_event(",
    )
    for forbidden in forbidden_store_writes:
        assert forbidden not in source

    assert "lifecycle.start(" in source
    assert "lifecycle.save_phase(" in source
    assert "lifecycle.mark_finished(" in source


def test_agent_promote_skills_uses_sdk_skill_runtime_service(tmp_path):
    async def _run():
        app = _build_app(tmp_path)
        session = app.manager.create(1, "dummy", str(tmp_path))
        session.modes.active_mode = "agent"

        class _BotAppSkillRuntime:
            def promote_run_skills(self, **_kwargs):
                raise AssertionError("Agent promote_skills must use SDK skill_runtime")

        class _SdkSkillRuntime:
            def __init__(self) -> None:
                self.calls = []

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
                self.calls.append(
                    (session, run_artifact_store, mode_id, actor_chat_id, access_policy, context, dest)
                )
                return types.SimpleNamespace(message="Skills promoted through SDK")

        app.mode_skill_runtime = _BotAppSkillRuntime()
        mode = app.mode_registry.get("agent")
        assert mode is not None
        sdk_skill_runtime = _SdkSkillRuntime()
        assert mode.mode_dependencies is not None
        artifact_store = object()
        mode.mode_dependencies = mode.mode_dependencies.with_overrides(
            skill_runtime=sdk_skill_runtime,
            run_artifacts=artifact_store,
        )

        edits = []
        sent = []

        async def _edit_message(_ctx, *, chat_id: int, message_id: int, text: str, reply_markup=None, **_kw):
            edits.append((chat_id, message_id, text, reply_markup))
            return True

        async def _send_message(_ctx, *, chat_id: int, text: str, **_kw):
            sent.append((chat_id, text))
            return None

        app._edit_message = _edit_message
        app._send_message = _send_message

        handler = CallbackHandler(app)
        update = types.SimpleNamespace(
            callback_query=_FakeQuery(
                build_mode_action_callback_data("agent", "promote_skills", session=session)
            )
        )
        await handler.handle_callback(update, context=object())

        assert len(sdk_skill_runtime.calls) == 1
        call_session, call_store, mode_id, actor_chat_id, access_policy, transport_context, dest = (
            sdk_skill_runtime.calls[0]
        )
        assert call_session is session
        assert call_store is artifact_store
        assert mode_id == "agent"
        assert actor_chat_id == 1
        assert access_policy is app.access_policy_service
        assert transport_context is not None
        assert dest["kind"] == "telegram"
        assert dest["chat_id"] == 1
        texts = [text for (_chat, _mid, text, _markup) in edits] + [text for (_chat, text) in sent]
        assert any("Skills promoted through SDK" in text for text in texts)

    asyncio.run(_run())


def test_agent_mode_enable_disable_via_mode_action_callback(tmp_path):
    async def _run():
        app = _build_app(tmp_path)
        session = app.manager.create(1, "dummy", str(tmp_path))
        session.modes.active_mode = None

        edits = []

        async def _edit_message(_ctx, *, chat_id: int, message_id: int, text: str, reply_markup=None, **_kw):
            edits.append((chat_id, message_id, text, reply_markup))
            return True

        app._edit_message = _edit_message

        handler = CallbackHandler(app)
        update = types.SimpleNamespace(callback_query=_FakeQuery("ma:agent:enable"))
        await handler.handle_callback(update, context=object())

        assert session.modes.active_mode == "agent"
        assert edits
        assert "Агент сейчас включен" in edits[-1][2]

        update2 = types.SimpleNamespace(callback_query=_FakeQuery("ma:agent:disable"))
        await handler.handle_callback(update2, context=object())

        assert session.modes.active_mode is None
        assert "Агент сейчас выключен" in edits[-1][2]

    asyncio.run(_run())


def test_agent_mode_status_via_mode_action_callback(tmp_path):
    async def _run():
        app = _build_app(tmp_path)
        session = app.manager.create(1, "dummy", str(tmp_path))
        session.modes.active_mode = "agent"

        edits = []

        async def _edit_message(_ctx, *, chat_id: int, message_id: int, text: str, reply_markup=None, **_kw):
            edits.append((chat_id, message_id, text, reply_markup))
            return True

        app._edit_message = _edit_message

        handler = CallbackHandler(app)
        update = types.SimpleNamespace(callback_query=_FakeQuery("ma:agent:status"))
        await handler.handle_callback(update, context=object())

        assert edits
        text = edits[-1][2]
        assert "🤖 Статус Агента" in text
        assert "Проект: не подключен" in text
        assert "Режим: включен" in text

    asyncio.run(_run())


def test_agent_plugins_callbacks_use_payload_session_when_active_session_differs(tmp_path):
    async def _run():
        app = _build_app(tmp_path)
        agent_session = app.manager.create(1, "dummy", str(tmp_path))
        agent_session.modes.active_mode = "agent"

        other_session = app.manager.create(1, "dummy", str(tmp_path))
        other_session.modes.active_mode = None
        assert app.manager.active(1) is other_session

        edits = []
        sent = []

        async def _edit_message(_ctx, *, chat_id: int, message_id: int, text: str, reply_markup=None, **_kw):
            edits.append((chat_id, message_id, text, reply_markup))
            return True

        async def _send_message(_ctx, *, chat_id: int, text: str, **_kw):
            sent.append((chat_id, text))
            return None

        app._edit_message = _edit_message
        app._send_message = _send_message

        class _FakeToolRegistry:
            plugins: dict[str, object] = {}

            @staticmethod
            def list_tool_names():
                return []

        class _FakePluginUiRuntime:
            @staticmethod
            def supports_capability(cap: str) -> bool:
                return str(cap or "").strip() == "plugin_ui"

            @staticmethod
            def get_plugin_ui(_profile):
                return {
                    "plugin_menu": [
                        {
                            "plugin_id": "plug",
                            "label": "Plugin",
                            "actions": [],
                            "plugin": None,
                        }
                    ]
                }

        app._tool_registry = _FakeToolRegistry()
        app.register_mode_runtime("fake_plugin_ui", _FakePluginUiRuntime())

        handler = CallbackHandler(app)
        agent_session_uid = session_runtime_uid(agent_session)
        update_plugins = types.SimpleNamespace(
            callback_query=_FakeQuery(f"ma:agent:plugins:s={agent_session_uid}")
        )
        await handler.handle_callback(update_plugins, context=object())

        texts = [text for (_chat, _mid, text, _markup) in edits] + [text for (_chat, text) in sent]
        assert any("Плагины:" in text for text in texts)
        assert all("Агент не активен." not in text for text in texts)
        assert edits
        plugin_markup = edits[-1][3]
        assert plugin_markup is not None
        callback_data = [
            str(getattr(button, "callback_data", "") or "")
            for row in plugin_markup.inline_keyboard
            for button in row
        ]
        plugin_callbacks = [
            token
            for token in callback_data
            if token.startswith(f"ma:agent:plugin:s={agent_session_uid}|p=")
        ]
        assert plugin_callbacks

        edits.clear()
        sent.clear()
        update_plugin = types.SimpleNamespace(
            callback_query=_FakeQuery(plugin_callbacks[0])
        )
        await handler.handle_callback(update_plugin, context=object())

        texts = [text for (_chat, _mid, text, _markup) in edits] + [text for (_chat, text) in sent]
        assert any(text.rstrip().endswith(":") for text in texts)
        assert all("Агент не активен." not in text for text in texts)
        assert all("Плагин недоступен." not in text for text in texts)

    asyncio.run(_run())


def test_agent_plugin_raw_session_id_payload_is_rejected_for_scoped_session(tmp_path):
    async def _run():
        app = _build_app(tmp_path)
        agent_session = app.manager.create(1, "dummy", str(tmp_path))
        agent_session.modes.active_mode = "agent"

        edits = []
        sent = []

        async def _edit_message(_ctx, *, chat_id: int, message_id: int, text: str, reply_markup=None, **_kw):
            edits.append((chat_id, message_id, text, reply_markup))
            return True

        async def _send_message(_ctx, *, chat_id: int, text: str, **_kw):
            sent.append((chat_id, text))
            return None

        app._edit_message = _edit_message
        app._send_message = _send_message

        handler = CallbackHandler(app)
        update = types.SimpleNamespace(
            callback_query=_FakeQuery(f"ma:agent:plugins:s={agent_session.id}")
        )
        await handler.handle_callback(update, context=object())

        texts = [text for (_chat, _mid, text, _markup) in edits] + [text for (_chat, text) in sent]
        assert any("Сессия для этого меню недоступна" in text for text in texts)
        assert all("Плагины:" not in text for text in texts)

    asyncio.run(_run())


def test_agent_plugin_lookup_uses_sdk_runtime_for_fake_session_fallback(caplog):
    fake_session = types.SimpleNamespace(id="fake-session", chat_id=1)
    active_session = types.SimpleNamespace(id="active", chat_id=1)
    calls = []

    class _FakeRuntime:
        def get_session_by_uid(self, session_uid: str, *, chat_id: int | None = None):
            calls.append((session_uid, chat_id))
            return fake_session if session_uid == "fake-session" else None

    mode = AgentMode()
    mode.initialize(services={"agent_runtime": _FakeRuntime()})

    resolved, missing = mode._resolve_plugin_callback_session(
        bot_app=object(),
        session=active_session,
        chat_id=1,
        payload={"s": "fake-session"},
    )

    assert resolved is fake_session
    assert missing is False
    assert calls == [("fake-session", 1)]

    caplog.set_level(logging.WARNING, logger="modes.sdk.services.runtime")
    mode.initialize(services={"agent_runtime": AgentRuntimeService()})
    resolved, missing = mode._resolve_plugin_callback_session(
        bot_app=object(),
        session=active_session,
        chat_id=1,
        payload={"s": "chat:1:s1"},
    )

    assert resolved is None
    assert missing is True
    assert "agent runtime session lookup backend unavailable" in caplog.text


def test_agent_plugin_callback_inventory_roundtrips_colon_session_uid():
    session = types.SimpleNamespace(
        id="s1",
        project_root=None,
        conversation_scope=ConversationScope.from_parts(1),
        modes=types.SimpleNamespace(active_mode="agent"),
    )
    session_uid = session_runtime_uid(session)

    _text, markup = build_agent_menu(session, "sess_active", "Назад")
    menu_callbacks = [
        str(getattr(button, "callback_data", "") or "")
        for row in markup.inline_keyboard
        for button in row
    ]
    plugin_menu_callback = next(
        token for token in menu_callbacks if token.startswith("ma:agent:plugins:")
    )
    plugin_callback = build_mode_action_callback_data(
        "agent",
        "plugin",
        payload={"s": session_uid, "p": "plug"},
    )

    inventory = {
        "ma:agent:plugins": plugin_menu_callback,
        "ma:agent:plugin": plugin_callback,
    }
    assert set(inventory) == {"ma:agent:plugins", "ma:agent:plugin"}
    for prefix, callback_data in inventory.items():
        assert callback_data.startswith(f"{prefix}:")
        payload = parse_compact_callback_payload(callback_data.split(":", 3)[3])
        assert payload["s"] == session_uid
        assert ":" in payload["s"]
    assert parse_compact_callback_payload(plugin_callback.split(":", 3)[3])["p"] == "plug"


def test_agent_plugins_back_button_opens_mode_menu_for_target_session(tmp_path):
    async def _run():
        app = _build_app(tmp_path)
        agent_session = app.manager.create(1, "dummy", str(tmp_path))
        agent_session.modes.active_mode = "agent"

        other_session = app.manager.create(1, "dummy", str(tmp_path))
        other_session.modes.active_mode = None
        assert app.manager.active(1) is other_session

        edits = []

        async def _edit_message(_ctx, *, chat_id: int, message_id: int, text: str, reply_markup=None, **_kw):
            edits.append((chat_id, message_id, text, reply_markup))
            return True

        app._edit_message = _edit_message

        class _FakePluginUiRuntime:
            @staticmethod
            def supports_capability(cap: str) -> bool:
                return str(cap or "").strip() == "plugin_ui"

            @staticmethod
            def get_plugin_ui(_profile):
                return {
                    "plugin_menu": [
                        {
                            "plugin_id": "plug",
                            "label": "Plugin",
                            "actions": [],
                            "plugin": None,
                        }
                    ]
                }

        class _FakeToolRegistry:
            @staticmethod
            def list_tool_names():
                return []

        app._tool_registry = _FakeToolRegistry()
        app.mode_runtime_registry = {"fake_plugin_ui": _FakePluginUiRuntime()}

        handler = CallbackHandler(app)
        agent_session_uid = session_runtime_uid(agent_session)
        update_plugins = types.SimpleNamespace(
            callback_query=_FakeQuery(f"ma:agent:plugins:s={agent_session_uid}")
        )
        await handler.handle_callback(update_plugins, context=object())
        assert edits
        plugin_markup = edits[-1][3]
        assert plugin_markup is not None
        callback_data = [
            str(getattr(button, "callback_data", "") or "")
            for row in plugin_markup.inline_keyboard
            for button in row
        ]
        back_callbacks = [token for token in callback_data if token.startswith("sess_mode_pick:")]
        assert back_callbacks
        assert back_callbacks[0] == f"sess_mode_pick:{agent_session_uid}"

        edits.clear()
        update_back = types.SimpleNamespace(callback_query=_FakeQuery(back_callbacks[0]))
        await handler.handle_callback(update_back, context=object())

        assert app.manager.active(1) is other_session
        assert edits
        assert any("Агент сейчас включен" in text for (_chat, _mid, text, _markup) in edits)

    asyncio.run(_run())


def test_agent_mode_project_pick_blocked_while_busy_and_switches_after_release(tmp_path):
    async def _run():
        app = _build_app(tmp_path)
        session = app.manager.create(1, "dummy", str(tmp_path))
        session.modes.active_mode = "agent"

        project_a = tmp_path / "project_a"
        project_b = tmp_path / "project_b"
        project_a.mkdir()
        project_b.mkdir()
        app.user_projects = lambda _chat_id: [str(project_a), str(project_b)]
        session.project_root = str(project_a.resolve())

        edited = []
        sent = []

        async def _edit_message(_ctx, *, chat_id: int, message_id: int, text: str, reply_markup=None, **_kw):
            _ = reply_markup
            edited.append((chat_id, message_id, text))
            return True

        async def _send_message(_ctx, *, chat_id: int, text: str, **_kw):
            sent.append((chat_id, text))
            return None

        app._edit_message = _edit_message
        app._send_message = _send_message

        handler = CallbackHandler(app)

        session.busy = True
        update_busy = types.SimpleNamespace(callback_query=_FakeQuery(_project_callback("project_pick", session, idx=1)))
        await handler.handle_callback(update_busy, context=object())

        assert session.project_root == str(project_a.resolve())
        busy_texts = [text for (_chat, _mid, text) in edited] + [text for (_chat, text) in sent]
        assert any("Сессия занята" in text for text in busy_texts)

        edited.clear()
        sent.clear()

        session.busy = False
        update_free = types.SimpleNamespace(callback_query=_FakeQuery(_project_callback("project_pick", session, idx=1)))
        await handler.handle_callback(update_free, context=object())

        assert session.project_root == str(project_b.resolve())
        success_texts = [text for (_chat, _mid, text) in edited] + [text for (_chat, text) in sent]
        assert any("Проект подключен:" in text for text in success_texts)

    asyncio.run(_run())


def test_agent_mode_project_pick_blocked_while_run_lock_is_locked(tmp_path):
    async def _run():
        app = _build_app(tmp_path)
        session = app.manager.create(1, "dummy", str(tmp_path))
        session.modes.active_mode = "agent"

        project_a = tmp_path / "project_a"
        project_b = tmp_path / "project_b"
        project_a.mkdir()
        project_b.mkdir()
        app.user_projects = lambda _chat_id: [str(project_a), str(project_b)]
        session.project_root = str(project_a.resolve())

        edited = []
        sent = []

        async def _edit_message(_ctx, *, chat_id: int, message_id: int, text: str, reply_markup=None, **_kw):
            _ = reply_markup
            edited.append((chat_id, message_id, text))
            return True

        async def _send_message(_ctx, *, chat_id: int, text: str, **_kw):
            sent.append((chat_id, text))
            return None

        app._edit_message = _edit_message
        app._send_message = _send_message

        await session.run_lock.acquire()
        try:
            handler = CallbackHandler(app)
            update_busy = types.SimpleNamespace(callback_query=_FakeQuery(_project_callback("project_pick", session, idx=1)))
            await handler.handle_callback(update_busy, context=object())

            assert session.project_root == str(project_a.resolve())
            blocked_texts = [text for (_chat, _mid, text) in edited] + [text for (_chat, text) in sent]
            assert any("Сессия занята" in text for text in blocked_texts)
        finally:
            session.run_lock.release()

        edited.clear()
        sent.clear()
        update_free = types.SimpleNamespace(callback_query=_FakeQuery(_project_callback("project_pick", session, idx=1)))
        await handler.handle_callback(update_free, context=object())

        assert session.project_root == str(project_b.resolve())
        success_texts = [text for (_chat, _mid, text) in edited] + [text for (_chat, text) in sent]
        assert any("Проект подключен:" in text for text in success_texts)

    asyncio.run(_run())


def test_agent_mode_project_pick_blocked_while_tick_active(tmp_path):
    async def _run():
        app = _build_app(tmp_path)
        session = app.manager.create(1, "dummy", str(tmp_path))
        session.modes.active_mode = "agent"

        project_a = tmp_path / "project_a"
        project_b = tmp_path / "project_b"
        project_a.mkdir()
        project_b.mkdir()
        app.user_projects = lambda _chat_id: [str(project_a), str(project_b)]
        session.project_root = str(project_a.resolve())
        session.is_active_by_tick = lambda: True

        edited = []
        sent = []

        async def _edit_message(_ctx, *, chat_id: int, message_id: int, text: str, reply_markup=None, **_kw):
            _ = reply_markup
            edited.append((chat_id, message_id, text))
            return True

        async def _send_message(_ctx, *, chat_id: int, text: str, **_kw):
            sent.append((chat_id, text))
            return None

        app._edit_message = _edit_message
        app._send_message = _send_message

        handler = CallbackHandler(app)
        update_busy = types.SimpleNamespace(callback_query=_FakeQuery(_project_callback("project_pick", session, idx=1)))
        await handler.handle_callback(update_busy, context=object())

        assert session.project_root == str(project_a.resolve())
        blocked_texts = [text for (_chat, _mid, text) in edited] + [text for (_chat, text) in sent]
        assert any("Сессия занята" in text for text in blocked_texts)

        edited.clear()
        sent.clear()
        session.is_active_by_tick = lambda: False
        update_free = types.SimpleNamespace(callback_query=_FakeQuery(_project_callback("project_pick", session, idx=1)))
        await handler.handle_callback(update_free, context=object())

        assert session.project_root == str(project_b.resolve())
        success_texts = [text for (_chat, _mid, text) in edited] + [text for (_chat, text) in sent]
        assert any("Проект подключен:" in text for text in success_texts)

    asyncio.run(_run())


@pytest.mark.parametrize("action", ["project_connect", "project_change"])
def test_agent_mode_project_connect_blocked_by_all_busy_signals_and_recovers(tmp_path, action):
    async def _run():
        app = _build_app(tmp_path)
        session = app.manager.create(1, "dummy", str(tmp_path))
        session.modes.active_mode = "agent"

        mode = app.mode_registry.get("agent")
        assert mode is not None

        flow_calls = []

        class _FakeDirsFlow:
            @staticmethod
            async def start_flow(**kwargs):
                flow_calls.append(dict(kwargs))
                return None

        mode._dirs_flow = lambda: _FakeDirsFlow()

        edited = []
        sent = []
        tick_state = {"active": False}
        session.is_active_by_tick = lambda: bool(tick_state["active"])

        async def _edit_message(_ctx, *, chat_id: int, message_id: int, text: str, reply_markup=None, **_kw):
            _ = reply_markup
            edited.append((chat_id, message_id, text))
            return True

        async def _send_message(_ctx, *, chat_id: int, text: str, **_kw):
            sent.append((chat_id, text))
            return None

        app._edit_message = _edit_message
        app._send_message = _send_message

        handler = CallbackHandler(app)
        update = types.SimpleNamespace(callback_query=_FakeQuery(_project_callback(action, session)))

        session.busy = True
        await handler.handle_callback(update, context=object())
        assert not flow_calls
        blocked_texts = [text for (_chat, _mid, text) in edited] + [text for (_chat, text) in sent]
        assert any("Сессия занята" in text for text in blocked_texts)

        session.busy = False
        await handler.handle_callback(update, context=object())
        assert flow_calls
        first_calls = len(flow_calls)

        await session.run_lock.acquire()
        try:
            await handler.handle_callback(update, context=object())
            assert len(flow_calls) == first_calls
            blocked_texts = [text for (_chat, _mid, text) in edited] + [text for (_chat, text) in sent]
            assert any("Сессия занята" in text for text in blocked_texts)
        finally:
            session.run_lock.release()

        await handler.handle_callback(update, context=object())
        assert len(flow_calls) == first_calls + 1

        tick_state["active"] = True
        await handler.handle_callback(update, context=object())
        assert len(flow_calls) == first_calls + 1
        blocked_texts = [text for (_chat, _mid, text) in edited] + [text for (_chat, text) in sent]
        assert any("Сессия занята" in text for text in blocked_texts)

        tick_state["active"] = False
        await handler.handle_callback(update, context=object())
        assert len(flow_calls) == first_calls + 2

    asyncio.run(_run())


def test_agent_mode_project_pick_invalid_payload_keeps_project_and_reports_stale_menu(tmp_path):
    async def _run():
        app = _build_app(tmp_path)
        session = app.manager.create(1, "dummy", str(tmp_path))
        session.modes.active_mode = "agent"

        project_a = tmp_path / "project_a"
        project_b = tmp_path / "project_b"
        project_a.mkdir()
        project_b.mkdir()
        app.user_projects = lambda _chat_id: [str(project_a), str(project_b)]
        session.project_root = str(project_a.resolve())

        edited = []
        sent = []

        async def _edit_message(_ctx, *, chat_id: int, message_id: int, text: str, reply_markup=None, **_kw):
            _ = reply_markup
            edited.append((chat_id, message_id, text))
            return True

        async def _send_message(_ctx, *, chat_id: int, text: str, **_kw):
            sent.append((chat_id, text))
            return None

        app._edit_message = _edit_message
        app._send_message = _send_message

        handler = CallbackHandler(app)
        update = types.SimpleNamespace(callback_query=_FakeQuery("ma:agent:project_pick:not_json"))
        await handler.handle_callback(update, context=object())

        assert session.project_root == str(project_a.resolve())
        texts = [text for (_chat, _mid, text) in edited] + [text for (_chat, text) in sent]
        assert any("устарело" in text.lower() for text in texts)

    asyncio.run(_run())




def test_agent_mode_double_launch_queues_second_input_while_first_is_running(tmp_path):
    async def _run():
        app = _build_app(tmp_path)
        session = app.manager.create(1, "dummy", str(tmp_path))
        session.modes.active_mode = "agent"

        sent = []

        async def _send_message(_ctx, *, chat_id: int, text: str, **_kw):
            sent.append((chat_id, text))
            return None

        app._send_message = _send_message

        class _SlowAgentRuntime:
            def __init__(self) -> None:
                self.calls: list[str] = []
                self.started = asyncio.Event()
                self.unblock = asyncio.Event()
                self.finished = asyncio.Event()

            def supports_capability(self, cap: str) -> bool:
                return str(cap or "").strip() == "run_agent"

            async def run(self, session, user_text, bot_app, context, dest):
                _ = session, bot_app, context, dest
                self.calls.append(str(user_text or ""))
                self.started.set()
                await self.unblock.wait()
                self.finished.set()
                return "ok"

        runtime = _SlowAgentRuntime()
        app.register_mode_runtime("agent", runtime)

        await app._handle_user_input(session, "first", 1, context=object())
        for _ in range(200):
            if runtime.started.is_set():
                break
            await asyncio.sleep(0.01)
        assert runtime.started.is_set()

        await app._handle_user_input(session, "second", 1, context=object())
        assert runtime.calls == ["first"]
        assert sent == [("1", InputDispatchService.queue_confirm_prompt_text())]
        pending = InputDispatchService.pending_head(app.ui_state.pending, app.telegram_ui_key("1"))
        assert pending is not None
        assert str(getattr(pending, "text", "") or "") == "second"
        assert list(session.queue) == []

        runtime.unblock.set()
        for _ in range(200):
            if runtime.finished.is_set():
                break
            await asyncio.sleep(0.01)
        assert runtime.finished.is_set()

    asyncio.run(_run())


def test_agent_mode_project_pick_blocked_during_pipeline_and_recovers_after_completion(tmp_path):
    async def _run():
        app = _build_app(tmp_path)
        session = app.manager.create(1, "dummy", str(tmp_path))
        session.modes.active_mode = "agent"

        project_a = tmp_path / "project_a"
        project_b = tmp_path / "project_b"
        project_a.mkdir()
        project_b.mkdir()
        app.user_projects = lambda _chat_id: [str(project_a), str(project_b)]
        session.project_root = str(project_a.resolve())

        edited = []
        sent = []

        async def _edit_message(_ctx, *, chat_id: int, message_id: int, text: str, reply_markup=None, **_kw):
            _ = reply_markup
            edited.append((chat_id, message_id, text))
            return True

        async def _send_message(_ctx, *, chat_id: int, text: str, **_kw):
            sent.append((chat_id, text))
            return None

        app._edit_message = _edit_message
        app._send_message = _send_message

        class _SlowAgentRuntime:
            def __init__(self) -> None:
                self.started = asyncio.Event()
                self.unblock = asyncio.Event()
                self.finished = asyncio.Event()

            def supports_capability(self, cap: str) -> bool:
                return str(cap or "").strip() == "run_agent"

            async def run(self, session, user_text, bot_app, context, dest):
                _ = session, user_text, bot_app, context, dest
                self.started.set()
                await self.unblock.wait()
                self.finished.set()
                return "ok"

        runtime = _SlowAgentRuntime()
        app.register_mode_runtime("agent", runtime)

        await app._handle_user_input(session, "long running task", 1, context=object())
        for _ in range(200):
            if runtime.started.is_set():
                break
            await asyncio.sleep(0.01)
        assert runtime.started.is_set()
        assert bool(session.busy) is True

        handler = CallbackHandler(app)
        update_busy = types.SimpleNamespace(callback_query=_FakeQuery(_project_callback("project_pick", session, idx=1)))
        await handler.handle_callback(update_busy, context=object())

        assert session.project_root == str(project_a.resolve())
        blocked_texts = [text for (_chat, _mid, text) in edited] + [text for (_chat, text) in sent]
        assert any("Сессия занята" in text for text in blocked_texts)

        runtime.unblock.set()
        for _ in range(200):
            if runtime.finished.is_set():
                break
            await asyncio.sleep(0.01)
        assert runtime.finished.is_set()
        for _ in range(200):
            run_lock = getattr(session, "run_lock", None)
            if (
                not bool(getattr(session, "busy", False))
                and not bool(run_lock and run_lock.locked())
            ):
                break
            await asyncio.sleep(0.01)
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
        assert (
            not bool(getattr(session, "busy", False))
            and not bool(run_lock and run_lock.locked())
            and not tick_active
            and not is_session_busy(probe_session, run_lock)
        )
        session.last_tick_ts = time.time() - 4.0

        edited.clear()
        sent.clear()
        update_free = types.SimpleNamespace(callback_query=_FakeQuery(_project_callback("project_pick", session, idx=1)))
        await handler.handle_callback(update_free, context=object())

        assert session.project_root == str(project_b.resolve())
        success_texts = [text for (_chat, _mid, text) in edited] + [text for (_chat, text) in sent]
        assert any("Проект подключен:" in text for text in success_texts)

    asyncio.run(_run())


def test_agent_mode_status_includes_operational_details(tmp_path):
    async def _run():
        app = _build_app(tmp_path)
        session = app.manager.create(1, "dummy", str(tmp_path))
        session.modes.active_mode = "agent"
        session.queue.append({"text": "queued task", "dest": {"kind": "telegram", "chat_id": 1, "user_id": 77}})
        app.ui_state.pending_questions["q_agent"] = {
            "chat_id": 1,
            "session_id": session.id,
            "awaiting_custom": True,
            "created_at": 1_700_000_001.0,
        }
        app.mode_agent_project_pending_by_chat.set(
            agent_project_scope_key(1),
            {
                "session_id": session.id,
                "session_scoped_key": session_scoped_key(session),
                "ui_chat_id": 1,
                "message_thread_id": None,
            },
        )

        edits = []

        async def _edit_message(_ctx, *, chat_id: int, message_id: int, text: str, reply_markup=None, **_kw):
            edits.append((chat_id, message_id, text, reply_markup))
            return True

        app._edit_message = _edit_message

        handler = CallbackHandler(app)
        update = types.SimpleNamespace(callback_query=_FakeQuery("ma:agent:status"))
        await handler.handle_callback(update, context=object())

        assert edits
        text = edits[-1][2]
        assert "Pending questions: 1; active=q_agent; custom=да" in text
        assert "Active plugin flow: project_connect,ask_user" in text
        assert "Template/override: selected=n/a | runtime=n/a | effective=n/a" in text
        assert "Queue origin: telegram | chat=1 | user=77 | text=queued task" in text

    asyncio.run(_run())


def test_agent_mode_handle_input_schedules_background_task(tmp_path):
    async def _run():
        app = _build_app(tmp_path)
        session = app.manager.create(1, "dummy", str(tmp_path))
        session.modes.active_mode = "agent"

        ran = {"n": 0}

        async def _fake_run_mode_pipeline(_session, _prompt, _dest, _context, *, mode_id):
            assert mode_id == "agent"
            ran["n"] += 1
            return None

        app.session_management.run_mode_pipeline = _fake_run_mode_pipeline

        # Route via BotApp._handle_user_input so we test the actual message routing.
        await app._handle_user_input(session, "hi", 1, context=object())
        await asyncio.sleep(0)
        assert ran["n"] == 1
        assert app.mode_tasks.list(session_uid=session_runtime_uid(session), mode_id="agent") == []

    asyncio.run(_run())


def test_agent_dirs_selection_cancelled_is_noop(tmp_path):
    async def _run():
        app = _build_app(tmp_path)
        session = app.manager.create(1, "dummy", str(tmp_path))
        agent = app.mode_registry.get("agent")
        assert agent is not None

        agent._pending_project_by_scope[agent_project_scope_key(1)] = {
            "session_id": session.id,
            "session_scoped_key": session_scoped_key(session),
            "ui_chat_id": 1,
            "message_thread_id": None,
        }
        session.project_root = None
        result = await agent.handle_dirs_selection(
            flow="project",
            event="cancelled",
            path="",
            ctx={"bot_app": app, "chat_id": 1, "context": object(), "session": session},
        )

        assert result is not None
        assert bool(result.ok) is True
        assert "отменен" in str(result.output or "").lower()
        assert session.project_root is None
        assert agent_project_scope_key(1) not in agent._pending_project_by_scope

    asyncio.run(_run())


def test_agent_project_selection_is_isolated_by_thread_and_clears_pending_state(tmp_path):
    async def _run():
        app = _build_app(tmp_path)
        session_one = app.manager.create(1, "dummy", str(tmp_path))
        session_two = app.manager.create(1, "dummy", str(tmp_path))
        session_one.modes.active_mode = "agent"
        session_two.modes.active_mode = "agent"
        session_one.conversation_scope = ConversationScope.from_parts(1, 101)
        session_two.conversation_scope = ConversationScope.from_parts(1, 202)

        thread_sessions = {101: session_one, 202: session_two}

        def _resolve_scope_session(*, reply_chat_id: int, message_thread_id=None, owner_chat_id=None):
            _ = owner_chat_id
            return thread_sessions.get(int(message_thread_id or 0))

        def _resolve_callback_scope(query):
            thread_id = int(getattr(getattr(query, "message", None), "message_thread_id", 0) or 0)
            return 1, thread_id, 1, thread_sessions.get(thread_id)

        app.resolve_telegram_scope_session = _resolve_scope_session  # type: ignore[method-assign]
        app.resolve_telegram_callback_scope = _resolve_callback_scope  # type: ignore[method-assign]

        agent = app.mode_registry.get("agent")
        assert agent is not None

        flow_calls = []

        class _FakeDirsFlow:
            @staticmethod
            async def start_flow(**kwargs):
                flow_calls.append(dict(kwargs))
                return None

        agent._dirs_flow = lambda: _FakeDirsFlow()

        handler = CallbackHandler(app)
        await handler.handle_callback(
            types.SimpleNamespace(callback_query=_FakeQuery(_project_callback("project_connect", session_one), message_thread_id=101)),
            context=object(),
        )
        await handler.handle_callback(
            types.SimpleNamespace(callback_query=_FakeQuery(_project_callback("project_connect", session_two), message_thread_id=202)),
            context=object(),
        )

        scope_one = agent_project_scope_key(1, 101)
        scope_two = agent_project_scope_key(1, 202)
        assert scope_one in agent._pending_project_by_scope
        assert scope_two in agent._pending_project_by_scope
        assert len(flow_calls) == 2

        project_one = tmp_path / "project_one"
        project_two = tmp_path / "project_two"
        project_one.mkdir()
        project_two.mkdir()

        result_one = await agent.handle_dirs_selection(
            flow="project",
            event="selected",
            path=str(project_one),
            ctx={
                "bot_app": app,
                "chat_id": 1,
                "context": types.SimpleNamespace(chat_id=1, message_thread_id=101),
                "session": session_one,
            },
        )

        assert result_one is not None
        assert bool(result_one.ok) is True
        assert session_one.project_root == str(project_one.resolve())
        assert session_two.project_root is None
        assert scope_one not in agent._pending_project_by_scope
        assert scope_two in agent._pending_project_by_scope

        result_two = await agent.handle_dirs_selection(
            flow="project",
            event="cancelled",
            path="",
            ctx={
                "bot_app": app,
                "chat_id": 1,
                "context": types.SimpleNamespace(chat_id=1, message_thread_id=202),
                "session": session_two,
            },
        )

        assert result_two is not None
        assert bool(result_two.ok) is True
        assert "отменен" in str(result_two.output or "").lower()
        assert scope_two not in agent._pending_project_by_scope
        assert session_two.project_root is None

    asyncio.run(_run())


def test_agent_project_selection_late_callback_from_old_session_is_ignored(tmp_path):
    async def _run():
        app = _build_app(tmp_path)
        stale_session = app.manager.create(1, "dummy", str(tmp_path))
        current_session = app.manager.create(1, "dummy", str(tmp_path))
        stale_session.modes.active_mode = "agent"
        current_session.modes.active_mode = "agent"

        project_a = tmp_path / "project_a"
        project_b = tmp_path / "project_b"
        project_a.mkdir()
        project_b.mkdir()
        app.user_projects = lambda _chat_id: [str(project_a), str(project_b)]
        current_session.project_root = str(project_a.resolve())

        edited = []
        sent = []

        async def _edit_message(_ctx, *, chat_id: int, message_id: int, text: str, reply_markup=None, **_kw):
            _ = reply_markup
            edited.append((chat_id, message_id, text))
            return True

        async def _send_message(_ctx, *, chat_id: int, text: str, **_kw):
            sent.append((chat_id, text))
            return None

        app._edit_message = _edit_message
        app._send_message = _send_message

        handler = CallbackHandler(app)
        update = types.SimpleNamespace(callback_query=_FakeQuery(_project_callback("project_pick", stale_session, idx=1)))
        await handler.handle_callback(update, context=object())

        assert current_session.project_root == str(project_a.resolve())
        texts = [text for (_chat, _mid, text) in edited] + [text for (_chat, text) in sent]
        assert any("устарело" in text.lower() for text in texts)

    asyncio.run(_run())


def test_agent_project_selection_disable_clears_pending_and_late_dirs_event_is_ignored(tmp_path):
    async def _run():
        app = _build_app(tmp_path)
        session = app.manager.create(1, "dummy", str(tmp_path))
        session.modes.active_mode = "agent"
        agent = app.mode_registry.get("agent")
        assert agent is not None

        flow_calls = []

        class _FakeDirsFlow:
            @staticmethod
            async def start_flow(**kwargs):
                flow_calls.append(dict(kwargs))
                return None

        agent._dirs_flow = lambda: _FakeDirsFlow()

        handler = CallbackHandler(app)
        await handler.handle_callback(
            types.SimpleNamespace(callback_query=_FakeQuery(_project_callback("project_connect", session))),
            context=object(),
        )

        scope_key = agent_project_scope_key(1)
        assert scope_key in agent._pending_project_by_scope

        await handler.handle_callback(
            types.SimpleNamespace(callback_query=_FakeQuery("ma:agent:disable")),
            context=object(),
        )

        assert scope_key not in agent._pending_project_by_scope
        assert session.modes.active_mode is None

        project_root = tmp_path / "project_after_disable"
        project_root.mkdir()
        result = await agent.handle_dirs_selection(
            flow="project",
            event="selected",
            path=str(project_root),
            ctx={"bot_app": app, "chat_id": 1, "context": object(), "session": session},
        )

        assert result is not None
        assert bool(result.ok) is True
        assert "устарело" in str(result.output or "").lower()
        assert session.project_root is None

    asyncio.run(_run())


def test_agent_mode_handle_input_passes_user_id_into_dest(tmp_path):
    async def _run():
        app = _build_app(tmp_path)
        session = app.manager.create(1, "dummy", str(tmp_path))
        session.modes.active_mode = "agent"
        mode = app.mode_registry.get("agent")
        assert mode is not None

        captured = {"dest": None}

        async def _fake_run_mode_pipeline(_session, _prompt, dest, _context, *, mode_id):
            assert mode_id == "agent"
            captured["dest"] = dict(dest)
            return None

        app.session_management.run_mode_pipeline = _fake_run_mode_pipeline

        await mode.handle_input(
            MessageModel(text="hi", chat_id=1, user_id=77),
            {
                "bot_app": app,
                "session": session,
                "chat_id": 1,
                "context": object(),
            },
        )
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        assert captured["dest"] is not None
        assert captured["dest"].get("user_id") == 77

    asyncio.run(_run())


def test_agent_set_project_root_clear_session_cache_error_is_non_fatal(tmp_path):
    app = _build_app(tmp_path)
    session = app.manager.create(1, "dummy", str(tmp_path))
    mode = app.mode_registry.get("agent")
    assert mode is not None

    class _FailingRuntime:
        def interrupt_session(self, _session_id: str, _chat_id: int, _context) -> None:
            return None

        def clear_session_cache(self, _session_id: str) -> None:
            raise RuntimeError("cache error")

    if mode.mode_dependencies is not None:
        mode.mode_dependencies = mode.mode_dependencies.with_overrides(agent_runtime=_FailingRuntime())
    else:
        mode._extra_services["agent_runtime"] = _FailingRuntime()
    ok, msg = mode._set_project_root(app, session, 1, object(), None)

    assert ok is True
    assert msg == "Проект отключен."
