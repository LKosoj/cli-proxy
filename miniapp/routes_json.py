from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict

from aiohttp import web

from .route_context import MiniAppRouteContext


@dataclass(frozen=True)
class JsonRouteServices:
    def require_object_body(self, body: Any) -> Dict[str, Any]:
        if not isinstance(body, dict):
            raise web.HTTPBadRequest(reason="request body must be an object")
        return dict(body)

    async def read_json_object(self, request: web.Request) -> Dict[str, Any]:
        try:
            body = await request.json()
        except Exception as exc:
            raise web.HTTPBadRequest(reason="invalid json body") from exc
        return self.require_object_body(body)


def register_json_routes(
    app: web.Application,
    ctx: MiniAppRouteContext,
    services: JsonRouteServices,
) -> None:
    _ = (app, ctx, services)
