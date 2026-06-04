from __future__ import annotations

import asyncio
import datetime
import os
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, List, Optional

from aiohttp import web

from session import session_runtime_uid
from utils.paths import cli_proxy_artifact_path

from .route_context import MiniAppRouteContext


RequireAccess = Callable[[web.Request], Awaitable[Dict[str, Any]]]
JsonError = Callable[[int, Any], Awaitable[web.Response]]

_REPORTS_ARTIFACT = ".manager_reports"
_ALLOWED_EXTS = {".md"}


@dataclass(frozen=True)
class ReportsRouteServices:
    require_access: RequireAccess
    json_error: JsonError


def _collect_visible_sessions(
    bot_app: Any, *, user_id: int, is_admin: bool
) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    manager = getattr(bot_app, "manager", None)
    if manager is None:
        return out
    if is_admin:
        by_chat = dict(getattr(manager, "sessions_by_chat", {}) or {})
        for by_id in by_chat.values():
            if not isinstance(by_id, dict):
                continue
            for session in by_id.values():
                suid = session_runtime_uid(session)
                if suid:
                    out[suid] = session
        return out
    try:
        by_id = dict(manager.sessions_for_chat(int(user_id)) or {})
    except Exception:
        return out
    for session in by_id.values():
        suid = session_runtime_uid(session)
        if suid:
            out[suid] = session
    return out


def _reports_dir(session: Any) -> Optional[str]:
    workdir = str(getattr(session, "workdir", "") or "").strip()
    if not workdir:
        return None
    return cli_proxy_artifact_path(workdir, _REPORTS_ARTIFACT)


def _scan_reports(reports_dir: str) -> List[Dict[str, Any]]:
    """Return report metadata sorted newest-first."""
    if not os.path.isdir(reports_dir):
        return []
    entries: List[Dict[str, Any]] = []
    try:
        for entry in os.scandir(reports_dir):
            if not entry.is_file():
                continue
            _, ext = os.path.splitext(entry.name.lower())
            if ext not in _ALLOWED_EXTS:
                continue
            stat = entry.stat()
            entries.append(
                {
                    "id": entry.name,
                    "name": entry.name,
                    "size": stat.st_size,
                    "mtime": stat.st_mtime,
                    "date": datetime.datetime.fromtimestamp(
                        stat.st_mtime, tz=datetime.timezone.utc
                    ).strftime("%Y-%m-%d %H:%M UTC"),
                }
            )
    except OSError:
        return []
    entries.sort(key=lambda e: e["mtime"], reverse=True)
    return entries


def _safe_report_id(value: str) -> Optional[str]:
    """Validate report_id: filename only, no path traversal, allowed ext."""
    name = os.path.basename(str(value or "").strip())
    if not name:
        return None
    _, ext = os.path.splitext(name.lower())
    if ext not in _ALLOWED_EXTS:
        return None
    return name


