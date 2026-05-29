"""Integration tests for PUT /api/session/{uid}/settings with remote control fields."""

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
from session import SessionManager, session_runtime_uid


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


class _FakeSSHService:
    """Fake SSH service for testing preflight without real connections."""

    def __init__(self, ok=True, error=None):
        self._ok = ok
        self._error = error
        self.calls = []

    async def exec(self, workdir, host_alias, command, *, timeout_sec=30, chat_id=None):
        self.calls.append((workdir, host_alias, command))
        if self._error:
            raise ConnectionError(self._error)
        return SimpleNamespace(stdout="ok\n", stderr="", exit_code=0)


def test_put_rc_enabled_and_alias(tmp_path) -> None:
    """PUT sets remote_control_enabled and remote_control_host_alias."""
    async def _run() -> None:
        cfg = _build_config(tmp_path)
        app_inst = BotApp(cfg)
        app_inst.ssh_service = _FakeSSHService()
        session = app_inst.manager.create(1, "dummy", str(tmp_path))
        session.modes.ssh_remote_enabled = True

        save_ssh_config(str(tmp_path), {
            "prod": SSHHostConfig(host="1.1.1.1", user="u", remote_project_root="/srv"),
        })

        routes = MiniAppRoutes(app_inst)
        web_app = web.Application()
        routes.register(web_app)

        admin_data = _build_init_data("t", 1)
        uid = session_runtime_uid(session)

        async with TestClient(TestServer(web_app)) as client:
            with patch("miniapp.routes.logger.info") as info_mock:
                resp = await client.put(
                    f"/api/session/{uid}/settings",
                    json={
                        "remote_control_host_alias": "prod",
                        "remote_control_enabled": True,
                    },
                    headers={"X-Telegram-Init-Data": admin_data},
                )
            assert resp.status == 200
            body = await resp.json()
            assert body["ok"] is True
            assert "remote_control_enabled" in body["changed"]
            assert "remote_control_host_alias" in body["changed"]
            enabled_calls = [
                call for call in info_mock.call_args_list
                if call.args and call.args[0] == "remote_control_enabled"
            ]
            assert enabled_calls
            enabled_extra = enabled_calls[-1].kwargs["extra"]
            assert enabled_extra["actor"] == "telegram:1"
            assert enabled_extra["session_uid"] == uid
            assert enabled_extra["surface"] == "miniapp"
            assert enabled_extra["provider"] == "local"
            assert enabled_extra["host"] == "1.1.1.1"
            assert enabled_extra["remote_project_root"] == "/srv"
            assert enabled_extra["result"] == "ok"
            host_changed_calls = [
                call for call in info_mock.call_args_list
                if call.args and call.args[0] == "remote_control_host_changed"
            ]
            assert host_changed_calls
            host_changed_extra = host_changed_calls[-1].kwargs["extra"]
            assert host_changed_extra["actor"] == "telegram:1"
            assert host_changed_extra["session_uid"] == uid
            assert host_changed_extra["surface"] == "miniapp"
            assert host_changed_extra["host"] == "1.1.1.1"
            assert host_changed_extra["remote_project_root"] == "/srv"
            assert host_changed_extra["result"] == "ok"

        assert session.modes.remote_control_enabled is True
        assert session.modes.remote_control_host_alias == "prod"

        app_inst.shutdown_html_process_pool()

    asyncio.run(_run())


def test_put_rc_validation_rejects_busy(tmp_path) -> None:
    """PUT returns 409 when session is busy."""
    async def _run() -> None:
        cfg = _build_config(tmp_path)
        app_inst = BotApp(cfg)
        session = app_inst.manager.create(1, "dummy", str(tmp_path))
        session.modes.ssh_remote_enabled = True
        session.modes.remote_control_host_alias = "prod"
        session.busy = True

        save_ssh_config(str(tmp_path), {
            "prod": SSHHostConfig(host="1.1.1.1", user="u"),
        })

        routes = MiniAppRoutes(app_inst)
        web_app = web.Application()
        routes.register(web_app)

        admin_data = _build_init_data("t", 1)
        uid = session_runtime_uid(session)

        async with TestClient(TestServer(web_app)) as client:
            resp = await client.put(
                f"/api/session/{uid}/settings",
                json={"remote_control_enabled": True},
                headers={"X-Telegram-Init-Data": admin_data},
            )
            assert resp.status == 409

        app_inst.shutdown_html_process_pool()

    asyncio.run(_run())


