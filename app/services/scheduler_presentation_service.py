from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping

from app.services.scheduler_service import (
    SchedulerNotFoundError,
    SchedulerOwnershipError,
    SchedulerValidationError,
)


SchedulerServiceProvider = Callable[[], Any]


@dataclass(frozen=True)
class SchedulerPresentationService:
    scheduler_service: Any | SchedulerServiceProvider
    default_owner_id: str | int | None = None

    def serialize_job(self, job: Any, project_slug: str | None = None) -> dict[str, Any]:
        payload = self._job_payload(job)
        notification_target = getattr(job, "notification_target", None)
        resolved_project_slug = (
            str(project_slug)
            if project_slug is not None
            else str(payload.get("project_slug") or "")
        )
        return {
            "job_id": str(getattr(job, "job_id", "") or ""),
            "job_name": str(getattr(job, "job_name", "") or ""),
            "owner_id": str(getattr(job, "owner_id", "") or ""),
            "cron": str(getattr(job, "cron", "") or ""),
            "target_mode": str(getattr(job, "target_mode", "") or ""),
            "enabled": bool(getattr(job, "enabled", False)),
            "next_run_at": float(getattr(job, "next_run_at", 0.0) or 0.0),
            "last_fired_at": float(getattr(job, "last_fired_at", 0.0) or 0.0),
            "last_status": str(getattr(job, "last_status", "") or ""),
            "last_error": str(getattr(job, "last_error", "") or ""),
            "run_count": max(int(getattr(job, "run_count", 0) or 0), 0),
            "scheduled_for": float(getattr(job, "scheduled_for", 0.0) or 0.0),
            "project_slug": resolved_project_slug,
            "notification_target": self._notification_target_payload(notification_target),
            "payload": payload,
        }

    def project_slug_for_job(self, job: Any) -> str:
        return str(self._job_payload(job).get("project_slug") or "").strip()

    def payload_for_project(
        self,
        project_slug: str,
        payload: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        merged = dict(payload or {}) if isinstance(payload, Mapping) else {}
        merged["project_slug"] = str(project_slug or "").strip()
        return merged

    def require_project_job(
        self,
        project_slug: str,
        job_id: str,
        *,
        owner_id: str | int | None = None,
    ) -> Any:
        slug = str(project_slug or "").strip()
        token_job_id = str(job_id or "").strip()
        if not slug:
            raise SchedulerValidationError("project_slug is required")
        if not token_job_id:
            raise SchedulerValidationError("job_id is required")

        effective_owner_id = owner_id if owner_id is not None else self.default_owner_id
        if effective_owner_id is None or not str(effective_owner_id).strip():
            raise SchedulerValidationError("owner_id is required")

        job = self._resolved_scheduler_service().get_job(
            owner_id=effective_owner_id,
            job_id=token_job_id,
        )
        if job is None:
            raise SchedulerNotFoundError("scheduled job is not found")
        if self.project_slug_for_job(job) != slug:
            raise SchedulerOwnershipError("scheduled job does not belong to project")
        return job

    def _resolved_scheduler_service(self) -> Any:
        service = self.scheduler_service
        if callable(service) and not hasattr(service, "get_job"):
            service = service()
        return service

    @staticmethod
    def _job_payload(job: Any) -> dict[str, Any]:
        payload = getattr(job, "payload", {}) or {}
        return dict(payload) if isinstance(payload, Mapping) else {}

    @staticmethod
    def _notification_target_payload(notification_target: Any) -> dict[str, Any]:
        if hasattr(notification_target, "to_payload"):
            return dict(notification_target.to_payload())
        if isinstance(notification_target, Mapping):
            return dict(notification_target)
        if hasattr(notification_target, "__dict__"):
            return dict(vars(notification_target))
        return {}
