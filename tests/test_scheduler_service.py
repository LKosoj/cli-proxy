from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime, timezone

import pytest

from app.events.bus import ModeLaunchCompletedEvent, ScheduledJobEvent, SystemEventBus
from app.services.actor_identity import telegram_actor_id
from app.services.scheduled_job_repository import ScheduledJobRepository, ScheduledJobRepositoryError
from app.services.scheduler_presentation_service import SchedulerPresentationService
from app.services.scheduler_service import (
    CronSchedule,
    NotificationTarget,
    SchedulerJob,
    SchedulerNotFoundError,
    SchedulerOwnershipError,
    SchedulerService,
    SchedulerValidationError,
)
from config import AppConfig, DefaultsConfig, MCPConfig, MiniAppConfig, SchedulerConfig, TelegramConfig, ToolConfig


def _ts(year: int, month: int, day: int, hour: int, minute: int, second: int = 0) -> float:
    return datetime(year, month, day, hour, minute, second, tzinfo=timezone.utc).timestamp()


def _build_config(tmp_path, *, intent: str) -> AppConfig:
    workdir = tmp_path / f"workdir_{intent}"
    runtime = tmp_path / f"runtime_{intent}"
    logs = tmp_path / f"logs_{intent}"
    workdir.mkdir(parents=True, exist_ok=True)
    runtime.mkdir(parents=True, exist_ok=True)
    logs.mkdir(parents=True, exist_ok=True)
    return AppConfig(
        telegram=TelegramConfig(token="token", whitelist_chat_ids=[1], admlist_chat_ids=[1]),
        tools={
            "dummy": ToolConfig(
                name="dummy",
                mode="headless",
                cmd=["bash", "-lc", "cat"],
            )
        },
        defaults=DefaultsConfig(
            workdir=str(workdir),
            state_path=str(runtime / "state.json"),
            toolhelp_path=str(runtime / "toolhelp.json"),
            log_path=str(logs / "bot.log"),
        ),
        mcp=MCPConfig(enabled=False),
        mcp_clients=[],
        presets=[],
        path=str(tmp_path / f"config_{intent}.yaml"),
        miniapp=MiniAppConfig(),
        scheduler=SchedulerConfig(
            enabled=True,
            timezone="UTC",
            tick_interval_sec=1,
            max_concurrent_jobs=2,
            misfire_grace_sec=30,
        ),
    )


def test_scheduler_presentation_service_contract_for_job_serialization_and_project_lookup() -> None:
    job = SchedulerJob(
        job_id="job-alpha",
        owner_id=telegram_actor_id(101),
        job_name="Alpha digest",
        cron="*/15 * * * *",
        target_mode="manager",
        notification_target=NotificationTarget(telegram_session_uid="thread:101:55"),
        payload={"project_slug": "alpha", "intent": "digest"},
        enabled=True,
        scheduled_for=10.0,
        next_run_at=20.0,
        last_fired_at=5.0,
        last_status="idle",
        last_error="",
        run_count=0,
    )

    class FakeSchedulerService:
        def get_job(self, *, owner_id, job_id):
            if job_id == "missing":
                return None
            assert owner_id == telegram_actor_id(101)
            assert job_id == "job-alpha"
            return job

    service = SchedulerPresentationService(FakeSchedulerService())

    assert service.project_slug_for_job(job) == "alpha"
    assert service.payload_for_project("alpha", {"intent": "digest"}) == {
        "intent": "digest",
        "project_slug": "alpha",
    }
    assert service.serialize_job(job) == {
        "job_id": "job-alpha",
        "job_name": "Alpha digest",
        "owner_id": telegram_actor_id(101),
        "cron": "*/15 * * * *",
        "target_mode": "manager",
        "enabled": True,
        "next_run_at": 20.0,
        "last_fired_at": 5.0,
        "last_status": "idle",
        "last_error": "",
        "run_count": 0,
        "scheduled_for": 10.0,
        "project_slug": "alpha",
        "notification_target": {"telegram_session_uid": "thread:101:55"},
        "payload": {"project_slug": "alpha", "intent": "digest"},
    }
    assert service.serialize_job(job, project_slug="override")["project_slug"] == "override"
    assert service.require_project_job("alpha", "job-alpha", owner_id=telegram_actor_id(101)) is job
    default_owner_service = SchedulerPresentationService(
        FakeSchedulerService(),
        default_owner_id=telegram_actor_id(101),
    )
    assert default_owner_service.require_project_job("alpha", "job-alpha") is job

    with pytest.raises(SchedulerNotFoundError, match="scheduled job is not found"):
        service.require_project_job("alpha", "missing", owner_id=telegram_actor_id(101))
    with pytest.raises(SchedulerOwnershipError, match="scheduled job does not belong to project"):
        service.require_project_job("beta", "job-alpha", owner_id=telegram_actor_id(101))
    with pytest.raises(SchedulerValidationError, match="job_id is required"):
        service.require_project_job("alpha", "", owner_id=telegram_actor_id(101))
    with pytest.raises(SchedulerValidationError, match="owner_id is required"):
        service.require_project_job("alpha", "job-alpha")


