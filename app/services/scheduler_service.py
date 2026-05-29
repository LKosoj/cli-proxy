from __future__ import annotations

import asyncio
import copy
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Mapping, Optional
from zoneinfo import ZoneInfo

from app.events.bus import ModeLaunchCompletedEvent, ScheduledJobEvent, SystemEventBus
from app.services.actor_identity import normalize_actor_id
from app.services.scheduled_job_repository import (
    ScheduledJobAuditRecord,
    ScheduledJobRecord,
    ScheduledJobRepository,
)
from config import SchedulerConfig


logger = logging.getLogger(__name__)


class SchedulerServiceError(RuntimeError):
    """Base error for scheduler service operations."""


class SchedulerValidationError(SchedulerServiceError):
    """Raised when a scheduled job definition is invalid."""


class SchedulerOwnershipError(SchedulerServiceError):
    """Raised when a job is accessed by a non-owner."""


class SchedulerNotFoundError(SchedulerServiceError):
    """Raised when a scheduled job cannot be found."""


@dataclass(frozen=True)
class NotificationTarget:
    telegram_session_uid: str

    def to_payload(self) -> dict[str, Any]:
        return {"telegram_session_uid": str(self.telegram_session_uid or "")}


@dataclass(frozen=True)
class SchedulerJob:
    job_id: str
    owner_id: str
    job_name: str
    cron: str
    target_mode: str
    notification_target: NotificationTarget
    payload: dict[str, Any] = field(default_factory=dict)
    enabled: bool = True
    scheduled_for: float = 0.0
    next_run_at: float = 0.0
    last_fired_at: float = 0.0
    last_status: str = ""
    last_error: str = ""
    run_count: int = 0


@dataclass(frozen=True)
class _CronField:
    values: frozenset[int]
    is_wildcard: bool = False

    def matches(self, value: int) -> bool:
        return int(value) in self.values


@dataclass(frozen=True)
class CronSchedule:
    raw: str
    minute: _CronField
    hour: _CronField
    day: _CronField
    month: _CronField
    weekday: _CronField
    timezone: ZoneInfo

    @classmethod
    def parse(cls, expression: str, *, timezone_name: str) -> "CronSchedule":
        token = str(expression or "").strip()
        parts = token.split()
        if len(parts) != 5:
            raise SchedulerValidationError("cron expression must contain 5 fields")
        try:
            zone = ZoneInfo(str(timezone_name or "UTC"))
        except Exception as exc:
            raise SchedulerValidationError(f"invalid scheduler timezone: {timezone_name}") from exc
        return cls(
            raw=token,
            minute=_parse_cron_field(parts[0], minimum=0, maximum=59),
            hour=_parse_cron_field(parts[1], minimum=0, maximum=23),
            day=_parse_cron_field(parts[2], minimum=1, maximum=31),
            month=_parse_cron_field(parts[3], minimum=1, maximum=12),
            weekday=_parse_cron_field(parts[4], minimum=0, maximum=7, weekday=True),
            timezone=zone,
        )

    def matches(self, dt: datetime) -> bool:
        weekday_value = (dt.weekday() + 1) % 7
        day_matches = self.day.matches(dt.day)
        weekday_matches = self.weekday.matches(weekday_value)
        if self.day.is_wildcard and self.weekday.is_wildcard:
            day_filter_matches = True
        elif self.day.is_wildcard:
            day_filter_matches = weekday_matches
        elif self.weekday.is_wildcard:
            day_filter_matches = day_matches
        else:
            day_filter_matches = day_matches or weekday_matches
        return (
            self.minute.matches(dt.minute)
            and self.hour.matches(dt.hour)
            and self.month.matches(dt.month)
            and day_filter_matches
        )

    def next_after(self, current_ts: float) -> float:
        cursor = datetime.fromtimestamp(float(current_ts), tz=self.timezone)
        cursor = cursor.replace(second=0, microsecond=0)
        if cursor.timestamp() <= float(current_ts):
            cursor += timedelta(minutes=1)
        for _ in range(366 * 24 * 60 * 2):
            if self.matches(cursor):
                return float(cursor.timestamp())
            cursor += timedelta(minutes=1)
        raise SchedulerValidationError(f"cron expression has no upcoming run: {self.raw}")


