from __future__ import annotations

import asyncio
import logging

from app.events.bus import (
    DesktopCommandEvent,
    ManageTasksChangedEvent,
    MiniAppCommandEvent,
    ModeLaunchCompletedEvent,
    ModeLaunchRequestedEvent,
    NotificationRequestedEvent,
    ScheduledJobEvent,
    SystemEventBus,
    TelegramIngressEvent,
    WebhookReceivedEvent,
)


def test_system_event_bus_delivers_typed_and_named_subscribers_for_typed_event() -> None:
    bus = SystemEventBus()
    received: list[tuple[str, object]] = []

    async def _named_handler(event_name: str, payload: dict) -> None:
        received.append(("named", (event_name, dict(payload))))

    def _typed_handler(event: TelegramIngressEvent) -> None:
        received.append(("typed", event))

    bus.subscribe(TelegramIngressEvent.EVENT_NAME, _named_handler)
    bus.subscribe(TelegramIngressEvent, _typed_handler)

    asyncio.run(
        bus.publish(
            TelegramIngressEvent(
                chat_id=101,
                user_id=202,
                update_type="message",
                payload={"text": "hello"},
            )
        )
    )

    assert len(received) == 2
    named = received[0]
    typed = received[1]
    assert named[0] == "named"
    assert named[1] == (
        TelegramIngressEvent.EVENT_NAME,
        {
            "chat_id": 101,
            "user_id": 202,
            "update_type": "message",
            "payload": {"text": "hello"},
        },
    )
    assert typed[0] == "typed"
    assert isinstance(typed[1], TelegramIngressEvent)
    assert typed[1].chat_id == 101
    assert typed[1].user_id == 202


def test_system_event_bus_processes_events_independently_and_isolates_handler_failures(caplog) -> None:
    bus = SystemEventBus()
    processed: list[int] = []

    async def _exercise() -> None:
        release_first = asyncio.Event()
        first_started = asyncio.Event()

        async def _slow_handler(event_name: str, payload: dict) -> None:
            seq = int(payload["payload"]["seq"])
            if seq == 1:
                first_started.set()
                await release_first.wait()
            processed.append(seq)

        async def _failing_handler(_event_name: str, payload: dict) -> None:
            if int(payload["payload"]["seq"]) == 2:
                raise RuntimeError("boom")

        bus.subscribe(ScheduledJobEvent.EVENT_NAME, _slow_handler)
        bus.subscribe(ScheduledJobEvent.EVENT_NAME, _failing_handler)

        first_publish = asyncio.create_task(
            bus.publish(
                ScheduledJobEvent(job_name="job-1", status="running", scheduled_for=1.0, payload={"seq": 1})
            )
        )
        await first_started.wait()

        second_publish = asyncio.create_task(
            bus.publish(
                ScheduledJobEvent(job_name="job-2", status="running", scheduled_for=2.0, payload={"seq": 2})
            )
        )

        await asyncio.wait_for(second_publish, timeout=1.0)
        assert processed == [2]

        release_first.set()
        await asyncio.wait_for(first_publish, timeout=1.0)

    caplog.set_level(logging.ERROR, logger="app.events.bus")
    asyncio.run(_exercise())

    assert processed == [2, 1]
    assert "system event handler failed event=scheduler.job" in caplog.text


def test_system_event_bus_supports_string_publish_to_typed_subscriber_and_sequential_runs() -> None:
    bus = SystemEventBus()
    deliveries: list[tuple[str, str]] = []

    def _typed_handler(event: WebhookReceivedEvent) -> None:
        deliveries.append((event.source, event.path))

    bus.subscribe(WebhookReceivedEvent, _typed_handler)

    asyncio.run(
        bus.publish(
            WebhookReceivedEvent.EVENT_NAME,
            {
                "source": "miniapp",
                "path": "/run-1",
                "method": "POST",
                "payload": {"intent": "first"},
            },
        )
    )
    asyncio.run(
        bus.publish(
            WebhookReceivedEvent.EVENT_NAME,
            {
                "source": "miniapp",
                "path": "/run-2",
                "method": "POST",
                "payload": {"intent": "second"},
            },
        )
    )

    assert deliveries == [("miniapp", "/run-1"), ("miniapp", "/run-2")]