def test_scheduler_service_validates_cron_and_enforces_owner_policy(tmp_path) -> None:
    cfg = _build_config(tmp_path, intent="ownership")
    service = SchedulerService(
        repository=ScheduledJobRepository(cfg.defaults.state_path),
        event_bus=SystemEventBus(),
        scheduler_config=cfg.scheduler,
    )

    with pytest.raises(SchedulerValidationError):
        service.create_job(
            owner_id=101,
            cron="bad cron",
            target_mode="manager",
            notification_target_telegram_session_uid="thread:101:1",
            now=_ts(2026, 1, 1, 12, 0, 0),
        )

    created = service.create_job(
        owner_id=101,
        job_id="job-owner",
        job_name="Owner digest",
        cron="*/5 * * * *",
        target_mode="manager",
        notification_target_telegram_session_uid="thread:101:1",
        payload={"intent": "alpha"},
        now=_ts(2026, 1, 1, 12, 0, 1),
    )

    assert created.owner_id == telegram_actor_id(101)
    assert created.next_run_at == _ts(2026, 1, 1, 12, 5, 0)
    assert created.last_status == "idle"
    assert created.last_error == ""
    assert created.run_count == 0
    assert service.get_job(owner_id=101, job_id=created.job_id) is not None
    assert [job.job_id for job in service.list_jobs(owner_id=101)] == ["job-owner"]
    assert service.list_jobs(owner_id=202) == []

    with pytest.raises(SchedulerOwnershipError):
        service.get_job(owner_id=202, job_id=created.job_id)

    with pytest.raises(SchedulerOwnershipError):
        service.update_job(
            owner_id=202,
            job_id=created.job_id,
            target_mode="agent",
        )

    with pytest.raises(SchedulerOwnershipError):
        service.delete_job(owner_id=202, job_id=created.job_id)

    updated = service.update_job(
        owner_id=101,
        job_id=created.job_id,
        cron="*/10 * * * *",
        target_mode="agent",
        notification_target_telegram_session_uid="thread:101:2",
        now=_ts(2026, 1, 1, 12, 0, 1),
    )
    assert updated.target_mode == "agent"
    assert updated.notification_target.telegram_session_uid == "thread:101:2"
    assert updated.next_run_at == _ts(2026, 1, 1, 12, 10, 0)
    paused = service.pause_job(
        owner_id=101,
        job_id=created.job_id,
        now=_ts(2026, 1, 1, 12, 0, 2),
    )
    assert paused.enabled is False
    assert paused.last_status == "paused"
    resumed = service.resume_job(
        owner_id=101,
        job_id=created.job_id,
        now=_ts(2026, 1, 1, 12, 0, 3),
    )
    assert resumed.enabled is True
    assert resumed.next_run_at == _ts(2026, 1, 1, 12, 10, 0)
    assert service.delete_job(owner_id=101, job_id=created.job_id) is True
    assert service.get_job(owner_id=101, job_id=created.job_id) is None


