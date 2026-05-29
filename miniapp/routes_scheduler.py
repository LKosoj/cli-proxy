from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, List

from aiohttp import web

from app.services.scheduler_presentation_service import SchedulerPresentationService
from app.services.scheduler_service import (
    SchedulerNotFoundError,
    SchedulerOwnershipError,
    SchedulerValidationError,
)

from .route_context import MiniAppRouteContext


RequireAccess = Callable[[web.Request], Awaitable[Dict[str, Any]]]
ReadJsonObject = Callable[[web.Request], Awaitable[Dict[str, Any]]]
RequireObjectBody = Callable[[Any], Dict[str, Any]]
JsonError = Callable[[int, Any], Awaitable[web.Response]]
SchedulerServiceDependency = Any | Callable[[], Any]
ListOwnedProjects = Callable[..., List[Dict[str, Any]]]
RequireOwnedProject = Callable[..., Dict[str, Any]]
ListNotificationTargets = Callable[..., List[Dict[str, Any]]]
RequireNotificationTarget = Callable[..., str]


@dataclass(frozen=True)
class SchedulerRouteServices:
    scheduler_service: SchedulerServiceDependency
    presentation_service: SchedulerPresentationService
    require_access: RequireAccess
    read_json_object: ReadJsonObject
    require_object_body: RequireObjectBody
    json_error: JsonError
    list_owned_projects: ListOwnedProjects
    require_owned_project: RequireOwnedProject
    list_notification_targets: ListNotificationTargets
    require_notification_target: RequireNotificationTarget


def _serialize_scheduled_event(event: Any) -> Dict[str, Any]:
    return {
        "job_id": str(getattr(event, "job_id", "") or ""),
        "job_name": str(getattr(event, "job_name", "") or ""),
        "status": str(getattr(event, "status", "") or ""),
        "scheduled_for": float(getattr(event, "scheduled_for", 0.0) or 0.0),
        "cron": str(getattr(event, "cron", "") or ""),
        "target_mode": str(getattr(event, "target_mode", "") or ""),
        "owner_id": str(getattr(event, "owner_id", "") or ""),
        "notification_target": dict(getattr(event, "notification_target", {}) or {}),
        "payload": dict(getattr(event, "payload", {}) or {}),
    }


def _current_notification_target_uid(job: Any) -> str:
    notification_target = getattr(job, "notification_target", None)
    if hasattr(notification_target, "telegram_session_uid"):
        return str(getattr(notification_target, "telegram_session_uid", "") or "")
    return str(dict(notification_target or {}).get("telegram_session_uid") or "")


def _scheduler_service(services: SchedulerRouteServices) -> Any:
    service = services.scheduler_service
    if callable(service) and not hasattr(service, "list_jobs"):
        service = service()
    return service


async def _body_or_error(
    services: SchedulerRouteServices,
    request: web.Request,
) -> Dict[str, Any] | web.Response:
    try:
        return await services.read_json_object(request)
    except web.HTTPException as exc:
        return await services.json_error(int(exc.status), str(exc.reason or "invalid request"))


def _require_project_job(
    services: SchedulerRouteServices,
    *,
    owner_id: str,
    project_slug: str,
    job_id: str,
) -> Any:
    return services.presentation_service.require_project_job(
        project_slug,
        job_id,
        owner_id=owner_id,
    )


