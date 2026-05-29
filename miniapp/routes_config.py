from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict

from aiohttp import web

from app.services.config_service import ConfigDraftSaveResult, ConfigService

from .route_context import MiniAppRouteContext
from .services.config_service import (
    app_config_to_dict,
    config_schema,
    config_view_with_revision,
    draft_diff,
    restore_redacted_secret_values,
    validate_draft,
)


RequireAdmin = Callable[[web.Request], Awaitable[Dict[str, Any]]]
ReadJsonObject = Callable[[web.Request], Awaitable[Dict[str, Any]]]
JsonError = Callable[[int, Any], Awaitable[web.Response]]


@dataclass(frozen=True)
class ConfigRouteServices:
    config_service: ConfigService
    require_admin: RequireAdmin
    read_json_object: ReadJsonObject
    json_error: JsonError


def _empty_field_diff() -> Dict[str, Any]:
    return {"changed": [], "restart_required": [], "reloadable": [], "not_applied": [], "secret_changed": []}


def _save_response_payload(
    result: ConfigDraftSaveResult,
    *,
    field_diff: Dict[str, Any],
    reload_result: Dict[str, Any] | None,
) -> Dict[str, Any]:
    payload = {
        "ok": bool(result.ok),
        "revision": result.revision,
        "diff": field_diff,
        "changed": bool(result.changed),
        "restart_required": list(result.restart_required),
        "reloadable": list(result.reloadable),
        "not_applied": list(result.not_applied),
        "secret_changed": list(result.secret_changed),
        "errors": list(result.errors),
        "backup_path": result.backup_path,
    }
    if reload_result is not None:
        payload["reload"] = reload_result
        if reload_result.get("status") == "error":
            payload["ok"] = False
    return payload


def _is_revision_conflict(result: ConfigDraftSaveResult) -> bool:
    return not result.ok and result.errors == ["revision mismatch"]


def register_config_routes(
    app: web.Application,
    ctx: MiniAppRouteContext,
    services: ConfigRouteServices,
) -> None:
    async def config_schema_view(request: web.Request) -> web.Response:
        await services.require_admin(request)
        return web.json_response(config_schema())

    async def config_view(request: web.Request) -> web.Response:
        await services.require_admin(request)
        payload = config_view_with_revision(ctx.bot_app.config)
        payload["execution_target"] = "local"
        return web.json_response(payload)

    async def config_validate(request: web.Request) -> web.Response:
        user = await services.require_admin(request)
        try:
            body = await services.read_json_object(request)
        except web.HTTPException as exc:
            return await services.json_error(int(exc.status), str(exc.reason or "invalid request"))
        draft = body.get("draft")
        ok, errors, warnings = validate_draft(ctx.bot_app.config.path, draft)
        ctx.logger.info(
            "miniapp config validate",
            extra={
                "chat_id": user["user_id"],
                "user_id": user["user_id"],
                "action": "config_validate",
                "path": "config.yaml",
                "status": "ok" if ok else "error",
                "error": "; ".join(errors),
            },
        )
        return web.json_response({"ok": ok, "errors": errors, "warnings": warnings})

    async def config_diff(request: web.Request) -> web.Response:
        await services.require_admin(request)
        try:
            body = await services.read_json_object(request)
        except web.HTTPException as exc:
            return await services.json_error(int(exc.status), str(exc.reason or "invalid request"))
        draft = body.get("draft")
        if not isinstance(draft, dict):
            return await services.json_error(400, "draft must be an object")
        current = config_view_with_revision(ctx.bot_app.config).get("config", {})
        return web.json_response(draft_diff(current, draft))

    async def config_save(request: web.Request) -> web.Response:
        user = await services.require_admin(request)
        try:
            body = await services.read_json_object(request)
        except web.HTTPException as exc:
            return await services.json_error(int(exc.status), str(exc.reason or "invalid request"))
        draft = body.get("draft")
        expected_revision = body.get("expected_revision")
        if not isinstance(draft, dict):
            return await services.json_error(400, "draft must be an object")

        current_plain = app_config_to_dict(ctx.bot_app.config)
        current_redacted = config_view_with_revision(ctx.bot_app.config).get("config", {})
        field_diff = draft_diff(current_redacted, draft)
        draft_for_save = restore_redacted_secret_values(current_plain, draft)
        try:
            await services.config_service.load()
            result = await services.config_service.save_draft_with_revision(
                draft_for_save,
                expected_revision=expected_revision,
            )
        except Exception as exc:
            ctx.logger.exception("miniapp config save failed")
            return await services.json_error(500, str(exc))

        if _is_revision_conflict(result):
            return await services.json_error(409, "revision mismatch")
        if not result.ok:
            payload = _save_response_payload(
                result,
                field_diff=_empty_field_diff(),
                reload_result=None,
            )
            return web.json_response(payload, status=400)

        reload_result = await ctx.bot_app.reload_runtime_config()
        ctx.logger.info(
            "miniapp config save",
            extra={
                "chat_id": user["user_id"],
                "user_id": user["user_id"],
                "action": "config_save",
                "path": "config.yaml",
                "status": "ok" if reload_result.get("status") != "error" else "error",
                "error": "",
            },
        )
        payload = _save_response_payload(
            result,
            field_diff=field_diff,
            reload_result=reload_result,
        )
        return web.json_response(payload)

    app.router.add_get("/api/config/schema", config_schema_view)
    app.router.add_get("/api/config/view", config_view)
    app.router.add_post("/api/config/validate", config_validate)
    app.router.add_post("/api/config/diff", config_diff)
    app.router.add_post("/api/config/save", config_save)