def test_scheduled_job_repository_rejects_legacy_schema_without_owner_columns(tmp_path) -> None:
    cfg = _build_config(tmp_path, intent="scheduler_legacy")
    repo = ScheduledJobRepository(cfg.defaults.state_path)

    with sqlite3.connect(repo.db_path) as conn:
        conn.execute("DROP TABLE IF EXISTS scheduled_jobs")
        conn.execute("DROP TABLE IF EXISTS scheduled_job_audit_trail")
        conn.execute(
            """
            CREATE TABLE scheduled_jobs (
                job_id TEXT PRIMARY KEY,
                job_name TEXT NOT NULL,
                scheduled_for REAL NOT NULL,
                payload_json TEXT NOT NULL DEFAULT '{}',
                enabled INTEGER NOT NULL DEFAULT 1,
                cron TEXT NOT NULL DEFAULT '',
                target_mode TEXT NOT NULL DEFAULT '',
                notification_target_json TEXT NOT NULL DEFAULT '{}',
                next_run_at REAL NOT NULL DEFAULT 0,
                last_fired_at REAL NOT NULL DEFAULT 0,
                last_status TEXT NOT NULL DEFAULT '',
                last_error TEXT NOT NULL DEFAULT '',
                run_count INTEGER NOT NULL DEFAULT 0,
                created_at REAL NOT NULL DEFAULT 0,
                updated_at REAL NOT NULL DEFAULT 0
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE scheduled_job_audit_trail (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                correlation_id TEXT NOT NULL DEFAULT '',
                action TEXT NOT NULL,
                job_id TEXT NOT NULL,
                job_name TEXT NOT NULL DEFAULT '',
                origin TEXT NOT NULL DEFAULT '',
                provider TEXT NOT NULL DEFAULT '',
                timestamp REAL NOT NULL DEFAULT 0,
                before_json TEXT NOT NULL DEFAULT '{}',
                after_json TEXT NOT NULL DEFAULT '{}'
            )
            """
        )

    with pytest.raises(ScheduledJobRepositoryError, match=r"missing required columns owner_id"):
        ScheduledJobRepository(cfg.defaults.state_path)

    with sqlite3.connect(repo.db_path) as conn:
        columns = {
            str(row[1] or "")
            for row in conn.execute("PRAGMA table_info(scheduled_jobs)").fetchall()
        }
    assert "owner_id" not in columns


def test_scheduled_job_repository_round_trips_complex_payload_objects(tmp_path) -> None:
    cfg = _build_config(tmp_path, intent="complex_payload")
    repo = ScheduledJobRepository(cfg.defaults.state_path)
    complex_payload = {
        "prompt": "launch nested",
        "project_slug": "alpha",
        "project": {
            "slug": "alpha",
            "branch": "main",
            "meta": {"priority": 3, "tags": ["nightly", "digest"]},
        },
        "intent": {
            "kind": "digest",
            "params": {
                "sections": ["summary", "alerts"],
                "limits": {"files": 10, "tokens": 2048},
            },
        },
        "launch": {
            "dry_run": False,
            "inputs": [{"kind": "file", "path": "notes/today.md"}],
        },
    }

    stored = repo.upsert_job(
        job_id="job-complex",
        job_name="Complex payload",
        scheduled_for=_ts(2026, 1, 1, 12, 5, 0),
        payload=complex_payload,
        enabled=True,
        cron="*/5 * * * *",
        target_mode="manager",
        owner_id=101,
        notification_target={"telegram_session_uid": "thread:101:55"},
        next_run_at=_ts(2026, 1, 1, 12, 5, 0),
        last_status="idle",
    )

    fetched = repo.get_job("job-complex")
    listed = repo.list_jobs(owner_id=101)

    assert stored.payload == complex_payload
    assert fetched is not None
    assert fetched.payload == complex_payload
    assert [job.job_id for job in listed] == ["job-complex"]
    assert listed[0].payload == complex_payload


