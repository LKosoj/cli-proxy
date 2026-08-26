import asyncio
import hashlib
import hmac
import inspect
import json
import time
from dataclasses import is_dataclass
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import quote

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
import yaml

from bot import BotApp
from config import AppConfig, DefaultsConfig, MCPConfig, MiniAppConfig, TelegramConfig, ToolConfig
from miniapp.routes import MiniAppRoutes
from miniapp.routes_config import ConfigRouteServices, register_config_routes
from miniapp.routes_logs import LogsRouteServices, register_logs_routes
from miniapp.routes_scheduler import SchedulerRouteServices, register_scheduler_routes
from miniapp.routes_ssh import SshRouteServices, register_ssh_routes
from miniapp.services.config_service import SECRET_UNCHANGED_SENTINEL, app_config_to_dict
from sessions.conversation_scope import ConversationScope
from session import session_runtime_uid


def _build_init_data(bot_token: str, user_id: int = 1) -> str:
    payload = {
        "auth_date": str(int(time.time())),
        "query_id": "q1",
        "user": json.dumps({"id": user_id, "username": "admin", "first_name": "Admin"}, ensure_ascii=False),
    }
    check = "\n".join(f"{key}={value}" for key, value in sorted(payload.items()))
    secret = hmac.new(b"WebAppData", bot_token.encode("utf-8"), hashlib.sha256).digest()
    sig = hmac.new(secret, check.encode("utf-8"), hashlib.sha256).hexdigest()
    return (
        f"auth_date={payload['auth_date']}"
        f"&query_id=q1"
        f"&user={quote(payload['user'])}"
        f"&hash={sig}"
    )


