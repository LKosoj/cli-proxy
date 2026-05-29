import inspect
import logging
from dataclasses import FrozenInstanceError, fields, is_dataclass
from types import SimpleNamespace

import pytest
from aiohttp import web

from miniapp.route_context import MiniAppRouteContext
from miniapp.routes import MiniAppRoutes
from miniapp.routes_foundation import FoundationRouteServices, register_foundation_routes


def test_miniapp_route_context_is_small_and_domain_service_free():
    assert is_dataclass(MiniAppRouteContext)
    assert [field.name for field in fields(MiniAppRouteContext)] == ["bot_app", "logger"]

    domain_fields = {
        "config",
        "logs",
        "files",
        "runs",
        "admin",
        "scheduler",
        "config_service",
        "logs_service",
        "files_service",
        "runs_service",
        "admin_service",
        "scheduler_service",
    }
    assert not domain_fields.intersection(field.name for field in fields(MiniAppRouteContext))

    ctx = MiniAppRouteContext(bot_app=object(), logger=logging.getLogger("test.miniapp.context"))
    with pytest.raises(FrozenInstanceError):
        ctx.bot_app = object()


def test_foundation_route_module_documents_registration_shape():
    signature = inspect.signature(register_foundation_routes)
    assert list(signature.parameters) == ["app", "ctx", "services"]
    assert is_dataclass(FoundationRouteServices)

    app = web.Application()
    ctx = MiniAppRouteContext(bot_app=object(), logger=logging.getLogger("test.miniapp.foundation"))
    services = FoundationRouteServices()
    before = len(list(app.router.routes()))

    register_foundation_routes(app, ctx, services)

    assert len(list(app.router.routes())) == before


def test_miniapp_routes_wires_extracted_registration_with_context_and_services(monkeypatch):
    import miniapp.routes as routes_module

    captured = {}

    def fake_register(app, ctx, services):
        captured["app"] = app
        captured["ctx"] = ctx
        captured["services"] = services

    monkeypatch.setattr(routes_module, "register_foundation_routes", fake_register)
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
        "services": routes.foundation_route_services,
    }
