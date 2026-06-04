from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, List

from aiohttp import web

from .route_context import MiniAppRouteContext
from .session_visibility import collect_visible_sessions


RequireAccess = Callable[[web.Request], Awaitable[Dict[str, Any]]]
JsonError = Callable[[int, Any], Awaitable[web.Response]]


@dataclass(frozen=True)
class TasksRouteServices:
    require_access: RequireAccess
    json_error: JsonError


def _list_tasks(bot_app: Any, *, sessions: Dict[str, Any]) -> List[Dict[str, Any]]:
    mode_tasks = getattr(bot_app, "mode_tasks", None)
    if mode_tasks is None:
        return []
    tasks_store = getattr(mode_tasks, "tasks", {}) or {}
    out: List[Dict[str, Any]] = []
    seen_uids = set(sessions.keys())
    for (session_uid, mode_id), records in list(tasks_store.items()):
        if session_uid not in seen_uids:
            continue
        for rec in list(records):
            task_obj = getattr(rec, "task", None)
            if task_obj is None or task_obj.done():
                continue
            out.append({
                "session_uid": str(session_uid),
                "mode_id": str(mode_id),
                "name": str(getattr(rec, "name", "") or ""),
                "status": "running",
            })
    return out


def register_tasks_routes(
    app: web.Application,
    ctx: MiniAppRouteContext,
    services: TasksRouteServices,
) -> None:
    async def tasks_list(request: web.Request) -> web.Response:
        try:
            user = await services.require_access(request)
        except web.HTTPException as exc:
            return await services.json_error(int(exc.status), str(exc.reason or "unauthorized"))
        try:
            session_uid_filter = str(request.query.get("session_uid", "") or "").strip() or None
            sessions = collect_visible_sessions(
                ctx.bot_app,
                user_id=int(user["user_id"]),
                is_admin=bool(user.get("is_admin", False)),
            )
            if session_uid_filter:
                if session_uid_filter not in sessions:
                    return await services.json_error(403, "session not accessible")
                sessions = {session_uid_filter: sessions[session_uid_filter]}
            tasks = _list_tasks(ctx.bot_app, sessions=sessions)
            return web.json_response({"ok": True, "tasks": tasks})
        except Exception:
            ctx.logger.exception("miniapp tasks list failed")
            return await services.json_error(500, "tasks list failed")

    async def tasks_cancel(request: web.Request) -> web.Response:
        try:
            user = await services.require_access(request)
        except web.HTTPException as exc:
            return await services.json_error(int(exc.status), str(exc.reason or "unauthorized"))
        session_uid = str(request.match_info.get("session_uid", "") or "").strip()
        if not session_uid:
            return await services.json_error(400, "session_uid is required")
        try:
            sessions = collect_visible_sessions(
                ctx.bot_app,
                user_id=int(user["user_id"]),
                is_admin=bool(user.get("is_admin", False)),
            )
            if session_uid not in sessions:
                return await services.json_error(403, "session not accessible")
            mode_tasks = getattr(ctx.bot_app, "mode_tasks", None)
            cancelled = 0
            if mode_tasks is not None:
                cancelled = await mode_tasks.cancel_session(
                    session_uid=session_uid,
                    timeout_s=1.0,
                )
            return web.json_response({"ok": True, "cancelled": cancelled, "session_uid": session_uid})
        except Exception:
            ctx.logger.exception("miniapp tasks cancel failed session_uid=%s", session_uid)
            return await services.json_error(500, "tasks cancel failed")

    app.router.add_get("/api/tasks", tasks_list)
    app.router.add_post("/api/tasks/{session_uid}/cancel", tasks_cancel)
