"""Tests for remote_project_root field in SSHHostConfig and related layers."""

import asyncio
import dataclasses
import hashlib
import hmac
import json
import time
from urllib.parse import quote

import yaml
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from app.services.ssh_config_loader import load_ssh_config, save_ssh_config
from bot import BotApp
from config import (
    AppConfig, DefaultsConfig, MCPConfig, MiniAppConfig,
    SSHHostConfig, TelegramConfig, ToolConfig,
)
from miniapp.routes import MiniAppRoutes
from miniapp.services.config_service import app_config_to_dict


# ---------------------------------------------------------------------------
# SSHHostConfig dataclass tests
# ---------------------------------------------------------------------------


def test_remote_project_root_default_none():
    cfg = SSHHostConfig(host="10.0.0.1", user="deploy")
    assert cfg.remote_project_root is None


def test_remote_project_root_set():
    cfg = SSHHostConfig(host="10.0.0.1", user="deploy", remote_project_root="/srv/app")
    assert cfg.remote_project_root == "/srv/app"


def test_remote_project_root_in_asdict():
    cfg = SSHHostConfig(host="h", user="u", remote_project_root="/opt/proj")
    d = dataclasses.asdict(cfg)
    assert d["remote_project_root"] == "/opt/proj"


def test_remote_project_root_none_in_asdict():
    cfg = SSHHostConfig(host="h", user="u")
    d = dataclasses.asdict(cfg)
    assert d["remote_project_root"] is None


# ---------------------------------------------------------------------------
# ssh_config_loader roundtrip tests
# ---------------------------------------------------------------------------


def test_loader_reads_remote_project_root(tmp_path):
    ssh_dir = tmp_path / ".cli-proxy"
    ssh_dir.mkdir()
    (ssh_dir / "ssh.yaml").write_text(yaml.safe_dump({
        "hosts": {
            "prod": {
                "host": "10.0.0.1",
                "user": "deploy",
                "remote_project_root": "/srv/app",
            }
        }
    }))
    hosts = load_ssh_config(str(tmp_path))
    assert hosts["prod"].remote_project_root == "/srv/app"


def test_loader_missing_field_defaults_none(tmp_path):
    ssh_dir = tmp_path / ".cli-proxy"
    ssh_dir.mkdir()
    (ssh_dir / "ssh.yaml").write_text(yaml.safe_dump({
        "hosts": {
            "staging": {"host": "10.0.0.2", "user": "ci"}
        }
    }))
    hosts = load_ssh_config(str(tmp_path))
    assert hosts["staging"].remote_project_root is None


def test_loader_ignores_relative_path(tmp_path):
    ssh_dir = tmp_path / ".cli-proxy"
    ssh_dir.mkdir()
    (ssh_dir / "ssh.yaml").write_text(yaml.safe_dump({
        "hosts": {
            "bad": {
                "host": "10.0.0.3",
                "user": "u",
                "remote_project_root": "relative/path",
            }
        }
    }))
    hosts = load_ssh_config(str(tmp_path))
    assert hosts["bad"].remote_project_root is None


def test_loader_empty_string_becomes_none(tmp_path):
    ssh_dir = tmp_path / ".cli-proxy"
    ssh_dir.mkdir()
    (ssh_dir / "ssh.yaml").write_text(yaml.safe_dump({
        "hosts": {
            "empty": {"host": "10.0.0.4", "user": "u", "remote_project_root": ""}
        }
    }))
    hosts = load_ssh_config(str(tmp_path))
    assert hosts["empty"].remote_project_root is None


def test_save_load_roundtrip_with_remote_project_root(tmp_path):
    hosts = {
        "web": SSHHostConfig(host="1.2.3.4", user="app", remote_project_root="/var/www"),
        "db": SSHHostConfig(host="5.6.7.8", user="dba"),
    }
    save_ssh_config(str(tmp_path), hosts)
    loaded = load_ssh_config(str(tmp_path))
    assert loaded["web"].remote_project_root == "/var/www"
    assert loaded["db"].remote_project_root is None


