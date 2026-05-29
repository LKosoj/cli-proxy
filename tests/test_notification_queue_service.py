import asyncio
from types import SimpleNamespace
from unittest.mock import patch

from app.services.notification_queue_service import NotificationQueueService
from app.services.telegram_transport import TelegramTransportContext, TelegramTransportService
from sessions.conversation_scope import ConversationScope


def test_notification_queue_service_serializes_notifications_per_scope() -> None:
    async def _run() -> None:
        clock_state = {"now": 0.0}
        sleeps: list[float] = []

        async def _fake_sleep(seconds: float) -> None:
            sleeps.append(seconds)
            clock_state["now"] += seconds

        service = NotificationQueueService(
            min_interval_sec=0.5,
            clock=lambda: clock_state["now"],
            sleep=_fake_sleep,
        )
        await service.start()
        scope_one = ConversationScope.from_parts(-100777000111, 101)
        scope_two = ConversationScope.from_parts(-100777000111, 202)
        events: list[tuple[str, str, float]] = []
        first_started = asyncio.Event()
        first_released = asyncio.Event()
        second_scope_done = asyncio.Event()

        async def _first() -> str:
            events.append(("scope1", "first:start", clock_state["now"]))
            first_started.set()
            await first_released.wait()
            events.append(("scope1", "first:end", clock_state["now"]))
            return "first"

        async def _second() -> str:
            events.append(("scope1", "second", clock_state["now"]))
            return "second"

        async def _other_scope() -> str:
            events.append(("scope2", "only", clock_state["now"]))
            second_scope_done.set()
            return "other"

        first_task = asyncio.create_task(
            service.enqueue(scope_one, operation="send_message", factory=_first)
        )
        await asyncio.wait_for(first_started.wait(), timeout=1.0)
        second_task = asyncio.create_task(
            service.enqueue(scope_one, operation="send_document", factory=_second)
        )
        other_scope_task = asyncio.create_task(
            service.enqueue(scope_two, operation="send_message", factory=_other_scope)
        )

        await asyncio.wait_for(second_scope_done.wait(), timeout=1.0)
        assert second_task.done() is False

        first_released.set()

        assert await first_task == "first"
        assert await second_task == "second"
        assert await other_scope_task == "other"
        assert events == [
            ("scope1", "first:start", 0.0),
            ("scope2", "only", 0.0),
            ("scope1", "first:end", 0.0),
            ("scope1", "second", 0.5),
        ]
        assert sleeps == [0.5]
        await service.shutdown()

    asyncio.run(_run())


def test_notification_queue_service_applies_pacing_within_single_scope() -> None:
    async def _run() -> None:
        clock_state = {"now": 0.0}
        sleeps: list[float] = []

        async def _fake_sleep(seconds: float) -> None:
            sleeps.append(seconds)
            clock_state["now"] += seconds

        service = NotificationQueueService(
            min_interval_sec=0.25,
            clock=lambda: clock_state["now"],
            sleep=_fake_sleep,
        )
        await service.start()
        scope = ConversationScope.from_parts(-100777000111, 101)
        deliveries: list[tuple[str, float]] = []

        async def _deliver(label: str) -> str:
            deliveries.append((label, clock_state["now"]))
            return label

        first_task = asyncio.create_task(
            service.enqueue(scope, operation="send_message", factory=lambda: _deliver("first"))
        )
        second_task = asyncio.create_task(
            service.enqueue(scope, operation="send_document", factory=lambda: _deliver("second"))
        )

        assert await first_task == "first"
        assert await second_task == "second"
        assert deliveries == [("first", 0.0), ("second", 0.25)]
        assert sleeps == [0.25]
        await service.shutdown()

    asyncio.run(_run())


def test_notification_queue_service_supports_sequential_asyncio_runs_without_state_leak() -> None:
    service = NotificationQueueService()
    deliveries: list[str] = []

    async def _run_once(scope: ConversationScope, label: str) -> None:
        await service.start()

        async def _deliver() -> str:
            deliveries.append(label)
            return label

        result = await service.enqueue(scope, operation="send_message", factory=_deliver)
        assert result == label
        await service.drain()
        await service.shutdown()

    asyncio.run(_run_once(ConversationScope.from_parts(-100777000111, 101), "run-1"))
    asyncio.run(_run_once(ConversationScope.from_parts(-100777000111, 202), "run-2"))

    assert deliveries == ["run-1", "run-2"]


def test_notification_queue_service_start_and_shutdown_are_idempotent() -> None:
    service = NotificationQueueService()

    async def _run() -> None:
        await service.start()
        await service.start()
        assert service._started is True
        assert service._loop is asyncio.get_running_loop()
        await service.shutdown()
        await service.shutdown()
        assert service._started is False
        assert service._loop is None

    asyncio.run(_run())


