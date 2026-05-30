from __future__ import annotations

import asyncio
import datetime
import os
import re
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, List

from aiohttp import web

from .route_context import MiniAppRouteContext
from .services.logs_service import LogEntryAccumulator, LogsService, LogsServiceError


RequireAccess = Callable[[web.Request], Awaitable[Dict[str, Any]]]
JsonError = Callable[[int, Any], Awaitable[web.Response]]
IssueWsTicket = Callable[[Dict[str, Any]], str]
ConsumeWsTicket = Callable[[str], Dict[str, Any]]
ValidateSessionUid = Callable[[Any], str]
ConsumeWsMessages = Callable[[web.WebSocketResponse], Awaitable[None]]


@dataclass(frozen=True)
class LogsRouteServices:
    logs: LogsService
    require_access: RequireAccess
    json_error: JsonError
    issue_ws_ticket: IssueWsTicket
    consume_ws_ticket: ConsumeWsTicket
    validate_session_uid: ValidateSessionUid
    consume_ws_messages: ConsumeWsMessages
    ws_ticket_ttl_sec: int


def _compact_log_text(raw_text: str) -> str:
    lines = str(raw_text or "").split("\n")
    if not lines:
        return ""
    lines[0] = re.sub(r"\s+\[chat=[^\]]+\]", "", lines[0])
    return "\n".join(lines)


def _history_limit(value: Any) -> int:
    try:
        return int(str(value or "0").strip())
    except Exception as exc:
        raise web.HTTPBadRequest(reason="history must be an integer") from exc


def _validate_history_option(services: LogsRouteServices, history_limit: int) -> None:
    if int(history_limit) not in set(services.logs.history_options):
        raise web.HTTPBadRequest(reason="unsupported history option")


def _session_uid_filter(
    services: LogsRouteServices,
    request: web.Request,
) -> str | None:
    session_uid_filter = str(request.query.get("session_uid", "") or "").strip() or None
    if session_uid_filter:
        session_uid_filter = services.validate_session_uid(session_uid_filter) or None
    return session_uid_filter


def _session_id_filter(request: web.Request) -> str | None:
    return str(request.query.get("session_id", "") or "").strip() or None


async def _download_user(
    services: LogsRouteServices,
    request: web.Request,
) -> Dict[str, Any] | web.Response:
    ticket = str(request.query.get("ticket", "") or "").strip()
    if ticket:
        try:
            return services.consume_ws_ticket(ticket)
        except web.HTTPException as exc:
            return web.Response(status=int(exc.status), text=str(exc.reason or "unauthorized"))
    try:
        return await services.require_access(request)
    except web.HTTPException as exc:
        return web.Response(status=int(exc.status), text=str(exc.reason or "unauthorized"))


