import asyncio
from types import SimpleNamespace

from app.services.run_artifact_store import RunArtifactStore
from session import session_runtime_uid
from modes.analyst.state_store import AnalystStateStore, build_context_key
from tg.command_registry import build_command_registry
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from modes.sdk.services.callback_data import build_session_overview_callback_data
from config import AppConfig, DefaultsConfig, MCPConfig, MiniAppConfig, TelegramConfig, ThreadModeConfig, ToolConfig
from bot import BotApp
from utils import cli_proxy_artifact_path, format_session_label
from telegram.error import BadRequest


def _build_app(tmp_path):
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
        ),
        mcp=MCPConfig(enabled=False),
        mcp_clients=[],
        presets=[],
        path=str(tmp_path / "config.yaml"),
    )
    app = BotApp(cfg)
    app.get_runtime_by_capability("message_tracking").record_message = lambda *_args, **_kwargs: None
    return app


def _build_threaded_app(tmp_path):
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
            state_path=str(tmp_path / "state_threaded.json"),
            toolhelp_path=str(tmp_path / "toolhelp_threaded.json"),
            log_path=str(tmp_path / "bot_threaded.log"),
        ),
        mcp=MCPConfig(enabled=False),
        mcp_clients=[],
        presets=[],
        path=str(tmp_path / "config_threaded.yaml"),
        miniapp=MiniAppConfig(),
        thread_mode=ThreadModeConfig(
            enabled=True,
            mode="group",
            topics_chat_id=-100777000111,
            topic_title_prefix="cli",
        ),
    )
    app = BotApp(cfg)
    app.get_runtime_by_capability("message_tracking").record_message = lambda *_args, **_kwargs: None
    return app


class _FakeThreadBot:
    def __init__(self, thread_ids):
        self._thread_ids = list(thread_ids)
        self.deleted_topics = []
        self.deleted_threads = set()

    async def create_forum_topic(self, *, chat_id: int, name: str):
        _ = str(name)
        thread_id = int(self._thread_ids.pop(0))
        self.deleted_threads.discard((int(chat_id), thread_id))
        return SimpleNamespace(message_thread_id=thread_id)

    async def delete_forum_topic(self, *, chat_id: int, message_thread_id: int):
        self.deleted_threads.add((int(chat_id), int(message_thread_id)))
        self.deleted_topics.append(
            {
                "chat_id": int(chat_id),
                "message_thread_id": int(message_thread_id),
            }
        )
        return True

    async def edit_forum_topic(self, *, chat_id: int, message_thread_id: int, name: str):
        _ = str(name)
        if (int(chat_id), int(message_thread_id)) in self.deleted_threads:
            raise BadRequest("Message thread not found")
        return True


def test_command_registry_hides_new_agent_manager_from_menu(tmp_path):
    app = _build_app(tmp_path)
    menu_names = [e["name"] for e in build_command_registry(app) if e.get("menu")]
    assert "new" not in menu_names
    assert "agent" not in menu_names
    assert "manager" not in menu_names
    assert "sessions" in menu_names
    assert "interrupt" in menu_names


def test_sessions_overview_without_sessions_has_new_and_cancel_buttons(tmp_path):
    app = _build_app(tmp_path)
    text, keyboard = app.handlers.build_sessions_active_overview(1)
    assert "Активных сессий нет." in text
    rows = keyboard.inline_keyboard
    assert len(rows) == 2
    assert len(rows[0]) == 1
    assert len(rows[1]) == 1
    assert rows[0][0].callback_data == "sess_new"
    assert rows[1][0].callback_data == "sess_close_menu"


