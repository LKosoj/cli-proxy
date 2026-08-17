from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import time
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import quote

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from app.events.bus import SystemEventBus
from app.services.app_runtime_service import AppRuntimeService
from app.services.config_service import ConfigProvider, ConfigService
from app.services.session_service import SessionService
from app.services.task_service import TaskService
from bot import BotApp
from config import (
    AppConfig,
    DefaultsConfig,
    MCPConfig,
    MiniAppConfig,
    TelegramConfig,
    ThreadModeConfig,
    ToolConfig,
    load_config,
    save_config,
)
from miniapp.routes import MiniAppRoutes
from miniapp.services.config_service import app_config_to_dict, validate_draft
from modes.registry import ModeRegistry
from modes.sdk import BaseMode, ToolResult
from modes.sdk.services.mode_registry import ModeRegistryService
from session import SessionManager
from sessions.session_state_access import get_active_mode


class _InMemoryConfigProvider(ConfigProvider):
    def __init__(self, config: AppConfig) -> None:
        self.config = config

    async def load(self) -> AppConfig:
        return self.config

    async def get(self, key: str, default=None):  # type: ignore[no-untyped-def]
        current = self.config
        for part in str(key or "").split("."):
            token = part.strip()
            if not token:
                continue
            if isinstance(current, dict):
                if token not in current:
                    return default
                current = current[token]
                continue
            if not hasattr(current, token):
                return default
            current = getattr(current, token)
        return current


class _FakeTopicBot:
    def __init__(self, thread_ids: list[int]) -> None:
        self._thread_ids = list(thread_ids)
        self.sent_messages: list[dict[str, object]] = []

    async def create_forum_topic(self, *, chat_id: int, name: str):
        if not self._thread_ids:
            raise RuntimeError("no fake thread ids left")
        return SimpleNamespace(message_thread_id=int(self._thread_ids.pop(0)))

    async def send_message(self, **kwargs):
        self.sent_messages.append(dict(kwargs))
        return SimpleNamespace(message_id=len(self.sent_messages))


def _build_init_data(bot_token: str, *, user_id: int) -> str:
    payload = {
        "auth_date": str(int(time.time())),
        "query_id": "q1",
        "user": json.dumps({"id": user_id, "username": f"u{user_id}", "first_name": "User"}, ensure_ascii=False),
    }
    check = "\n".join(f"{key}={value}" for key, value in sorted(payload.items()))
    secret = hmac.new(b"WebAppData", bot_token.encode("utf-8"), hashlib.sha256).digest()
    signature = hmac.new(secret, check.encode("utf-8"), hashlib.sha256).hexdigest()
    return (
        f"auth_date={payload['auth_date']}&query_id=q1&user={quote(payload['user'])}"
        f"&hash={signature}"
    )


def _build_config(
    tmp_path: Path,
    *,
    intent: str,
    chat_id: int = 101,
    telegram_token: str = "token",
    thread_mode_enabled: bool = False,
    topics_chat_id: int | None = None,
    extra_tool: str | None = None,
    miniapp_enabled: bool = False,
) -> AppConfig:
    workdir = tmp_path / f"workdir_{intent}"
    runtime = tmp_path / f"runtime_{intent}"
    logs = tmp_path / f"logs_{intent}"
    workdir.mkdir(parents=True, exist_ok=True)
    runtime.mkdir(parents=True, exist_ok=True)
    logs.mkdir(parents=True, exist_ok=True)

    tools = {
        "dummy": ToolConfig(
            name="dummy",
            mode="headless",
            cmd=["bash", "-lc", "cat"],
        )
    }
    if extra_tool:
        tools[str(extra_tool)] = ToolConfig(
            name=str(extra_tool),
            mode="headless",
            cmd=["bash", "-lc", "cat"],
        )

    return AppConfig(
        telegram=TelegramConfig(
            token=telegram_token,
            whitelist_chat_ids=[chat_id],
            admlist_chat_ids=[chat_id],
            user_workdirs={chat_id: [str(tmp_path)]},
        ),
        tools=tools,
        defaults=DefaultsConfig(
            workdir=str(workdir),
            state_path=str(runtime / "state.json"),
            toolhelp_path=str(runtime / "toolhelp.json"),
            log_path=str(logs / "bot.log"),
        ),
        mcp=MCPConfig(enabled=False),
        mcp_clients=[],
        presets=[],
        path=str(tmp_path / f"config_{intent}.yaml"),
        miniapp=MiniAppConfig(enabled=miniapp_enabled),
        thread_mode=(
            ThreadModeConfig(
                enabled=True,
                mode="group",
                topics_chat_id=topics_chat_id,
                topic_title_prefix="cli",
            )
            if thread_mode_enabled
            else ThreadModeConfig()
        ),
    )


def _save_and_reload_config(config: AppConfig) -> AppConfig:
    save_config(config)
    return load_config(config.path)


