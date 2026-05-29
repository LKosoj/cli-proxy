import asyncio
import hashlib
import hmac
import json
import time
from typing import Any, Dict, List
from urllib.parse import quote

import yaml
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from bot import BotApp
from config import AppConfig, DefaultsConfig, MCPConfig, MiniAppConfig, TelegramConfig, ToolConfig
from miniapp.routes import MiniAppRoutes
from miniapp.services.config_service import app_config_to_dict
from modes.admin.chat_memory import ChatMemory, ChatPendingStore
from modes.admin.chat_service import AdminChatService
from modes.admin.state_store import AdminStateStore
from session import session_runtime_uid


def _build_init_data(bot_token: str, user_id: int = 1) -> str:
    payload = {
        "auth_date": str(int(time.time())),
        "query_id": "q1",
        "user": json.dumps(
            {"id": user_id, "username": "admin", "first_name": "Admin"},
            ensure_ascii=False,
        ),
    }
    check = "\n".join(f"{k}={v}" for k, v in sorted(payload.items()))
    secret = hmac.new(b"WebAppData", bot_token.encode("utf-8"), hashlib.sha256).digest()
    sig = hmac.new(secret, check.encode("utf-8"), hashlib.sha256).hexdigest()
    return (
        f"auth_date={payload['auth_date']}&query_id=q1"
        f"&user={quote(payload['user'])}&hash={sig}"
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
    with open(cfg.path, "w", encoding="utf-8") as f:
        yaml.safe_dump(app_config_to_dict(cfg), f, sort_keys=False, allow_unicode=False)
    return cfg


def _register_routes(web_app: web.Application, app: BotApp) -> MiniAppRoutes:
    routes = MiniAppRoutes(app)
    routes._require_access = routes._require_access_real  # type: ignore[method-assign]
    routes.register(web_app)
    return routes


class _FakeLocalResult:
    def __init__(self, action_id: str) -> None:
        self.action_id = action_id
        self.returncode = 0
        self.stdout = "ok"
        self.stderr = ""
        self.timed_out = False
        self.duration_ms = 1


class _FakeLocalTransport:
    async def run(self, spec: Any) -> _FakeLocalResult:
        return _FakeLocalResult(spec.action_id)


def _write_admin_config(workdir, admin_cfg: Dict[str, Any]) -> None:
    cli_proxy = workdir / ".cli-proxy"
    cli_proxy.mkdir(exist_ok=True)
    admin_dir = cli_proxy / ".admin"
    admin_dir.mkdir(exist_ok=True)
    (admin_dir / "config.yaml").write_text(
        yaml.safe_dump({"admin": admin_cfg}),
        encoding="utf-8",
    )


_ADMIN_CFG = {
    "actions": {
        "local": {
            "check_disk": {
                "argv": ["df", "-h"],
                "timeout_sec": 10,
                "risk_level": "low",
                "read_only": True,
            },
        },
    },
}


def _install_fake_chat_service(
    app: BotApp, *, llm_responses: List[str] = None
) -> List[Dict[str, str]]:
    plugin = app.mode_registry.get("admin")
    assert plugin is not None, "admin mode must be registered"
    responses = list(llm_responses or [])
    calls: List[Dict[str, str]] = []

    def factory(_bot_app: Any):
        async def provider(system: str, user: str) -> str:
            calls.append({"system": system, "user": user})
            if not responses:
                return ""
            return responses.pop(0)

        return provider

    plugin._chat_service = AdminChatService(
        local_transport=_FakeLocalTransport(),
        ssh_transport=None,
        llm_provider_factory=factory,
    )
    return calls


def _make_session(app: BotApp, tmp_path, *, user_id: int = 1):
    workdir = tmp_path / f"session-{user_id}"
    workdir.mkdir()
    session = app.manager.create(user_id, "dummy", str(workdir))
    store = AdminStateStore(app.config.defaults.state_path)
    store.upsert_session_state(session.id, chat_id=user_id, enabled=True)
    return session, workdir


def test_miniapp_chat_messages_get_empty(tmp_path) -> None:
    async def _run() -> None:
        cfg = _build_config(tmp_path, token="t")
        app = BotApp(cfg)
        _install_fake_chat_service(app)
        session, _ = _make_session(app, tmp_path)

        web_app = web.Application()
        _register_routes(web_app, app)

        server = TestServer(web_app)
        await server.start_server()
        client = TestClient(server)
        await client.start_server()
        try:
            headers = {"X-Telegram-Init-Data": _build_init_data("t", 1)}
            resp = await client.get(
                f"/api/v1/admin/chat/messages?session_uid={session_runtime_uid(session)}",
                headers=headers,
            )
            assert resp.status == 200
            body = await resp.json()
            assert body == {"ok": True, "messages": []}
        finally:
            await client.close()
            await server.close()

    asyncio.run(_run())


def test_miniapp_chat_messages_get_returns_stored_entries(tmp_path) -> None:
    async def _run() -> None:
        cfg = _build_config(tmp_path, token="t")
        app = BotApp(cfg)
        _install_fake_chat_service(app)
        session, workdir = _make_session(app, tmp_path)
        ChatMemory(str(workdir)).append(role="user", text="hi")

        web_app = web.Application()
        _register_routes(web_app, app)

        server = TestServer(web_app)
        await server.start_server()
        client = TestClient(server)
        await client.start_server()
        try:
            headers = {"X-Telegram-Init-Data": _build_init_data("t", 1)}
            resp = await client.get(
                f"/api/v1/admin/chat/messages?session_uid={session_runtime_uid(session)}",
                headers=headers,
            )
            assert resp.status == 200
            body = await resp.json()
            assert body["ok"] is True
            assert len(body["messages"]) == 1
            assert body["messages"][0]["role"] == "user"
            assert body["messages"][0]["text"] == "hi"
        finally:
            await client.close()
            await server.close()

    asyncio.run(_run())


def test_miniapp_chat_pending_get_returns_items(tmp_path) -> None:
    async def _run() -> None:
        cfg = _build_config(tmp_path, token="t")
        app = BotApp(cfg)
        _install_fake_chat_service(app)
        session, workdir = _make_session(app, tmp_path)
        ChatPendingStore(str(workdir)).save(
            "chat-x",
            {"approval_id": "chat-x", "intent": {"type": "propose_action"}},
        )

        web_app = web.Application()
        _register_routes(web_app, app)

        server = TestServer(web_app)
        await server.start_server()
        client = TestClient(server)
        await client.start_server()
        try:
            headers = {"X-Telegram-Init-Data": _build_init_data("t", 1)}
            resp = await client.get(
                f"/api/v1/admin/chat/pending?session_uid={session_runtime_uid(session)}",
                headers=headers,
            )
            assert resp.status == 200
            body = await resp.json()
            assert body["ok"] is True
            assert [i["approval_id"] for i in body["items"]] == ["chat-x"]
        finally:
            await client.close()
            await server.close()

    asyncio.run(_run())


def test_miniapp_chat_memory_roundtrip(tmp_path) -> None:
    async def _run() -> None:
        cfg = _build_config(tmp_path, token="t")
        app = BotApp(cfg)
        _install_fake_chat_service(app)
        session, _ = _make_session(app, tmp_path)

        web_app = web.Application()
        _register_routes(web_app, app)

        server = TestServer(web_app)
        await server.start_server()
        client = TestClient(server)
        await client.start_server()
        try:
            headers = {"X-Telegram-Init-Data": _build_init_data("t", 1)}
            read1 = await client.get(
                f"/api/v1/admin/chat/memory?session_uid={session_runtime_uid(session)}",
                headers=headers,
            )
            assert read1.status == 200
            body1 = await read1.json()
            assert body1 == {"ok": True, "text": ""}

            put = await client.put(
                "/api/v1/admin/chat/memory",
                headers=headers,
                json={
                    "session_uid": session_runtime_uid(session),
                    "text": "nginx only at night",
                },
            )
            assert put.status == 200
            put_body = await put.json()
            assert put_body == {"ok": True}

            read2 = await client.get(
                f"/api/v1/admin/chat/memory?session_uid={session_runtime_uid(session)}",
                headers=headers,
            )
            body2 = await read2.json()
            assert "nginx only at night" in body2["text"]
        finally:
            await client.close()
            await server.close()

    asyncio.run(_run())


def test_miniapp_chat_pending_reject_removes_record(tmp_path) -> None:
    async def _run() -> None:
        cfg = _build_config(tmp_path, token="t")
        app = BotApp(cfg)
        _install_fake_chat_service(app)
        session, workdir = _make_session(app, tmp_path)
        store = ChatPendingStore(str(workdir))
        store.save(
            "chat-r",
            {"approval_id": "chat-r", "intent": {"type": "propose_action"}},
        )

        web_app = web.Application()
        _register_routes(web_app, app)

        server = TestServer(web_app)
        await server.start_server()
        client = TestClient(server)
        await client.start_server()
        try:
            headers = {"X-Telegram-Init-Data": _build_init_data("t", 1)}
            resp = await client.post(
                "/api/v1/admin/chat/pending/chat-r/reject",
                headers=headers,
                json={"session_uid": session_runtime_uid(session)},
            )
            assert resp.status == 200
            body = await resp.json()
            assert body["ok"] is True
            assert store.get("chat-r") is None
        finally:
            await client.close()
            await server.close()

    asyncio.run(_run())


def test_miniapp_chat_pending_reject_missing_returns_error(tmp_path) -> None:
    async def _run() -> None:
        cfg = _build_config(tmp_path, token="t")
        app = BotApp(cfg)
        _install_fake_chat_service(app)
        session, _ = _make_session(app, tmp_path)

        web_app = web.Application()
        _register_routes(web_app, app)

        server = TestServer(web_app)
        await server.start_server()
        client = TestClient(server)
        await client.start_server()
        try:
            headers = {"X-Telegram-Init-Data": _build_init_data("t", 1)}
            resp = await client.post(
                "/api/v1/admin/chat/pending/ghost/reject",
                headers=headers,
                json={"session_uid": session_runtime_uid(session)},
            )
            assert resp.status == 200
            body = await resp.json()
            assert body["ok"] is False
            assert body["error"] == "approval_not_found"
        finally:
            await client.close()
            await server.close()

    asyncio.run(_run())


def test_miniapp_chat_messages_post_requires_text(tmp_path) -> None:
    async def _run() -> None:
        cfg = _build_config(tmp_path, token="t")
        app = BotApp(cfg)
        _install_fake_chat_service(app)
        session, _ = _make_session(app, tmp_path)

        web_app = web.Application()
        _register_routes(web_app, app)

        server = TestServer(web_app)
        await server.start_server()
        client = TestClient(server)
        await client.start_server()
        try:
            headers = {"X-Telegram-Init-Data": _build_init_data("t", 1)}
            resp = await client.post(
                "/api/v1/admin/chat/messages",
                headers=headers,
                json={"session_uid": session_runtime_uid(session), "text": ""},
            )
            assert resp.status == 400
        finally:
            await client.close()
            await server.close()

    asyncio.run(_run())


def test_miniapp_chat_messages_post_propose_action_saves_pending(tmp_path) -> None:
    async def _run() -> None:
        cfg = _build_config(tmp_path, token="t")
        app = BotApp(cfg)
        _install_fake_chat_service(
            app,
            llm_responses=[
                json.dumps(
                    {
                        "type": "propose_action",
                        "action_id": "check_disk",
                        "target": "local",
                        "text": "disk?",
                    }
                )
            ],
        )
        session, workdir = _make_session(app, tmp_path)
        _write_admin_config(workdir, _ADMIN_CFG)

        web_app = web.Application()
        _register_routes(web_app, app)

        server = TestServer(web_app)
        await server.start_server()
        client = TestClient(server)
        await client.start_server()
        try:
            headers = {"X-Telegram-Init-Data": _build_init_data("t", 1)}
            resp = await client.post(
                "/api/v1/admin/chat/messages",
                headers=headers,
                json={"session_uid": session_runtime_uid(session), "text": "check disk"},
            )
            assert resp.status == 200
            body = await resp.json()
            assert body["ok"] is True
            assert body["intent"]["type"] == "propose_action"
            pending_id = body.get("pending_action_id")
            assert pending_id
            assert ChatPendingStore(str(workdir)).get(pending_id) is not None
        finally:
            await client.close()
            await server.close()

    asyncio.run(_run())


def test_miniapp_chat_approve_executes_local_action(tmp_path) -> None:
    async def _run() -> None:
        cfg = _build_config(tmp_path, token="t")
        app = BotApp(cfg)
        _install_fake_chat_service(app)
        session, workdir = _make_session(app, tmp_path)
        _write_admin_config(workdir, _ADMIN_CFG)
        ChatPendingStore(str(workdir)).save(
            "chat-local",
            {
                "approval_id": "chat-local",
                "intent": {
                    "type": "propose_action",
                    "action_id": "check_disk",
                    "target": "local",
                    "text": "x",
                },
            },
        )

        web_app = web.Application()
        _register_routes(web_app, app)

        server = TestServer(web_app)
        await server.start_server()
        client = TestClient(server)
        await client.start_server()
        try:
            headers = {"X-Telegram-Init-Data": _build_init_data("t", 1)}
            resp = await client.post(
                "/api/v1/admin/chat/pending/chat-local/approve",
                headers=headers,
                json={"session_uid": session_runtime_uid(session)},
            )
            assert resp.status == 200
            body = await resp.json()
            assert body["ok"] is True
            assert body["exit_code"] == 0
            assert body["target_kind"] == "local"
        finally:
            await client.close()
            await server.close()

    asyncio.run(_run())