def test_sessions_overview_with_active_session_has_expected_buttons(tmp_path):
    app = _build_app(tmp_path)
    s = app.manager.create(1, "dummy", str(tmp_path))
    session_uid = session_runtime_uid(s)
    text, keyboard = app.handlers.build_sessions_active_overview(1)
    assert "Активная сессия:" in text
    callbacks = [btn.callback_data for row in keyboard.inline_keyboard for btn in row]
    assert f"sess_status:{s.id}" in callbacks
    assert f"sess_rename:{s.id}" in callbacks
    assert f"sess_resume:{s.id}" in callbacks
    assert f"sess_queue:{s.id}" not in callbacks
    assert f"sess_clearqueue:{s.id}" not in callbacks
    assert f"sess_state:{s.id}" in callbacks
    assert f"sess_close:{s.id}" in callbacks
    assert f"sess_reset:{s.id}" in callbacks
    assert f"sess_cli:{session_uid}:dummy" in callbacks
    assert f"sess_mode_pick:{session_uid}:agent" in callbacks
    assert f"sess_mode_pick:{session_uid}:manager" in callbacks
    assert f"sess_mode_pick:{session_uid}:analyst" in callbacks
    assert f"sess_mode_pick:{session_uid}:webmaster" in callbacks
    assert "sess_new" in callbacks
    assert "sess_list" in callbacks
    assert "sess_close_menu" in callbacks


def test_sessions_overview_shows_queue_buttons_only_when_queue_not_empty(tmp_path):
    app = _build_app(tmp_path)
    s = app.manager.create(1, "dummy", str(tmp_path))
    s.queue.append({"text": "queued", "dest": {"kind": "telegram"}})

    _text, keyboard = app.handlers.build_sessions_active_overview(1)
    callbacks = [btn.callback_data for row in keyboard.inline_keyboard for btn in row]

    assert f"sess_queue:{s.id}" in callbacks
    assert f"sess_clearqueue:{s.id}" in callbacks


def test_sessions_overview_resolves_active_session_via_scope_resolver(tmp_path):
    app = _build_app(tmp_path)
    s = app.manager.create(1, "dummy", str(tmp_path))
    resolve_calls = []

    def _resolve_scope_session(*, reply_chat_id: int, message_thread_id=None, owner_chat_id=None):
        resolve_calls.append(
            {
                "reply_chat_id": int(reply_chat_id),
                "message_thread_id": message_thread_id,
                "owner_chat_id": int(owner_chat_id or reply_chat_id),
            }
        )
        return s

    app.resolve_telegram_scope_session = _resolve_scope_session  # type: ignore[method-assign]

    text, keyboard = app.handlers.build_sessions_active_overview(1)

    assert "Активная сессия:" in text
    callbacks = [btn.callback_data for row in keyboard.inline_keyboard for btn in row]
    assert f"sess_status:{s.id}" in callbacks
    assert resolve_calls == [
        {
            "reply_chat_id": 1,
            "message_thread_id": None,
            "owner_chat_id": 1,
        }
    ]


def test_sessions_overview_prefers_session_uid_before_scope_resolver(tmp_path):
    app = _build_app(tmp_path)
    s = app.manager.create(1, "dummy", str(tmp_path))
    session_uid = str(getattr(getattr(s, "conversation_scope", None), "session_uid", "") or "")
    uid_calls = []

    def _get_by_uid(uid: str):
        uid_calls.append(str(uid))
        if str(uid) == session_uid:
            return s
        return None

    app.manager.get_by_uid = _get_by_uid  # type: ignore[method-assign]
    app.resolve_telegram_scope_session = lambda **_kwargs: (_ for _ in ()).throw(  # type: ignore[method-assign]
        AssertionError("resolver should not be used when session_uid is provided")
    )

    text, keyboard = app.handlers.build_sessions_active_overview(1, session_uid=session_uid)

    assert "Активная сессия:" in text
    callbacks = [btn.callback_data for row in keyboard.inline_keyboard for btn in row]
    assert f"sess_status:{s.id}" in callbacks
    assert uid_calls == [session_uid]


def test_format_session_label_matches_topic_title_format(tmp_path):
    app = _build_app(tmp_path)
    s = app.manager.create(1, "dummy", str(tmp_path))
    s.modes.active_mode = "analyst"

    text = format_session_label(s)
    assert text == f"{s.id} | {s.name}"