def test_integration_cleanup_miniapp_config_session_telegram_scenario(tmp_path) -> None:
    async def _run() -> None:
        topics_chat_id = -100777000111
        source_cfg = _build_config(
            tmp_path,
            intent="miniapp_session_telegram",
            chat_id=101,
            telegram_token="token",
            thread_mode_enabled=True,
            topics_chat_id=topics_chat_id,
            miniapp_enabled=True,
        )
        draft = app_config_to_dict(source_cfg)
        ok, errors, warnings = validate_draft(source_cfg.path, draft)
        assert ok is True
        assert errors == []
        assert warnings == []

        cfg = _save_and_reload_config(source_cfg)
        app = BotApp(cfg)
        fake_bot = _FakeTopicBot([501])
        tracking_runtime = app.get_runtime_by_capability("message_tracking")
        if tracking_runtime is not None and hasattr(tracking_runtime, "record_message"):
            tracking_runtime.record_message = lambda *_args, **_kwargs: None

        workdir = tmp_path / "miniapp-project"
        workdir.mkdir()
        session = None
        client: TestClient | None = None
        server: TestServer | None = None
        try:
            session, err = await app.session_creation_service.create_session(
                101,
                "dummy",
                str(workdir),
                bot=fake_bot,
            )
            assert err is None
            assert session is not None
            assert session.conversation_scope is not None
            assert session.conversation_scope.session_uid == "thread:-100777000111:501"

            web_app = web.Application()
            MiniAppRoutes(app).register(web_app)
            server = TestServer(web_app)
            await server.start_server()
            client = TestClient(server)
            await client.start_server()

            headers = {"X-Telegram-Init-Data": _build_init_data("token", user_id=101)}
            ticket_resp = await client.get("/api/status/ws_ticket", headers=headers)
            assert ticket_resp.status == 200
            ticket = str((await ticket_resp.json()).get("ticket") or "")
            assert ticket

            ws = await client.ws_connect(
                f"/api/status/ws?ticket={quote(ticket)}&session_uid={session.conversation_scope.session_uid}"
            )
            try:
                snapshot = await ws.receive_json(timeout=2)
                assert snapshot["type"] == "snapshot"
                status = snapshot.get("status") or {}
                active = status.get("active_session") or {}
                assert status.get("selected_session_uid") == session.conversation_scope.session_uid
                assert active.get("session_uid") == session.conversation_scope.session_uid
                assert active.get("id") == session.id
            finally:
                await ws.close()

            update = SimpleNamespace(
                effective_chat=SimpleNamespace(id=topics_chat_id),
                effective_user=SimpleNamespace(id=101),
                effective_message=SimpleNamespace(message_thread_id=501),
                message=SimpleNamespace(message_thread_id=501),
            )
            route = app.resolve_telegram_inbound_route(update)
            assert route.unknown_thread is False
            assert route.session is session
            assert route.session_uid == session.conversation_scope.session_uid

            authorized = await app.ensure_telegram_inbound_authorized(
                update,
                SimpleNamespace(bot=fake_bot),
            )
            assert authorized is not None
            assert authorized.session is session
            assert authorized.session_uid == session.conversation_scope.session_uid
        finally:
            if client is not None:
                await client.close()
            if server is not None:
                await server.close()
            app.shutdown_html_process_pool()

    asyncio.run(_run())


@pytest.mark.asyncio
def test_integration_cleanup_config_reload_session_persistence_scenario(tmp_path) -> None:
    config_path = tmp_path / "config_reload.yaml"
    source_cfg = _build_config(
        tmp_path,
        intent="config_reload_initial",
        chat_id=1,
        telegram_token="token-a",
        thread_mode_enabled=True,
        topics_chat_id=1,
    )
    source_cfg.path = str(config_path)
    cfg = _save_and_reload_config(source_cfg)

    manager = SessionManager(cfg)
    workdir = tmp_path / "reload-project"
    workdir.mkdir()
    session = manager.create(1, "dummy", str(workdir), message_thread_id=42)
    session.state_summary = "kept across reload"
    session.modes.active_mode = "manager"
    assert manager.persist_session(1, session.id) is True

    bus = SystemEventBus()
    events: list[tuple[str, dict[str, object]]] = []

    async def _capture(event: str, payload: dict) -> None:
        events.append((str(event), dict(payload)))

    bus.subscribe(AppRuntimeService.EVENT_RELOADED, _capture)

    bot_app = SimpleNamespace(
        config=cfg,
        manager=manager,
        git=SimpleNamespace(config=cfg),
        mcp=SimpleNamespace(config=cfg),
        session_ui=SimpleNamespace(config=cfg),
        system_event_bus=bus,
        is_admin=lambda chat_id: int(chat_id) == 1,
        is_user=lambda chat_id: int(chat_id) == 1,
        iter_mode_runtimes=lambda: [],
    )
    runtime_service = AppRuntimeService(bot_app)

    fresh_cfg = _build_config(
        tmp_path,
        intent="config_reload_initial",
        chat_id=1,
        telegram_token="token-b",
        thread_mode_enabled=True,
        topics_chat_id=1,
        extra_tool="backup",
    )
    fresh_cfg.path = str(config_path)
    save_config(fresh_cfg)

    result = asyncio.run(runtime_service.reload_runtime_config())

    assert result["status"] == "success_with_warnings"
    assert bot_app.config.telegram.token == "token-a"
    assert "backup" in bot_app.config.tools
    assert session.config.telegram.token == "token-a"
    assert session.tool is bot_app.config.tools["dummy"]
    assert manager.get_by_uid(session.conversation_scope.session_uid) is session
    assert manager.persist_session(1, session.id) is True

    restored_manager = SessionManager(bot_app.config)
    restored_session = restored_manager.get_by_uid(session.conversation_scope.session_uid)
    assert restored_session is not None
    assert restored_session.conversation_scope is not None
    assert restored_session.conversation_scope.session_uid == session.conversation_scope.session_uid
    assert restored_session.modes.active_mode == "manager"
    assert restored_session.state_summary == "kept across reload"
    assert restored_session.config.telegram.token == "token-a"
    assert events == [
        (
            AppRuntimeService.EVENT_RELOADED,
            {
                "path": str(config_path),
                "status": "success_with_warnings",
                "applied": result["applied"],
                "restart_required": result["restart_required"],
                "warnings": result["warnings"],
            },
        )
    ]
