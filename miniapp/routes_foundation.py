from __future__ import annotations

from dataclasses import dataclass

from aiohttp import web

from .route_context import MiniAppRouteContext


@dataclass(frozen=True)
class FoundationRouteServices:
    """No-op services object that documents the extracted route-module shape."""


def register_foundation_routes(
    app: web.Application,
    ctx: MiniAppRouteContext,
    services: FoundationRouteServices,
) -> None:
    """Registration pattern example: routes_<area>.py receives ctx and area services."""
    _ = (app, ctx, services)