def test_session_reset_clears_runtime_and_state_fields(tmp_path):
    async def _run() -> None:
        app = _build_app(tmp_path)
        s = app.manager.create(1, "dummy", str(tmp_path))
        s.resume_token = "token"
        s.state_summary = "summary"
        s.state_updated_at = 123.0
        s.queue.append({"text": "x", "dest": {"kind": "telegram"}})
        s.modes.active_mode = "agent"
        s.manager_quiet_mode = True
        s.agent_memory = {"k": "v"}
        s.project_root = "/tmp/project"
        store = AnalystStateStore(cli_proxy_artifact_path(str(tmp_path), ".analyst_data"))
        context_key = build_context_key(s.chat_id, s.id)
        analyst_ctx = store.load(context_key)
        analyst_ctx.needs_clarification = True
        analyst_ctx.clarification_is_blocking = True
        analyst_ctx.clarification_topic = "scope"
        analyst_ctx.source_user_text = "старый запрос"
        analyst_ctx.clarification_answers = ["mobile"]
        analyst_ctx.last_draft = "stale draft"
        analyst_ctx.last_draft_updated_at = 42.0
        store.save(analyst_ctx)
        run_store = RunArtifactStore(app.config)
        run = run_store.start_run(
            session=s,
            mode_id="analyst",
            run_id="run_20260412T100500Z_session_reset",
            phase="intent",
        )
        original_tool = s.tool
        original_workdir = s.workdir
        original_name = s.name

        class _Msg:
            chat_id = 1
            message_id = 1

        class _Query:
            data = f"sess_reset:{s.id}"
            message = _Msg()

        async def _fake_edit_msg(_context, _query, text, *, reply_markup=None):
            assert text == "Сессия сброшена."
            return True

        app.session_ui._edit_msg = _fake_edit_msg
        handled = await app.session_ui.handle_callback(_Query(), 1, None)

        assert handled is True
        assert s.tool is original_tool
        assert s.workdir == original_workdir
        assert s.name == original_name
        assert s.resume_token is None
        assert s.state_summary is None
        assert s.state_updated_at is None
        assert list(s.queue) == []
        assert s.modes.active_mode is None
        assert s.manager_quiet_mode is False
        assert s.agent_memory == {}
        assert s.project_root is None
        updated_ctx = store.load(context_key)
        assert updated_ctx.needs_clarification is False
        assert updated_ctx.clarification_is_blocking is False
        assert updated_ctx.clarification_topic == ""
        assert updated_ctx.source_user_text == ""
        assert updated_ctx.clarification_answers == []
        assert updated_ctx.last_draft == ""
        assert updated_ctx.last_draft_updated_at == 0.0
        assert run_store.load_state(run)["status"] == "superseded"

    asyncio.run(_run())


def test_session_reset_restores_single_allowed_mode_as_default(tmp_path):
    async def _run() -> None:
        cfg = AppConfig(
            telegram=TelegramConfig(
                token="",
                whitelist_chat_ids=[1],
                admlist_chat_ids=[],
                user_workdirs={1: [str(tmp_path)]},
                user_modes={1: ["agent"]},
            ),
            tools={
                "dummy": ToolConfig(
                    name="dummy",
                    mode="headless",
                    cmd=["bash", "-lc", "cat"],
                )
            },
            defaults=DefaultsConfig(
                workdir=str(tmp_path),
                state_path=str(tmp_path / "state_user.json"),
                toolhelp_path=str(tmp_path / "toolhelp_user.json"),
                log_path=str(tmp_path / "bot_user.log"),
            ),
            mcp=MCPConfig(enabled=False),
            mcp_clients=[],
            presets=[],
            path=str(tmp_path / "config_user.yaml"),
        )
        app = BotApp(cfg)
        app.get_runtime_by_capability("message_tracking").record_message = lambda *_args, **_kwargs: None
        s = app.manager.create(1, "dummy", str(tmp_path))
        s.modes.active_mode = "webmaster"
        s.resume_token = "token"

        class _Msg:
            chat_id = 1
            message_id = 1

        class _Query:
            data = f"sess_reset:{s.id}"
            message = _Msg()

        async def _fake_edit_msg(_context, _query, text, *, reply_markup=None):
            _ = reply_markup
            assert text == "Сессия сброшена."
            return True

        app.session_ui._edit_msg = _fake_edit_msg
        handled = await app.session_ui.handle_callback(_Query(), 1, None)

        assert handled is True
        assert s.resume_token is None
        assert s.modes.active_mode == "agent"

    asyncio.run(_run())