def test_telegram_transport_serializes_outbound_by_conversation_scope() -> None:
    async def _run() -> None:
        clock_state = {"now": 0.0}
        sleeps: list[float] = []

        async def _fake_sleep(seconds: float) -> None:
            sleeps.append(seconds)
            clock_state["now"] += seconds

        queue_service = NotificationQueueService(
            min_interval_sec=0.5,
            clock=lambda: clock_state["now"],
            sleep=_fake_sleep,
        )
        await queue_service.start()
        events: list[tuple[str, int, str, float]] = []
        first_started = asyncio.Event()
        release_first = asyncio.Event()
        other_scope_done = asyncio.Event()

        class _FakeBot:
            async def send_message(self, **kwargs):
                thread_id = int(kwargs.get("message_thread_id") or 0)
                text = str(kwargs.get("text") or "")
                if thread_id == 101 and text == "first":
                    events.append(("message:start", thread_id, text, clock_state["now"]))
                    first_started.set()
                    await release_first.wait()
                    events.append(("message:end", thread_id, text, clock_state["now"]))
                elif thread_id == 202:
                    events.append(("message", thread_id, text, clock_state["now"]))
                    other_scope_done.set()
                else:
                    events.append(("message", thread_id, text, clock_state["now"]))
                return SimpleNamespace(message_id=len(events))

            async def send_document(self, **kwargs):
                thread_id = int(kwargs.get("message_thread_id") or 0)
                events.append(("document", thread_id, "doc", clock_state["now"]))
                return True

        raw_context = SimpleNamespace(bot=_FakeBot())
        bot_app = SimpleNamespace(
            notification_queue_service=queue_service,
            _last_delivery_error=None,
            iter_mode_runtimes=lambda: [],
        )
        transport = TelegramTransportService(bot_app)
        thread_one = TelegramTransportContext(
            raw_context,
            chat_id=-100777000111,
            message_thread_id=101,
            require_thread_id=True,
            session_uid="thread:-100777000111:101",
        )
        thread_two = TelegramTransportContext(
            raw_context,
            chat_id=-100777000111,
            message_thread_id=202,
            require_thread_id=True,
            session_uid="thread:-100777000111:202",
        )

        first_task = asyncio.create_task(transport.send_message(thread_one, text="first", md2=False))
        await asyncio.wait_for(first_started.wait(), timeout=1.0)
        second_task = asyncio.create_task(
            transport.send_document(thread_one, document=SimpleNamespace(name="note.txt"))
        )
        other_scope_task = asyncio.create_task(
            transport.send_message(thread_two, text="other", md2=False)
        )

        await asyncio.wait_for(other_scope_done.wait(), timeout=1.0)
        assert second_task.done() is False

        release_first.set()

        first_result = await first_task
        assert getattr(first_result, "message_id", 0) == 3
        assert await second_task is True
        await other_scope_task
        await queue_service.shutdown()

        assert events == [
            ("message:start", 101, "first", 0.0),
            ("message", 202, "other", 0.0),
            ("message:end", 101, "first", 0.0),
            ("document", 101, "doc", 0.5),
        ]
        assert sleeps == [0.5]

    asyncio.run(_run())


def test_telegram_transport_chunked_messages_do_not_interleave_within_same_scope() -> None:
    async def _run() -> None:
        queue_service = NotificationQueueService(min_interval_sec=0.0)
        await queue_service.start()
        delivery_order: list[tuple[int, str]] = []
        first_chunk_sent = asyncio.Event()
        release_first_message = asyncio.Event()

        class _FakeBot:
            async def send_message(self, **kwargs):
                thread_id = int(kwargs.get("message_thread_id") or 0)
                text = str(kwargs.get("text") or "")
                delivery_order.append((thread_id, text))
                if text == "alpha-1":
                    first_chunk_sent.set()
                    await release_first_message.wait()
                return SimpleNamespace(message_id=len(delivery_order))

        def _fake_split_text(text: str, limit=None) -> list[str]:
            if text == "alpha":
                return ["alpha-1", "alpha-2", "alpha-3"]
            if text == "beta":
                return ["beta-1", "beta-2"]
            return [text]

        raw_context = SimpleNamespace(bot=_FakeBot())
        bot_app = SimpleNamespace(
            notification_queue_service=queue_service,
            _last_delivery_error=None,
            iter_mode_runtimes=lambda: [],
        )
        transport = TelegramTransportService(bot_app)
        scope = TelegramTransportContext(
            raw_context,
            chat_id=-100777000111,
            message_thread_id=101,
            require_thread_id=True,
            session_uid="thread:-100777000111:101",
        )

        with patch.object(TelegramTransportService, "_split_text", side_effect=_fake_split_text):
            first_task = asyncio.create_task(transport.send_message(scope, text="alpha", md2=False))
            await asyncio.wait_for(first_chunk_sent.wait(), timeout=1.0)
            second_task = asyncio.create_task(transport.send_message(scope, text="beta", md2=False))
            await asyncio.sleep(0)
            assert second_task.done() is False

            release_first_message.set()

            first_result = await first_task
            second_result = await second_task

        assert getattr(first_result, "message_id", 0) == 3
        assert getattr(second_result, "message_id", 0) == 5
        assert delivery_order == [
            (101, "alpha-1"),
            (101, "alpha-2"),
            (101, "alpha-3"),
            (101, "beta-1"),
            (101, "beta-2"),
        ]
        await queue_service.shutdown()

    asyncio.run(_run())


