import asyncio
import inspect
import logging
from dataclasses import is_dataclass
from types import SimpleNamespace

import pytest
from aiohttp import web

from miniapp.route_context import MiniAppRouteContext
from miniapp.routes import MiniAppRoutes
from miniapp.routes_json import JsonRouteServices, register_json_routes


class FakeJsonRequest:
    def __init__(self, payload=None, exc=None):
        self.payload = payload
        self.exc = exc

    async def json(self):
        if self.exc is not None:
            raise self.exc
        return self.payload


def test_json_route_services_preserve_object_body_contract():
    services = JsonRouteServices()
    body = {"value": 1}

    assert services.require_object_body(body) == body
    assert services.require_object_body(body) is not body

    with pytest.raises(web.HTTPBadRequest) as exc_info:
        services.require_object_body([])
    assert exc_info.value.reason == "request body must be an object"


def test_json_route_services_preserve_read_json_object_contract():
    services = JsonRouteServices()

    result = asyncio.run(services.read_json_object(FakeJsonRequest({"ok": True})))
    assert result == {"ok": True}

    with pytest.raises(web.HTTPBadRequest) as non_object:
        asyncio.run(services.read_json_object(FakeJsonRequest(["not", "object"])))
    assert non_object.value.reason == "request body must be an object"

    with pytest.raises(web.HTTPBadRequest) as malformed:
        asyncio.run(services.read_json_object(FakeJsonRequest(exc=ValueError("bad json"))))
    assert malformed.value.reason == "invalid json body"


def test_json_route_module_uses_registration_pattern_without_routes():
    signature = inspect.signature(register_json_routes)
    assert list(signature.parameters) == ["app", "ctx", "services"]
    assert is_dataclass(JsonRouteServices)

    app = web.Application()
    ctx = MiniAppRouteContext(bot_app=object(), logger=logging.getLogger("test.miniapp.json"))
    services = JsonRouteServices()
    before = len(list(app.router.routes()))

    register_json_routes(app, ctx, services)

    assert len(list(app.router.routes())) == before


def test_miniapp_routes_wires_json_registration_with_context_and_services(monkeypatch):
    import miniapp.routes as routes_module

    captured = {}

    def fake_register(app, ctx, services):
        captured["app"] = app
        captured["ctx"] = ctx
        captured["services"] = services

    monkeypatch.setattr(routes_module, "register_json_routes", fake_register)
    bot_app = SimpleNamespace(
        config=SimpleNamespace(
            defaults=SimpleNamespace(log_path="bot.log"),
            miniapp=SimpleNamespace(max_edit_file_size_kb=5120),
        ),
        container=SimpleNamespace(config_service=SimpleNamespace()),
    )
    web_app = web.Application()
    routes = MiniAppRoutes(bot_app)

    routes.register(web_app)

    assert captured == {
        "app": web_app,
        "ctx": routes.route_context,
        "services": routes.json_route_services,
    }