def _build_config(tmp_path, *, token: str = "t") -> AppConfig:
    cfg = AppConfig(
        telegram=TelegramConfig(token=token, whitelist_chat_ids=[1], admlist_chat_ids=[1]),
        tools={"dummy": ToolConfig(name="dummy", mode="headless", cmd=["bash", "-lc", "cat"])},
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
    with open(cfg.path, "w", encoding="utf-8") as handle:
        yaml.safe_dump(app_config_to_dict(cfg), handle, sort_keys=False, allow_unicode=False)
    return cfg


def _fake_container() -> SimpleNamespace:
    return SimpleNamespace(config_service=SimpleNamespace())


def _register_routes(web_app: web.Application, app: BotApp) -> MiniAppRoutes:
    routes = MiniAppRoutes(app)
    routes.register(web_app)
    return routes


def _write_config_file(cfg: AppConfig) -> None:
    Path(cfg.path).write_text(
        yaml.safe_dump(app_config_to_dict(cfg), sort_keys=False, allow_unicode=False),
        encoding="utf-8",
    )


async def _post_raw_json(client: TestClient, path: str, *, headers: dict[str, str], body: str):
    request_headers = dict(headers)
    request_headers["Content-Type"] = "application/json"
    return await client.post(path, headers=request_headers, data=body)


async def _put_raw_json(client: TestClient, path: str, *, headers: dict[str, str], body: str):
    request_headers = dict(headers)
    request_headers["Content-Type"] = "application/json"
    return await client.put(path, headers=request_headers, data=body)


def test_config_route_module_uses_registration_pattern() -> None:
    signature = inspect.signature(register_config_routes)
    assert list(signature.parameters) == ["app", "ctx", "services"]
    assert is_dataclass(ConfigRouteServices)


def test_scheduler_route_module_uses_registration_pattern() -> None:
    signature = inspect.signature(register_scheduler_routes)
    assert list(signature.parameters) == ["app", "ctx", "services"]
    assert is_dataclass(SchedulerRouteServices)


def test_logs_route_module_uses_registration_pattern() -> None:
    signature = inspect.signature(register_logs_routes)
    assert list(signature.parameters) == ["app", "ctx", "services"]
    assert is_dataclass(LogsRouteServices)


def test_ssh_route_module_uses_registration_pattern() -> None:
    signature = inspect.signature(register_ssh_routes)
    assert list(signature.parameters) == ["app", "ctx", "services"]
    assert is_dataclass(SshRouteServices)


def test_miniapp_routes_wires_config_registration_with_context_and_services(monkeypatch) -> None:
    import miniapp.routes as routes_module

    captured = {}

    def fake_register(app, ctx, services):
        captured["app"] = app
        captured["ctx"] = ctx
        captured["services"] = services

    monkeypatch.setattr(routes_module, "register_config_routes", fake_register)
    bot_app = SimpleNamespace(
        config=SimpleNamespace(
            path="config.yaml",
            defaults=SimpleNamespace(log_path="bot.log"),
            miniapp=SimpleNamespace(max_edit_file_size_kb=5120),
        ),
        container=_fake_container(),
    )
    web_app = web.Application()
    routes = MiniAppRoutes(bot_app)

    routes.register(web_app)

    assert captured == {
        "app": web_app,
        "ctx": routes.route_context,
        "services": routes.config_route_services,
    }
    assert routes.config_route_services.config_service is bot_app.container.config_service


def test_miniapp_routes_uses_container_config_service_for_real_botapp(tmp_path) -> None:
    cfg = _build_config(tmp_path, token="t")
    app = BotApp(cfg)
    try:
        routes = MiniAppRoutes(app)

        assert routes.config_route_services.config_service is app.container.config_service
    finally:
        app.shutdown_html_process_pool()


def test_miniapp_status_payload_reports_backend_switch_read_only(tmp_path) -> None:
    cfg = _build_config(tmp_path, token="t")
    cfg.tools["dummy"].interactive_cmd = ["bash", "-lc", "cat"]
    cfg.tools["dummy"].execution_backends = ["headless", "tmux"]
    cfg.tools["dummy"].default_execution_backend = "headless"
    app = BotApp(cfg)
    try:
        routes = MiniAppRoutes(app)
        session = SimpleNamespace(
            id="s1",
            name="Status session",
            chat_id=1,
            conversation_scope=ConversationScope.from_parts(1),
            config=cfg,
            tool=cfg.tools["dummy"],
            workdir=str(tmp_path),
            cli=SimpleNamespace(active_cli="dummy", resume_tokens={}),
            git=SimpleNamespace(busy=False, conflict=False, conflict_files=[]),
            modes=SimpleNamespace(),
            queue=[],
            busy=False,
            current_proc=None,
            child=None,
            _active_execution_backend="none",
        )

        payload = routes._build_session_payload(session, session_chat_id=1, is_admin=True)

        assert payload["available_execution_backends"] == ["headless", "tmux"]
        assert payload["backend_switch_allowed"] is False
        assert payload["backend_switch_blockers"] == ["configured in settings"]
    finally:
        app.shutdown_html_process_pool()


def test_miniapp_session_payload_and_option_report_unread_flag(tmp_path) -> None:
    cfg = _build_config(tmp_path, token="t")
    app = BotApp(cfg)
    try:
        routes = MiniAppRoutes(app)
        session = SimpleNamespace(
            id="s1",
            name="Unread session",
            chat_id=1,
            conversation_scope=ConversationScope.from_parts(1),
            config=cfg,
            tool=cfg.tools["dummy"],
            workdir=str(tmp_path),
            cli=SimpleNamespace(active_cli="dummy", resume_tokens={}),
            git=SimpleNamespace(busy=False, conflict=False, conflict_files=[]),
            modes=SimpleNamespace(),
            queue=[],
            busy=False,
            unread=False,
            current_proc=None,
            child=None,
            _active_execution_backend="none",
        )

        payload_off = routes._build_session_payload(session, session_chat_id=1, is_admin=True)
        option_off = routes._build_session_option(session_uid="s1", session=session, is_admin=True)
        assert payload_off["unread"] is False
        assert option_off["unread"] is False

        session.unread = True
        payload_on = routes._build_session_payload(session, session_chat_id=1, is_admin=True)
        option_on = routes._build_session_option(session_uid="s1", session=session, is_admin=True)
        assert payload_on["unread"] is True
        assert option_on["unread"] is True
    finally:
        app.shutdown_html_process_pool()


def test_config_view_redacts_all_secret_values_with_valid_miniapp_auth(tmp_path) -> None:
    async def _run() -> None:
        cfg = _build_config(tmp_path, token="route-secret-telegram-token")
        secret_values = {
            "telegram.token": cfg.telegram.token,
            "defaults.openai_api_key": "route-secret-openai",
            "defaults.zai_api_key": "route-secret-zai",
            "defaults.github_token": "route-secret-github",
            "defaults.tavily_api_key": "route-secret-tavily",
            "defaults.jina_api_key": "route-secret-jina",
            "defaults.gemini_oauth_client_secret": "route-secret-gemini",
            "webhooks.secret_token": "route-secret-webhooks",
            "mcp.token": "route-secret-mcp",
        }
        cfg.defaults.openai_api_key = secret_values["defaults.openai_api_key"]
        cfg.defaults.zai_api_key = secret_values["defaults.zai_api_key"]
        cfg.defaults.github_token = secret_values["defaults.github_token"]
        cfg.defaults.tavily_api_key = secret_values["defaults.tavily_api_key"]
        cfg.defaults.jina_api_key = secret_values["defaults.jina_api_key"]
        cfg.defaults.gemini_oauth_client_secret = secret_values["defaults.gemini_oauth_client_secret"]
        cfg.webhooks.secret_token = secret_values["webhooks.secret_token"]
        cfg.mcp.token = secret_values["mcp.token"]
        _write_config_file(cfg)

        app = BotApp(cfg)
        web_app = web.Application()
        _register_routes(web_app, app)

        server = TestServer(web_app)
        await server.start_server()
        client = TestClient(server)
        await client.start_server()
        try:
            headers = {"X-Telegram-Init-Data": _build_init_data(cfg.telegram.token, 1)}
            response = await client.get("/api/config/view", headers=headers)
            assert response.status == 200
            body = await response.json()
            redaction = body["redaction"]
            view_config = body["config"]
            assert redaction["sentinel"] == SECRET_UNCHANGED_SENTINEL
            assert set(secret_values) <= set(redaction["fields"])
            assert view_config["telegram"]["token"] == SECRET_UNCHANGED_SENTINEL
            assert view_config["defaults"]["openai_api_key"] == SECRET_UNCHANGED_SENTINEL
            assert view_config["defaults"]["zai_api_key"] == SECRET_UNCHANGED_SENTINEL
            assert view_config["defaults"]["github_token"] == SECRET_UNCHANGED_SENTINEL
            assert view_config["defaults"]["tavily_api_key"] == SECRET_UNCHANGED_SENTINEL
            assert view_config["defaults"]["jina_api_key"] == SECRET_UNCHANGED_SENTINEL
            assert view_config["defaults"]["gemini_oauth_client_secret"] == SECRET_UNCHANGED_SENTINEL
            assert view_config["webhooks"]["secret_token"] == SECRET_UNCHANGED_SENTINEL
            assert view_config["mcp"]["token"] == SECRET_UNCHANGED_SENTINEL

            serialized_body = json.dumps(body, sort_keys=True)
            for value in secret_values.values():
                assert value not in serialized_body
        finally:
            await client.close()
            await server.close()

    asyncio.run(_run())


def test_miniapp_routes_wires_scheduler_registration_with_context_and_services(monkeypatch) -> None:
    import miniapp.routes as routes_module

    captured = {}

    def fake_register(app, ctx, services):
        captured["app"] = app
        captured["ctx"] = ctx
        captured["services"] = services

    monkeypatch.setattr(routes_module, "register_scheduler_routes", fake_register)
    bot_app = SimpleNamespace(
        config=SimpleNamespace(
            path="config.yaml",
            defaults=SimpleNamespace(log_path="bot.log"),
            miniapp=SimpleNamespace(max_edit_file_size_kb=5120),
        ),
        container=_fake_container(),
    )
    web_app = web.Application()
    routes = MiniAppRoutes(bot_app)

    routes.register(web_app)

    assert captured == {
        "app": web_app,
        "ctx": routes.route_context,
        "services": routes.scheduler_route_services,
    }


def test_miniapp_routes_wires_logs_registration_with_context_and_services(monkeypatch) -> None:
    import miniapp.routes as routes_module

    captured = {}

    def fake_register(app, ctx, services):
        captured["app"] = app
        captured["ctx"] = ctx
        captured["services"] = services

    monkeypatch.setattr(routes_module, "register_logs_routes", fake_register)
    bot_app = SimpleNamespace(
        config=SimpleNamespace(
            path="config.yaml",
            defaults=SimpleNamespace(log_path="bot.log"),
            miniapp=SimpleNamespace(max_edit_file_size_kb=5120),
        ),
        container=_fake_container(),
    )
    web_app = web.Application()
    routes = MiniAppRoutes(bot_app)

    routes.register(web_app)

    assert captured == {
        "app": web_app,
        "ctx": routes.route_context,
        "services": routes.logs_route_services,
    }


def test_miniapp_routes_wires_ssh_registration_with_context_and_services(monkeypatch) -> None:
    import miniapp.routes as routes_module

    captured = {}

    def fake_register(app, ctx, services):
        captured["app"] = app
        captured["ctx"] = ctx
        captured["services"] = services

    monkeypatch.setattr(routes_module, "register_ssh_routes", fake_register)
    bot_app = SimpleNamespace(
        config=SimpleNamespace(
            path="config.yaml",
            defaults=SimpleNamespace(log_path="bot.log"),
            miniapp=SimpleNamespace(max_edit_file_size_kb=5120),
        ),
        container=_fake_container(),
    )
    web_app = web.Application()
    routes = MiniAppRoutes(bot_app)

    routes.register(web_app)

    assert captured == {
        "app": web_app,
        "ctx": routes.route_context,
        "services": routes.ssh_route_services,
    }


def test_miniapp_routes_does_not_register_ssh_endpoints_directly() -> None:
    source = (Path(__file__).resolve().parents[1] / "miniapp" / "routes.py").read_text(
        encoding="utf-8"
    )

    assert 'app.router.add_get("/api/ssh/hosts"' not in source
    assert 'app.router.add_get("/api/ssh/hosts/{alias}"' not in source
    assert 'app.router.add_post("/api/ssh/hosts"' not in source
    assert 'app.router.add_post("/api/ssh/test-connection"' not in source
    assert 'app.router.add_post("/api/ssh/keygen"' not in source
    assert 'app.router.add_post("/api/ssh/secret"' not in source
    assert "register_ssh_routes(app, self.route_context, self.ssh_route_services)" in source

def test_malformed_json_400_response(tmp_path) -> None:
    async def _run() -> None:
        cfg = _build_config(tmp_path, token="t")
        app = BotApp(cfg)
        app.reload_runtime_config = AsyncMock(
            return_value={
                "status": "success",
                "applied": [],
                "restart_required": [],
                "warnings": [],
            }
        )
        workdir = tmp_path / "workdir"
        workdir.mkdir()
        session = app.manager.create(1, "dummy", str(workdir))
        session_uid = session_runtime_uid(session)

        web_app = web.Application()
        _register_routes(web_app, app)

        server = TestServer(web_app)
        await server.start_server()
        client = TestClient(server)
        await client.start_server()
        try:
            headers = {"X-Telegram-Init-Data": _build_init_data("t", 1)}
            malformed_paths = [
                "/api/config/validate",
                "/api/config/diff",
                "/api/config/save",
                "/api/files/write",
                "/api/files/create",
                "/api/files/delete",
            ]

            for path in malformed_paths:
                response = await _post_raw_json(client, path, headers=headers, body="{")
                assert response.status == 400
                assert await response.json() == {"ok": False, "error": "invalid json body"}

            non_object_cases = [
                ("/api/config/validate", "[]"),
                ("/api/config/diff", "[]"),
                ("/api/config/save", "[]"),
                ("/api/files/write", "\"oops\""),
                ("/api/files/create", "\"oops\""),
                ("/api/files/delete", "\"oops\""),
            ]
            for path, raw_body in non_object_cases:
                response = await _post_raw_json(client, path, headers=headers, body=raw_body)
                assert response.status == 400
                assert await response.json() == {"ok": False, "error": "request body must be an object"}

            view_response = await client.get("/api/config/view", headers=headers)
            assert view_response.status == 200
            view_body = await view_response.json()
            assert view_body["config"]["telegram"]["token"] == SECRET_UNCHANGED_SENTINEL
            assert view_body["redaction"]["sentinel"] == SECRET_UNCHANGED_SENTINEL
            draft = dict(view_body["config"])
            draft["defaults"] = dict(draft.get("defaults") or {})
            draft["defaults"]["idle_timeout_sec"] = int(draft["defaults"].get("idle_timeout_sec", 0)) + 1

            validate_response = await client.post("/api/config/validate", headers=headers, json={"draft": draft})
            assert validate_response.status == 200
            assert (await validate_response.json())["ok"] is True

            diff_response = await client.post("/api/config/diff", headers=headers, json={"draft": draft})
            assert diff_response.status == 200
            diff_body = await diff_response.json()
            assert any(item.get("field") == "defaults.idle_timeout_sec" for item in diff_body.get("changed", []))

            conflict_response = await client.post(
                "/api/config/save",
                headers=headers,
                json={"draft": draft, "expected_revision": "stale"},
            )
            assert conflict_response.status == 409
            assert await conflict_response.json() == {"ok": False, "error": "revision mismatch"}

            save_response = await client.post(
                "/api/config/save",
                headers=headers,
                json={"draft": draft, "expected_revision": view_body["revision"]},
            )
            assert save_response.status == 200
            save_body = await save_response.json()
            assert save_body["ok"] is True
            expected_save_fields = {
                "ok",
                "revision",
                "diff",
                "changed",
                "restart_required",
                "reloadable",
                "not_applied",
                "secret_changed",
                "errors",
                "backup_path",
            }
            assert expected_save_fields <= set(save_body)
            assert save_body["changed"] is True
            assert any(item.get("field") == "defaults.idle_timeout_sec" for item in save_body["diff"]["changed"])
            assert "defaults.idle_timeout_sec" in save_body["restart_required"]
            assert "defaults.idle_timeout_sec" not in save_body["reloadable"]
            assert save_body["not_applied"] == []
            assert save_body["secret_changed"] == []
            assert save_body["errors"] == []
            assert save_body["backup_path"] == f"{cfg.path}.bak"
            saved_config = yaml.safe_load(Path(cfg.path).read_text(encoding="utf-8"))
            assert saved_config["telegram"]["token"] == "t"
            app.reload_runtime_config.assert_awaited_once()

            create_response = await client.post(
                "/api/files/create",
                headers=headers,
                json={"session_uid": session_uid, "path": "notes.txt", "kind": "file"},
            )
            assert create_response.status == 200
            assert (await create_response.json())["ok"] is True

            write_response = await client.post(
                "/api/files/write",
                headers=headers,
                json={"session_uid": session_uid, "path": "notes.txt", "content": "hello"},
            )
            assert write_response.status == 200
            assert (await write_response.json())["ok"] is True

            delete_response = await client.post(
                "/api/files/delete",
                headers=headers,
                json={"session_uid": session_uid, "path": "notes.txt"},
            )
            assert delete_response.status == 200
            assert (await delete_response.json())["ok"] is True
        finally:
            await client.close()
            await server.close()

    asyncio.run(_run())