def test_non_admin_sessions_overview_shows_only_allowed_modes(tmp_path):
    cfg = AppConfig(
        telegram=TelegramConfig(
            token="",
            whitelist_chat_ids=[1],
            admlist_chat_ids=[999],
            user_workdirs={1: [str(tmp_path)]},
            user_modes={1: ["agent", "webmaster"]},
        ),
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
        ),
        mcp=MCPConfig(enabled=False),
        mcp_clients=[],
        presets=[],
        path=str(tmp_path / "config.yaml"),
    )
    app = BotApp(cfg)
    session = app.manager.create(1, "dummy", str(tmp_path))
    session_uid = session_runtime_uid(session)

    text, keyboard = app.handlers.build_sessions_active_overview(1)
    callbacks = [btn.callback_data for row in keyboard.inline_keyboard for btn in row]

    assert f"sess_status:{session.id}" in callbacks
    assert f"sess_reset:{session.id}" in callbacks
    assert f"sess_cli:{session_uid}:dummy" not in callbacks
    assert f"sess_rename:{session.id}" not in callbacks
    assert f"sess_resume:{session.id}" not in callbacks
    assert f"sess_mode_pick:{session_uid}:agent" in callbacks
    assert f"sess_mode_pick:{session_uid}:webmaster" in callbacks
    assert f"sess_mode_pick:{session_uid}:analyst" not in callbacks
    assert f"sess_mode_pick:{session_uid}:manager" not in callbacks
    assert "sess_new" in callbacks
    assert "sess_list" not in callbacks
    assert f"sess_state:{next(iter(app.manager.sessions_for_chat(1).keys()))}" not in callbacks
    assert f"sess_close:{next(iter(app.manager.sessions_for_chat(1).keys()))}" not in callbacks
    assert not any(str(item).startswith("user_project_menu") for item in callbacks)
    assert "🤖 Агент:" in text
    assert "🌐 Вебмастер:" in text
    assert "🧠 Аналитик:" not in text
    assert "🏗 Менеджер:" not in text
    assert "Оркестратор:" not in text


def test_non_admin_session_status_hides_unavailable_modes(tmp_path):
    async def _run() -> None:
        cfg = AppConfig(
            telegram=TelegramConfig(
                token="",
                whitelist_chat_ids=[1],
                admlist_chat_ids=[999],
                user_workdirs={1: [str(tmp_path)]},
                user_modes={1: ["agent", "webmaster"]},
            ),
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
            ),
            mcp=MCPConfig(enabled=False),
            mcp_clients=[],
            presets=[],
            path=str(tmp_path / "config.yaml"),
        )
        app = BotApp(cfg)
        session = app.manager.create(1, "dummy", str(tmp_path))

        class _Msg:
            chat_id = 1
            message_id = 1

        class _Query:
            data = f"sess_status:{session.id}"
            message = _Msg()

        sent = {"text": None}

        async def _fake_edit_msg(_context, _query, text, *, reply_markup=None):
            _ = reply_markup
            sent["text"] = text
            return True

        app.session_ui._edit_msg = _fake_edit_msg
        handled = await app.session_ui.handle_callback(_Query(), 1, None)

        assert handled is True
        assert sent["text"] is not None
        assert "🤖 Агент:" in sent["text"]
        assert "🌐 Вебмастер:" in sent["text"]
        assert "🧠 Аналитик:" not in sent["text"]
        assert "🏗 Менеджер:" not in sent["text"]
        assert "Оркестратор:" not in sent["text"]

    asyncio.run(_run())