# ---------------------------------------------------------------------------
# MiniApp CRUD tests
# ---------------------------------------------------------------------------


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
            app_config_to_dict(cfg), f, sort_keys=False, allow_unicode=False,
        )
    return cfg


def test_miniapp_create_with_remote_project_root(tmp_path):
    async def _run():
        cfg = _build_config(tmp_path)
        app_inst = BotApp(cfg)
        routes = MiniAppRoutes(app_inst)
        web_app = web.Application()
        routes.register(web_app)

        admin_data = _build_init_data("t", 1)
        qs = f"?workdir={tmp_path}"

        async with TestClient(TestServer(web_app)) as client:
            # Create with remote_project_root
            resp = await client.post(
                f"/api/ssh/hosts{qs}",
                json={
                    "alias": "web",
                    "host": "10.0.0.1",
                    "user": "deploy",
                    "remote_project_root": "/srv/app",
                },
                headers={"X-Telegram-Init-Data": admin_data},
            )
            assert resp.status == 200

            # List shows remote_project_root
            resp = await client.get(
                f"/api/ssh/hosts{qs}",
                headers={"X-Telegram-Init-Data": admin_data},
            )
            body = await resp.json()
            assert body["hosts"]["web"]["remote_project_root"] == "/srv/app"

            # Detail shows remote_project_root
            resp = await client.get(
                f"/api/ssh/hosts/web{qs}",
                headers={"X-Telegram-Init-Data": admin_data},
            )
            body = await resp.json()
            assert body["host"]["remote_project_root"] == "/srv/app"

        app_inst.shutdown_html_process_pool()

    asyncio.run(_run())


def test_miniapp_ssh_list_scopes_workdir_and_filters_host_acl(tmp_path):
    async def _run():
        cfg = _build_config(tmp_path)
        app_inst = BotApp(cfg)
        routes = MiniAppRoutes(app_inst)
        web_app = web.Application()
        routes.register(web_app)

        allowed = tmp_path
        forbidden = tmp_path / "other-project"
        forbidden.mkdir()
        save_ssh_config(str(allowed), {
            "allowed": SSHHostConfig(
                host="10.0.0.1",
                user="deploy",
                allowed_chat_ids=[2],
                remote_project_root="/srv/allowed",
            ),
            "restricted": SSHHostConfig(
                host="10.0.0.2",
                user="deploy",
                allowed_chat_ids=[999],
                remote_project_root="/srv/restricted",
            ),
        })
        save_ssh_config(str(forbidden), {
            "leaked": SSHHostConfig(host="10.0.0.3", user="deploy", remote_project_root="/srv/leaked"),
        })

        user_data = _build_init_data("t", 2)

        async with TestClient(TestServer(web_app)) as client:
            resp = await client.get(
                f"/api/ssh/hosts?workdir={forbidden}",
                headers={"X-Telegram-Init-Data": user_data},
            )
            assert resp.status == 403

            resp = await client.get(
                f"/api/ssh/hosts?workdir={allowed}",
                headers={"X-Telegram-Init-Data": user_data},
            )
            assert resp.status == 200
            body = await resp.json()
            assert set(body["hosts"]) == {"allowed"}

            resp = await client.get(
                f"/api/ssh/hosts/restricted?workdir={allowed}",
                headers={"X-Telegram-Init-Data": user_data},
            )
            assert resp.status == 404

        app_inst.shutdown_html_process_pool()

    asyncio.run(_run())


def test_miniapp_create_rejects_relative_path(tmp_path):
    async def _run():
        cfg = _build_config(tmp_path)
        app_inst = BotApp(cfg)
        routes = MiniAppRoutes(app_inst)
        web_app = web.Application()
        routes.register(web_app)

        admin_data = _build_init_data("t", 1)
        qs = f"?workdir={tmp_path}"

        async with TestClient(TestServer(web_app)) as client:
            resp = await client.post(
                f"/api/ssh/hosts{qs}",
                json={
                    "alias": "bad",
                    "host": "10.0.0.1",
                    "user": "u",
                    "remote_project_root": "relative/path",
                },
                headers={"X-Telegram-Init-Data": admin_data},
            )
            assert resp.status == 400

        app_inst.shutdown_html_process_pool()

    asyncio.run(_run())