def test_system_event_bus_supports_required_additional_typed_events() -> None:
    bus = SystemEventBus()
    deliveries: list[tuple[str, str]] = []

    def _desktop_handler(event: DesktopCommandEvent) -> None:
        deliveries.append(("desktop", event.command))

    def _miniapp_handler(event: MiniAppCommandEvent) -> None:
        deliveries.append(("miniapp", event.command))

    def _launch_requested_handler(event: ModeLaunchRequestedEvent) -> None:
        deliveries.append(("launch_requested", event.mode_id))

    def _launch_completed_handler(event: ModeLaunchCompletedEvent) -> None:
        deliveries.append(("launch_completed", event.status))

    def _notification_handler(event: NotificationRequestedEvent) -> None:
        deliveries.append(("notification", event.producer))

    def _manage_tasks_handler(event: ManageTasksChangedEvent) -> None:
        deliveries.append(("manage_tasks", event.scope_key))

    bus.subscribe(DesktopCommandEvent, _desktop_handler)
    bus.subscribe(MiniAppCommandEvent, _miniapp_handler)
    bus.subscribe(ModeLaunchRequestedEvent, _launch_requested_handler)
    bus.subscribe(ModeLaunchCompletedEvent, _launch_completed_handler)
    bus.subscribe(NotificationRequestedEvent, _notification_handler)
    bus.subscribe(ManageTasksChangedEvent, _manage_tasks_handler)

    async def _publish_events() -> None:
        await bus.publish(
            DesktopCommandEvent.EVENT_NAME,
            {
                "session_uid": "desktop:s1",
                "project_slug": "alpha",
                "command": "run",
            },
        )
        await bus.publish(
            MiniAppCommandEvent(
                user_id="telegram:1",
                session_uid="thread:-100:10",
                project_slug="alpha",
                command="open",
            )
        )
        await bus.publish(
            ModeLaunchRequestedEvent(
                origin="scheduler",
                mode_id="manager",
                session_uid="thread:-100:10",
            )
        )
        await bus.publish(
            ModeLaunchCompletedEvent(
                origin="scheduler",
                mode_id="manager",
                session_uid="thread:-100:10",
                status="ok",
            )
        )
        await bus.publish(
            NotificationRequestedEvent(
                channel="telegram",
                session_uid="thread:-100:10",
                producer="scheduler",
            )
        )
        await bus.publish(
            ManageTasksChangedEvent.EVENT_NAME,
            {
                "session_uid": "thread:-100:10",
                "scope_key": "thread:-100:10:manage_tasks:run-1",
                "tasks": [{"id": "t1", "content": "Inspect", "status": "pending"}],
            },
        )
        await bus.shutdown()

    asyncio.run(_publish_events())

    assert deliveries == [
        ("desktop", "run"),
        ("miniapp", "open"),
        ("launch_requested", "manager"),
        ("launch_completed", "ok"),
        ("notification", "scheduler"),
        ("manage_tasks", "thread:-100:10:manage_tasks:run-1"),
    ]


def test_system_event_bus_shutdown_does_not_resurrect_worker_from_inflight_publish() -> None:
    bus = SystemEventBus()
    seen: list[int] = []

    async def _exercise() -> None:
        release = asyncio.Event()
        started = asyncio.Event()

        async def _handler(_event_name: str, payload: dict) -> None:
            seq = int((payload.get("payload") or {}).get("seq") or 0)
            seen.append(seq)
            if seq == 1:
                started.set()
                await release.wait()
                await bus.publish(
                    ScheduledJobEvent(
                        job_name="late",
                        status="running",
                        scheduled_for=2.0,
                        payload={"seq": 2},
                    )
                )

        bus.subscribe(ScheduledJobEvent.EVENT_NAME, _handler)
        publish_task = asyncio.create_task(
            bus.publish(
                ScheduledJobEvent(
                    job_name="first",
                    status="running",
                    scheduled_for=1.0,
                    payload={"seq": 1},
                )
            )
        )
        await started.wait()
        shutdown_task = asyncio.create_task(bus.shutdown())
        await asyncio.sleep(0)
        release.set()
        await asyncio.wait_for(shutdown_task, timeout=1.0)
        await asyncio.wait_for(publish_task, timeout=1.0)

    asyncio.run(_exercise())

    assert seen == [1]