def test_scheduler_service_restores_persisted_jobs_and_emits_events_without_state_leak(tmp_path) -> None:
    cfg_a = _build_config(tmp_path, intent="restore_a")
    cfg_b = _build_config(tmp_path, intent="restore_b")

    async def _exercise() -> None:
        repo_a = ScheduledJobRepository(cfg_a.defaults.state_path)
        creator = SchedulerService(
            repository=repo_a,
            event_bus=SystemEventBus(),
            scheduler_config=cfg_a.scheduler,
        )
        created = creator.create_job(
            owner_id=101,
            job_id="job-minute",
            job_name="Minute digest",
            cron="* * * * *",
            target_mode="manager",
            notification_target_telegram_session_uid="thread:101:55",
            payload={"intent": "alpha"},
            now=_ts(2026, 1, 1, 12, 0, 1),
        )
        assert created.next_run_at == _ts(2026, 1, 1, 12, 1, 0)

        deliveries: list[ScheduledJobEvent] = []
        bus_a = SystemEventBus()

        def _capture(event: ScheduledJobEvent) -> None:
            deliveries.append(event)

        bus_a.subscribe(ScheduledJobEvent, _capture)
        restored_service = SchedulerService(
            repository=repo_a,
            event_bus=bus_a,
            scheduler_config=cfg_a.scheduler,
        )
        restored_jobs = await restored_service.restore_jobs(now=_ts(2026, 1, 1, 12, 0, 30))
        assert [job.job_id for job in restored_jobs] == ["job-minute"]

        emitted = await restored_service.run_due_jobs(now=_ts(2026, 1, 1, 12, 1, 0))
        assert [event.job_id for event in emitted] == ["job-minute"]
        assert [event.job_id for event in deliveries] == ["job-minute"]
        assert deliveries[0].target_mode == "manager"
        assert deliveries[0].owner_id == telegram_actor_id(101)
        assert deliveries[0].notification_target == {"telegram_session_uid": "thread:101:55"}
        assert deliveries[0].payload == {"intent": "alpha"}

        persisted = repo_a.get_job("job-minute")
        assert persisted is not None
        assert persisted.last_fired_at == _ts(2026, 1, 1, 12, 1, 0)
        assert persisted.next_run_at == _ts(2026, 1, 1, 12, 2, 0)
        assert persisted.scheduled_for == _ts(2026, 1, 1, 12, 2, 0)
        assert persisted.last_status == "triggered"
        assert persisted.last_error == ""
        assert persisted.run_count == 1

        await bus_a.publish(
            ModeLaunchCompletedEvent(
                origin="scheduler",
                mode_id="manager",
                session_uid="thread:101:55",
                status="completed",
                payload={"job_id": "job-minute"},
                result={},
            )
        )
        completed = repo_a.get_job("job-minute")
        assert completed is not None
        assert completed.last_status == "completed"
        assert completed.last_error == ""
        assert completed.run_count == 1

        isolated = SchedulerService(
            repository=ScheduledJobRepository(cfg_b.defaults.state_path),
            event_bus=SystemEventBus(),
            scheduler_config=cfg_b.scheduler,
        )
        assert await isolated.restore_jobs(now=_ts(2026, 1, 1, 12, 1, 0)) == []

    asyncio.run(_exercise())


def test_scheduler_service_run_now_enforces_owner_and_emits_manual_event(tmp_path) -> None:
    cfg = _build_config(tmp_path, intent="run_now")

    async def _exercise() -> None:
        deliveries: list[ScheduledJobEvent] = []
        bus = SystemEventBus()
        bus.subscribe(ScheduledJobEvent, deliveries.append)
        service = SchedulerService(
            repository=ScheduledJobRepository(cfg.defaults.state_path),
            event_bus=bus,
            scheduler_config=cfg.scheduler,
        )

        created = service.create_job(
            owner_id=101,
            job_id="job-manual",
            job_name="Manual digest",
            cron="*/15 * * * *",
            target_mode="manager",
            notification_target_telegram_session_uid="thread:101:77",
            payload={"intent": "manual"},
            now=_ts(2026, 1, 1, 12, 0, 1),
        )

        with pytest.raises(SchedulerOwnershipError):
            await service.run_now(
                owner_id=202,
                job_id=created.job_id,
                now=_ts(2026, 1, 1, 12, 3, 0),
            )

        event = await service.run_now(
            owner_id=101,
            job_id=created.job_id,
            now=_ts(2026, 1, 1, 12, 3, 0),
        )

        assert event.job_id == "job-manual"
        assert event.status == "manual"
        assert str(event.correlation_id or "").strip()
        assert event.dry_run is False
        assert event.owner_id == telegram_actor_id(101)
        assert event.notification_target == {"telegram_session_uid": "thread:101:77"}
        assert event.payload == {"intent": "manual"}
        assert [item.job_id for item in deliveries] == ["job-manual"]
        assert deliveries[0].status == "manual"

        persisted = service.get_job(owner_id=101, job_id=created.job_id)
        assert persisted is not None
        assert persisted.last_fired_at == _ts(2026, 1, 1, 12, 3, 0)
        assert persisted.next_run_at == created.next_run_at
        assert persisted.last_status == "manual"
        assert persisted.last_error == ""
        assert persisted.run_count == 1

    asyncio.run(_exercise())