def test_put_rc_acl_rejects_non_admin(tmp_path) -> None:
    """PUT returns 409 when user is not in allowed_chat_ids."""
    async def _run() -> None:
        cfg = _build_config(tmp_path)
        app_inst = BotApp(cfg)
        session = app_inst.manager.create(2, "dummy", str(tmp_path))
        session.modes.ssh_remote_enabled = True
        session.modes.remote_control_host_alias = "restricted"

        save_ssh_config(str(tmp_path), {
            "restricted": SSHHostConfig(host="1.1.1.1", user="u", allowed_chat_ids=[999]),
        })

        routes = MiniAppRoutes(app_inst)
        web_app = web.Application()
        routes.register(web_app)

        user_data = _build_init_data("t", 2)
        uid = session_runtime_uid(session)

        async with TestClient(TestServer(web_app)) as client:
            resp = await client.put(
                f"/api/session/{uid}/settings",
                json={"remote_control_enabled": True},
                headers={"X-Telegram-Init-Data": user_data},
            )
            assert resp.status == 409

        assert session.modes.remote_control_enabled is False

        app_inst.shutdown_html_process_pool()

    asyncio.run(_run())


def test_put_rc_disable_no_validation(tmp_path) -> None:
    """Disabling remote_control doesn't require validation or preflight."""
    async def _run() -> None:
        cfg = _build_config(tmp_path)
        app_inst = BotApp(cfg)
        session = app_inst.manager.create(1, "dummy", str(tmp_path))
        session.modes.ssh_remote_enabled = True
        session.modes.remote_control_enabled = True
        session.modes.remote_control_host_alias = "prod"

        save_ssh_config(str(tmp_path), {
            "prod": SSHHostConfig(host="1.1.1.1", user="u", remote_project_root="/srv"),
        })

        routes = MiniAppRoutes(app_inst)
        web_app = web.Application()
        routes.register(web_app)

        admin_data = _build_init_data("t", 1)
        uid = session_runtime_uid(session)

        async with TestClient(TestServer(web_app)) as client:
            with patch("miniapp.routes.logger.info") as info_mock:
                resp = await client.put(
                    f"/api/session/{uid}/settings",
                    json={"remote_control_enabled": False},
                    headers={"X-Telegram-Init-Data": admin_data},
                )
            assert resp.status == 200
            body = await resp.json()
            assert body["ok"] is True
            calls = [
                call for call in info_mock.call_args_list
                if call.args and call.args[0] == "remote_control_disabled"
            ]
            assert calls
            extra = calls[-1].kwargs["extra"]
            assert extra["actor"] == "telegram:1"
            assert extra["session_uid"] == uid
            assert extra["surface"] == "miniapp"
            assert extra["provider"] == "local"
            assert extra["host"] == "1.1.1.1"
            assert extra["remote_project_root"] == "/srv"
            assert extra["result"] == "ok"

        assert session.modes.remote_control_enabled is False
        assert session.modes.remote_control_host_alias == "prod"  # preserved

        app_inst.shutdown_html_process_pool()

    asyncio.run(_run())


def test_put_rc_preflight_runs_on_enable(tmp_path) -> None:
    """PUT runs preflight when enabling remote control."""
    async def _run() -> None:
        cfg = _build_config(tmp_path)
        app_inst = BotApp(cfg)
        fake_ssh = _FakeSSHService()
        app_inst.ssh_service = fake_ssh
        session = app_inst.manager.create(1, "dummy", str(tmp_path))
        session.modes.ssh_remote_enabled = True
        session.modes.remote_control_host_alias = "srv"

        save_ssh_config(str(tmp_path), {
            "srv": SSHHostConfig(host="1.1.1.1", user="u", remote_project_root="/data"),
        })

        routes = MiniAppRoutes(app_inst)
        web_app = web.Application()
        routes.register(web_app)

        admin_data = _build_init_data("t", 1)
        uid = session_runtime_uid(session)

        async with TestClient(TestServer(web_app)) as client:
            resp = await client.put(
                f"/api/session/{uid}/settings",
                json={"remote_control_enabled": True},
                headers={"X-Telegram-Init-Data": admin_data},
            )
            assert resp.status == 200
            body = await resp.json()
            assert body["ok"] is True

        # Verify preflight was called
        assert len(fake_ssh.calls) >= 1
        assert any("echo ok" in c[2] for c in fake_ssh.calls)

        app_inst.shutdown_html_process_pool()

    asyncio.run(_run())


