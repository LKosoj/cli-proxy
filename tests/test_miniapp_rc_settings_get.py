"""Integration tests for GET /api/session/{uid}/settings with remote control fields."""

import asyncio
import hashlib
import hmac
import json
import time
from urllib.parse import quote

import yaml
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from app.services.ssh_config_loader import save_ssh_config
from bot import BotApp
from config import (
    AppConfig, DefaultsConfig, MCPConfig, MiniAppConfig,
    SSHHostConfig, TelegramConfig, ToolConfig,
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
            whitelist_chat_ids=[1, 2],
            admlist_chat_ids=[1],
            user_workdirs={2: [str(tmp_path)]},
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


def test_get_settings_returns_rc_fields(tmp_path) -> None:
    """GET settings includes remote_control_enabled, host_alias, hosts, effective."""
    async def _run() -> None:
        cfg = _build_config(tmp_path)
        app_inst = BotApp(cfg)
        session = app_inst.manager.create(1, "dummy", str(tmp_path))
        session.modes.ssh_remote_enabled = True
        session.modes.remote_control_enabled = True
        session.modes.remote_control_host_alias = "prod"

        # Create SSH hosts
        save_ssh_config(str(tmp_path), {
            "prod": SSHHostConfig(
                host="1.1.1.1", user="deploy",
                remote_project_root="/srv/app",
            ),
        })

        routes = MiniAppRoutes(app_inst)
        web_app = web.Application()
        routes.register(web_app)

        admin_data = _build_init_data("t", 1)
        uid = session_runtime_uid(session)

        async with TestClient(TestServer(web_app)) as client:
            resp = await client.get(
                f"/api/session/{uid}/settings",
                headers={"X-Telegram-Init-Data": admin_data},
            )
            assert resp.status == 200
            body = await resp.json()
            assert body["ok"] is True

            settings = body["settings"]
            assert settings["remote_control_enabled"] is True
            assert settings["remote_control_host_alias"] == "prod"

            hosts = body["available"]["remote_control_hosts"]
            assert "prod" in hosts
            assert hosts["prod"]["host"] == "1.1.1.1"
            assert hosts["prod"]["remote_project_root"] == "/srv/app"

            effective = body["effective"]
            assert effective["execution_target"] == "remote"
            assert effective["host_alias"] == "prod"
            assert effective["remote_project_root"] == "/srv/app"
            assert effective["git_available"] is True

        app_inst.shutdown_html_process_pool()

    asyncio.run(_run())


def test_get_settings_default_rc_fields(tmp_path) -> None:
    """GET settings returns defaults when remote control is not configured."""
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
            resp = await client.get(
                f"/api/session/{uid}/settings",
                headers={"X-Telegram-Init-Data": admin_data},
            )
            assert resp.status == 200
            body = await resp.json()

            assert body["settings"]["remote_control_enabled"] is False
            assert body["settings"]["remote_control_host_alias"] is None
            assert body["available"]["remote_control_hosts"] == {}
            assert body["effective"]["execution_target"] == "local"
            assert body["effective"]["host_alias"] is None

        app_inst.shutdown_html_process_pool()

    asyncio.run(_run())


def test_get_settings_acl_filters_hosts(tmp_path) -> None:
    """Non-admin user sees only hosts allowed by ACL."""
    async def _run() -> None:
        cfg = _build_config(tmp_path)
        app_inst = BotApp(cfg)
        session = app_inst.manager.create(2, "dummy", str(tmp_path))
        session.modes.ssh_remote_enabled = True

        # Create hosts: "allowed" permits chat_id=2, "restricted" only chat_id=999
        save_ssh_config(str(tmp_path), {
            "allowed": SSHHostConfig(
                host="1.1.1.1", user="u",
                allowed_chat_ids=[2],
                remote_project_root="/a",
            ),
            "restricted": SSHHostConfig(
                host="2.2.2.2", user="u",
                allowed_chat_ids=[999],
            ),
            "open": SSHHostConfig(
                host="3.3.3.3", user="u",
            ),
        })

        routes = MiniAppRoutes(app_inst)
        web_app = web.Application()
        routes.register(web_app)

        user_data = _build_init_data("t", 2)
        uid = session_runtime_uid(session)

        async with TestClient(TestServer(web_app)) as client:
            resp = await client.get(
                f"/api/session/{uid}/settings",
                headers={"X-Telegram-Init-Data": user_data},
            )
            assert resp.status == 200
            body = await resp.json()

            hosts = body["available"]["remote_control_hosts"]
            assert "allowed" in hosts
            assert "open" in hosts
            assert "restricted" not in hosts

        app_inst.shutdown_html_process_pool()

    asyncio.run(_run())


def test_get_settings_admin_sees_all_hosts(tmp_path) -> None:
    """Admin user sees all hosts regardless of ACL."""
    async def _run() -> None:
        cfg = _build_config(tmp_path)
        app_inst = BotApp(cfg)
        session = app_inst.manager.create(1, "dummy", str(tmp_path))
        session.modes.ssh_remote_enabled = True

        save_ssh_config(str(tmp_path), {
            "restricted": SSHHostConfig(
                host="2.2.2.2", user="u",
                allowed_chat_ids=[999],
            ),
        })

        routes = MiniAppRoutes(app_inst)
        web_app = web.Application()
        routes.register(web_app)

        admin_data = _build_init_data("t", 1)
        uid = session_runtime_uid(session)

        async with TestClient(TestServer(web_app)) as client:
            resp = await client.get(
                f"/api/session/{uid}/settings",
                headers={"X-Telegram-Init-Data": admin_data},
            )
            assert resp.status == 200
            body = await resp.json()
            assert "restricted" in body["available"]["remote_control_hosts"]

        app_inst.shutdown_html_process_pool()

    asyncio.run(_run())
