"""Tests for REQ-7: Logs and Config always remain local regardless of remote_control_enabled."""

import asyncio
import hashlib
import hmac
import json
import time
from types import SimpleNamespace
from urllib.parse import quote

import yaml
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from bot import BotApp
from config import (
    AppConfig, DefaultsConfig, MCPConfig, MiniAppConfig,
    TelegramConfig, ToolConfig,
)
from miniapp.routes import MiniAppRoutes
from miniapp.services.config_service import app_config_to_dict
from session import session_runtime_uid


def _build_init_data(bot_token: str, user_id: int) -> str:
    payload = {
        "auth_date": str(int(time.time())),
        "query_id": "q1",
        "user": json.dumps(
            {"id": user_id, "username": f"u{user_id}", "first_name": "User"},
            ensure_ascii=False,
        ),
    }
    check = "\n".join(f"{k}={v}" for k, v in sorted(payload.items()))
    secret = hmac.new(
        b"WebAppData", bot_token.encode("utf-8"), hashlib.sha256
    ).digest()
    sig = hmac.new(secret, check.encode("utf-8"), hashlib.sha256).hexdigest()
    return (
        f"auth_date={payload['auth_date']}"
        f"&query_id=q1"
        f"&user={quote(payload['user'])}"
        f"&hash={sig}"
    )


def _build_config(tmp_path) -> AppConfig:
    cfg = AppConfig(
        telegram=TelegramConfig(
            token="t",
            whitelist_chat_ids=[1],
            admlist_chat_ids=[1],
        ),
        tools={
            "dummy": ToolConfig(
                name="dummy", mode="headless", cmd=["bash", "-lc", "cat"]
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
        miniapp=MiniAppConfig(enabled=True),
    )
    with open(cfg.path, "w", encoding="utf-8") as f:
        yaml.safe_dump(
            app_config_to_dict(cfg), f, sort_keys=False, allow_unicode=False,
        )
    return cfg


def test_logs_meta_always_local(tmp_path) -> None:
    """GET /api/logs/meta always returns execution_target=local."""
    async def _run() -> None:
        cfg = _build_config(tmp_path)
        app_inst = BotApp(cfg)
        session = app_inst.manager.create(1, "dummy", str(tmp_path))
        # Enable remote control
        session.modes.ssh_remote_enabled = True
        session.modes.remote_control_enabled = True
        session.modes.remote_control_host_alias = "prod"

        routes = MiniAppRoutes(app_inst)
        web_app = web.Application()
        routes.register(web_app)

        admin_data = _build_init_data("t", 1)

        async with TestClient(TestServer(web_app)) as client:
            resp = await client.get(
                "/api/logs/meta",
                headers={"X-Telegram-Init-Data": admin_data},
            )
            assert resp.status == 200
            body = await resp.json()
            assert body["execution_target"] == "local"

        app_inst.shutdown_html_process_pool()

    asyncio.run(_run())


def test_config_view_always_local(tmp_path) -> None:
    """GET /api/config/view always returns execution_target=local."""
    async def _run() -> None:
        cfg = _build_config(tmp_path)
        app_inst = BotApp(cfg)
        session = app_inst.manager.create(1, "dummy", str(tmp_path))
        session.modes.ssh_remote_enabled = True
        session.modes.remote_control_enabled = True

        routes = MiniAppRoutes(app_inst)
        web_app = web.Application()
        routes.register(web_app)

        admin_data = _build_init_data("t", 1)

        async with TestClient(TestServer(web_app)) as client:
            resp = await client.get(
                "/api/config/view",
                headers={"X-Telegram-Init-Data": admin_data},
            )
            assert resp.status == 200
            body = await resp.json()
            assert body["execution_target"] == "local"

        app_inst.shutdown_html_process_pool()

    asyncio.run(_run())


def test_settings_rejects_logs_remote_mode(tmp_path) -> None:
    """PUT settings rejects logs_remote_mode=true with 400."""
    async def _run() -> None:
        cfg = _build_config(tmp_path)
        app_inst = BotApp(cfg)
        session = app_inst.manager.create(1, "dummy", str(tmp_path))

        routes = MiniAppRoutes(app_inst)
        web_app = web.Application()
        routes.register(web_app)

        admin_data = _build_init_data("t", 1)
        uid = session_runtime_uid(session)

        async with TestClient(TestServer(web_app)) as client:
            resp = await client.put(
                f"/api/session/{uid}/settings",
                json={"logs_remote_mode": True},
                headers={"X-Telegram-Init-Data": admin_data},
            )
            assert resp.status == 400

        app_inst.shutdown_html_process_pool()

    asyncio.run(_run())


def test_settings_rejects_config_remote_mode(tmp_path) -> None:
    """PUT settings rejects config_remote_mode=true with 400."""
    async def _run() -> None:
        cfg = _build_config(tmp_path)
        app_inst = BotApp(cfg)
        session = app_inst.manager.create(1, "dummy", str(tmp_path))

        routes = MiniAppRoutes(app_inst)
        web_app = web.Application()
        routes.register(web_app)

        admin_data = _build_init_data("t", 1)
        uid = session_runtime_uid(session)

        async with TestClient(TestServer(web_app)) as client:
            resp = await client.put(
                f"/api/session/{uid}/settings",
                json={"config_remote_mode": True},
                headers={"X-Telegram-Init-Data": admin_data},
            )
            assert resp.status == 400

        app_inst.shutdown_html_process_pool()

    asyncio.run(_run())


def test_settings_allows_logs_remote_mode_false(tmp_path) -> None:
    """PUT settings accepts logs_remote_mode=false (no-op)."""
    async def _run() -> None:
        cfg = _build_config(tmp_path)
        app_inst = BotApp(cfg)
        session = app_inst.manager.create(1, "dummy", str(tmp_path))

        routes = MiniAppRoutes(app_inst)
        web_app = web.Application()
        routes.register(web_app)

        admin_data = _build_init_data("t", 1)
        uid = session_runtime_uid(session)

        async with TestClient(TestServer(web_app)) as client:
            resp = await client.put(
                f"/api/session/{uid}/settings",
                json={"logs_remote_mode": False},
                headers={"X-Telegram-Init-Data": admin_data},
            )
            assert resp.status == 200

        app_inst.shutdown_html_process_pool()

    asyncio.run(_run())


def test_desktop_rejects_logs_remote_mode() -> None:
    """Desktop facade rejects logs_remote_mode=True."""
    from desktop.services.application_facade import ApplicationFacade

    facade = SimpleNamespace(
        session_service=SimpleNamespace(
            get_session_by_uid=lambda uid: SimpleNamespace(
                workdir="/tmp", modes=SimpleNamespace(),
            ),
        ),
        logger=__import__("logging").getLogger("test"),
    )
    result = asyncio.run(
        ApplicationFacade.update_session_setting(facade, "1:s1", "logs_remote_mode", True)
    )
    assert result is False


def test_desktop_rejects_config_remote_mode() -> None:
    """Desktop facade rejects config_remote_mode=True."""
    from desktop.services.application_facade import ApplicationFacade

    facade = SimpleNamespace(
        session_service=SimpleNamespace(
            get_session_by_uid=lambda uid: SimpleNamespace(
                workdir="/tmp", modes=SimpleNamespace(),
            ),
        ),
        logger=__import__("logging").getLogger("test"),
    )
    result = asyncio.run(
        ApplicationFacade.update_session_setting(facade, "1:s1", "config_remote_mode", True)
    )
    assert result is False
