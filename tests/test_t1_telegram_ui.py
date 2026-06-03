"""T1 Telegram-UI localisation tests."""
from __future__ import annotations

import types
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from i18n import SUPPORTED_LANGS
from i18n.resolver import lang_from_query, lang_from_update
from i18n.translator import reload_catalogs
from tg.handlers import BotHandlers, build_lang_menu
from tg.command_registry import build_command_registry


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_config(
    user_languages: dict[int, str] | None = None,
    default_language: str | None = "ru",
) -> Any:
    tg = MagicMock()
    tg.user_languages = user_languages or {}
    defaults = MagicMock()
    defaults.default_language = default_language
    cfg = MagicMock()
    cfg.telegram = tg
    cfg.defaults = defaults
    return cfg


def _make_bot_app(*, available_tools=None, projects=None, lang="ru", config=None):
    """Minimal bot_app stub for BotHandlers tests."""
    if available_tools is None:
        available_tools = ["codex", "gemini"]
    if projects is None:
        projects = ["/tmp/proj1"]
    if config is None:
        config = _make_config(default_language=lang)
    app = MagicMock()
    app.config = config
    app._available_tools = MagicMock(return_value=available_tools)
    app._expected_tools = MagicMock(return_value="codex,gemini")
    app.user_projects = MagicMock(return_value=projects)
    app.access_policy_service = None
    # Minimal session manager
    session = MagicMock()
    session.id = "sess1"
    session.tool = MagicMock()
    session.tool.name = "codex"
    session.active_cli = "codex"
    session.chat_id = 101
    session.busy = False
    session.run_lock = None
    session.is_active_by_tick = None
    app.manager = MagicMock()
    app.manager.get_by_uid = MagicMock(return_value=session)
    app.manager.sessions_for_chat = MagicMock(return_value={"sess1": session})
    return app, session


def _extract_all_button_texts(keyboard) -> list[str]:
    texts = []
    for row in keyboard.inline_keyboard:
        for btn in row:
            texts.append(btn.text)
    return texts


def _extract_callback_datas(keyboard) -> list[str]:
    datas = []
    for row in keyboard.inline_keyboard:
        for btn in row:
            datas.append(btn.callback_data)
    return datas


# ---------------------------------------------------------------------------
# build_lang_menu
# ---------------------------------------------------------------------------

def test_lang_menu_marks_current_lang() -> None:
    reload_catalogs()
    _text, keyboard = build_lang_menu("en")
    btns = _extract_all_button_texts(keyboard)
    # en button should be checked, others unchecked
    assert any("✅" in b and "English" in b for b in btns)
    assert any("⬜" in b and "Русский" in b for b in btns)
    assert any("⬜" in b and "中文" in b for b in btns)
    assert any("⬜" in b and "Deutsch" in b for b in btns)


def test_lang_menu_back_callback() -> None:
    reload_catalogs()
    _text, keyboard = build_lang_menu("ru", back_callback="my_back")
    datas = _extract_callback_datas(keyboard)
    assert "my_back" in datas


def test_lang_menu_lang_set_callbacks() -> None:
    reload_catalogs()
    _text, keyboard = build_lang_menu("ru")
    datas = _extract_callback_datas(keyboard)
    for code in SUPPORTED_LANGS:
        assert f"lang_set:{code}" in datas


def test_lang_menu_all_lang_set_callbacks_under_64_bytes() -> None:
    for code in SUPPORTED_LANGS:
        cb = f"lang_set:{code}"
        assert len(cb.encode()) <= 64, f"callback_data too long: {cb!r}"


# ---------------------------------------------------------------------------
# _cb_lang_menu
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_cb_lang_menu_shows_menu() -> None:
    reload_catalogs()
    from tg.callback_actions.session import SessionActionsMixin

    config = _make_config(user_languages={42: "en"}, default_language="ru")
    bot_app = MagicMock()
    bot_app.config = config

    mixin = SessionActionsMixin()
    mixin.bot_app = bot_app
    mixin._edit_msg = AsyncMock()
    mixin._callback_scope = MagicMock(return_value=(42, 42, None))

    query = MagicMock()
    query.from_user = MagicMock()
    query.from_user.id = 42
    query.from_user.language_code = "ru"

    await mixin._cb_lang_menu(data="lang_menu", chat_id=42, query=query, context=MagicMock())

    mixin._edit_msg.assert_called_once()
    _call_args = mixin._edit_msg.call_args
    keyboard = _call_args.kwargs.get("reply_markup") or _call_args[1].get("reply_markup")
    assert keyboard is not None
    datas = _extract_callback_datas(keyboard)
    assert "lang_set:en" in datas
    # en should be marked as current
    btns = _extract_all_button_texts(keyboard)
    assert any("✅" in b and "English" in b for b in btns)