def test_put_rc_non_git_target_enables_remote_control_without_git_requirement(tmp_path) -> None:
    """PUT enable succeeds for non-git remote_project_root and preflight does not probe git."""
    async def _run() -> None:
        cfg = _build_config(tmp_path)
        app_inst = BotApp(cfg)
        fake_ssh = _FakeSSHService()
        app_inst.ssh_service = fake_ssh
        session = app_inst.manager.create(1, "dummy", str(tmp_path))
        session.modes.ssh_remote_enabled = True

        save_ssh_config(str(tmp_path), {
            "plain": SSHHostConfig(host="1.1.1.1", user="u", remote_project_root="/srv/plain"),
        })

        routes = MiniAppRoutes(app_inst)
        web_app = web.Application()
        routes.register(web_app)

        admin_data = _build_init_data("t", 1)
        uid = session_runtime_uid(session)

        async with TestClient(TestServer(web_app)) as client:
            resp = await client.put(
                f"/api/session/{uid}/settings",
                json={
                    "remote_control_host_alias": "plain",
                    "remote_control_enabled": True,
                },
                headers={"X-Telegram-Init-Data": admin_data},
            )
            assert resp.status == 200
            body = await resp.json()
            assert body["ok"] is True
            if "preflight" in body:
                assert body["preflight"]["ok"] is True

            resp = await client.get(
                f"/api/session/{uid}/settings",
                headers={"X-Telegram-Init-Data": admin_data},
            )
            assert resp.status == 200
            body = await resp.json()
            assert body["settings"]["remote_control_enabled"] is True
            assert body["settings"]["remote_control_host_alias"] == "plain"
            assert body["effective"]["execution_target"] == "remote"
            assert body["effective"]["host_alias"] == "plain"
            assert body["effective"]["remote_project_root"] == "/srv/plain"

        assert len(fake_ssh.calls) >= 1
        assert any("echo ok" in c[2] for c in fake_ssh.calls)
        assert not any("git " in c[2] for c in fake_ssh.calls)

        app_inst.shutdown_html_process_pool()

    asyncio.run(_run())


def test_put_rc_preflight_failure_reported(tmp_path) -> None:
    """PUT reports preflight failure in response."""
    async def _run() -> None:
        cfg = _build_config(tmp_path)
        app_inst = BotApp(cfg)
        app_inst.ssh_service = _FakeSSHService(ok=False, error="connection refused")
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
                resp = await client.put(
                    f"/api/session/{uid}/settings",
                    json={"remote_control_enabled": True},
                    headers={"X-Telegram-Init-Data": admin_data},
                )
            assert resp.status == 409
            body = await resp.json()
            assert body["ok"] is False
            assert body["preflight"]["ok"] is False
            assert "connection refused" in body["preflight"]["error"]
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
            assert "connection refused" in extra["reason"]

        app_inst.shutdown_html_process_pool()

    asyncio.run(_run())


