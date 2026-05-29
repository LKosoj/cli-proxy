import asyncio

from bot import BotApp, TelegramInboundRoute
from config import AppConfig, DefaultsConfig, MCPConfig, TelegramConfig, ToolConfig
from modes.admin.state_store import AdminStateStore


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

    def _current(chat_id: int):
        sessions = list(app.manager.sessions_for_chat(int(chat_id)).values())
        return sessions[-1] if sessions else None

    def _set_active(chat_id: int, session_id: str) -> bool:
        session = app.manager.get(int(chat_id), str(session_id))
        return session is not None

    def _active(chat_id: int):
        return _current(int(chat_id))

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

    app.manager.active = _active  # type: ignore[attr-defined]
    app.manager.set_active = _set_active  # type: ignore[attr-defined]
    app.resolve_telegram_inbound_route = _resolve_inbound_route  # type: ignore[method-assign]
    mode = app.mode_registry.get("admin")
    if mode is not None:
        mode._resolve_runner_service = lambda: None
    return app


def test_admin_on_enable_activates_runtime_and_marks_session_enabled(tmp_path) -> None:
    async def _run() -> None:
        app = _build_app(tmp_path)
        try:
            session = app.manager.create(1, "dummy", str(tmp_path))
            app.manager.set_active(1, session.id)

            mode = app.mode_registry.get("admin")
            assert mode is not None

            result = await mode.on_enable(
                {
                    "session": session,
                    "bot_app": app,
                    "chat_id": 1,
                    "user_id": 42,
                    "context": None,
                }
            )
            assert result is None or getattr(result, "success", False) is True

            store = AdminStateStore(app.config.defaults.state_path)
            state = store.get_session_state(session.id, chat_id=1)
            assert state is not None
            assert bool(state.get("enabled")) is True
            assert bool(getattr(session, "admin_enabled", False)) is True
        finally:
            app.shutdown_html_process_pool()

    asyncio.run(_run())


def test_admin_on_enable_is_idempotent(tmp_path) -> None:
    async def _run() -> None:
        app = _build_app(tmp_path)
        try:
            session = app.manager.create(1, "dummy", str(tmp_path))
            app.manager.set_active(1, session.id)

            mode = app.mode_registry.get("admin")
            assert mode is not None

            ctx = {
                "session": session,
                "bot_app": app,
                "chat_id": 1,
                "user_id": 42,
                "context": None,
            }

            await mode.on_enable(ctx)
            await mode.on_enable(ctx)
            await mode.on_enable(ctx)

            store = AdminStateStore(app.config.defaults.state_path)
            state = store.get_session_state(session.id, chat_id=1)
            assert state is not None
            assert bool(state.get("enabled")) is True
        finally:
            app.shutdown_html_process_pool()

    asyncio.run(_run())


def test_admin_on_disable_cancels_runtime_and_marks_session_disabled(tmp_path) -> None:
    async def _run() -> None:
        app = _build_app(tmp_path)
        try:
            session = app.manager.create(1, "dummy", str(tmp_path))
            app.manager.set_active(1, session.id)

            mode = app.mode_registry.get("admin")
            assert mode is not None

            ctx = {
                "session": session,
                "bot_app": app,
                "chat_id": 1,
                "user_id": 42,
                "context": None,
            }

            await mode.on_enable(ctx)
            await mode.on_disable(ctx)

            store = AdminStateStore(app.config.defaults.state_path)
            state = store.get_session_state(session.id, chat_id=1)
            assert state is not None
            assert bool(state.get("enabled")) is False
            assert bool(getattr(session, "admin_enabled", True)) is False
        finally:
            app.shutdown_html_process_pool()

    asyncio.run(_run())


def test_admin_on_disable_without_prior_enable_is_safe(tmp_path) -> None:
    async def _run() -> None:
        app = _build_app(tmp_path)
        try:
            session = app.manager.create(1, "dummy", str(tmp_path))
            app.manager.set_active(1, session.id)

            mode = app.mode_registry.get("admin")
            assert mode is not None

            ctx = {
                "session": session,
                "bot_app": app,
                "chat_id": 1,
                "user_id": 42,
                "context": None,
            }

            result = await mode.on_disable(ctx)
            assert result is None or getattr(result, "success", True) is True
        finally:
            app.shutdown_html_process_pool()

    asyncio.run(_run())


def test_admin_on_enable_without_chat_id_returns_none(tmp_path) -> None:
    async def _run() -> None:
        app = _build_app(tmp_path)
        try:
            session = app.manager.create(1, "dummy", str(tmp_path))
            mode = app.mode_registry.get("admin")
            assert mode is not None

            result = await mode.on_enable(
                {
                    "session": session,
                    "bot_app": app,
                    "chat_id": None,
                    "user_id": 42,
                    "context": None,
                }
            )
            assert result is None
        finally:
            app.shutdown_html_process_pool()

    asyncio.run(_run())


def test_admin_on_enable_then_disable_then_enable_idempotent(tmp_path) -> None:
    async def _run() -> None:
        app = _build_app(tmp_path)
        try:
            session = app.manager.create(1, "dummy", str(tmp_path))
            app.manager.set_active(1, session.id)

            mode = app.mode_registry.get("admin")
            assert mode is not None

            ctx = {
                "session": session,
                "bot_app": app,
                "chat_id": 1,
                "user_id": 42,
                "context": None,
            }

            await mode.on_enable(ctx)
            await mode.on_disable(ctx)
            await mode.on_enable(ctx)

            store = AdminStateStore(app.config.defaults.state_path)
            state = store.get_session_state(session.id, chat_id=1)
            assert state is not None
            assert bool(state.get("enabled")) is True
            assert bool(getattr(session, "admin_enabled", False)) is True
        finally:
            app.shutdown_html_process_pool()

    asyncio.run(_run())