def test_telegram_transport_chunked_messages_keep_atomicity_while_other_scope_progresses() -> None:
    async def _run() -> None:
        queue_service = NotificationQueueService(min_interval_sec=0.0)
        await queue_service.start()
        delivery_order: list[tuple[int, str]] = []
        first_chunk_sent = asyncio.Event()
        other_scope_done = asyncio.Event()
        release_first_message = asyncio.Event()

        class _FakeBot:
            async def send_message(self, **kwargs):
                thread_id = int(kwargs.get("message_thread_id") or 0)
                text = str(kwargs.get("text") or "")
                delivery_order.append((thread_id, text))
                if text == "alpha-1":
                    first_chunk_sent.set()
                    await release_first_message.wait()
                if text == "gamma-1":
                    other_scope_done.set()
                return SimpleNamespace(message_id=len(delivery_order))

        def _fake_split_text(text: str, limit=None) -> list[str]:
            if text == "alpha":
                return ["alpha-1", "alpha-2", "alpha-3"]
            if text == "beta":
                return ["beta-1", "beta-2"]
            if text == "gamma":
                return ["gamma-1"]
            return [text]

        raw_context = SimpleNamespace(bot=_FakeBot())
        bot_app = SimpleNamespace(
            notification_queue_service=queue_service,
            _last_delivery_error=None,
            iter_mode_runtimes=lambda: [],
        )
        transport = TelegramTransportService(bot_app)
        scope_one = TelegramTransportContext(
            raw_context,
            chat_id=-100777000111,
            message_thread_id=101,
            require_thread_id=True,
            session_uid="thread:-100777000111:101",
        )
        scope_two = TelegramTransportContext(
            raw_context,
            chat_id=-100777000111,
            message_thread_id=202,
            require_thread_id=True,
            session_uid="thread:-100777000111:202",
        )

        with patch.object(TelegramTransportService, "_split_text", side_effect=_fake_split_text):
            first_task = asyncio.create_task(transport.send_message(scope_one, text="alpha", md2=False))
            await asyncio.wait_for(first_chunk_sent.wait(), timeout=1.0)
            second_task = asyncio.create_task(transport.send_message(scope_one, text="beta", md2=False))
            other_scope_task = asyncio.create_task(transport.send_message(scope_two, text="gamma", md2=False))

            await asyncio.wait_for(other_scope_done.wait(), timeout=1.0)
            assert second_task.done() is False

            release_first_message.set()

            await first_task
            await second_task
            await other_scope_task

        assert delivery_order == [
            (101, "alpha-1"),
            (202, "gamma-1"),
            (101, "alpha-2"),
            (101, "alpha-3"),
            (101, "beta-1"),
            (101, "beta-2"),
        ]
        await queue_service.shutdown()

    asyncio.run(_run())


def test_notification_queue_service_cancel_scope_cancels_active_and_queued_items() -> None:
    async def _run() -> None:
        service = NotificationQueueService(min_interval_sec=0.0)
        await service.start()
        scope = ConversationScope.from_parts(-100777000111, 101)
        events: list[str] = []
        first_started = asyncio.Event()

        async def _first() -> str:
            events.append("first:start")
            first_started.set()
            await asyncio.Event().wait()
            return "first"

        async def _second() -> str:
            events.append("second")
            return "second"

        first_task = asyncio.create_task(
            service.enqueue(scope, operation="send_message", factory=_first)
        )
        await asyncio.wait_for(first_started.wait(), timeout=1.0)
        second_task = asyncio.create_task(
            service.enqueue(scope, operation="send_document", factory=_second)
        )
        await asyncio.sleep(0)

        cancelled = await service.cancel_scope(scope.session_uid)
        results = await asyncio.gather(first_task, second_task, return_exceptions=True)

        assert cancelled >= 2
        assert events == ["first:start"]
        assert all(isinstance(item, asyncio.CancelledError) for item in results)
        await service.shutdown()

    asyncio.run(_run())