def test_non_admin_active_overview_hides_queue_controls_when_queue_is_empty(tmp_path):
    cfg = AppConfig(
        telegram=TelegramConfig(
            token="",
            whitelist_chat_ids=[1],
            admlist_chat_ids=[999],
            user_workdirs={1: [str(tmp_path)]},
        ),
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
        ),
        mcp=MCPConfig(enabled=False),
        mcp_clients=[],
        presets=[],
        path=str(tmp_path / "config.yaml"),
    )
    app = BotApp(cfg)
    session = app.manager.create(1, "dummy", str(tmp_path))

    _text, keyboard = app.handlers.build_sessions_active_overview(1)
    callbacks = {btn.callback_data for row in keyboard.inline_keyboard for btn in row}

    expected = {
        f"sess_status:{session.id}",
        f"sess_reset:{session.id}",
        "sess_new",
        "sess_close_menu",
    }
    assert expected.issubset(callbacks)
    assert f"sess_rename:{session.id}" not in callbacks
    assert f"sess_resume:{session.id}" not in callbacks
    assert f"sess_state:{session.id}" not in callbacks
    assert f"sess_close:{session.id}" not in callbacks
    assert f"sess_queue:{session.id}" not in callbacks
    assert f"sess_clearqueue:{session.id}" not in callbacks


def test_non_admin_sessions_overview_shows_orchestrator_toggle_only_when_allowed(tmp_path):
    cfg = AppConfig(
        telegram=TelegramConfig(
            token="",
            whitelist_chat_ids=[1],
            admlist_chat_ids=[999],
            user_workdirs={1: [str(tmp_path)]},
            user_modes={1: ["agent", "orchestrator"]},
        ),
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
        ),
        mcp=MCPConfig(enabled=False),
        mcp_clients=[],
        presets=[],
        path=str(tmp_path / "config.yaml"),
    )
    app = BotApp(cfg)
    session = app.manager.create(1, "dummy", str(tmp_path))

    text, keyboard = app.handlers.build_sessions_active_overview(1)
    callbacks = [btn.callback_data for row in keyboard.inline_keyboard for btn in row]

    assert f"sess_orch_toggle:{session.id}" in callbacks
    assert "Оркестратор:" in text


def test_non_admin_cannot_toggle_orchestrator_without_virtual_mode_access(tmp_path):
    async def _run() -> None:
        cfg = AppConfig(
            telegram=TelegramConfig(
                token="",
                whitelist_chat_ids=[1],
                admlist_chat_ids=[999],
                user_workdirs={1: [str(tmp_path)]},
                user_modes={1: ["agent"]},
            ),
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
            ),
            mcp=MCPConfig(enabled=False),
            mcp_clients=[],
            presets=[],
            path=str(tmp_path / "config.yaml"),
        )
        app = BotApp(cfg)
        session = app.manager.create(1, "dummy", str(tmp_path))

        class _Msg:
            chat_id = 1
            message_id = 1

        class _Query:
            data = f"sess_orch_toggle:{session.id}"
            message = _Msg()

        sent = {"text": None}

        async def _fake_edit_msg(_context, _query, text, *, reply_markup=None):
            _ = reply_markup
            sent["text"] = text
            return True

        app.session_ui._edit_msg = _fake_edit_msg
        handled = await app.session_ui.handle_callback(_Query(), 1, None)

        assert handled is True
        assert sent["text"] == "Оркестратор недоступен для вашего пользователя."
        assert getattr(getattr(session, "orchestrator", None), "enabled", False) is False

    asyncio.run(_run())


def test_non_admin_single_active_mode_hides_mode_picker(tmp_path):
    cfg = AppConfig(
        telegram=TelegramConfig(
            token="",
            whitelist_chat_ids=[1],
            admlist_chat_ids=[],
            user_workdirs={1: [str(tmp_path)]},
            user_modes={1: ["agent"]},
        ),
        tools={
            "dummy": ToolConfig(
                name="dummy",
                mode="headless",
                cmd=["bash", "-lc", "cat"],
            )
        },
        defaults=DefaultsConfig(
            workdir=str(tmp_path),
            state_path=str(tmp_path / "state-single.json"),
            toolhelp_path=str(tmp_path / "toolhelp-single.json"),
            log_path=str(tmp_path / "bot-single.log"),
        ),
        mcp=MCPConfig(enabled=False),
        mcp_clients=[],
        presets=[],
        path=str(tmp_path / "config-single.yaml"),
    )
    app = BotApp(cfg)
    session = app.manager.create(1, "dummy", str(tmp_path))
    session.modes.active_mode = "agent"

    _text, keyboard = app.handlers.build_sessions_active_overview(1)
    callbacks = [btn.callback_data for row in keyboard.inline_keyboard for btn in row]

    assert not any(str(item).startswith("sess_mode_pick:") for item in callbacks)