# ---------------------------------------------------------------------------
# _cb_lang_set
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_cb_lang_set_valid_writes_config() -> None:
    reload_catalogs()
    from tg.callback_actions.session import SessionActionsMixin

    config = _make_config(user_languages={}, default_language="ru")
    config_service = MagicMock()
    config_service.set_user_language = AsyncMock(return_value=MagicMock(ok=True))

    bot_app = MagicMock()
    bot_app.config = config
    bot_app.config_service = config_service
    bot_app.handlers = MagicMock()
    bot_app.handlers.build_sessions_active_overview = MagicMock(
        return_value=("text", MagicMock())
    )

    mixin = SessionActionsMixin()
    mixin.bot_app = bot_app
    mixin._edit_msg = AsyncMock()
    mixin._callback_scope = MagicMock(return_value=(101, 101, None))

    query = MagicMock()
    query.from_user = MagicMock()
    query.from_user.id = 99
    query.answer = AsyncMock()

    await mixin._cb_lang_set(data="lang_set:en", chat_id=101, query=query, context=MagicMock())

    config_service.set_user_language.assert_called_once_with(99, "en")


@pytest.mark.asyncio
async def test_cb_lang_set_answers_with_native_name() -> None:
    reload_catalogs()
    from tg.callback_actions.session import SessionActionsMixin

    config = _make_config(user_languages={}, default_language="ru")
    config_service = MagicMock()
    config_service.set_user_language = AsyncMock(return_value=MagicMock(ok=True))

    bot_app = MagicMock()
    bot_app.config = config
    bot_app.config_service = config_service
    bot_app.handlers = MagicMock()
    bot_app.handlers.build_sessions_active_overview = MagicMock(return_value=("text", MagicMock()))

    mixin = SessionActionsMixin()
    mixin.bot_app = bot_app
    mixin._edit_msg = AsyncMock()
    mixin._callback_scope = MagicMock(return_value=(101, 101, None))

    query = MagicMock()
    query.from_user = MagicMock()
    query.from_user.id = 99
    query.answer = AsyncMock()

    await mixin._cb_lang_set(data="lang_set:de", chat_id=101, query=query, context=MagicMock())

    answer_text = query.answer.call_args[0][0]
    assert "Deutsch" in answer_text


@pytest.mark.asyncio
async def test_cb_lang_set_redraws_menu_in_new_lang() -> None:
    reload_catalogs()
    from tg.callback_actions.session import SessionActionsMixin

    config = _make_config(user_languages={}, default_language="ru")
    config_service = MagicMock()
    config_service.set_user_language = AsyncMock(return_value=MagicMock(ok=True))

    bot_app = MagicMock()
    bot_app.config = config
    bot_app.config_service = config_service
    bot_app.handlers = MagicMock()
    bot_app.handlers.build_sessions_active_overview = MagicMock(return_value=("text", MagicMock()))

    mixin = SessionActionsMixin()
    mixin.bot_app = bot_app
    mixin._edit_msg = AsyncMock()
    mixin._callback_scope = MagicMock(return_value=(101, 101, None))

    query = MagicMock()
    query.from_user = MagicMock()
    query.from_user.id = 99
    query.answer = AsyncMock()

    await mixin._cb_lang_set(data="lang_set:en", chat_id=101, query=query, context=MagicMock())

    bot_app.handlers.build_sessions_active_overview.assert_called_once()
    kwargs = bot_app.handlers.build_sessions_active_overview.call_args.kwargs
    assert kwargs.get("lang") == "en"
    config_service.set_user_language.assert_awaited_once_with(99, "en")
    # W2: live in-memory config reflects the choice for later resolve_user_lang().
    assert config.telegram.user_languages == {99: "en"}