def test_put_rc_admin_override_logs_audit_event(tmp_path) -> None:
    """Admin changing another user's session writes admin_remote_override audit."""
    async def _run() -> None:
        cfg = _build_config(tmp_path)
        app_inst = BotApp(cfg)
        app_inst.ssh_service = _FakeSSHService()
        session = app_inst.manager.create(2, "dummy", str(tmp_path))
        session.modes.ssh_remote_enabled = True

        save_ssh_config(str(tmp_path), {
            "alpha": SSHHostConfig(
                host="10.0.0.1",
                user="deploy",
                remote_project_root="/srv/alpha",
            ),
        })

        routes = MiniAppRoutes(app_inst)
        web_app = web.Application()
        routes.register(web_app)

        admin_data = _build_init_data("t", 1)
        uid = session_runtime_uid(session)

        async with TestClient(TestServer(web_app)) as client:
            with patch("miniapp.routes.logger.info") as info_mock:
                resp = await client.put(
                    f"/api/session/{uid}/settings",
                    json={
                        "remote_control_host_alias": "alpha",
                        "remote_control_enabled": True,
                    },
                    headers={"X-Telegram-Init-Data": admin_data},
                )
            assert resp.status == 200
            body = await resp.json()
            assert body["ok"] is True
            calls = [
                call for call in info_mock.call_args_list
                if call.args and call.args[0] == "admin_remote_override"
            ]
            assert calls
            extra = calls[-1].kwargs["extra"]
            assert extra["actor"] == "telegram:1"
            assert extra["session_uid"] == uid
            assert extra["surface"] == "miniapp"
            assert extra["provider"] == "local"
            assert extra["host"] == "10.0.0.1"
            assert extra["remote_project_root"] == "/srv/alpha"
            assert extra["result"] == "ok"
            assert "admin override" in extra["reason"]

        app_inst.shutdown_html_process_pool()

    asyncio.run(_run())


def test_put_rc_verify_effective_via_get(tmp_path) -> None:
    """PUT enable then GET confirms effective state changed to remote."""
    async def _run() -> None:
        cfg = _build_config(tmp_path)
        app_inst = BotApp(cfg)
        app_inst.ssh_service = _FakeSSHService()
        session = app_inst.manager.create(1, "dummy", str(tmp_path))
        session.modes.ssh_remote_enabled = True

        save_ssh_config(str(tmp_path), {
            "web": SSHHostConfig(host="1.1.1.1", user="u", remote_project_root="/var/www"),
        })

        routes = MiniAppRoutes(app_inst)
        web_app = web.Application()
        routes.register(web_app)

        admin_data = _build_init_data("t", 1)
        uid = session_runtime_uid(session)

        async with TestClient(TestServer(web_app)) as client:
            resp = await client.put(
                f"/api/session/{uid}/settings",
                json={
                    "remote_control_host_alias": "web",
                    "remote_control_enabled": True,
                },
                headers={"X-Telegram-Init-Data": admin_data},
            )
            assert resp.status == 200

            resp = await client.get(
                f"/api/session/{uid}/settings",
                headers={"X-Telegram-Init-Data": admin_data},
            )
            assert resp.status == 200
            body = await resp.json()
            assert body["settings"]["remote_control_enabled"] is True
            assert body["effective"]["execution_target"] == "remote"
            assert body["effective"]["host_alias"] == "web"

        app_inst.shutdown_html_process_pool()

    asyncio.run(_run())


def test_put_rc_allowed_user_can_select_host_and_save_settings(tmp_path) -> None:
    """Non-admin user can select an allowed host, save settings, and GET sees filtered hosts only."""
    async def _run() -> None:
        cfg = _build_config(tmp_path)
        app_inst = BotApp(cfg)
        app_inst.ssh_service = _FakeSSHService()
        session = app_inst.manager.create(2, "dummy", str(tmp_path))
        session.modes.ssh_remote_enabled = True

        save_ssh_config(str(tmp_path), {
            "alpha": SSHHostConfig(
                host="10.0.0.1",
                user="deploy",
                allowed_chat_ids=[2],
                remote_project_root="/srv/alpha",
            ),
            "restricted": SSHHostConfig(
                host="10.0.0.2",
                user="deploy",
                allowed_chat_ids=[999],
                remote_project_root="/srv/restricted",
            ),
        })

        routes = MiniAppRoutes(app_inst)
        web_app = web.Application()
        routes.register(web_app)

        user_data = _build_init_data("t", 2)
        uid = session_runtime_uid(session)

        async with TestClient(TestServer(web_app)) as client:
            resp = await client.put(
                f"/api/session/{uid}/settings",
                json={
                    "remote_control_host_alias": "alpha",
                    "remote_control_enabled": True,
                },
                headers={"X-Telegram-Init-Data": user_data},
            )
            assert resp.status == 200
            body = await resp.json()
            assert body["ok"] is True
            assert set(body["changed"]) == {"remote_control_host_alias", "remote_control_enabled"}

            resp = await client.get(
                f"/api/session/{uid}/settings",
                headers={"X-Telegram-Init-Data": user_data},
            )
            assert resp.status == 200
            body = await resp.json()
            assert body["settings"]["remote_control_enabled"] is True
            assert body["settings"]["remote_control_host_alias"] == "alpha"
            assert body["effective"]["execution_target"] == "remote"
            assert body["effective"]["host_alias"] == "alpha"
            assert body["effective"]["remote_project_root"] == "/srv/alpha"
            assert "alpha" in body["remote_control_hosts"]
            assert "restricted" not in body["remote_control_hosts"]

        app_inst.shutdown_html_process_pool()

    asyncio.run(_run())


