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
from sessions.session_state_access import (
    get_active_mode,
    is_remote_control_enabled,
    is_ssh_remote_enabled,
    set_active_mode,
)


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


def _build_config(tmp_path, *, user_modes=None, with_openai: bool = True) -> AppConfig:
    defaults_kwargs = {
        "workdir": str(tmp_path),
        "state_path": str(tmp_path / "state.json"),
        "toolhelp_path": str(tmp_path / "toolhelp.json"),
        "log_path": str(tmp_path / "bot.log"),
    }
    if with_openai:
        defaults_kwargs.update(
            {
                "openai_api_key": "test-openai-key",
                "openai_model": "test-model",
            }
        )
    cfg = AppConfig(
        telegram=TelegramConfig(
            token="t",
            whitelist_chat_ids=[2],
            admlist_chat_ids=[1],
            user_workdirs={2: [str(tmp_path)]},
            user_modes=user_modes or {},
        ),
        tools={
            "dummy": ToolConfig(
                name="dummy", mode="headless", cmd=["bash", "-lc", "cat"]
            )
        },
        defaults=DefaultsConfig(**defaults_kwargs),
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


def test_session_settings_get_filters_modes_for_non_admin_user(tmp_path) -> None:
    async def _run() -> None:
        cfg = _build_config(tmp_path, user_modes={2: ["sdd"]})
        app_inst = BotApp(cfg)
        session = app_inst.manager.create(2, "dummy", str(tmp_path))
        suid = session_runtime_uid(session)
        routes = MiniAppRoutes(app_inst)
        web_app = web.Application()
        routes.register(web_app)

        init_data = _build_init_data("t", 2)
        async with TestClient(TestServer(web_app)) as client:
            resp = await client.get(
                f"/api/session/{suid}/settings",
                headers={"X-Telegram-Init-Data": init_data},
            )

            assert resp.status == 200
            body = await resp.json()
            mode_ids = [item["id"] for item in body["available"]["modes"]]
            assert "sdd" in mode_ids
            assert "manager" not in mode_ids

        app_inst.shutdown_html_process_pool()

    asyncio.run(_run())


def test_miniapp_status_payload_filters_modes_for_non_admin_user(tmp_path) -> None:
    cfg = _build_config(tmp_path, user_modes={2: ["sdd"]})
    app_inst = BotApp(cfg)
    app_inst.manager.create(2, "dummy", str(tmp_path))
    routes = MiniAppRoutes(app_inst)

    payload = routes._build_status_payload({"user_id": 2, "is_admin": False})

    mode_ids = [item["id"] for item in payload["modes"]]
    assert "sdd" in mode_ids
    assert "manager" not in mode_ids
    assert payload["direct_cli_allowed"] is False
    app_inst.shutdown_html_process_pool()


def test_session_settings_put_rejects_active_mode_not_allowed_for_user(tmp_path) -> None:
    async def _run() -> None:
        cfg = _build_config(tmp_path, user_modes={2: ["sdd"]})
        app_inst = BotApp(cfg)
        session = app_inst.manager.create(2, "dummy", str(tmp_path))
        suid = session_runtime_uid(session)
        routes = MiniAppRoutes(app_inst)
        web_app = web.Application()
        routes.register(web_app)

        init_data = _build_init_data("t", 2)
        async with TestClient(TestServer(web_app)) as client:
            resp = await client.put(
                f"/api/session/{suid}/settings",
                json={"active_mode": "manager"},
                headers={"X-Telegram-Init-Data": init_data},
            )

            assert resp.status == 403
            body = await resp.json()
            assert body["ok"] is False
            assert get_active_mode(session, "") in ("", None)

        app_inst.shutdown_html_process_pool()

    asyncio.run(_run())


def test_session_settings_put_rejects_direct_cli_when_not_allowed_for_user(tmp_path) -> None:
    async def _run() -> None:
        cfg = _build_config(tmp_path, user_modes={2: ["sdd"]})
        app_inst = BotApp(cfg)
        session = app_inst.manager.create(2, "dummy", str(tmp_path))
        suid = session_runtime_uid(session)
        routes = MiniAppRoutes(app_inst)
        web_app = web.Application()
        routes.register(web_app)

        init_data = _build_init_data("t", 2)
        async with TestClient(TestServer(web_app)) as client:
            resp = await client.put(
                f"/api/session/{suid}/settings",
                json={"active_mode": ""},
                headers={"X-Telegram-Init-Data": init_data},
            )

            assert resp.status == 403
            body = await resp.json()
            assert body["ok"] is False
            assert get_active_mode(session, "") in ("", None)

        app_inst.shutdown_html_process_pool()

    asyncio.run(_run())


def test_session_settings_put_allows_empty_active_mode_noop_in_full_save(tmp_path) -> None:
    async def _run() -> None:
        cfg = _build_config(tmp_path, user_modes={2: ["sdd"]})
        app_inst = BotApp(cfg)
        session = app_inst.manager.create(2, "dummy", str(tmp_path))
        suid = session_runtime_uid(session)
        routes = MiniAppRoutes(app_inst)
        web_app = web.Application()
        routes.register(web_app)

        init_data = _build_init_data("t", 2)
        async with TestClient(TestServer(web_app)) as client:
            resp = await client.put(
                f"/api/session/{suid}/settings",
                json={"active_mode": "", "ssh_remote_enabled": True},
                headers={"X-Telegram-Init-Data": init_data},
            )

            assert resp.status == 200
            body = await resp.json()
            assert body["ok"] is True
            assert "ssh_remote_enabled" in body["changed"]
            assert "active_mode" not in body["changed"]
            assert get_active_mode(session, "") in ("", None)
            assert is_ssh_remote_enabled(session) is True

        app_inst.shutdown_html_process_pool()

    asyncio.run(_run())


def test_session_settings_put_rejects_active_mode_change_when_busy(tmp_path) -> None:
    async def _run() -> None:
        cfg = _build_config(tmp_path, user_modes={2: ["sdd"]})
        app_inst = BotApp(cfg)
        session = app_inst.manager.create(2, "dummy", str(tmp_path))
        session.busy = True
        suid = session_runtime_uid(session)
        routes = MiniAppRoutes(app_inst)
        web_app = web.Application()
        routes.register(web_app)

        init_data = _build_init_data("t", 2)
        async with TestClient(TestServer(web_app)) as client:
            resp = await client.put(
                f"/api/session/{suid}/settings",
                json={"active_mode": "sdd"},
                headers={"X-Telegram-Init-Data": init_data},
            )

            assert resp.status == 409
            body = await resp.json()
            assert body["ok"] is False
            assert get_active_mode(session, "") in ("", None)

        app_inst.shutdown_html_process_pool()

    asyncio.run(_run())


def test_session_settings_put_allows_active_mode_for_user(tmp_path) -> None:
    async def _run() -> None:
        cfg = _build_config(tmp_path, user_modes={2: ["sdd"]})
        app_inst = BotApp(cfg)
        session = app_inst.manager.create(2, "dummy", str(tmp_path))
        suid = session_runtime_uid(session)
        routes = MiniAppRoutes(app_inst)
        web_app = web.Application()
        routes.register(web_app)

        init_data = _build_init_data("t", 2)
        async with TestClient(TestServer(web_app)) as client:
            resp = await client.put(
                f"/api/session/{suid}/settings",
                json={"active_mode": "sdd"},
                headers={"X-Telegram-Init-Data": init_data},
            )

            assert resp.status == 200
            body = await resp.json()
            assert body["ok"] is True
            assert "active_mode" in body["changed"]
            assert get_active_mode(session, "") == "sdd"

        app_inst.shutdown_html_process_pool()

    asyncio.run(_run())


def test_session_settings_put_restores_current_mode_when_next_enable_fails(tmp_path) -> None:
    class _OldMode:
        async def on_disable(self, ctx):
            set_active_mode(ctx["session"], None)
            ctx["session"].cli_work_type = None
            ctx["session"].executor_profile = None
            return None

    class _NewMode:
        async def on_enable(self, _ctx):
            return type("Result", (), {"success": False, "error": "new mode failed"})()

    class _Registry:
        def __init__(self):
            self._modes = {"old": _OldMode(), "new": _NewMode()}

        def list_modes(self):
            return [("old", "Old"), ("new", "New")]

        def get(self, mode_id):
            return self._modes.get(str(mode_id or ""))

    async def _run() -> None:
        cfg = _build_config(tmp_path)
        app_inst = BotApp(cfg)
        app_inst.mode_registry_service = _Registry()
        session = app_inst.manager.create(1, "dummy", str(tmp_path))
        set_active_mode(session, "old")
        session.cli_work_type = "old-cli"
        session.executor_profile = "old-profile"
        suid = session_runtime_uid(session)
        routes = MiniAppRoutes(app_inst)
        web_app = web.Application()
        routes.register(web_app)

        init_data = _build_init_data("t", 1)
        async with TestClient(TestServer(web_app)) as client:
            resp = await client.put(
                f"/api/session/{suid}/settings",
                json={"active_mode": "new"},
                headers={"X-Telegram-Init-Data": init_data},
            )

            assert resp.status == 409
            body = await resp.json()
            assert body["ok"] is False
            assert get_active_mode(session, "") == "old"
            assert session.cli_work_type == "old-cli"
            assert session.executor_profile == "old-profile"

        app_inst.shutdown_html_process_pool()

    asyncio.run(_run())


def test_session_settings_put_restores_current_mode_when_disable_fails(tmp_path) -> None:
    class _OldMode:
        async def on_disable(self, ctx):
            set_active_mode(ctx["session"], None)
            ctx["session"].cli_work_type = None
            ctx["session"].executor_profile = None
            return type("Result", (), {"success": False, "error": "disable failed"})()

    class _NewMode:
        async def on_enable(self, _ctx):
            return None

    class _Registry:
        def __init__(self):
            self._modes = {"old": _OldMode(), "new": _NewMode()}

        def list_modes(self):
            return [("old", "Old"), ("new", "New")]

        def get(self, mode_id):
            return self._modes.get(str(mode_id or ""))

    async def _run() -> None:
        cfg = _build_config(tmp_path)
        app_inst = BotApp(cfg)
        app_inst.mode_registry_service = _Registry()
        session = app_inst.manager.create(1, "dummy", str(tmp_path))
        set_active_mode(session, "old")
        session.cli_work_type = "old-cli"
        session.executor_profile = "old-profile"
        suid = session_runtime_uid(session)
        routes = MiniAppRoutes(app_inst)
        web_app = web.Application()
        routes.register(web_app)

        init_data = _build_init_data("t", 1)
        async with TestClient(TestServer(web_app)) as client:
            resp = await client.put(
                f"/api/session/{suid}/settings",
                json={"active_mode": "new"},
                headers={"X-Telegram-Init-Data": init_data},
            )

            assert resp.status == 409
            body = await resp.json()
            assert body["ok"] is False
            assert get_active_mode(session, "") == "old"
            assert session.cli_work_type == "old-cli"
            assert session.executor_profile == "old-profile"

        app_inst.shutdown_html_process_pool()

    asyncio.run(_run())


def test_session_settings_put_restores_current_mode_when_disable_raises(tmp_path) -> None:
    class _OldMode:
        async def on_disable(self, ctx):
            set_active_mode(ctx["session"], None)
            ctx["session"].cli_work_type = None
            ctx["session"].executor_profile = None
            raise RuntimeError("disable exploded")

    class _NewMode:
        async def on_enable(self, _ctx):
            return None

    class _Registry:
        def __init__(self):
            self._modes = {"old": _OldMode(), "new": _NewMode()}

        def list_modes(self):
            return [("old", "Old"), ("new", "New")]

        def get(self, mode_id):
            return self._modes.get(str(mode_id or ""))

    async def _run() -> None:
        cfg = _build_config(tmp_path)
        app_inst = BotApp(cfg)
        app_inst.mode_registry_service = _Registry()
        session = app_inst.manager.create(1, "dummy", str(tmp_path))
        set_active_mode(session, "old")
        session.cli_work_type = "old-cli"
        session.executor_profile = "old-profile"
        suid = session_runtime_uid(session)
        routes = MiniAppRoutes(app_inst)
        web_app = web.Application()
        routes.register(web_app)

        init_data = _build_init_data("t", 1)
        async with TestClient(TestServer(web_app)) as client:
            resp = await client.put(
                f"/api/session/{suid}/settings",
                json={"active_mode": "new"},
                headers={"X-Telegram-Init-Data": init_data},
            )

            assert resp.status == 500
            body = await resp.json()
            assert body["ok"] is False
            assert get_active_mode(session, "") == "old"
            assert session.cli_work_type == "old-cli"
            assert session.executor_profile == "old-profile"

        app_inst.shutdown_html_process_pool()

    asyncio.run(_run())


def test_session_settings_put_rejects_manager_without_openai(tmp_path) -> None:
    async def _run() -> None:
        cfg = _build_config(tmp_path, user_modes={2: ["manager"]}, with_openai=False)
        app_inst = BotApp(cfg)
        session = app_inst.manager.create(2, "dummy", str(tmp_path))
        suid = session_runtime_uid(session)
        routes = MiniAppRoutes(app_inst)
        web_app = web.Application()
        routes.register(web_app)

        init_data = _build_init_data("t", 2)
        async with TestClient(TestServer(web_app)) as client:
            resp = await client.put(
                f"/api/session/{suid}/settings",
                json={"active_mode": "manager"},
                headers={"X-Telegram-Init-Data": init_data},
            )

            assert resp.status == 409
            body = await resp.json()
            assert body["ok"] is False
            assert "OpenAI" in body["error"]
            assert get_active_mode(session, "") in ("", None)

        app_inst.shutdown_html_process_pool()

    asyncio.run(_run())


def test_session_settings_put_keeps_current_mode_when_sdd_preflight_fails(tmp_path) -> None:
    async def _run() -> None:
        cfg = _build_config(tmp_path, user_modes={2: ["manager", "sdd"]}, with_openai=False)
        app_inst = BotApp(cfg)
        session = app_inst.manager.create(2, "dummy", str(tmp_path))
        set_active_mode(session, "manager")
        suid = session_runtime_uid(session)
        routes = MiniAppRoutes(app_inst)
        web_app = web.Application()
        routes.register(web_app)

        init_data = _build_init_data("t", 2)
        async with TestClient(TestServer(web_app)) as client:
            resp = await client.put(
                f"/api/session/{suid}/settings",
                json={"active_mode": "sdd", "ssh_remote_enabled": True},
                headers={"X-Telegram-Init-Data": init_data},
            )

            assert resp.status == 409
            body = await resp.json()
            assert body["ok"] is False
            assert "OpenAI" in body["error"]
            assert get_active_mode(session, "") == "manager"
            assert is_ssh_remote_enabled(session) is False
            assert not (tmp_path / ".cli-proxy" / "ssh.yaml").exists()

        app_inst.shutdown_html_process_pool()

    asyncio.run(_run())


def test_session_settings_put_rolls_back_active_mode_when_ssh_template_raises(tmp_path, monkeypatch) -> None:
    async def _run() -> None:
        cfg = _build_config(tmp_path, user_modes={2: ["manager", "sdd"]})
        app_inst = BotApp(cfg)
        session = app_inst.manager.create(2, "dummy", str(tmp_path))
        set_active_mode(session, "manager")
        suid = session_runtime_uid(session)
        routes = MiniAppRoutes(app_inst)
        web_app = web.Application()
        routes.register(web_app)

        def _raise_template_error(workdir: str) -> None:
            raise RuntimeError("template failure")

        monkeypatch.setattr(
            "app.services.ssh_config_loader.ensure_ssh_config_template",
            _raise_template_error,
        )

        init_data = _build_init_data("t", 2)
        async with TestClient(TestServer(web_app)) as client:
            resp = await client.put(
                f"/api/session/{suid}/settings",
                json={"active_mode": "sdd", "ssh_remote_enabled": True},
                headers={"X-Telegram-Init-Data": init_data},
            )

            assert resp.status == 500
            assert get_active_mode(session, "") == "manager"
            assert is_ssh_remote_enabled(session) is False

        app_inst.shutdown_html_process_pool()

    asyncio.run(_run())


def test_session_settings_put_rejects_full_save_without_partial_remote_mutation(tmp_path) -> None:
    async def _run() -> None:
        cfg = _build_config(tmp_path, user_modes={2: ["sdd"]})
        app_inst = BotApp(cfg)
        session = app_inst.manager.create(2, "dummy", str(tmp_path))
        suid = session_runtime_uid(session)
        routes = MiniAppRoutes(app_inst)
        web_app = web.Application()
        routes.register(web_app)

        init_data = _build_init_data("t", 2)
        async with TestClient(TestServer(web_app)) as client:
            resp = await client.put(
                f"/api/session/{suid}/settings",
                json={
                    "active_mode": "sdd",
                    "ssh_remote_enabled": True,
                    "remote_control_enabled": True,
                    "remote_control_host_alias": "",
                },
                headers={"X-Telegram-Init-Data": init_data},
            )

            assert resp.status == 409
            body = await resp.json()
            assert body["ok"] is False
            assert "remote_control_host_alias" in body["error"]
            assert get_active_mode(session, "") in ("", None)
            assert is_ssh_remote_enabled(session) is False
            assert is_remote_control_enabled(session) is False
            assert not (tmp_path / ".cli-proxy" / "ssh.yaml").exists()

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