def register_reports_routes(
    app: web.Application,
    ctx: MiniAppRouteContext,
    services: ReportsRouteServices,
) -> None:
    async def reports_list(request: web.Request) -> web.Response:
        try:
            user = await services.require_access(request)
        except web.HTTPException as exc:
            return await services.json_error(int(exc.status), str(exc.reason or "unauthorized"))

        session_uid_filter = str(request.query.get("session_uid", "") or "").strip() or None
        try:
            sessions = _collect_visible_sessions(
                ctx.bot_app,
                user_id=int(user["user_id"]),
                is_admin=bool(user.get("is_admin", False)),
            )
            if session_uid_filter:
                if session_uid_filter not in sessions:
                    return await services.json_error(403, "session not accessible")
                sessions = {session_uid_filter: sessions[session_uid_filter]}

            result: List[Dict[str, Any]] = []
            for suid, session in sessions.items():
                rdir = _reports_dir(session)
                if not rdir:
                    continue
                reports = await asyncio.to_thread(_scan_reports, rdir)
                for rep in reports:
                    result.append({**rep, "session_uid": suid})
            result.sort(key=lambda r: r["mtime"], reverse=True)
            return web.json_response({"ok": True, "reports": result})
        except Exception:
            ctx.logger.exception("miniapp reports list failed")
            return await services.json_error(500, "reports list failed")

    async def reports_content(request: web.Request) -> web.Response:
        try:
            user = await services.require_access(request)
        except web.HTTPException as exc:
            return await services.json_error(int(exc.status), str(exc.reason or "unauthorized"))

        report_id = _safe_report_id(str(request.match_info.get("report_id", "") or ""))
        if not report_id:
            return await services.json_error(400, "invalid report_id")

        session_uid = str(request.query.get("session_uid", "") or "").strip()
        if not session_uid:
            return await services.json_error(400, "session_uid is required")

        try:
            sessions = _collect_visible_sessions(
                ctx.bot_app,
                user_id=int(user["user_id"]),
                is_admin=bool(user.get("is_admin", False)),
            )
            if session_uid not in sessions:
                return await services.json_error(403, "session not accessible")

            rdir = _reports_dir(sessions[session_uid])
            if not rdir:
                return await services.json_error(404, "no reports directory for session")

            path = os.path.join(rdir, report_id)
            if not os.path.isfile(path):
                return await services.json_error(404, "report not found")

            def _read() -> str:
                with open(path, "r", encoding="utf-8") as f:
                    return f.read()

            content = await asyncio.to_thread(_read)
            return web.json_response({"ok": True, "id": report_id, "content": content})
        except Exception:
            ctx.logger.exception("miniapp reports content failed report_id=%s", report_id)
            return await services.json_error(500, "report read failed")

    async def reports_download(request: web.Request) -> web.Response:
        try:
            user = await services.require_access(request)
        except web.HTTPException as exc:
            return web.Response(status=int(exc.status), text=str(exc.reason or "unauthorized"))

        report_id = _safe_report_id(str(request.match_info.get("report_id", "") or ""))
        if not report_id:
            return web.Response(status=400, text="invalid report_id")

        session_uid = str(request.query.get("session_uid", "") or "").strip()
        if not session_uid:
            return web.Response(status=400, text="session_uid is required")

        fmt = str(request.query.get("format", "md") or "md").strip().lower()
        if fmt not in ("md", "pdf"):
            fmt = "md"

        try:
            sessions = _collect_visible_sessions(
                ctx.bot_app,
                user_id=int(user["user_id"]),
                is_admin=bool(user.get("is_admin", False)),
            )
            if session_uid not in sessions:
                return web.Response(status=403, text="session not accessible")

            rdir = _reports_dir(sessions[session_uid])
            if not rdir:
                return web.Response(status=404, text="no reports directory for session")

            path = os.path.join(rdir, report_id)
            if not os.path.isfile(path):
                return web.Response(status=404, text="report not found")

            if fmt == "pdf":
                # PDF conversion not available in MiniApp — serve MD with note
                return web.Response(
                    status=200,
                    text=(
                        "PDF export is not available in MiniApp. "
                        "Please download as MD and convert locally."
                    ),
                    content_type="text/plain",
                )

            def _read_bytes() -> bytes:
                with open(path, "rb") as f:
                    return f.read()

            data = await asyncio.to_thread(_read_bytes)
            return web.Response(
                body=data,
                content_type="text/markdown",
                headers={
                    "Content-Disposition": f'attachment; filename="{report_id}"',
                    "Cache-Control": "no-store",
                },
            )
        except Exception:
            ctx.logger.exception(
                "miniapp reports download failed report_id=%s", report_id
            )
            return web.Response(status=500, text="report download failed")

    app.router.add_get("/api/reports", reports_list)
    app.router.add_get("/api/reports/{report_id}", reports_content)
    app.router.add_get("/api/reports/{report_id}/download", reports_download)