@pytest.mark.asyncio
async def test_cb_lang_set_invalid_code() -> None:
    reload_catalogs()
    from tg.callback_actions.session import SessionActionsMixin

    config = _make_config(user_languages={}, default_language="ru")
    config_service = MagicMock()
    config_service.set_user_language = AsyncMock()

    bot_app = MagicMock()
    bot_app.config = config
    bot_app.config_service = config_service

    mixin = SessionActionsMixin()
    mixin.bot_app = bot_app
    mixin._edit_msg = AsyncMock()

    query = MagicMock()
    query.from_user = MagicMock()
    query.from_user.id = 99
    query.answer = AsyncMock()

    await mixin._cb_lang_set(data="lang_set:xx", chat_id=101, query=query, context=MagicMock())

    config_service.set_user_language.assert_not_called()
    query.answer.assert_called_once()
    assert query.answer.call_args.kwargs.get("show_alert") is True


@pytest.mark.asyncio
async def test_cb_lang_set_no_user_id() -> None:
    reload_catalogs()
    from tg.callback_actions.session import SessionActionsMixin

    config = _make_config(user_languages={}, default_language="ru")
    config_service = MagicMock()
    config_service.set_user_language = AsyncMock()

    bot_app = MagicMock()
    bot_app.config = config
    bot_app.config_service = config_service

    mixin = SessionActionsMixin()
    mixin.bot_app = bot_app
    mixin._edit_msg = AsyncMock()
    mixin._callback_scope = MagicMock(return_value=(101, 101, None))

    query = MagicMock()
    query.from_user = None
    query.answer = AsyncMock()

    # Should not raise
    await mixin._cb_lang_set(data="lang_set:en", chat_id=101, query=query, context=MagicMock())

    config_service.set_user_language.assert_not_called()


# ---------------------------------------------------------------------------
# build_sessions_active_overview with lang
# ---------------------------------------------------------------------------

def test_build_sessions_overview_en_buttons() -> None:
    reload_catalogs()
    app, session = _make_bot_app()
    handlers = BotHandlers(app)

    # Patch visibility and state helpers
    handlers._is_admin = MagicMock(return_value=True)
    handlers._visible_sessions_for_chat = MagicMock(return_value={"sess1": session})
    handlers._resolve_overview_session = MagicMock(return_value=session)
    handlers._is_session_visible_for_chat = MagicMock(return_value=True)
    handlers._registered_modes = MagicMock(return_value=[])
    handlers._active_session_status_text = MagicMock(return_value="Status")
    handlers._ssh_remote_button = MagicMock(return_value=None)

    mock_vis = MagicMock()
    mock_vis.allows = MagicMock(return_value=False)

    import app.services.menu_visibility_policy as mvp
    original = mvp.build_session_overview_visibility
    mvp.build_session_overview_visibility = MagicMock(return_value=mock_vis)
    try:
        _text, keyboard = handlers.build_sessions_active_overview(101, session=session, lang="en")
    finally:
        mvp.build_session_overview_visibility = original

    btns = _extract_all_button_texts(keyboard)
    # Should have English lang button
    assert any("Language" in b for b in btns)
    # Should have English cancel
    assert any("Cancel" in b for b in btns)


def test_build_sessions_overview_resolves_lang_from_chat_when_not_passed() -> None:
    """W1: when callers omit lang, the overview must resolve it from chat_id's
    persisted language rather than silently defaulting to Russian."""
    reload_catalogs()
    config = _make_config(user_languages={101: "en"}, default_language="ru")
    app, session = _make_bot_app(config=config)
    handlers = BotHandlers(app)

    handlers._is_admin = MagicMock(return_value=True)
    handlers._visible_sessions_for_chat = MagicMock(return_value={"sess1": session})
    handlers._resolve_overview_session = MagicMock(return_value=session)
    handlers._is_session_visible_for_chat = MagicMock(return_value=True)
    handlers._registered_modes = MagicMock(return_value=[])
    handlers._active_session_status_text = MagicMock(return_value="Status")
    handlers._ssh_remote_button = MagicMock(return_value=None)

    mock_vis = MagicMock()
    mock_vis.allows = MagicMock(return_value=False)

    import app.services.menu_visibility_policy as mvp
    original = mvp.build_session_overview_visibility
    mvp.build_session_overview_visibility = MagicMock(return_value=mock_vis)
    try:
        # No lang kwarg — must auto-resolve to "en" from user_languages[101].
        _text, keyboard = handlers.build_sessions_active_overview(101, session=session)
    finally:
        mvp.build_session_overview_visibility = original

    btns = _extract_all_button_texts(keyboard)
    assert any("Language" in b for b in btns)
    assert any("Cancel" in b for b in btns)