def _parse_cron_field(
    token: str,
    *,
    minimum: int,
    maximum: int,
    weekday: bool = False,
) -> _CronField:
    raw = str(token or "").strip()
    if not raw:
        raise SchedulerValidationError("cron field cannot be empty")
    values: set[int] = set()
    for part in raw.split(","):
        item = str(part or "").strip()
        if not item:
            raise SchedulerValidationError(f"invalid cron field: {raw}")
        step = 1
        if "/" in item:
            base, step_token = item.split("/", 1)
            try:
                step = int(step_token)
            except Exception as exc:
                raise SchedulerValidationError(f"invalid cron step: {item}") from exc
            if step <= 0:
                raise SchedulerValidationError(f"cron step must be > 0: {item}")
        else:
            base = item
        if base == "*":
            start = minimum
            end = maximum
        elif "-" in base:
            start_token, end_token = base.split("-", 1)
            try:
                start = int(start_token)
                end = int(end_token)
            except Exception as exc:
                raise SchedulerValidationError(f"invalid cron range: {item}") from exc
        else:
            try:
                start = int(base)
                end = int(base)
            except Exception as exc:
                raise SchedulerValidationError(f"invalid cron value: {item}") from exc
        if start < minimum or start > maximum or end < minimum or end > maximum:
            raise SchedulerValidationError(f"cron value out of range: {item}")
        if end < start:
            raise SchedulerValidationError(f"cron range must be ascending: {item}")
        values.update(range(start, end + 1, step))
    if not values:
        raise SchedulerValidationError(f"cron field is empty after parsing: {raw}")
    if weekday and 7 in values:
        values.remove(7)
        values.add(0)
    return _CronField(values=frozenset(values), is_wildcard=(raw == "*"))


