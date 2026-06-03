import hashlib
import hmac
import json
import time
from types import SimpleNamespace
from urllib.parse import quote
import asyncio

import pytest
import yaml
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from app.security.interfaces import AuthenticationResult, AuthDecision
from bot import BotApp
from config import AppConfig, DefaultsConfig, MCPConfig, MiniAppConfig, TelegramConfig, ToolConfig
from miniapp.auth import MiniAppAuthError, verify_telegram_init_data
from miniapp.routes import MiniAppRoutes
from miniapp.services.config_service import app_config_to_dict


def _build_init_data(bot_token: str, user_id: int = 123) -> str:
    payload = {
        "auth_date": str(int(time.time())),
        "query_id": "q1",
        "user": json.dumps({"id": user_id, "username": "admin", "first_name": "Admin"}, ensure_ascii=False),
    }
    check = "\n".join(f"{k}={v}" for k, v in sorted(payload.items()))
    secret = hmac.new(b"WebAppData", bot_token.encode("utf-8"), hashlib.sha256).digest()
    sig = hmac.new(secret, check.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"auth_date={payload['auth_date']}&query_id=q1&user={quote(payload['user'])}&hash={sig}"


def _build_init_data_with_lang(bot_token: str, user_id: int = 123, language_code: str = "en") -> str:
    user_obj = {"id": user_id, "username": "admin", "first_name": "Admin", "language_code": language_code}
    payload = {
        "auth_date": str(int(time.time())),
        "query_id": "q1",
        "user": json.dumps(user_obj, ensure_ascii=False),
    }
    check = "\n".join(f"{k}={v}" for k, v in sorted(payload.items()))
    secret = hmac.new(b"WebAppData", bot_token.encode("utf-8"), hashlib.sha256).digest()
    sig = hmac.new(secret, check.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"auth_date={payload['auth_date']}&query_id=q1&user={quote(payload['user'])}&hash={sig}"


def test_verify_telegram_init_data_extracts_language_code() -> None:
    token = "123:abc"
    init_data = _build_init_data_with_lang(token, language_code="en")
    user = verify_telegram_init_data(init_data, token)
    assert user.language_code == "en"


def test_verify_telegram_init_data_language_code_missing() -> None:
    token = "123:abc"
    # _build_init_data does not include language_code in user payload
    init_data = _build_init_data(token)
    user = verify_telegram_init_data(init_data, token)
    assert user.language_code == ""


def test_verify_telegram_init_data_ok() -> None:
    token = "123:abc"
    init_data = _build_init_data(token)
    user = verify_telegram_init_data(init_data, token)
    assert user.user_id == 123
    assert user.username == "admin"


def test_verify_telegram_init_data_bad_hash() -> None:
    token = "123:abc"
    init_data = _build_init_data(token) + "broken"
    with pytest.raises(MiniAppAuthError):
        verify_telegram_init_data(init_data, token)


def _build_config(tmp_path, *, token: str = "123:abc", admins=None, whitelist=None, user_workdirs=None) -> AppConfig:
    if admins is None:
        admins = [123]
    if whitelist is None:
        whitelist = []
    if user_workdirs is None:
        user_workdirs = {}
    cfg = AppConfig(
        telegram=TelegramConfig(
            token=token,
            whitelist_chat_ids=list(whitelist),
            admlist_chat_ids=list(admins),
            user_workdirs=dict(user_workdirs),
        ),
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


def test_auth_me_requires_header(tmp_path) -> None:
    async def _run() -> None:
        cfg = _build_config(tmp_path)
        app = BotApp(cfg)

        web_app = web.Application()
        MiniAppRoutes(app).register(web_app)
        server = TestServer(web_app)
        await server.start_server()
        client = TestClient(server)
        await client.start_server()
        try:
            resp = await client.get("/api/auth/me")
            assert resp.status == 401
        finally:
            await client.close()
            await server.close()

    asyncio.run(_run())


def test_auth_me_forbidden_for_non_admin(tmp_path) -> None:
    async def _run() -> None:
        cfg = _build_config(tmp_path, admins=[999])
        app = BotApp(cfg)

        web_app = web.Application()
        MiniAppRoutes(app).register(web_app)
        server = TestServer(web_app)
        await server.start_server()
        client = TestClient(server)
        await client.start_server()
        try:
            headers = {"X-Telegram-Init-Data": _build_init_data("123:abc", user_id=123)}
            resp = await client.get("/api/auth/me", headers=headers)
            assert resp.status == 403
        finally:
            await client.close()
            await server.close()

    asyncio.run(_run())


def test_auth_me_rejects_invalid_init_data(tmp_path) -> None:
    async def _run() -> None:
        cfg = _build_config(tmp_path)
        app = BotApp(cfg)

        web_app = web.Application()
        MiniAppRoutes(app).register(web_app)
        server = TestServer(web_app)
        await server.start_server()
        client = TestClient(server)
        await client.start_server()
        try:
            headers = {"X-Telegram-Init-Data": "broken"}
            resp = await client.get("/api/auth/me", headers=headers)
            assert resp.status == 401
        finally:
            await client.close()
            await server.close()

    asyncio.run(_run())


def test_auth_me_allows_regular_user_with_project_acl(tmp_path) -> None:
    async def _run() -> None:
        cfg = _build_config(
            tmp_path,
            admins=[999],
            whitelist=[123],
            user_workdirs={123: [str(tmp_path)]},
        )
        app = BotApp(cfg)

        web_app = web.Application()
        MiniAppRoutes(app).register(web_app)
        server = TestServer(web_app)
        await server.start_server()
        client = TestClient(server)
        await client.start_server()
        try:
            headers = {"X-Telegram-Init-Data": _build_init_data("123:abc", user_id=123)}
            resp = await client.get("/api/auth/me", headers=headers)
            assert resp.status == 200
            body = await resp.json()
            assert body["is_admin"] is False
            assert body["user_id"] == 123
        finally:
            await client.close()
            await server.close()

    asyncio.run(_run())


def test_auth_me_uses_security_facade_for_auth_and_authorize(tmp_path) -> None:
    async def _run() -> None:
        init_data = _build_init_data("123:abc", user_id=123)
        auth_calls = []
        authorize_calls = []

        def _authenticate(credentials, *, strategy=None):
            auth_calls.append({"credentials": dict(credentials), "strategy": strategy})
            return AuthenticationResult(
                strategy="telegram_init_data",
                authenticated=True,
                subject="123",
                claims={"user_id": 123, "username": "admin", "first_name": "Admin"},
            )

        def _authorize(chat_id: int, *, scope: str = "generic", require_admin: bool = False):
            authorize_calls.append(
                {"chat_id": int(chat_id), "scope": str(scope), "require_admin": bool(require_admin)}
            )
            return AuthDecision(
                chat_id=int(chat_id),
                allowed=True,
                scope=str(scope),
                is_admin=False,
                is_user=True,
                reason="",
            )

        app = type("FakeMiniApp", (), {})()
        app.config = _build_config(tmp_path)
        app.container = SimpleNamespace(config_service=SimpleNamespace())
        app.security = type("FakeSecurity", (), {"authenticate": staticmethod(_authenticate), "authorize": staticmethod(_authorize)})()
        app.is_admin = lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("legacy is_admin used"))
        app.is_user = lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("legacy is_user used"))

        web_app = web.Application()
        MiniAppRoutes(app).register(web_app)
        server = TestServer(web_app)
        await server.start_server()
        client = TestClient(server)
        await client.start_server()
        try:
            headers = {"X-Telegram-Init-Data": init_data}
            resp = await client.get("/api/auth/me", headers=headers)
            assert resp.status == 200
            body = await resp.json()
            assert body["user_id"] == 123
            assert body["is_admin"] is False
            assert auth_calls == [{"credentials": {"init_data": init_data}, "strategy": "telegram_init_data"}]
            assert authorize_calls == [{"chat_id": 123, "scope": "miniapp", "require_admin": False}]
        finally:
            await client.close()
            await server.close()

    asyncio.run(_run())