def test_build_sessions_overview_lang_button_present() -> None:
    reload_catalogs()
    app, session = _make_bot_app()
    handlers = BotHandlers(app)

    handlers._is_admin = MagicMock(return_value=True)
    handlers._visible_sessions_for_chat = MagicMock(return_value={"sess1": session})
    handlers._resolve_overview_session = MagicMock(return_value=session)
    handlers._is_session_visible_for_chat = MagicMock(return_value=True)
    handlers._registered_modes = MagicMock(return_value=[])
    handlers._active_session_status_text = MagicMock(return_value="Status")
    handlers._ssh_remote_button = MagicMock(return_value=None)

    mock_vis = MagicMock()
    mock_vis.allows = MagicMock(return_value=False)

    import app.services.menu_visibility_policy as mvp
    original = mvp.build_session_overview_visibility
    mvp.build_session_overview_visibility = MagicMock(return_value=mock_vis)
    try:
        _text, keyboard = handlers.build_sessions_active_overview(101, session=session, lang="ru")
    finally:
        mvp.build_session_overview_visibility = original

    datas = _extract_callback_datas(keyboard)
    assert "lang_menu" in datas


# ---------------------------------------------------------------------------
# _bot_commands localisation
# ---------------------------------------------------------------------------

def _make_registry_app():
    return types.SimpleNamespace(
        cmd_start=object(),
        cmd_sessions=object(),
        cmd_interrupt=object(),
        cmd_git=object(),
        cmd_files=object(),
        cmd_miniapp=object(),
        cmd_selfupdate=object(),
        cmd_preset=object(),
        cmd_metrics=object(),
        cmd_tools=object(),
        cmd_newpath=object(),
        cmd_close=object(),
        cmd_status=object(),
        cmd_limits=object(),
        cmd_queue=object(),
        cmd_clearqueue=object(),
        cmd_rename=object(),
        cmd_cwd=object(),
        cmd_dirs=object(),
        cmd_resume=object(),
        cmd_state=object(),
        cmd_setprompt=object(),
        cmd_send=object(),
        cmd_lint_evolution_status=object(),
        cmd_lint_autopause_resume=object(),
        cmd_lint_schema_history=object(),
        cmd_lint_gate_dry_run=object(),
        mode_registry_service=None,
    )


def test_bot_commands_lang_en() -> None:
    reload_catalogs()
    app = _make_registry_app()
    bot_app = MagicMock()
    bot_app.config = _make_config(default_language="en")
    handlers = BotHandlers(bot_app)

    # Need command registry — patch bot_app
    import tg.command_registry as cr
    original_build = cr.build_command_registry
    cr.build_command_registry = MagicMock(return_value=original_build(app))
    try:
        commands = handlers._bot_commands(lang="en")
    finally:
        cr.build_command_registry = original_build

    descs = {cmd.command: cmd.description for cmd in commands}
    assert "start" in descs
    assert "Session management menu." in descs.get("sessions", ""), descs.get("sessions")


def test_bot_commands_lang_ru() -> None:
    reload_catalogs()
    app = _make_registry_app()
    bot_app = MagicMock()
    bot_app.config = _make_config(default_language="ru")
    handlers = BotHandlers(bot_app)

    import tg.command_registry as cr
    original_build = cr.build_command_registry
    cr.build_command_registry = MagicMock(return_value=original_build(app))
    try:
        commands = handlers._bot_commands(lang="ru")
    finally:
        cr.build_command_registry = original_build

    descs = {cmd.command: cmd.description for cmd in commands}
    assert "sessions" in descs
    assert "Меню управления сессиями." in descs.get("sessions", ""), descs.get("sessions")


def test_bot_commands_mode_desc_key() -> None:
    reload_catalogs()
    # Mode entry must have desc_key="cmd.mode.desc" and desc_params with {label}
    app = _make_registry_app()

    mock_mode = MagicMock()
    mock_mode.display_name = "Аналитик"
    mode_service = MagicMock()
    mode_service.list_modes = MagicMock(return_value=[("analyst", "Аналитик")])
    mode_service.get = MagicMock(return_value=mock_mode)
    app_with_mode = types.SimpleNamespace(**{
        attr: getattr(app, attr) for attr in dir(app) if not attr.startswith("_")
    })
    app_with_mode.mode_registry_service = mode_service

    registry = build_command_registry(app_with_mode)
    mode_entries = [e for e in registry if e.get("name") == "analyst"]
    assert mode_entries, "analyst command not registered"
    entry = mode_entries[0]
    assert entry.get("desc_key") == "cmd.mode.desc"
    assert "label" in (entry.get("desc_params") or {})


