"""Verification: MiniApp SSH API JSON response structure matches frontend expectations.

Tests verify the exact JSON schema of:
- PUT /api/session/{uid}/settings → {ok, changed}
- GET /api/ssh/hosts → {ok, hosts: {alias: {host, port, user, ...}}}
- POST /api/ssh/hosts → {ok, alias}
- POST /api/ssh/hosts/delete → {ok, alias}
"""

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


def test_put_session_settings_response_schema(tmp_path) -> None:
    async def _run() -> None:
        cfg = _build_config(tmp_path)
        app_inst = BotApp(cfg)
        session = app_inst.manager.create(1, "dummy", str(tmp_path))
        suid = session_runtime_uid(session)

        ssh_dir = os.path.join(str(tmp_path), ".cli-proxy")
        os.makedirs(ssh_dir, exist_ok=True)
        with open(os.path.join(ssh_dir, "ssh.yaml"), "w") as f:
            f.write("hosts:\n  prod:\n    host: 1.2.3.4\n    user: deploy\n")

        routes = MiniAppRoutes(app_inst)
        web_app = web.Application()
        routes.register(web_app)
        init_data = _build_init_data("t", 1)

        async with TestClient(TestServer(web_app)) as client:
            resp = await client.put(
                f"/api/session/{suid}/settings",
                json={"ssh_remote_enabled": True},
                headers={"X-Telegram-Init-Data": init_data},
            )
            assert resp.status == 200
            body = await resp.json()

            # Schema: {ok: bool, changed: list[str]}
            assert "ok" in body
            assert isinstance(body["ok"], bool)
            assert body["ok"] is True
            assert "changed" in body
            assert isinstance(body["changed"], list)
            assert "ssh_remote_enabled" in body["changed"]

        app_inst.shutdown_html_process_pool()

    asyncio.run(_run())


def test_get_ssh_hosts_response_schema(tmp_path) -> None:
    async def _run() -> None:
        cfg = _build_config(tmp_path)
        app_inst = BotApp(cfg)

        ssh_dir = os.path.join(str(tmp_path), ".cli-proxy")
        os.makedirs(ssh_dir, exist_ok=True)
        with open(os.path.join(ssh_dir, "ssh.yaml"), "w") as f:
            f.write(
                "hosts:\n"
                "  prod:\n"
                "    host: 10.0.0.1\n"
                "    port: 2222\n"
                "    user: deploy\n"
                "    auth: key\n"
                "    sudo: true\n"
                "    roles: [web, app]\n"
                '    description: "Production"\n'
            )

        routes = MiniAppRoutes(app_inst)
        web_app = web.Application()
        routes.register(web_app)
        init_data = _build_init_data("t", 1)
        qs = f"?workdir={str(tmp_path)}"

        async with TestClient(TestServer(web_app)) as client:
            resp = await client.get(
                f"/api/ssh/hosts{qs}",
                headers={"X-Telegram-Init-Data": init_data},
            )
            assert resp.status == 200
            body = await resp.json()

            # Schema: {ok: bool, hosts: {alias: {host, port, user, auth, ...}}}
            assert body["ok"] is True
            assert "hosts" in body
            assert isinstance(body["hosts"], dict)
            assert "prod" in body["hosts"]

            host = body["hosts"]["prod"]
            assert host["host"] == "10.0.0.1"
            assert host["port"] == 2222
            assert host["user"] == "deploy"
            assert host["auth"] == "key"
            assert host["sudo"] is True
            assert host["roles"] == ["web", "app"]
            assert host["description"] == "Production"
            assert "has_password" in host
            assert "has_sudo_password" in host
            assert "has_key" in host
            assert isinstance(host["has_password"], bool)

        app_inst.shutdown_html_process_pool()

    asyncio.run(_run())


def test_post_ssh_host_create_response_schema(tmp_path) -> None:
    async def _run() -> None:
        cfg = _build_config(tmp_path)
        app_inst = BotApp(cfg)

        routes = MiniAppRoutes(app_inst)
        web_app = web.Application()
        routes.register(web_app)
        init_data = _build_init_data("t", 1)
        qs = f"?workdir={str(tmp_path)}"

        async with TestClient(TestServer(web_app)) as client:
            resp = await client.post(
                f"/api/ssh/hosts{qs}",
                json={
                    "alias": "staging",
                    "host": "10.0.0.2",
                    "user": "admin",
                    "auth": "password",
                    "roles": ["db"],
                    "description": "Staging",
                },
                headers={"X-Telegram-Init-Data": init_data},
            )
            assert resp.status == 200
            body = await resp.json()

            # Schema: {ok: bool, alias: str}
            assert body["ok"] is True
            assert "alias" in body
            assert body["alias"] == "staging"

        app_inst.shutdown_html_process_pool()

    asyncio.run(_run())


def test_post_ssh_host_delete_response_schema(tmp_path) -> None:
    async def _run() -> None:
        cfg = _build_config(tmp_path)
        app_inst = BotApp(cfg)

        ssh_dir = os.path.join(str(tmp_path), ".cli-proxy")
        os.makedirs(ssh_dir, exist_ok=True)
        with open(os.path.join(ssh_dir, "ssh.yaml"), "w") as f:
            f.write("hosts:\n  todel:\n    host: 1.2.3.4\n    user: u\n")

        routes = MiniAppRoutes(app_inst)
        web_app = web.Application()
        routes.register(web_app)
        init_data = _build_init_data("t", 1)
        qs = f"?workdir={str(tmp_path)}"

        async with TestClient(TestServer(web_app)) as client:
            resp = await client.post(
                f"/api/ssh/hosts/delete{qs}",
                json={"alias": "todel"},
                headers={"X-Telegram-Init-Data": init_data},
            )
            assert resp.status == 200
            body = await resp.json()

            # Schema: {ok: bool, alias: str}
            assert body["ok"] is True
            assert body["alias"] == "todel"

        app_inst.shutdown_html_process_pool()

    asyncio.run(_run())


def test_get_ssh_hosts_empty_response(tmp_path) -> None:
    async def _run() -> None:
        cfg = _build_config(tmp_path)
        app_inst = BotApp(cfg)

        routes = MiniAppRoutes(app_inst)
        web_app = web.Application()
        routes.register(web_app)
        init_data = _build_init_data("t", 1)
        qs = f"?workdir={str(tmp_path)}"

        async with TestClient(TestServer(web_app)) as client:
            resp = await client.get(
                f"/api/ssh/hosts{qs}",
                headers={"X-Telegram-Init-Data": init_data},
            )
            body = await resp.json()
            assert body == {"ok": True, "hosts": {}}

        app_inst.shutdown_html_process_pool()

    asyncio.run(_run())
