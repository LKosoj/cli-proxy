"""Tests for MiniApp SSH host CRUD and test-connection endpoints."""

import asyncio
import hashlib
import hmac
import json
import time
from urllib.parse import quote

import yaml
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from app.services.ssh_config_loader import load_ssh_config, load_ssh_secrets
from bot import BotApp
from config import (
    AppConfig, DefaultsConfig, MCPConfig, MiniAppConfig, TelegramConfig, ToolConfig,
)
from miniapp.routes import MiniAppRoutes
from miniapp.services.config_service import app_config_to_dict


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


def test_ssh_hosts_crud_roundtrip(tmp_path) -> None:
    async def _run() -> None:
        cfg = _build_config(tmp_path)
        app_inst = BotApp(cfg)
        routes = MiniAppRoutes(app_inst)
        web_app = web.Application()
        routes.register(web_app)

        admin_data = _build_init_data("t", 1)
        workdir = str(tmp_path)
        qs = f"?workdir={workdir}"

        async with TestClient(TestServer(web_app)) as client:
            # List empty
            resp = await client.get(
                f"/api/ssh/hosts{qs}",
                headers={"X-Telegram-Init-Data": admin_data},
            )
            assert resp.status == 200
            body = await resp.json()
            assert body["ok"] is True
            assert body["hosts"] == {}

            # Create host
            resp = await client.post(
                f"/api/ssh/hosts{qs}",
                json={
                    "alias": "prod",
                    "host": "10.0.0.1",
                    "user": "deploy",
                    "auth": "key",
                    "allowed_chat_ids": [2, 3],
                    "roles": ["web"],
                    "description": "Production",
                    "remote_project_root": "/srv/app",
                },
                headers={"X-Telegram-Init-Data": admin_data},
            )
            assert resp.status == 200
            body = await resp.json()
            assert body["ok"] is True
            assert body["alias"] == "prod"

            # List should have one host
            resp = await client.get(
                f"/api/ssh/hosts{qs}",
                headers={"X-Telegram-Init-Data": admin_data},
            )
            body = await resp.json()
            assert "prod" in body["hosts"]
            assert body["hosts"]["prod"]["host"] == "10.0.0.1"
            assert body["hosts"]["prod"]["allowed_chat_ids"] == [2, 3]
            assert body["hosts"]["prod"]["remote_project_root"] == "/srv/app"
            assert body["hosts"]["prod"]["roles"] == ["web"]

            # Duplicate should fail
            resp = await client.post(
                f"/api/ssh/hosts{qs}",
                json={"alias": "prod", "host": "2.2.2.2", "user": "u"},
                headers={"X-Telegram-Init-Data": admin_data},
            )
            assert resp.status == 409

            # Update host
            resp = await client.post(
                f"/api/ssh/hosts/update{qs}",
                json={
                    "alias": "prod",
                    "host": "10.0.0.1",
                    "user": "deploy-updated",
                    "auth": "key",
                    "allowed_chat_ids": [2],
                    "remote_project_root": "/srv/app-v2",
                },
                headers={"X-Telegram-Init-Data": admin_data},
            )
            assert resp.status == 200
            body = await resp.json()
            assert body["ok"] is True

            # Verify update
            resp = await client.get(
                f"/api/ssh/hosts{qs}",
                headers={"X-Telegram-Init-Data": admin_data},
            )
            body = await resp.json()
            assert body["hosts"]["prod"]["user"] == "deploy-updated"
            assert body["hosts"]["prod"]["allowed_chat_ids"] == [2]
            assert body["hosts"]["prod"]["remote_project_root"] == "/srv/app-v2"

            # Update nonexistent should fail
            resp = await client.post(
                f"/api/ssh/hosts/update{qs}",
                json={"alias": "missing", "host": "1.1.1.1", "user": "u"},
                headers={"X-Telegram-Init-Data": admin_data},
            )
            assert resp.status == 404

            # Delete host
            resp = await client.post(
                f"/api/ssh/hosts/delete{qs}",
                json={"alias": "prod"},
                headers={"X-Telegram-Init-Data": admin_data},
            )
            assert resp.status == 200
            body = await resp.json()
            assert body["ok"] is True

            # List should be empty again
            resp = await client.get(
                f"/api/ssh/hosts{qs}",
                headers={"X-Telegram-Init-Data": admin_data},
            )
            body = await resp.json()
            assert body["hosts"] == {}

        app_inst.shutdown_html_process_pool()

    asyncio.run(_run())