def test_non_admin_sessions_overview_without_sessions_matches_admin_shell(tmp_path):
    cfg = AppConfig(
        telegram=TelegramConfig(
            token="",
            whitelist_chat_ids=[1],
            admlist_chat_ids=[999],
            user_workdirs={1: [str(tmp_path)]},
        ),
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
        ),
        mcp=MCPConfig(enabled=False),
        mcp_clients=[],
        presets=[],
        path=str(tmp_path / "config.yaml"),
    )
    app = BotApp(cfg)

    text, keyboard = app.handlers.build_sessions_active_overview(1)

    assert "Активных сессий нет." in text
    rows = keyboard.inline_keyboard
    assert len(rows) == 2
    assert rows[0][0].callback_data == "sess_new"
    assert rows[1][0].callback_data == "sess_close_menu"


def test_non_admin_sessions_overview_without_scope_session_shows_list_and_new(tmp_path):
    cfg = AppConfig(
        telegram=TelegramConfig(
            token="",
            whitelist_chat_ids=[1],
            admlist_chat_ids=[999],
            user_workdirs={1: [str(tmp_path)]},
        ),
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
        ),
        mcp=MCPConfig(enabled=False),
        mcp_clients=[],
        presets=[],
        path=str(tmp_path / "config.yaml"),
    )
    app = BotApp(cfg)
    app.manager.create(1, "dummy", str(tmp_path))
    app.resolve_telegram_scope_session = lambda **_kwargs: None  # type: ignore[method-assign]

    text, keyboard = app.handlers.build_sessions_active_overview(1, session=None, session_uid=None)
    callbacks = [btn.callback_data for row in keyboard.inline_keyboard for btn in row]

    assert "Текущая scope-bound сессия не определена." in text
    assert "sess_list" in callbacks
    assert "sess_new" in callbacks
    assert "sess_close_menu" in callbacks
    assert not any(str(item).startswith("user_project_pick") for item in callbacks)


def test_non_admin_sessions_list_filters_out_disallowed_workdirs(tmp_path):
    allowed = tmp_path / "allowed"
    denied = tmp_path / "denied"
    allowed.mkdir()
    denied.mkdir()
    cfg = AppConfig(
        telegram=TelegramConfig(
            token="",
            whitelist_chat_ids=[1],
            admlist_chat_ids=[999],
            user_workdirs={1: [str(allowed)]},
        ),
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
        ),
        mcp=MCPConfig(enabled=False),
        mcp_clients=[],
        presets=[],
        path=str(tmp_path / "config.yaml"),
    )
    app = BotApp(cfg)
    s_ok = app.manager.create(1, "dummy", str(allowed))
    s_bad = app.manager.create(1, "dummy", str(denied))

    keyboard = app.session_ui.build_sessions_menu(1, include_back=True)
    callbacks = [btn.callback_data for row in keyboard.inline_keyboard for btn in row]

    assert build_session_overview_callback_data(s_ok) in callbacks
    assert build_session_overview_callback_data(s_bad) not in callbacks


def test_sessions_menu_uses_shared_overview_callback(tmp_path):
    app = _build_app(tmp_path)
    session = app.manager.create(1, "dummy", str(tmp_path))

    keyboard = app.session_ui.build_sessions_menu(1, include_back=True)
    callbacks = [btn.callback_data for row in keyboard.inline_keyboard for btn in row]

    assert build_session_overview_callback_data(session) in callbacks
    assert f"sess_pick:{session.id}" not in callbacks


