from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict

from aiohttp import web

from app.services.admin_config_service import AdminConfigService, AdminConfigServiceError

from .route_context import MiniAppRouteContext


RequireAdmin = Callable[[web.Request], Awaitable[Dict[str, Any]]]
ReadJsonObject = Callable[[web.Request], Awaitable[Dict[str, Any]]]
JsonError = Callable[[int, Any], Awaitable[web.Response]]


@dataclass(frozen=True)
class AdminRouteServices:
    admin_config_service: AdminConfigService
    require_admin: RequireAdmin
    read_json_object: ReadJsonObject
    json_error: JsonError


async def _admin_service_error(services: AdminRouteServices, exc: AdminConfigServiceError) -> web.Response:
    return await services.json_error(int(getattr(exc, "status", 400)), str(exc))


def register_admin_routes(
    app: web.Application,
    ctx: MiniAppRouteContext,
    services: AdminRouteServices,
) -> None:
    async def admin_config_get(request: web.Request) -> web.Response:
        await services.require_admin(request)
        session_uid = str(request.query.get("session_uid", "") or "").strip()
        try:
            result = await asyncio.to_thread(services.admin_config_service.get_yaml, session_uid)
        except AdminConfigServiceError as exc:
            return await _admin_service_error(services, exc)
        except Exception:
            ctx.logger.exception(
                "miniapp admin_config_get failed session_uid=%s",
                session_uid,
            )
            return await services.json_error(500, "admin config read failed")
        return web.json_response({
            "ok": True,
            "config_path": result["config_path"],
            "yaml": result["yaml"],
        })

    async def admin_config_put(request: web.Request) -> web.Response:
        await services.require_admin(request)
        try:
            body = await services.read_json_object(request)
        except web.HTTPException as exc:
            return await services.json_error(int(exc.status), str(exc.reason or "invalid request"))
        session_uid = str(body.get("session_uid", "") or "").strip()
        yaml_text = str(body.get("yaml", "") or "")
        expected_revision = body.get("expected_revision")
        try:
            await asyncio.to_thread(
                services.admin_config_service.save_yaml,
                session_uid,
                yaml_text,
                expected_revision,
            )
        except AdminConfigServiceError as exc:
            return await _admin_service_error(services, exc)
        except Exception:
            ctx.logger.exception(
                "miniapp admin_config_put failed session_uid=%s",
                session_uid,
            )
            return await services.json_error(500, "admin config write failed")
        return web.json_response({"ok": True})

    async def admin_monitor_servers_get(request: web.Request) -> web.Response:
        await services.require_admin(request)
        session_uid = str(request.query.get("session_uid", "") or "").strip()
        try:
            result = await asyncio.to_thread(services.admin_config_service.get_monitor_servers, session_uid)
        except AdminConfigServiceError as exc:
            return await _admin_service_error(services, exc)
        except Exception:
            ctx.logger.exception(
                "miniapp admin_monitor_servers_get failed session_uid=%s",
                session_uid,
            )
            return await services.json_error(500, "admin monitor servers read failed")
        payload = {"ok": True}
        payload.update(result)
        return web.json_response(payload)

    async def admin_monitor_servers_put(request: web.Request) -> web.Response:
        await services.require_admin(request)
        try:
            body = await services.read_json_object(request)
        except web.HTTPException as exc:
            return await services.json_error(int(exc.status), str(exc.reason or "invalid request"))
        session_uid = str(body.get("session_uid", "") or "").strip()
        try:
            await asyncio.to_thread(
                services.admin_config_service.save_monitor_servers,
                session_uid,
                body,
            )
        except AdminConfigServiceError as exc:
            return await _admin_service_error(services, exc)
        except Exception:
            ctx.logger.exception(
                "miniapp admin_monitor_servers_put failed session_uid=%s",
                session_uid,
            )
            return await services.json_error(500, "admin monitor servers write failed")
        return web.json_response({"ok": True})

    app.router.add_get("/api/v1/admin/config", admin_config_get)
    app.router.add_put("/api/v1/admin/config", admin_config_put)
    app.router.add_get("/api/v1/admin/monitor/servers", admin_monitor_servers_get)
    app.router.add_put("/api/v1/admin/monitor/servers", admin_monitor_servers_put)