class SchedulerService:
    def __init__(
        self,
        repository: ScheduledJobRepository,
        event_bus: SystemEventBus,
        scheduler_config: SchedulerConfig,
        *,
        logger_: Optional[logging.Logger] = None,
        time_fn=None,
    ) -> None:
        self._repository = repository
        self._event_bus = event_bus
        self._config = scheduler_config
        self._logger = logger_ or logging.getLogger(__name__)
        self._time_fn = time_fn or __import__("time").time
        self._runner_task: asyncio.Task[None] | None = None
        self._dispatch_semaphore = asyncio.Semaphore(max(1, int(self._config.max_concurrent_jobs or 1)))
        self._mode_launch_unsubscribe: Any = None
        self._ensure_mode_launch_subscription()

    async def start(self) -> None:
        if not bool(self._config.enabled):
            return
        await self.restore_jobs()
        await self._subscribe_mode_launch_events()
        if self._runner_task is None or self._runner_task.done():
            self._runner_task = asyncio.create_task(self._run_loop(), name="scheduler-service")

    async def stop(self) -> None:
        task = self._runner_task
        self._runner_task = None
        if callable(self._mode_launch_unsubscribe):
            try:
                self._mode_launch_unsubscribe()
            except Exception:
                self._logger.exception("scheduler mode launch unsubscribe failed")
        self._mode_launch_unsubscribe = None
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            return

    async def _subscribe_mode_launch_events(self) -> None:
        self._ensure_mode_launch_subscription()

    def _ensure_mode_launch_subscription(self) -> None:
        if callable(self._mode_launch_unsubscribe):
            return
        if not hasattr(self._event_bus, "subscribe"):
            return
        self._mode_launch_unsubscribe = self._event_bus.subscribe(
            ModeLaunchCompletedEvent,
            self._handle_mode_launch_completed,
        )

    async def restore_jobs(self, *, now: float | None = None) -> list[SchedulerJob]:
        current = float(self._time_fn() if now is None else now)
        restored: list[SchedulerJob] = []
        for record in self._repository.list_jobs(enabled_only=True):
            if not str(record.cron or "").strip():
                continue
            normalized = record
            if float(record.next_run_at or 0.0) <= 0.0:
                schedule = self._parse_cron(record.cron)
                next_run = schedule.next_after(current)
                self._repository.upsert_job(
                    job_id=record.job_id,
                    job_name=record.job_name,
                    scheduled_for=next_run,
                    payload=record.payload,
                    enabled=record.enabled,
                    cron=record.cron,
                    target_mode=record.target_mode,
                    owner_id=record.owner_id,
                    notification_target=record.notification_target,
                    next_run_at=next_run,
                    last_fired_at=record.last_fired_at,
                    last_status=record.last_status,
                    last_error=record.last_error,
                    run_count=record.run_count,
                )
                refreshed = self._repository.get_job(record.job_id)
                assert refreshed is not None
                normalized = refreshed
            restored.append(self._to_job(normalized))
        return restored

    def create_job(
        self,
        *,
        owner_id: str | int,
        cron: str,
        target_mode: str,
        notification_target_telegram_session_uid: str,
        payload: Mapping[str, Any] | None = None,
        enabled: bool = True,
        job_id: str | None = None,
        job_name: str | None = None,
        now: float | None = None,
    ) -> SchedulerJob:
        owner = normalize_actor_id(owner_id, default_surface="telegram")
        if not owner:
            raise SchedulerValidationError("owner_id is required")
        target_mode_token = str(target_mode or "").strip()
        notification_uid = str(notification_target_telegram_session_uid or "").strip()
        if not target_mode_token:
            raise SchedulerValidationError("target_mode is required")
        if not notification_uid:
            raise SchedulerValidationError("notification_target.telegram_session_uid is required")
        schedule = self._parse_cron(cron)
        current = float(self._time_fn() if now is None else now)
        next_run = schedule.next_after(current) if bool(enabled) else 0.0
        token_job_id = str(job_id or uuid.uuid4().hex).strip()
        token_job_name = str(job_name or target_mode_token).strip()
        record = self._repository.upsert_job(
            job_id=token_job_id,
            job_name=token_job_name,
            scheduled_for=next_run,
            payload=self._copy_payload(payload),
            enabled=enabled,
            cron=schedule.raw,
            target_mode=target_mode_token,
            owner_id=owner,
            notification_target={"telegram_session_uid": notification_uid},
            next_run_at=next_run,
            last_fired_at=0.0,
            last_status="idle" if bool(enabled) else "paused",
            last_error="",
            run_count=0,
        )
        audit = self._repository.append_audit_record(
            correlation_id=self._new_correlation_id(),
            action="create",
            owner_id=owner,
            job_id=record.job_id,
            job_name=record.job_name,
            after=self._record_to_payload(record),
        )
        self._logger.info(
            "scheduler job mutation action=create correlation_id=%s job_id=%s owner_id=%s target_mode=%s",
            audit.correlation_id,
            record.job_id,
            owner,
            record.target_mode,
        )
        return self._to_job(record)

    def get_job(self, *, owner_id: str | int, job_id: str) -> Optional[SchedulerJob]:
        record = self._repository.get_job(job_id)
        if record is None:
            return None
        self._require_owner(record, owner_id=owner_id)
        return self._to_job(record)

    def list_jobs(self, *, owner_id: str | int, enabled_only: bool = False) -> list[SchedulerJob]:
        return [
            self._to_job(record)
            for record in self._repository.list_jobs(owner_id=owner_id, enabled_only=enabled_only)
            if str(record.cron or "").strip()
        ]

    def update_job(
        self,
        *,
        owner_id: str | int,
        job_id: str,
        cron: str | None = None,
        target_mode: str | None = None,
        notification_target_telegram_session_uid: str | None = None,
        payload: Mapping[str, Any] | None = None,
        enabled: bool | None = None,
        job_name: str | None = None,
        now: float | None = None,
    ) -> SchedulerJob:
        normalized_owner_id = normalize_actor_id(owner_id, default_surface="telegram")
        record = self._require_record(job_id=job_id, owner_id=normalized_owner_id)
        before_payload = self._record_to_payload(record)
        cron_token = str(cron if cron is not None else record.cron).strip()
        schedule = self._parse_cron(cron_token)
        target_mode_token = str(target_mode if target_mode is not None else record.target_mode).strip()
        notification_uid = str(
            notification_target_telegram_session_uid
            if notification_target_telegram_session_uid is not None
            else record.notification_target.get("telegram_session_uid", "")
        ).strip()
        if not target_mode_token:
            raise SchedulerValidationError("target_mode is required")
        if not notification_uid:
            raise SchedulerValidationError("notification_target.telegram_session_uid is required")
        enabled_value = record.enabled if enabled is None else bool(enabled)
        current = float(self._time_fn() if now is None else now)
        next_run = record.next_run_at
        if enabled_value:
            if cron is not None or not float(record.next_run_at or 0.0):
                next_run = schedule.next_after(current)
        else:
            next_run = 0.0
        updated = self._repository.upsert_job(
            job_id=record.job_id,
            job_name=str(job_name or record.job_name or target_mode_token).strip(),
            scheduled_for=next_run,
            payload=record.payload if payload is None else self._copy_payload(payload),
            enabled=enabled_value,
            cron=schedule.raw,
            target_mode=target_mode_token,
            owner_id=record.owner_id,
            notification_target={"telegram_session_uid": notification_uid},
            next_run_at=next_run,
            last_fired_at=record.last_fired_at,
            last_status="paused" if not enabled_value else (record.last_status or "idle"),
            last_error="" if enabled_value else record.last_error,
            run_count=record.run_count,
        )
        audit = self._repository.append_audit_record(
            correlation_id=self._new_correlation_id(),
            action="update",
            owner_id=normalized_owner_id,
            job_id=updated.job_id,
            job_name=updated.job_name,
            before=before_payload,
            after=self._record_to_payload(updated),
        )
        self._logger.info(
            "scheduler job mutation action=update correlation_id=%s job_id=%s owner_id=%s target_mode=%s",
            audit.correlation_id,
            updated.job_id,
            normalized_owner_id,
            updated.target_mode,
        )
        return self._to_job(updated)

    def delete_job(self, *, owner_id: str | int, job_id: str) -> bool:
        normalized_owner_id = normalize_actor_id(owner_id, default_surface="telegram")
        record = self._require_record(job_id=job_id, owner_id=normalized_owner_id)
        deleted = self._repository.delete_job(job_id)
        if deleted:
            audit = self._repository.append_audit_record(
                correlation_id=self._new_correlation_id(),
                action="delete",
                owner_id=normalized_owner_id,
                job_id=record.job_id,
                job_name=record.job_name,
                before=self._record_to_payload(record),
                after={},
            )
            self._logger.info(
                "scheduler job mutation action=delete correlation_id=%s job_id=%s owner_id=%s",
                audit.correlation_id,
                record.job_id,
                normalized_owner_id,
            )
        return deleted

    def pause_job(self, *, owner_id: str | int, job_id: str, now: float | None = None) -> SchedulerJob:
        return self.update_job(
            owner_id=owner_id,
            job_id=job_id,
            enabled=False,
            now=now,
        )

    def resume_job(self, *, owner_id: str | int, job_id: str, now: float | None = None) -> SchedulerJob:
        return self.update_job(
            owner_id=owner_id,
            job_id=job_id,
            enabled=True,
            now=now,
        )

    def pause_job_for_project(
        self,
        *,
        owner_id: str | int,
        job_id: str,
        project_slug: str,
        now: float | None = None,
    ) -> SchedulerJob:
        return self._update_job_enabled_for_project(
            owner_id=owner_id,
            job_id=job_id,
            project_slug=project_slug,
            enabled=False,
            now=now,
        )

    def resume_job_for_project(
        self,
        *,
        owner_id: str | int,
        job_id: str,
        project_slug: str,
        now: float | None = None,
    ) -> SchedulerJob:
        return self._update_job_enabled_for_project(
            owner_id=owner_id,
            job_id=job_id,
            project_slug=project_slug,
            enabled=True,
            now=now,
        )

    async def run_now(
        self,
        *,
        owner_id: str | int,
        job_id: str,
        now: float | None = None,
    ) -> ScheduledJobEvent:
        normalized_owner_id = normalize_actor_id(owner_id, default_surface="telegram")
        record = self._require_record(job_id=job_id, owner_id=normalized_owner_id)
        current = float(self._time_fn() if now is None else now)
        refreshed = self._repository.upsert_job(
            job_id=record.job_id,
            job_name=record.job_name,
            scheduled_for=record.scheduled_for,
            payload=record.payload,
            enabled=record.enabled,
            cron=record.cron,
            target_mode=record.target_mode,
            owner_id=record.owner_id,
            notification_target=record.notification_target,
            next_run_at=record.next_run_at,
            last_fired_at=current,
            last_status="manual",
            last_error="",
            run_count=record.run_count + 1,
        )
        event = self._build_event(
            refreshed,
            status="manual",
            scheduled_for=current,
        )
        self._log_emitted_event(event)
        await self._event_bus.publish(event)
        return event

    async def run_due_jobs(self, *, now: float | None = None) -> list[ScheduledJobEvent]:
        current = float(self._time_fn() if now is None else now)
        due = self._repository.list_due_jobs(
            as_of=current,
            limit=max(1, int(self._config.max_concurrent_jobs or 1)),
        )
        if not due:
            return []
        results = await asyncio.gather(
            *(self._dispatch_due_job(record, current) for record in due),
            return_exceptions=False,
        )
        return [event for event in results if event is not None]

    async def _run_loop(self) -> None:
        try:
            while True:
                await self.run_due_jobs()
                await asyncio.sleep(max(1, int(self._config.tick_interval_sec or 60)))
        except asyncio.CancelledError:
            raise

    async def _dispatch_due_job(
        self,
        record: ScheduledJobRecord,
        current: float,
    ) -> Optional[ScheduledJobEvent]:
        async with self._dispatch_semaphore:
            schedule = self._parse_cron(record.cron)
            due_at = float(record.next_run_at or 0.0)
            next_run = schedule.next_after(max(current, due_at))
            misfire_delta = max(0.0, current - due_at)
            publish_event = misfire_delta <= float(self._config.misfire_grace_sec or 0)
            updated = self._repository.update_schedule(
                job_id=record.job_id,
                expected_next_run_at=due_at,
                next_run_at=next_run,
                scheduled_for=next_run,
                fired_at=current if publish_event else None,
                last_status="triggered" if publish_event else "idle",
                last_error="",
                increment_run_count=bool(publish_event),
            )
            if not updated:
                return None
            if not publish_event:
                return None
            refreshed = self._repository.get_job(record.job_id)
            if refreshed is None:
                return None
            event = self._build_event(refreshed, status="triggered", scheduled_for=due_at)
            self._log_emitted_event(event)
            await self._event_bus.publish(event)
            return event

    def _parse_cron(self, cron: str) -> CronSchedule:
        return CronSchedule.parse(cron, timezone_name=str(self._config.timezone or "UTC"))

    def _require_record(self, *, job_id: str, owner_id: str | int) -> ScheduledJobRecord:
        record = self._repository.get_job(job_id)
        if record is None or not str(record.cron or "").strip():
            raise SchedulerNotFoundError(f"scheduled job is not found: {job_id}")
        self._require_owner(record, owner_id=owner_id)
        return record

    def _update_job_enabled_for_project(
        self,
        *,
        owner_id: str | int,
        job_id: str,
        project_slug: str,
        enabled: bool,
        now: float | None = None,
    ) -> SchedulerJob:
        record = self._require_record(job_id=job_id, owner_id=owner_id)
        payload = dict(record.payload or {})
        payload_project_slug = str(payload.get("project_slug") or "").strip()
        expected_project_slug = str(project_slug or "").strip()
        if payload_project_slug != expected_project_slug:
            raise SchedulerOwnershipError("scheduled job does not belong to project")
        return self.update_job(
            owner_id=owner_id,
            job_id=job_id,
            enabled=bool(enabled),
            now=now,
        )

    @staticmethod
    def _require_owner(record: ScheduledJobRecord, *, owner_id: str | int) -> None:
        if str(record.owner_id) != normalize_actor_id(owner_id, default_surface="telegram"):
            raise SchedulerOwnershipError(
                f"scheduled job is owned by another actor: {record.job_id}"
            )

    @staticmethod
    def _to_job(record: ScheduledJobRecord) -> SchedulerJob:
        return SchedulerJob(
            job_id=record.job_id,
            owner_id=str(record.owner_id or ""),
            job_name=record.job_name,
            cron=record.cron,
            target_mode=record.target_mode,
            notification_target=NotificationTarget(
                telegram_session_uid=str(record.notification_target.get("telegram_session_uid", ""))
            ),
            payload=SchedulerService._copy_payload(record.payload),
            enabled=bool(record.enabled),
            scheduled_for=float(record.scheduled_for or 0.0),
            next_run_at=float(record.next_run_at or 0.0),
            last_fired_at=float(record.last_fired_at or 0.0),
            last_status=str(record.last_status or ""),
            last_error=str(record.last_error or ""),
            run_count=max(int(record.run_count or 0), 0),
        )

    @staticmethod
    def _build_event(
        record: ScheduledJobRecord,
        *,
        status: str,
        scheduled_for: float,
    ) -> ScheduledJobEvent:
        payload = SchedulerService._copy_payload(record.payload)
        return ScheduledJobEvent(
            job_id=record.job_id,
            job_name=record.job_name,
            status=str(status or "").strip(),
            scheduled_for=float(scheduled_for or 0.0),
            cron=record.cron,
            target_mode=record.target_mode,
            owner_id=str(record.owner_id or ""),
            correlation_id=uuid.uuid4().hex,
            dry_run=SchedulerService._extract_dry_run(payload),
            notification_target=dict(record.notification_target or {}),
            payload=payload,
        )

    def list_audit_trail(
        self,
        *,
        owner_id: str | int | None = None,
        job_id: str = "",
        action: str = "",
        limit: int = 100,
    ) -> list[ScheduledJobAuditRecord]:
        return self._repository.list_audit_records(
            limit=limit,
            owner_id=owner_id,
            job_id=job_id,
            action=action,
        )

    @staticmethod
    def _extract_dry_run(payload: Mapping[str, Any]) -> bool:
        launch = payload.get("launch")
        launch_payload = dict(launch) if isinstance(launch, dict) else {}
        if "dry_run" in launch_payload:
            return bool(launch_payload.get("dry_run"))
        return bool(payload.get("dry_run", False))

    @staticmethod
    def _record_to_payload(record: ScheduledJobRecord) -> dict[str, Any]:
        return {
            "job_id": str(record.job_id or ""),
            "job_name": str(record.job_name or ""),
            "scheduled_for": float(record.scheduled_for or 0.0),
            "payload": SchedulerService._copy_payload(record.payload),
            "enabled": bool(record.enabled),
            "cron": str(record.cron or ""),
            "target_mode": str(record.target_mode or ""),
            "owner_id": str(record.owner_id or ""),
            "notification_target": dict(record.notification_target or {}),
            "next_run_at": float(record.next_run_at or 0.0),
            "last_fired_at": float(record.last_fired_at or 0.0),
            "last_status": str(record.last_status or ""),
            "last_error": str(record.last_error or ""),
            "run_count": max(int(record.run_count or 0), 0),
        }

    @staticmethod
    def _new_correlation_id() -> str:
        return uuid.uuid4().hex

    @staticmethod
    def _copy_payload(payload: Mapping[str, Any] | None) -> dict[str, Any]:
        return copy.deepcopy(dict(payload or {}))

    def _log_emitted_event(self, event: ScheduledJobEvent) -> None:
        self._logger.info(
            "scheduler event emitted correlation_id=%s origin=scheduler provider=scheduler "
            "job_id=%s mode_id=%s session_uid=%s dry_run=%s status=%s",
            str(event.correlation_id or ""),
            str(event.job_id or ""),
            str(event.target_mode or ""),
            str((event.notification_target or {}).get("telegram_session_uid", "") or ""),
            bool(event.dry_run),
            str(event.status or ""),
        )

    async def _handle_mode_launch_completed(self, event: ModeLaunchCompletedEvent) -> None:
        if str(event.origin or "").strip() != "scheduler":
            return
        payload = dict(event.payload or {})
        job_id = str(payload.get("job_id") or "").strip()
        if not job_id:
            return
        status = str(event.status or "").strip()
        result = dict(event.result or {})
        last_error = str(result.get("error") or payload.get("error") or "").strip()
        self._repository.update_runtime_status(
            job_id=job_id,
            last_status=status,
            last_error=last_error,
            last_run_at=float(self._time_fn()),
            increment_run_count=False,
        )