def test_legacy_sess_pick_callback_reuses_active_overview_contract(tmp_path):
    async def _run() -> None:
        app = _build_app(tmp_path)
        session = app.manager.create(1, "dummy", str(tmp_path))

        expected_keyboard = InlineKeyboardMarkup(
            [[InlineKeyboardButton("🚫 Close", callback_data=f"sess_close:{session.id}")]]
        )
        calls = []

        def _build_overview(owner_chat_id, *, session=None, session_uid=None):
            calls.append(
                {
                    "owner_chat_id": owner_chat_id,
                    "session_id": getattr(session, "id", None),
                    "session_uid": session_uid,
                }
            )
            return "OVERVIEW", expected_keyboard

        class _Msg:
            chat_id = 1
            message_id = 1

        class _Query:
            data = f"sess_pick:{session.id}"
            message = _Msg()

        sent = {"text": None, "reply_markup": None}

        async def _fake_edit_msg(_context, _query, text, *, reply_markup=None):
            sent["text"] = text
            sent["reply_markup"] = reply_markup
            return True

        app.handlers.build_sessions_active_overview = _build_overview  # type: ignore[method-assign]
        app.session_ui._edit_msg = _fake_edit_msg

        handled = await app.session_ui.handle_callback(_Query(), 1, None)

        assert handled is True
        assert calls == [
            {
                "owner_chat_id": 1,
                "session_id": session.id,
                "session_uid": None,
            }
        ]
        assert sent == {
            "text": "OVERVIEW",
            "reply_markup": expected_keyboard,
        }

    asyncio.run(_run())


def test_non_admin_cannot_close_session_outside_allowed_projects(tmp_path):
    async def _run() -> None:
        allowed = tmp_path / "allowed"
        denied = tmp_path / "denied"
        allowed.mkdir()
        denied.mkdir()
        cfg = AppConfig(
            telegram=TelegramConfig(
                token="",
                whitelist_chat_ids=[1],
                admlist_chat_ids=[999],
                user_workdirs={1: [str(allowed)]},
            ),
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
            ),
            mcp=MCPConfig(enabled=False),
            mcp_clients=[],
            presets=[],
            path=str(tmp_path / "config.yaml"),
        )
        app = BotApp(cfg)
        s_bad = app.manager.create(1, "dummy", str(denied))

        class _Msg:
            chat_id = 1
            message_id = 1

        class _Query:
            data = f"sess_close:{s_bad.id}"
            message = _Msg()

        sent = {"text": None}

        async def _fake_edit_msg(_context, _query, text, *, reply_markup=None):
            sent["text"] = text
            return True

        app.session_ui._edit_msg = _fake_edit_msg
        handled = await app.session_ui.handle_callback(_Query(), 1, None)

        assert handled is True
        assert sent["text"] == "Сессия недоступна."
        assert app.manager.get(1, s_bad.id) is not None

    asyncio.run(_run())


def test_session_menu_close_deletes_forum_topic(tmp_path):
    async def _run() -> None:
        app = _build_threaded_app(tmp_path)
        fake_bot = _FakeThreadBot([515])
        workdir = tmp_path / "menu_close"
        workdir.mkdir()
        session, err = await app.session_creation_service.create_session(
            1,
            "dummy",
            str(workdir),
            bot=fake_bot,
        )
        assert err is None
        assert session is not None

        class _Msg:
            chat_id = -100777000111
            message_id = 1
            message_thread_id = 515

        class _Query:
            data = f"sess_close:{session.id}"
            message = _Msg()

        sent = {"text": None}

        async def _fake_edit_msg(_context, _query, text, *, reply_markup=None):
            _ = reply_markup
            sent["text"] = text
            return True

        app.session_ui._edit_msg = _fake_edit_msg
        handled = await app.session_ui.handle_callback(_Query(), 1, SimpleNamespace(bot=fake_bot))

        assert handled is True
        assert sent["text"] == "Сессия закрыта и удалена из состояния."
        assert app.manager.get(1, session.id) is None
        assert fake_bot.deleted_topics == [
            {
                "chat_id": -100777000111,
                "message_thread_id": 515,
            }
        ]
        assert app.session_thread_repository.get_by_session(owner_chat_id=1, session_id=session.id) is None

    asyncio.run(_run())