def test_scheduler_service_audit_trail_persists_mutations_and_dry_run_events(tmp_path) -> None:
    cfg_a = _build_config(tmp_path, intent="audit_a")
    cfg_b = _build_config(tmp_path, intent="audit_b")

    async def _exercise() -> None:
        service_a = SchedulerService(
            repository=ScheduledJobRepository(cfg_a.defaults.state_path),
            event_bus=SystemEventBus(),
            scheduler_config=cfg_a.scheduler,
        )
        created = service_a.create_job(
            owner_id=101,
            job_id="job-audit",
            job_name="Audit digest",
            cron="*/15 * * * *",
            target_mode="manager",
            notification_target_telegram_session_uid="thread:101:88",
            payload={"intent": "audit", "dry_run": True},
            now=_ts(2026, 1, 1, 12, 0, 1),
        )
        updated = service_a.update_job(
            owner_id=101,
            job_id=created.job_id,
            job_name="Audit digest v2",
            cron="*/30 * * * *",
            now=_ts(2026, 1, 1, 12, 1, 0),
        )
        event = await service_a.run_now(
            owner_id=101,
            job_id=created.job_id,
            now=_ts(2026, 1, 1, 12, 2, 0),
        )
        deleted = service_a.delete_job(owner_id=101, job_id=created.job_id)
        assert deleted is True
        assert event.dry_run is True
        assert str(event.correlation_id or "").strip()

        restored = SchedulerService(
            repository=ScheduledJobRepository(cfg_a.defaults.state_path),
            event_bus=SystemEventBus(),
            scheduler_config=cfg_a.scheduler,
        )
        trail = restored.list_audit_trail(owner_id=101)
        assert [item.action for item in trail] == ["delete", "update", "create"]
        assert all(str(item.correlation_id or "").strip() for item in trail)
        assert trail[0].before["job_id"] == "job-audit"
        assert trail[0].after == {}
        assert trail[1].before["job_name"] == "Audit digest"
        assert trail[1].after["job_name"] == "Audit digest v2"
        assert trail[2].after["target_mode"] == "manager"

        isolated = SchedulerService(
            repository=ScheduledJobRepository(cfg_b.defaults.state_path),
            event_bus=SystemEventBus(),
            scheduler_config=cfg_b.scheduler,
        )
        assert isolated.list_audit_trail(owner_id=101) == []
        assert updated.cron == "*/30 * * * *"

    asyncio.run(_exercise())


def test_cron_dom_dow_or_semantics() -> None:
    schedule = CronSchedule.parse("0 0 1 * 1", timezone_name="UTC")

    assert schedule.matches(datetime(2026, 6, 1, 0, 0, tzinfo=timezone.utc)) is True
    assert schedule.matches(datetime(2026, 6, 8, 0, 0, tzinfo=timezone.utc)) is True
    assert schedule.matches(datetime(2026, 7, 1, 0, 0, tzinfo=timezone.utc)) is True
    assert schedule.matches(datetime(2026, 7, 8, 0, 0, tzinfo=timezone.utc)) is False
    assert schedule.next_after(_ts(2026, 6, 1, 0, 0, 1)) == _ts(2026, 6, 8, 0, 0, 0)

    dom_only = CronSchedule.parse("0 0 1 * *", timezone_name="UTC")
    assert dom_only.matches(datetime(2026, 7, 1, 0, 0, tzinfo=timezone.utc)) is True
    assert dom_only.matches(datetime(2026, 6, 8, 0, 0, tzinfo=timezone.utc)) is False

    dow_only = CronSchedule.parse("0 0 * * 1", timezone_name="UTC")
    assert dow_only.matches(datetime(2026, 6, 8, 0, 0, tzinfo=timezone.utc)) is True
    assert dow_only.matches(datetime(2026, 7, 1, 0, 0, tzinfo=timezone.utc)) is False

    sunday_zero = CronSchedule.parse("0 0 * * 0", timezone_name="UTC")
    sunday_seven = CronSchedule.parse("0 0 * * 7", timezone_name="UTC")
    sunday = datetime(2026, 6, 7, 0, 0, tzinfo=timezone.utc)
    assert sunday_zero.matches(sunday) is True
    assert sunday_seven.matches(sunday) is True


def test_cron_simple_expression_regression() -> None:
    schedule = CronSchedule.parse("*/15 9-17 * * 1-5", timezone_name="UTC")

    assert schedule.matches(datetime(2026, 6, 8, 9, 30, tzinfo=timezone.utc)) is True
    assert schedule.matches(datetime(2026, 6, 8, 9, 31, tzinfo=timezone.utc)) is False
    assert schedule.matches(datetime(2026, 6, 7, 9, 30, tzinfo=timezone.utc)) is False
    assert schedule.next_after(_ts(2026, 6, 8, 9, 31, 0)) == _ts(2026, 6, 8, 9, 45, 0)
