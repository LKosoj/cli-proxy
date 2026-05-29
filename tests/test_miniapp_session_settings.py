"""Tests for MiniApp session settings endpoint (ssh_remote_enabled)."""

import asyncio
import hashlib
import hmac
import json
import os
import time
from urllib.parse import quote

import yaml
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from bot import BotApp
from config import (
    AppConfig, DefaultsConfig, MCPConfig, MiniAppConfig, TelegramConfig, ToolConfig,
)
from miniapp.routes import MiniAppRoutes
from miniapp.services.config_service import app_config_to_dict
from session import session_runtime_uid
from sessions.session_state_access import is_ssh_remote_enabled


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
            whitelist_chat_ids=[2],
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
            app_config_to_dict(cfg), f, sort_keys=False, allow_unicode=False
        )
    return cfg


def test_session_settings_update_ssh_remote_enabled(tmp_path) -> None:
    async def _run() -> None:
        cfg = _build_config(tmp_path)
        app_inst = BotApp(cfg)
        session = app_inst.manager.create(1, "dummy", str(tmp_path))
        suid = session_runtime_uid(session)

        # Write SSH config so ssh_remote_available returns True
        ssh_dir = os.path.join(str(tmp_path), ".cli-proxy")
        os.makedirs(ssh_dir, exist_ok=True)
        with open(os.path.join(ssh_dir, "ssh.yaml"), "w") as f:
            f.write("hosts:\n  prod:\n    host: 1.2.3.4\n    user: deploy\n")

        routes = MiniAppRoutes(app_inst)
        web_app = web.Application()
        routes.register(web_app)

        init_data = _build_init_data("t", 1)
        async with TestClient(TestServer(web_app)) as client:
            # Enable SSH
            resp = await client.put(
                f"/api/session/{suid}/settings",
                json={
                    "ssh_remote_enabled": True,
                },
                headers={"X-Telegram-Init-Data": init_data},
            )
            assert resp.status == 200
            body = await resp.json()
            assert body["ok"] is True
            assert "ssh_remote_enabled" in body["changed"]
            assert is_ssh_remote_enabled(session) is True

            # Disable SSH
            resp = await client.put(
                f"/api/session/{suid}/settings",
                json={
                    "ssh_remote_enabled": False,
                },
                headers={"X-Telegram-Init-Data": init_data},
            )
            assert resp.status == 200
            body = await resp.json()
            assert body["ok"] is True
            assert is_ssh_remote_enabled(session) is False

        app_inst.shutdown_html_process_pool()

    asyncio.run(_run())


def test_session_settings_get(tmp_path) -> None:
    async def _run() -> None:
        cfg = _build_config(tmp_path)
        app_inst = BotApp(cfg)
        session = app_inst.manager.create(1, "dummy", str(tmp_path))
        suid = session_runtime_uid(session)

        # Write SSH config so ssh_available returns True
        ssh_dir = os.path.join(str(tmp_path), ".cli-proxy")
        os.makedirs(ssh_dir, exist_ok=True)
        with open(os.path.join(ssh_dir, "ssh.yaml"), "w") as f:
            f.write("hosts:\n  prod:\n    host: 1.2.3.4\n    user: deploy\n")

        routes = MiniAppRoutes(app_inst)
        web_app = web.Application()
        routes.register(web_app)

        init_data = _build_init_data("t", 1)
        async with TestClient(TestServer(web_app)) as client:
            resp = await client.get(
                f"/api/session/{suid}/settings",
                headers={"X-Telegram-Init-Data": init_data},
            )
            assert resp.status == 200
            body = await resp.json()
            assert body["ok"] is True
            assert body["settings"]["name"] == (session.name or "")
            assert body["settings"]["ssh_remote_enabled"] is False
            assert body["available"]["ssh_config_exists"] is True
            assert body["available"]["ssh_available"] is True
            assert body["available"]["project_workdir"] == str(tmp_path)

        app_inst.shutdown_html_process_pool()

    asyncio.run(_run())


def test_session_settings_get_marks_missing_ssh_yaml_separately(tmp_path) -> None:
    async def _run() -> None:
        cfg = _build_config(tmp_path)
        app_inst = BotApp(cfg)
        session = app_inst.manager.create(1, "dummy", str(tmp_path))
        suid = session_runtime_uid(session)

        # No ssh.yaml exists
        routes = MiniAppRoutes(app_inst)
        web_app = web.Application()
        routes.register(web_app)

        init_data = _build_init_data("t", 1)
        async with TestClient(TestServer(web_app)) as client:
            resp = await client.get(
                f"/api/session/{suid}/settings",
                headers={"X-Telegram-Init-Data": init_data},
            )
            assert resp.status == 200
            body = await resp.json()
            assert body["ok"] is True
            assert body["available"]["ssh_config_exists"] is False
            assert body["available"]["ssh_available"] is False

        app_inst.shutdown_html_process_pool()

    asyncio.run(_run())


def test_session_settings_enable_creates_ssh_template_when_missing(tmp_path) -> None:
    async def _run() -> None:
        cfg = _build_config(tmp_path)
        app_inst = BotApp(cfg)
        session = app_inst.manager.create(1, "dummy", str(tmp_path))
        suid = session_runtime_uid(session)

        routes = MiniAppRoutes(app_inst)
        web_app = web.Application()
        routes.register(web_app)

        init_data = _build_init_data("t", 1)
        async with TestClient(TestServer(web_app)) as client:
            resp = await client.put(
                f"/api/session/{suid}/settings",
                json={
                    "ssh_remote_enabled": True,
                },
                headers={"X-Telegram-Init-Data": init_data},
            )
            assert resp.status == 200
            body = await resp.json()
            assert body["ok"] is True
            assert "ssh_remote_enabled" in body["changed"]
            assert is_ssh_remote_enabled(session) is True
            ssh_yaml = tmp_path / ".cli-proxy" / "ssh.yaml"
            assert ssh_yaml.is_file()
            assert "hosts: {}" in ssh_yaml.read_text(encoding="utf-8")

        app_inst.shutdown_html_process_pool()

    asyncio.run(_run())