def test_put_rc_host_alias_persists_when_disabling_rc_and_ssh_remote(tmp_path) -> None:
    """Selected host alias survives RC disable, SSH disable, and session restore."""
    async def _run() -> None:
        cfg = _build_config(tmp_path)
        app_inst = BotApp(cfg)
        app_inst.ssh_service = _FakeSSHService()
        session = app_inst.manager.create(1, "dummy", str(tmp_path))

        save_ssh_config(str(tmp_path), {
            "prod": SSHHostConfig(
                host="1.1.1.1",
                user="deploy",
                remote_project_root="/srv/app",
            ),
        })

        routes = MiniAppRoutes(app_inst)
        web_app = web.Application()
        routes.register(web_app)

        admin_data = _build_init_data("t", 1)
        uid = session_runtime_uid(session)

        async with TestClient(TestServer(web_app)) as client:
            resp = await client.put(
                f"/api/session/{uid}/settings",
                json={
                    "ssh_remote_enabled": True,
                    "remote_control_host_alias": "prod",
                    "remote_control_enabled": True,
                },
                headers={"X-Telegram-Init-Data": admin_data},
            )
            assert resp.status == 200
            assert (await resp.json())["ok"] is True

            resp = await client.get(
                f"/api/session/{uid}/settings",
                headers={"X-Telegram-Init-Data": admin_data},
            )
            assert resp.status == 200
            body = await resp.json()
            assert body["settings"]["ssh_remote_enabled"] is True
            assert body["settings"]["remote_control_enabled"] is True
            assert body["settings"]["remote_control_host_alias"] == "prod"

            resp = await client.put(
                f"/api/session/{uid}/settings",
                json={"remote_control_enabled": False},
                headers={"X-Telegram-Init-Data": admin_data},
            )
            assert resp.status == 200
            assert (await resp.json())["ok"] is True

            resp = await client.get(
                f"/api/session/{uid}/settings",
                headers={"X-Telegram-Init-Data": admin_data},
            )
            assert resp.status == 200
            body = await resp.json()
            assert body["settings"]["ssh_remote_enabled"] is True
            assert body["settings"]["remote_control_enabled"] is False
            assert body["settings"]["remote_control_host_alias"] == "prod"

            resp = await client.put(
                f"/api/session/{uid}/settings",
                json={"ssh_remote_enabled": False},
                headers={"X-Telegram-Init-Data": admin_data},
            )
            assert resp.status == 200
            assert (await resp.json())["ok"] is True

            resp = await client.get(
                f"/api/session/{uid}/settings",
                headers={"X-Telegram-Init-Data": admin_data},
            )
            assert resp.status == 200
            body = await resp.json()
            assert body["settings"]["ssh_remote_enabled"] is False
            assert body["settings"]["remote_control_enabled"] is False
            assert body["settings"]["remote_control_host_alias"] == "prod"

        restored_manager = SessionManager(cfg)
        restored = list(restored_manager.sessions_for_chat(1).values())[0]
        assert restored.modes.ssh_remote_enabled is False
        assert restored.modes.remote_control_enabled is False
        assert restored.modes.remote_control_host_alias == "prod"

        app_inst.shutdown_html_process_pool()

    asyncio.run(_run())