def register_scheduler_routes(
    app: web.Application,
    ctx: MiniAppRouteContext,
    services: SchedulerRouteServices,
) -> None:
    async def scheduler_jobs_list(request: web.Request) -> web.Response:
        user = await services.require_access(request)
        project_slug = str(request.query.get("project_slug", "") or "").strip()
        try:
            projects = services.list_owned_projects(user_id=int(user["user_id"]))
            selected_project = None
            jobs: List[Dict[str, Any]] = []
            notification_targets: List[Dict[str, Any]] = []
            if project_slug:
                selected_project = services.require_owned_project(
                    user_id=int(user["user_id"]),
                    project_slug=project_slug,
                )
                notification_targets = services.list_notification_targets(
                    user_id=int(user["user_id"]),
                    project_path=str(selected_project.get("path") or ""),
                    is_admin=bool(user.get("is_admin", False)),
                )
                selected_slug = str(selected_project.get("slug") or "")
                jobs = [
                    services.presentation_service.serialize_job(job)
                    for job in _scheduler_service(services).list_jobs(owner_id=str(user["actor_id"]))
                    if services.presentation_service.project_slug_for_job(job) == selected_slug
                ]
            return web.json_response(
                {
                    "ok": True,
                    "projects": projects,
                    "selected_project_slug": str(selected_project.get("slug") or "") if selected_project else "",
                    "notification_targets": notification_targets,
                    "jobs": jobs,
                }
            )
        except web.HTTPException as exc:
            ctx.logger.warning(
                "miniapp scheduler list denied",
                extra={
                    "chat_id": int(user["user_id"]),
                    "user_id": int(user["user_id"]),
                    "action": "scheduler_list",
                    "path": project_slug or "-",
                    "status": "error",
                    "error": str(exc.reason or "request failed"),
                },
            )
            return await services.json_error(int(exc.status), str(exc.reason or "request failed"))
        except Exception:
            ctx.logger.exception("miniapp scheduler list failed")
            return await services.json_error(500, "scheduler list failed")

    async def scheduler_jobs_create(request: web.Request) -> web.Response:
        user = await services.require_access(request)
        body = await _body_or_error(services, request)
        if isinstance(body, web.Response):
            return body
        try:
            project = services.require_owned_project(
                user_id=int(user["user_id"]),
                project_slug=str(body.get("project_slug") or ""),
            )
            raw_payload = body.get("payload")
            payload = dict(raw_payload) if isinstance(raw_payload, dict) else {}
            payload["project_slug"] = str(project.get("slug") or "")
            notification_target = services.require_notification_target(
                user_id=int(user["user_id"]),
                is_admin=bool(user.get("is_admin", False)),
                project=project,
                telegram_session_uid=str(
                    services.require_object_body(
                        body.get("notification_target") or {}
                    ).get("telegram_session_uid") or ""
                ),
            )
            job = _scheduler_service(services).create_job(
                owner_id=str(user["actor_id"]),
                cron=str(body.get("cron") or ""),
                target_mode=str(body.get("target_mode") or ""),
                notification_target_telegram_session_uid=notification_target,
                payload=payload,
                enabled=bool(body.get("enabled", True)),
                job_id=str(body.get("job_id") or "").strip() or None,
                job_name=str(body.get("job_name") or "").strip() or None,
            )
            return web.json_response(
                {"ok": True, "job": services.presentation_service.serialize_job(job)}
            )
        except SchedulerValidationError as exc:
            ctx.logger.warning("miniapp scheduler create validation failed")
            return await services.json_error(400, str(exc))
        except web.HTTPException as exc:
            ctx.logger.warning("miniapp scheduler create denied")
            return await services.json_error(int(exc.status), str(exc.reason or "request failed"))
        except Exception:
            ctx.logger.exception("miniapp scheduler create failed")
            return await services.json_error(500, "scheduler create failed")

    async def scheduler_jobs_update(request: web.Request) -> web.Response:
        user = await services.require_access(request)
        body = await _body_or_error(services, request)
        if isinstance(body, web.Response):
            return body
        try:
            project = services.require_owned_project(
                user_id=int(user["user_id"]),
                project_slug=str(body.get("project_slug") or ""),
            )
            job_id = str(body.get("job_id") or "").strip()
            if not job_id:
                raise web.HTTPBadRequest(reason="job_id is required")
            current_job = _require_project_job(
                services,
                owner_id=str(user["actor_id"]),
                project_slug=str(project.get("slug") or ""),
                job_id=job_id,
            )
            notification_body = body.get("notification_target")
            notification_object = (
                services.require_object_body(notification_body)
                if notification_body is not None
                else {}
            )
            notification_target = services.require_notification_target(
                user_id=int(user["user_id"]),
                is_admin=bool(user.get("is_admin", False)),
                project=project,
                telegram_session_uid=str(
                    notification_object.get("telegram_session_uid")
                    or _current_notification_target_uid(current_job)
                    or ""
                ),
            )
            raw_payload = body.get("payload")
            payload = None
            if raw_payload is not None:
                payload = dict(raw_payload) if isinstance(raw_payload, dict) else {}
                payload["project_slug"] = str(project.get("slug") or "")
            job = _scheduler_service(services).update_job(
                owner_id=str(user["actor_id"]),
                job_id=job_id,
                cron=str(body.get("cron")) if body.get("cron") is not None else None,
                target_mode=str(body.get("target_mode")) if body.get("target_mode") is not None else None,
                notification_target_telegram_session_uid=notification_target,
                payload=payload,
                enabled=body.get("enabled") if body.get("enabled") is not None else None,
                job_name=str(body.get("job_name")).strip() if body.get("job_name") is not None else None,
            )
            return web.json_response(
                {"ok": True, "job": services.presentation_service.serialize_job(job)}
            )
        except SchedulerValidationError as exc:
            ctx.logger.warning("miniapp scheduler update validation failed")
            return await services.json_error(400, str(exc))
        except SchedulerOwnershipError as exc:
            ctx.logger.warning("miniapp scheduler update denied")
            return await services.json_error(403, str(exc))
        except SchedulerNotFoundError as exc:
            ctx.logger.warning("miniapp scheduler update missing")
            return await services.json_error(404, str(exc))
        except web.HTTPException as exc:
            ctx.logger.warning("miniapp scheduler update rejected")
            return await services.json_error(int(exc.status), str(exc.reason or "request failed"))
        except Exception:
            ctx.logger.exception("miniapp scheduler update failed")
            return await services.json_error(500, "scheduler update failed")

    async def scheduler_jobs_delete(request: web.Request) -> web.Response:
        user = await services.require_access(request)
        body = await _body_or_error(services, request)
        if isinstance(body, web.Response):
            return body
        try:
            project = services.require_owned_project(
                user_id=int(user["user_id"]),
                project_slug=str(body.get("project_slug") or ""),
            )
            job_id = str(body.get("job_id") or "").strip()
            if not job_id:
                raise web.HTTPBadRequest(reason="job_id is required")
            _require_project_job(
                services,
                owner_id=str(user["actor_id"]),
                project_slug=str(project.get("slug") or ""),
                job_id=job_id,
            )
            deleted = _scheduler_service(services).delete_job(
                owner_id=str(user["actor_id"]),
                job_id=job_id,
            )
            return web.json_response({"ok": bool(deleted), "job_id": job_id})
        except SchedulerOwnershipError as exc:
            ctx.logger.warning("miniapp scheduler delete denied")
            return await services.json_error(403, str(exc))
        except SchedulerNotFoundError as exc:
            ctx.logger.warning("miniapp scheduler delete missing")
            return await services.json_error(404, str(exc))
        except web.HTTPException as exc:
            ctx.logger.warning("miniapp scheduler delete rejected")
            return await services.json_error(int(exc.status), str(exc.reason or "request failed"))
        except Exception:
            ctx.logger.exception("miniapp scheduler delete failed")
            return await services.json_error(500, "scheduler delete failed")

    async def scheduler_jobs_run_now(request: web.Request) -> web.Response:
        user = await services.require_access(request)
        body = await _body_or_error(services, request)
        if isinstance(body, web.Response):
            return body
        try:
            project = services.require_owned_project(
                user_id=int(user["user_id"]),
                project_slug=str(body.get("project_slug") or ""),
            )
            job_id = str(body.get("job_id") or "").strip()
            if not job_id:
                raise web.HTTPBadRequest(reason="job_id is required")
            _require_project_job(
                services,
                owner_id=str(user["actor_id"]),
                project_slug=str(project.get("slug") or ""),
                job_id=job_id,
            )
            event = await _scheduler_service(services).run_now(
                owner_id=str(user["actor_id"]),
                job_id=job_id,
            )
            return web.json_response({"ok": True, "event": _serialize_scheduled_event(event)})
        except SchedulerOwnershipError as exc:
            ctx.logger.warning("miniapp scheduler run_now denied")
            return await services.json_error(403, str(exc))
        except SchedulerNotFoundError as exc:
            ctx.logger.warning("miniapp scheduler run_now missing")
            return await services.json_error(404, str(exc))
        except web.HTTPException as exc:
            ctx.logger.warning("miniapp scheduler run_now rejected")
            return await services.json_error(int(exc.status), str(exc.reason or "request failed"))
        except Exception:
            ctx.logger.exception("miniapp scheduler run_now failed")
            return await services.json_error(500, "scheduler run_now failed")

    async def scheduler_jobs_get(request: web.Request) -> web.Response:
        user = await services.require_access(request)
        project_slug = str(request.query.get("project_slug", "") or "").strip()
        job_id = str(request.query.get("job_id", "") or "").strip()
        try:
            project = services.require_owned_project(
                user_id=int(user["user_id"]),
                project_slug=project_slug,
            )
            if not job_id:
                raise web.HTTPBadRequest(reason="job_id is required")
            current_job = _require_project_job(
                services,
                owner_id=str(user["actor_id"]),
                project_slug=str(project.get("slug") or ""),
                job_id=job_id,
            )
            return web.json_response(
                {"ok": True, "job": services.presentation_service.serialize_job(current_job)}
            )
        except SchedulerOwnershipError as exc:
            return await services.json_error(403, str(exc))
        except SchedulerNotFoundError as exc:
            return await services.json_error(404, str(exc))
        except web.HTTPException as exc:
            return await services.json_error(int(exc.status), str(exc.reason or "request failed"))

    async def scheduler_jobs_pause(request: web.Request) -> web.Response:
        user = await services.require_access(request)
        body = await _body_or_error(services, request)
        if isinstance(body, web.Response):
            return body
        try:
            project = services.require_owned_project(
                user_id=int(user["user_id"]),
                project_slug=str(body.get("project_slug") or ""),
            )
            job_id = str(body.get("job_id") or "").strip()
            if not job_id:
                raise web.HTTPBadRequest(reason="job_id is required")
            _require_project_job(
                services,
                owner_id=str(user["actor_id"]),
                project_slug=str(project.get("slug") or ""),
                job_id=job_id,
            )
            job = _scheduler_service(services).pause_job_for_project(
                owner_id=str(user["actor_id"]),
                job_id=job_id,
                project_slug=str(project.get("slug") or ""),
            )
            return web.json_response(
                {"ok": True, "job": services.presentation_service.serialize_job(job)}
            )
        except SchedulerOwnershipError as exc:
            return await services.json_error(403, str(exc))
        except SchedulerNotFoundError as exc:
            return await services.json_error(404, str(exc))
        except web.HTTPException as exc:
            return await services.json_error(int(exc.status), str(exc.reason or "request failed"))

    async def scheduler_jobs_resume(request: web.Request) -> web.Response:
        user = await services.require_access(request)
        body = await _body_or_error(services, request)
        if isinstance(body, web.Response):
            return body
        try:
            project = services.require_owned_project(
                user_id=int(user["user_id"]),
                project_slug=str(body.get("project_slug") or ""),
            )
            job_id = str(body.get("job_id") or "").strip()
            if not job_id:
                raise web.HTTPBadRequest(reason="job_id is required")
            _require_project_job(
                services,
                owner_id=str(user["actor_id"]),
                project_slug=str(project.get("slug") or ""),
                job_id=job_id,
            )
            job = _scheduler_service(services).resume_job_for_project(
                owner_id=str(user["actor_id"]),
                job_id=job_id,
                project_slug=str(project.get("slug") or ""),
            )
            return web.json_response(
                {"ok": True, "job": services.presentation_service.serialize_job(job)}
            )
        except SchedulerOwnershipError as exc:
            return await services.json_error(403, str(exc))
        except SchedulerNotFoundError as exc:
            return await services.json_error(404, str(exc))
        except web.HTTPException as exc:
            return await services.json_error(int(exc.status), str(exc.reason or "request failed"))

    app.router.add_get("/api/v1/scheduler/jobs", scheduler_jobs_list)
    app.router.add_get("/api/v1/scheduler/job", scheduler_jobs_get)
    app.router.add_post("/api/v1/scheduler/jobs", scheduler_jobs_create)
    app.router.add_post("/api/v1/scheduler/jobs/update", scheduler_jobs_update)
    app.router.add_post("/api/v1/scheduler/jobs/delete", scheduler_jobs_delete)
    app.router.add_post("/api/v1/scheduler/jobs/pause", scheduler_jobs_pause)
    app.router.add_post("/api/v1/scheduler/jobs/resume", scheduler_jobs_resume)
    app.router.add_post("/api/v1/scheduler/jobs/run_now", scheduler_jobs_run_now)
    app.router.add_get("/api/scheduler/jobs", scheduler_jobs_list)
    app.router.add_get("/api/scheduler/job", scheduler_jobs_get)
    app.router.add_post("/api/scheduler/jobs", scheduler_jobs_create)
    app.router.add_post("/api/scheduler/jobs/update", scheduler_jobs_update)
    app.router.add_post("/api/scheduler/jobs/delete", scheduler_jobs_delete)
    app.router.add_post("/api/scheduler/jobs/pause", scheduler_jobs_pause)
    app.router.add_post("/api/scheduler/jobs/resume", scheduler_jobs_resume)
    app.router.add_post("/api/scheduler/jobs/run_now", scheduler_jobs_run_now)
