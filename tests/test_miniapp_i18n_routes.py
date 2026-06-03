"""Tests for T4 i18n endpoints: GET /api/i18n/{lang}, GET/PUT /api/i18n/user-lang."""
import asyncio
import hashlib
import hmac
import json
import time
from urllib.parse import quote

import yaml
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from bot import BotApp
from config import AppConfig, DefaultsConfig, MCPConfig, MiniAppConfig, TelegramConfig, ToolConfig
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


def _build_config(
    tmp_path,
    *,
    token: str = "123:abc",
    admins=None,
    user_languages=None,
    default_language: str = "ru",
) -> AppConfig:
    if admins is None:
        admins = [123]
    if user_languages is None:
        user_languages = {}
    cfg = AppConfig(
        telegram=TelegramConfig(
            token=token,
            whitelist_chat_ids=[123],
            admlist_chat_ids=list(admins),
            user_workdirs={},
            user_languages=dict(user_languages),
        ),
        tools={"dummy": ToolConfig(name="dummy", mode="headless", cmd=["bash", "-lc", "cat"])},
        defaults=DefaultsConfig(
            workdir=str(tmp_path),
            state_path=str(tmp_path / "state.json"),
            toolhelp_path=str(tmp_path / "toolhelp.json"),
            log_path=str(tmp_path / "bot.log"),
            default_language=default_language,
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


def test_i18n_catalog_get_ru(tmp_path) -> None:
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
            resp = await client.get("/api/i18n/ru")
            assert resp.status == 200
            body = await resp.json()
            assert isinstance(body, dict)
            # should have some top-level keys
            assert len(body) > 0
            cache_ctrl = resp.headers.get("Cache-Control", "")
            assert "max-age=3600" in cache_ctrl
        finally:
            await client.close()
            await server.close()

    asyncio.run(_run())


def test_i18n_catalog_get_unknown_lang_falls_back_to_ru(tmp_path) -> None:
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
            resp_ru = await client.get("/api/i18n/ru")
            body_ru = await resp_ru.json()

            resp_fr = await client.get("/api/i18n/fr")
            assert resp_fr.status == 200
            body_fr = await resp_fr.json()
            # Unknown language falls back to ru catalog
            assert body_fr == body_ru
        finally:
            await client.close()
            await server.close()

    asyncio.run(_run())


def test_i18n_user_lang_get_returns_saved_lang(tmp_path) -> None:
    async def _run() -> None:
        cfg = _build_config(tmp_path, user_languages={123: "en"})
        app = BotApp(cfg)

        web_app = web.Application()
        MiniAppRoutes(app).register(web_app)
        server = TestServer(web_app)
        await server.start_server()
        client = TestClient(server)
        await client.start_server()
        try:
            headers = {"X-Telegram-Init-Data": _build_init_data("123:abc", user_id=123)}
            resp = await client.get("/api/i18n/user-lang", headers=headers)
            assert resp.status == 200
            body = await resp.json()
            assert body["lang"] == "en"
        finally:
            await client.close()
            await server.close()

    asyncio.run(_run())


def test_i18n_user_lang_get_falls_back_to_default(tmp_path) -> None:
    async def _run() -> None:
        cfg = _build_config(tmp_path, user_languages={}, default_language="de")
        app = BotApp(cfg)

        web_app = web.Application()
        MiniAppRoutes(app).register(web_app)
        server = TestServer(web_app)
        await server.start_server()
        client = TestClient(server)
        await client.start_server()
        try:
            headers = {"X-Telegram-Init-Data": _build_init_data("123:abc", user_id=123)}
            resp = await client.get("/api/i18n/user-lang", headers=headers)
            assert resp.status == 200
            body = await resp.json()
            assert body["lang"] == "de"
        finally:
            await client.close()
            await server.close()

    asyncio.run(_run())


def test_i18n_user_lang_put_valid(tmp_path) -> None:
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
            headers = {
                "X-Telegram-Init-Data": _build_init_data("123:abc", user_id=123),
                "Content-Type": "application/json",
            }
            resp = await client.put("/api/i18n/user-lang", data=json.dumps({"lang": "en"}), headers=headers)
            assert resp.status == 200
            body = await resp.json()
            assert body["ok"] is True
            assert body["lang"] == "en"
            # Persistence: set_user_language must write through to the config file
            # (the source of truth). Re-read it and confirm the user's lang was saved.
            with open(cfg.path, encoding="utf-8") as f:
                persisted = yaml.safe_load(f)
            saved_langs = persisted["telegram"]["user_languages"]
            assert saved_langs.get(123, saved_langs.get("123")) == "en"
        finally:
            await client.close()
            await server.close()

    asyncio.run(_run())


def test_i18n_user_lang_put_invalid_lang(tmp_path) -> None:
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
            headers = {
                "X-Telegram-Init-Data": _build_init_data("123:abc", user_id=123),
                "Content-Type": "application/json",
            }
            resp = await client.put("/api/i18n/user-lang", data=json.dumps({"lang": "fr"}), headers=headers)
            assert resp.status == 400
        finally:
            await client.close()
            await server.close()

    asyncio.run(_run())


def test_i18n_route_order_user_lang_not_matched_as_catalog(tmp_path) -> None:
    """GET /api/i18n/user-lang without auth header should return 401, not a JSON file."""
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
            # No auth header — should hit the auth endpoint (which requires auth)
            resp = await client.get("/api/i18n/user-lang")
            # The endpoint is auth-protected: must return 401, not 200 with a catalog
            assert resp.status == 401
        finally:
            await client.close()
            await server.close()

    asyncio.run(_run())
