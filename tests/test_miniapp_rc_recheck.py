"""Integration tests for POST /api/session/{uid}/remote-control/recheck."""

import asyncio
import hashlib
import hmac
import json
import time
from types import SimpleNamespace
from unittest.mock import patch
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


class _FakeSSHService:
    def __init__(self, ok=True, error=None):
        self._error = error
        self.calls = []

    async def exec(self, workdir, host_alias, command, *, timeout_sec=30, chat_id=None):
        self.calls.append((workdir, host_alias, command))
        if self._error:
            raise ConnectionError(self._error)
        return SimpleNamespace(stdout="ok\n", stderr="", exit_code=0)


def test_recheck_returns_preflight_ok(tmp_path) -> None:
    async def _run() -> None:
        cfg = _build_config(tmp_path)
        app_inst = BotApp(cfg)
        app_inst.ssh_service = _FakeSSHService()
        session = app_inst.manager.create(1, "dummy", str(tmp_path))
        session.modes.ssh_remote_enabled = True
        session.modes.remote_control_host_alias = "web"

        save_ssh_config(str(tmp_path), {
            "web": SSHHostConfig(host="1.1.1.1", user="u", remote_project_root="/srv"),
        })

        routes = MiniAppRoutes(app_inst)
        web_app = web.Application()
        routes.register(web_app)

        admin_data = _build_init_data("t", 1)
        uid = session_runtime_uid(session)

        async with TestClient(TestServer(web_app)) as client:
            resp = await client.post(
                f"/api/session/{uid}/remote-control/recheck",
                headers={"X-Telegram-Init-Data": admin_data},
            )
            assert resp.status == 200
            body = await resp.json()
            assert body["ok"] is True
            pf = body["preflight"]
            assert pf["ok"] is True
            assert pf["host_alias"] == "web"
            assert pf["remote_project_root"] == "/srv"
            assert pf["checked_at"] is not None
            assert pf["error"] is None

        app_inst.shutdown_html_process_pool()

    asyncio.run(_run())


def test_recheck_returns_preflight_failure(tmp_path) -> None:
    async def _run() -> None:
        cfg = _build_config(tmp_path)
        app_inst = BotApp(cfg)
        app_inst.ssh_service = _FakeSSHService(error="refused")
        session = app_inst.manager.create(1, "dummy", str(tmp_path))
        session.modes.ssh_remote_enabled = True
        session.modes.remote_control_host_alias = "bad"

        save_ssh_config(str(tmp_path), {
            "bad": SSHHostConfig(
                host="1.1.1.1",
                user="u",
                remote_project_root="/srv/bad",
            ),
        })

        routes = MiniAppRoutes(app_inst)
        web_app = web.Application()
        routes.register(web_app)

        admin_data = _build_init_data("t", 1)
        uid = session_runtime_uid(session)

        async with TestClient(TestServer(web_app)) as client:
            with patch("miniapp.routes.logger.info") as info_mock:
                resp = await client.post(
                    f"/api/session/{uid}/remote-control/recheck",
                    headers={"X-Telegram-Init-Data": admin_data},
                )
            assert resp.status == 200
            body = await resp.json()
            assert body["ok"] is True
            assert body["preflight"]["ok"] is False
            assert "refused" in body["preflight"]["error"]
            calls = [
                call for call in info_mock.call_args_list
                if call.args and call.args[0] == "remote_control_preflight_failed"
            ]
            assert calls
            extra = calls[-1].kwargs["extra"]
            assert extra["actor"] == "telegram:1"
            assert extra["session_uid"] == uid
            assert extra["surface"] == "miniapp"
            assert extra["provider"] == "local"
            assert extra["host"] == "1.1.1.1"
            assert extra["result"] == "error"
            assert "refused" in extra["reason"]

        app_inst.shutdown_html_process_pool()

    asyncio.run(_run())


def test_recheck_no_alias_returns_400(tmp_path) -> None:
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
            resp = await client.post(
                f"/api/session/{uid}/remote-control/recheck",
                headers={"X-Telegram-Init-Data": admin_data},
            )
            assert resp.status == 400

        app_inst.shutdown_html_process_pool()

    asyncio.run(_run())


def test_recheck_invalidates_cache_first(tmp_path) -> None:
    """Recheck invalidates old cache before running preflight."""
    async def _run() -> None:
        cfg = _build_config(tmp_path)
        app_inst = BotApp(cfg)
        fake_ssh = _FakeSSHService()
        app_inst.ssh_service = fake_ssh
        session = app_inst.manager.create(1, "dummy", str(tmp_path))
        session.modes.ssh_remote_enabled = True
        session.modes.remote_control_host_alias = "srv"

        save_ssh_config(str(tmp_path), {
            "srv": SSHHostConfig(
                host="1.1.1.1",
                user="u",
                remote_project_root="/srv/check",
            ),
        })

        routes = MiniAppRoutes(app_inst)
        web_app = web.Application()
        routes.register(web_app)

        admin_data = _build_init_data("t", 1)
        uid = session_runtime_uid(session)

        async with TestClient(TestServer(web_app)) as client:
            # First recheck
            resp = await client.post(
                f"/api/session/{uid}/remote-control/recheck",
                headers={"X-Telegram-Init-Data": admin_data},
            )
            assert resp.status == 200
            first_calls = len(fake_ssh.calls)
            assert first_calls >= 1

            # Second recheck should also call SSH (cache was invalidated)
            resp = await client.post(
                f"/api/session/{uid}/remote-control/recheck",
                headers={"X-Telegram-Init-Data": admin_data},
            )
            assert resp.status == 200
            assert len(fake_ssh.calls) > first_calls

        app_inst.shutdown_html_process_pool()

    asyncio.run(_run())