def test_ssh_test_connection_no_service(tmp_path) -> None:
    """test-connection gracefully handles missing SSH service."""
    async def _run() -> None:
        cfg = _build_config(tmp_path)
        app_inst = BotApp(cfg)
        # Remove ssh_service to simulate unavailability
        app_inst.ssh_service = None  # type: ignore[assignment]

        routes = MiniAppRoutes(app_inst)
        web_app = web.Application()
        routes.register(web_app)

        admin_data = _build_init_data("t", 1)
        qs = f"?workdir={str(tmp_path)}"

        async with TestClient(TestServer(web_app)) as client:
            resp = await client.post(
                f"/api/ssh/test-connection{qs}",
                json={"alias": "prod"},
                headers={"X-Telegram-Init-Data": admin_data},
            )
            assert resp.status == 503

        app_inst.shutdown_html_process_pool()

    asyncio.run(_run())


def test_ssh_host_create_validates_required_fields(tmp_path) -> None:
    async def _run() -> None:
        cfg = _build_config(tmp_path)
        app_inst = BotApp(cfg)
        routes = MiniAppRoutes(app_inst)
        web_app = web.Application()
        routes.register(web_app)

        admin_data = _build_init_data("t", 1)
        qs = f"?workdir={str(tmp_path)}"

        async with TestClient(TestServer(web_app)) as client:
            # Missing host and user
            resp = await client.post(
                f"/api/ssh/hosts{qs}",
                json={"alias": "x"},
                headers={"X-Telegram-Init-Data": admin_data},
            )
            assert resp.status == 400

            # Missing alias
            resp = await client.post(
                f"/api/ssh/hosts{qs}",
                json={"host": "1.2.3.4", "user": "u"},
                headers={"X-Telegram-Init-Data": admin_data},
            )
            assert resp.status == 400

        app_inst.shutdown_html_process_pool()

    asyncio.run(_run())


def test_ssh_host_create_uses_project_workdir_and_auto_saves_password_secret(tmp_path) -> None:
    async def _run() -> None:
        cfg = _build_config(tmp_path)
        app_inst = BotApp(cfg)
        routes = MiniAppRoutes(app_inst)
        web_app = web.Application()
        routes.register(web_app)

        admin_data = _build_init_data("t", 1)
        project_dir = tmp_path / "project-a"
        project_dir.mkdir()
        qs = f"?workdir={project_dir}"

        async with TestClient(TestServer(web_app)) as client:
            resp = await client.post(
                f"/api/ssh/hosts{qs}",
                json={
                    "alias": "Mb_test",
                    "host": "83.69.203.41",
                    "port": 37121,
                    "user": "la",
                    "auth": "password",
                    "password": "secret-text",
                    "remote_project_root": "/srv/app",
                },
                headers={"X-Telegram-Init-Data": admin_data},
            )
            assert resp.status == 200
            body = await resp.json()
            assert body["ok"] is True

        hosts = load_ssh_config(str(project_dir))
        assert "Mb_test" in hosts
        assert hosts["Mb_test"].auth == "password"
        assert hosts["Mb_test"].password_env == "SSH_MB_TEST_PASSWORD"
        assert hosts["Mb_test"].remote_project_root == "/srv/app"

        secrets = load_ssh_secrets(str(project_dir))
        assert secrets["SSH_MB_TEST_PASSWORD"] == "secret-text"
        assert not (tmp_path / ".cli-proxy" / "ssh.yaml").exists()
        assert (project_dir / ".cli-proxy" / "ssh.yaml").exists()

        app_inst.shutdown_html_process_pool()

    asyncio.run(_run())


def test_ssh_delete_nonexistent(tmp_path) -> None:
    async def _run() -> None:
        cfg = _build_config(tmp_path)
        app_inst = BotApp(cfg)
        routes = MiniAppRoutes(app_inst)
        web_app = web.Application()
        routes.register(web_app)

        admin_data = _build_init_data("t", 1)
        qs = f"?workdir={str(tmp_path)}"

        async with TestClient(TestServer(web_app)) as client:
            resp = await client.post(
                f"/api/ssh/hosts/delete{qs}",
                json={"alias": "nonexistent"},
                headers={"X-Telegram-Init-Data": admin_data},
            )
            assert resp.status == 404

        app_inst.shutdown_html_process_pool()

    asyncio.run(_run())