def test_miniapp_update_with_remote_project_root(tmp_path):
    async def _run():
        cfg = _build_config(tmp_path)
        app_inst = BotApp(cfg)
        routes = MiniAppRoutes(app_inst)
        web_app = web.Application()
        routes.register(web_app)

        admin_data = _build_init_data("t", 1)
        qs = f"?workdir={tmp_path}"

        async with TestClient(TestServer(web_app)) as client:
            # Create without remote_project_root
            resp = await client.post(
                f"/api/ssh/hosts{qs}",
                json={"alias": "srv", "host": "1.1.1.1", "user": "u"},
                headers={"X-Telegram-Init-Data": admin_data},
            )
            assert resp.status == 200

            # Update with remote_project_root
            resp = await client.post(
                f"/api/ssh/hosts/update{qs}",
                json={
                    "alias": "srv",
                    "host": "1.1.1.1",
                    "user": "u",
                    "remote_project_root": "/opt/project",
                },
                headers={"X-Telegram-Init-Data": admin_data},
            )
            assert resp.status == 200

            # Verify
            resp = await client.get(
                f"/api/ssh/hosts{qs}",
                headers={"X-Telegram-Init-Data": admin_data},
            )
            body = await resp.json()
            assert body["hosts"]["srv"]["remote_project_root"] == "/opt/project"

        app_inst.shutdown_html_process_pool()

    asyncio.run(_run())


def test_miniapp_update_rejects_relative_path(tmp_path):
    async def _run():
        cfg = _build_config(tmp_path)
        app_inst = BotApp(cfg)
        routes = MiniAppRoutes(app_inst)
        web_app = web.Application()
        routes.register(web_app)

        admin_data = _build_init_data("t", 1)
        qs = f"?workdir={tmp_path}"

        async with TestClient(TestServer(web_app)) as client:
            # Create host first
            resp = await client.post(
                f"/api/ssh/hosts{qs}",
                json={"alias": "srv", "host": "1.1.1.1", "user": "u"},
                headers={"X-Telegram-Init-Data": admin_data},
            )
            assert resp.status == 200

            # Update with relative path
            resp = await client.post(
                f"/api/ssh/hosts/update{qs}",
                json={
                    "alias": "srv",
                    "host": "1.1.1.1",
                    "user": "u",
                    "remote_project_root": "not/absolute",
                },
                headers={"X-Telegram-Init-Data": admin_data},
            )
            assert resp.status == 400

        app_inst.shutdown_html_process_pool()

    asyncio.run(_run())


def test_miniapp_create_empty_remote_project_root_ok(tmp_path):
    async def _run():
        cfg = _build_config(tmp_path)
        app_inst = BotApp(cfg)
        routes = MiniAppRoutes(app_inst)
        web_app = web.Application()
        routes.register(web_app)

        admin_data = _build_init_data("t", 1)
        qs = f"?workdir={tmp_path}"

        async with TestClient(TestServer(web_app)) as client:
            # Create with empty string
            resp = await client.post(
                f"/api/ssh/hosts{qs}",
                json={
                    "alias": "plain",
                    "host": "1.1.1.1",
                    "user": "u",
                    "remote_project_root": "",
                },
                headers={"X-Telegram-Init-Data": admin_data},
            )
            assert resp.status == 200

            resp = await client.get(
                f"/api/ssh/hosts{qs}",
                headers={"X-Telegram-Init-Data": admin_data},
            )
            body = await resp.json()
            assert body["hosts"]["plain"]["remote_project_root"] is None

        app_inst.shutdown_html_process_pool()

    asyncio.run(_run())