async def _run_logs_ws_stream(
    ctx: MiniAppRouteContext,
    services: LogsRouteServices,
    ws: web.WebSocketResponse,
    *,
    user: Dict[str, Any],
    log_type: str,
    session_uid_filter: str | None,
    session_id_filter: str | None,
) -> None:
    receive_task = asyncio.create_task(services.consume_ws_messages(ws))
    stream_task = asyncio.create_task(
        _stream_log_updates(
            ctx,
            services,
            ws,
            user=user,
            log_type=log_type,
            session_uid_filter=session_uid_filter,
            session_id_filter=session_id_filter,
        )
    )
    try:
        done, pending = await asyncio.wait(
            {receive_task, stream_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        for task in done:
            exc = task.exception()
            if exc is not None:
                raise exc
    finally:
        for task in (receive_task, stream_task):
            if not task.done():
                task.cancel()
        await asyncio.gather(receive_task, stream_task, return_exceptions=True)


def _read_available_lines(
    path: str,
    stream,
    stream_inode,
    start_pos: int,
) -> tuple:
    """Open (if needed) and drain all available lines from the log file.

    Returns (stream, stream_inode, start_pos, lines) where ``lines`` is a
    list of raw text lines read during this call.
    """
    if stream is None and os.path.exists(path):
        stream = open(path, "r", encoding="utf-8", errors="replace")
        stream.seek(max(0, int(start_pos)))
        stream_inode = os.fstat(stream.fileno()).st_ino
        start_pos = int(stream.tell())

    lines: List[str] = []
    if stream is not None:
        while True:
            raw_line = stream.readline()
            if not raw_line:
                break
            lines.append(raw_line)

    return stream, stream_inode, start_pos, lines


def _check_stream_rotation(
    path: str,
    stream,
    stream_inode,
    start_pos: int,
) -> tuple:
    """Detect log file rotation/truncation and close the stream if needed.

    Returns (stream, stream_inode, start_pos).
    """
    if stream is None:
        return stream, stream_inode, start_pos
    try:
        st = os.stat(path)
        current_pos = int(stream.tell())
        if st.st_ino != stream_inode or int(st.st_size) < current_pos:
            stream.close()
            start_pos = 0 if st.st_size < current_pos else current_pos
            return None, None, start_pos
    except FileNotFoundError:
        stream.close()
        return None, None, 0
    return stream, stream_inode, start_pos


async def _stream_log_updates(
    ctx: MiniAppRouteContext,
    services: LogsRouteServices,
    ws: web.WebSocketResponse,
    *,
    user: Dict[str, Any],
    log_type: str,
    session_uid_filter: str | None,
    session_id_filter: str | None,
) -> None:
    path = services.logs.resolve_log_path(log_type)
    user_id = int(user["user_id"])
    is_admin = bool(user.get("is_admin", False))

    start_pos = services.logs.file_end_position(log_type)
    stream = None
    stream_inode = None
    accumulator = LogEntryAccumulator()
    idle_flush_sec = 0.25
    keepalive_sec = 15.0
    last_emit_ts = time.monotonic()

    allowed_session_uids = None
    allowed_session_pairs = None
    last_permissions_ts = 0.0
    permissions_ttl = 30.0

    while not ws.closed:
        try:
            now_ts = time.monotonic()
            if now_ts - last_permissions_ts >= permissions_ttl:
                allowed_session_uids = services.logs.allowed_session_uids(user_id=user_id, is_admin=is_admin)
                allowed_session_pairs = services.logs.allowed_session_pairs(user_id=user_id, is_admin=is_admin)
                last_permissions_ts = now_ts

            stream, stream_inode, start_pos, raw_lines = await asyncio.to_thread(
                _read_available_lines, path, stream, stream_inode, start_pos
            )

            append_payload: List[Dict[str, str]] = []
            for raw_line in raw_lines:
                completed = accumulator.feed_line(raw_line)
                for entry in completed:
                    if services.logs.entry_allowed(
                        entry,
                        user_id=user_id,
                        is_admin=is_admin,
                        session_uid_filter=session_uid_filter,
                        session_id_filter=session_id_filter,
                        allowed_session_uids=allowed_session_uids,
                        allowed_session_pairs=allowed_session_pairs,
                    ):
                        append_payload.append(entry.to_payload())

            if stream is not None:
                stale = accumulator.flush_stale(now=now_ts, idle_sec=idle_flush_sec)
                if stale is not None and services.logs.entry_allowed(
                    stale,
                    user_id=user_id,
                    is_admin=is_admin,
                    session_uid_filter=session_uid_filter,
                    session_id_filter=session_id_filter,
                    allowed_session_uids=allowed_session_uids,
                    allowed_session_pairs=allowed_session_pairs,
                ):
                    append_payload.append(stale.to_payload())

            if append_payload:
                await ws.send_json({"type": "append", "entries": append_payload})
                last_emit_ts = now_ts

            if now_ts - last_emit_ts >= keepalive_sec:
                await ws.send_json({"type": "keepalive"})
                last_emit_ts = now_ts

            stream, stream_inode, start_pos = await asyncio.to_thread(
                _check_stream_rotation, path, stream, stream_inode, start_pos
            )

            await asyncio.sleep(0.25)
        except asyncio.CancelledError:
            raise
        except Exception:
            if ws.closed:
                break
            ctx.logger.exception("miniapp logs stream iteration failed")
            await asyncio.sleep(0.5)

    if stream is not None:
        stream.close()


def register_logs_routes(
    app: web.Application,
    ctx: MiniAppRouteContext,
    services: LogsRouteServices,
) -> None:
    async def logs_meta(request: web.Request) -> web.Response:
        user = await services.require_access(request)
        session_filters = await asyncio.to_thread(
            services.logs.list_session_filters,
            user_id=int(user["user_id"]),
            is_admin=bool(user.get("is_admin", False)),
        )
        log_types = await asyncio.to_thread(
            services.logs.list_log_types,
            include_paths=bool(user.get("is_admin", False)),
        )
        return web.json_response(
            {
                "log_types": log_types,
                "history_options": services.logs.history_options,
                "sessions": session_filters,
                "is_admin": bool(user.get("is_admin", False)),
                "execution_target": "local",
            }
        )

    async def logs_ws_ticket(request: web.Request) -> web.Response:
        user = await services.require_access(request)
        ticket = services.issue_ws_ticket(user)
        return web.json_response({"ticket": ticket, "expires_in": int(services.ws_ticket_ttl_sec)})

    async def logs_download(request: web.Request) -> web.StreamResponse:
        user = await _download_user(services, request)
        if isinstance(user, web.Response):
            return user

        log_type = str(request.query.get("log_type", "main") or "main").strip().lower()
        try:
            session_uid_filter = _session_uid_filter(services, request)
        except web.HTTPException as exc:
            return web.Response(status=int(exc.status), text=str(exc.reason or "bad request"))
        session_id_filter = _session_id_filter(request)
        try:
            history_limit = _history_limit(request.query.get("history", request.query.get("history_limit", "0")))
            _validate_history_option(services, history_limit)
        except web.HTTPException as exc:
            return web.Response(status=int(exc.status), text=str(exc.reason or "bad request"))

        try:
            services.logs.resolve_log_path(log_type)
            services.logs.ensure_session_scope_allowed(
                user_id=int(user["user_id"]),
                is_admin=bool(user.get("is_admin", False)),
                session_uid=session_uid_filter,
                session_id=session_id_filter,
            )
            entries = await asyncio.to_thread(
                services.logs.read_history,
                log_type=log_type,
                history_limit=int(history_limit),
                user_id=int(user["user_id"]),
                is_admin=bool(user.get("is_admin", False)),
                session_uid_filter=session_uid_filter,
                session_id_filter=session_id_filter,
            )
        except LogsServiceError as exc:
            return web.Response(status=int(getattr(exc, "status", 400)), text=str(exc))

        text = "\n".join(
            _compact_log_text(str(item.get("text", "") or "")).rstrip()
            for item in entries
            if str(item.get("text", "") or "").strip()
        )
        safe_log_type = re.sub(r"[^a-z0-9_-]+", "-", log_type).strip("-") or "log"
        stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        filename = f"miniapp-{safe_log_type}-{stamp}.log"
        return web.Response(
            text=text,
            content_type="text/plain",
            charset="utf-8",
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
            },
        )

    async def logs_ws(request: web.Request) -> web.StreamResponse:
        ticket = str(request.query.get("ticket", "") or "").strip()
        if ticket:
            try:
                user = services.consume_ws_ticket(ticket)
            except web.HTTPException as exc:
                return await services.json_error(int(exc.status), str(exc.reason or "unauthorized"))
        else:
            user = await services.require_access(request)
        log_type = str(request.query.get("log_type", "main") or "main").strip().lower()
        try:
            session_uid_filter = _session_uid_filter(services, request)
        except web.HTTPException as exc:
            return await services.json_error(int(exc.status), str(exc.reason or "bad request"))
        session_id_filter = _session_id_filter(request)

        try:
            history_limit = _history_limit(request.query.get("history", request.query.get("history_limit", "0")))
            _validate_history_option(services, history_limit)
        except web.HTTPException as exc:
            return await services.json_error(int(exc.status), str(exc.reason or "bad request"))

        try:
            services.logs.resolve_log_path(log_type)
            services.logs.ensure_session_scope_allowed(
                user_id=int(user["user_id"]),
                is_admin=bool(user.get("is_admin", False)),
                session_uid=session_uid_filter,
                session_id=session_id_filter,
            )
        except LogsServiceError as exc:
            return await services.json_error(getattr(exc, "status", 400), str(exc))

        ws = web.WebSocketResponse(heartbeat=20.0)
        await ws.prepare(request)

        try:
            history_entries = await asyncio.to_thread(
                services.logs.read_history,
                log_type=log_type,
                history_limit=int(history_limit),
                user_id=int(user["user_id"]),
                is_admin=bool(user.get("is_admin", False)),
                session_uid_filter=session_uid_filter,
                session_id_filter=session_id_filter,
            )
            await ws.send_json({"type": "snapshot", "entries": history_entries})
            await _run_logs_ws_stream(
                ctx,
                services,
                ws,
                user=user,
                log_type=log_type,
                session_uid_filter=session_uid_filter,
                session_id_filter=session_id_filter,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            ctx.logger.exception(
                "miniapp logs websocket failed",
                extra={
                    "chat_id": int(user["user_id"]),
                    "user_id": int(user["user_id"]),
                    "action": "logs_ws",
                    "path": log_type,
                    "status": "error",
                    "error": "",
                },
            )
            if not ws.closed:
                await ws.send_json({"type": "error", "error": "log stream failed"})
        finally:
            if not ws.closed:
                await ws.close()
        return ws

    app.router.add_get("/api/logs/meta", logs_meta)
    app.router.add_get("/api/logs/ws_ticket", logs_ws_ticket)
    app.router.add_get("/api/logs/download", logs_download)
    app.router.add_get("/api/logs/ws", logs_ws)