@pytest.mark.asyncio
async def test_set_bot_commands_calls_all_langs() -> None:
    reload_catalogs()

    bot_app = MagicMock()
    bot_app.config = _make_config(default_language="ru")
    bot_app.config.telegram.admlist_chat_ids = []
    handlers = BotHandlers(bot_app)

    set_my_commands_calls: list[dict] = []

    async def _fake_set_my_commands(cmds, *, scope=None, language_code=None):
        set_my_commands_calls.append({"language_code": language_code})

    tg_app = MagicMock()
    tg_app.bot.set_my_commands = _fake_set_my_commands

    import tg.command_registry as cr
    original_build = cr.build_command_registry
    # Return minimal registry — just start command
    cr.build_command_registry = MagicMock(return_value=[{
        "name": "start",
        "desc_key": "cmd.start.desc",
        "desc": "Start",
        "menu": True,
        "admin_only": False,
    }])
    try:
        await handlers.set_bot_commands(tg_app)
    finally:
        cr.build_command_registry = original_build

    lang_codes = {c["language_code"] for c in set_my_commands_calls}
    for lang in SUPPORTED_LANGS:
        assert lang in lang_codes, f"set_my_commands not called for lang={lang}"
    # Default scope (no language_code) must also be set as fallback for
    # users whose Telegram language is outside the supported set.
    assert None in lang_codes, "default (no language_code) scope must be set"


@pytest.mark.asyncio
async def test_set_bot_commands_admin_chat_has_default_scope() -> None:
    reload_catalogs()

    bot_app = MagicMock()
    bot_app.config = _make_config(default_language="ru")
    bot_app.config.telegram.admlist_chat_ids = [555]
    handlers = BotHandlers(bot_app)

    calls: list[dict] = []

    async def _fake_set_my_commands(cmds, *, scope=None, language_code=None):
        calls.append({"scope": type(scope).__name__, "language_code": language_code})

    tg_app = MagicMock()
    tg_app.bot.set_my_commands = _fake_set_my_commands

    import tg.command_registry as cr
    original_build = cr.build_command_registry
    cr.build_command_registry = MagicMock(return_value=[{
        "name": "admin",
        "desc_key": "cmd.start.desc",
        "desc": "Admin",
        "menu": True,
        "admin_only": True,
    }])
    try:
        await handlers.set_bot_commands(tg_app)
    finally:
        cr.build_command_registry = original_build

    chat_calls = [c for c in calls if c["scope"] == "BotCommandScopeChat"]
    assert chat_calls, "admin chat scope must be set"
    assert any(c["language_code"] is None for c in chat_calls), \
        "admin chat must get a default (no language_code) commands call"


# ---------------------------------------------------------------------------
# lang_from_query / lang_from_update resolution
# ---------------------------------------------------------------------------

def test_lang_from_query_saved_beats_auto() -> None:
    config = _make_config(user_languages={7: "de"}, default_language="ru")
    query = MagicMock()
    query.from_user = MagicMock()
    query.from_user.id = 7
    query.from_user.language_code = "en"

    result = lang_from_query(query, config)
    assert result == "de"


def test_lang_from_query_auto_fallback_to_default() -> None:
    config = _make_config(user_languages={}, default_language="en")
    query = MagicMock()
    query.from_user = MagicMock()
    query.from_user.id = 99
    query.from_user.language_code = "fr"  # unsupported

    result = lang_from_query(query, config)
    assert result == "en"


def test_lang_from_query_group_uses_from_user_id() -> None:
    """In groups, language must be resolved by from_user.id, not chat_id."""
    group_chat_id = -100123456
    user_id = 777
    config = _make_config(user_languages={user_id: "zh"}, default_language="ru")

    query = MagicMock()
    query.from_user = MagicMock()
    query.from_user.id = user_id
    query.from_user.language_code = "ru"
    # chat would have different id but we don't use it
    query.message = MagicMock()
    query.message.chat_id = group_chat_id

    result = lang_from_query(query, config)
    assert result == "zh"


def test_lang_from_update_saved_beats_auto() -> None:
    config = _make_config(user_languages={5: "de"}, default_language="ru")
    update = MagicMock()
    update.effective_user = MagicMock()
    update.effective_user.id = 5
    update.effective_user.language_code = "en"

    result = lang_from_update(update, config)
    assert result == "de"
