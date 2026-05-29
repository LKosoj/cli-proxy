import asyncio

from app.services.task_service import TaskService


def test_task_service_priority_sorting_and_progress():
    async def _run():
        svc = TaskService()
        started = asyncio.Event()

        async def sleeper(token):
            started.set()
            while not token.is_cancelled:
                await asyncio.sleep(0.01)

        low = svc.create(name="low", runner=sleeper, session_id="s1", priority=0)
        high = svc.create(name="high", runner=sleeper, session_id="s1", priority=5)
        await asyncio.wait_for(started.wait(), timeout=0.5)

        tasks = svc.list_active(session_id="s1")
        assert tasks[0].task_id == high.task_id
        assert tasks[1].task_id == low.task_id

        assert svc.set_priority(low.task_id, 10) is True
        tasks = svc.list_active(session_id="s1")
        assert tasks[0].task_id == low.task_id

        assert svc.set_progress(low.task_id, progress=0.5, stage="plan") is True
        rec = svc.get(low.task_id)
        assert rec is not None
        assert rec.progress == 0.5
        assert rec.stage == "plan"

        await svc.cancel_session("s1", timeout_s=0.2)

    asyncio.run(_run())


def test_task_service_segregates_same_raw_session_id_by_session_uid():
    async def _run():
        svc = TaskService()

        async def sleeper(token):
            while not token.is_cancelled:
                await asyncio.sleep(0.01)

        svc.create(name="scope-a", runner=sleeper, session_id="s1", session_uid="thread:1:101", priority=1)
        svc.create(name="scope-b", runner=sleeper, session_id="s1", session_uid="thread:1:202", priority=1)

        await asyncio.sleep(0.03)
        assert [rec.name for rec in svc.list_active(session_uid="thread:1:101")] == ["scope-a"]
        assert [rec.name for rec in svc.list_active(session_uid="thread:1:202")] == ["scope-b"]
        assert svc.list_active(session_id="s1") == []

        cancelled = await svc.cancel_session(session_uid="thread:1:101", timeout_s=0.2)
        assert cancelled == 1
        assert [rec.name for rec in svc.list_active(session_uid="thread:1:202")] == ["scope-b"]

        await svc.cancel_session(session_uid="thread:1:202", timeout_s=0.2)

    asyncio.run(_run())


def test_task_service_keeps_lingering_tasks_visible_after_cancel_timeout():
    async def _run():
        svc = TaskService()
        started = asyncio.Event()
        release = asyncio.Event()

        async def stubborn(token):
            _ = token
            started.set()
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                await release.wait()
                raise

        svc.create(name="stubborn", runner=stubborn, session_uid="thread:1:101", priority=1)
        await asyncio.wait_for(started.wait(), timeout=0.5)

        cancelled = await svc.cancel_session(session_uid="thread:1:101", timeout_s=0.01)
        assert cancelled == 1
        assert [rec.name for rec in svc.list_active(session_uid="thread:1:101")] == ["stubborn"]

        release.set()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert svc.list_active(session_uid="thread:1:101") == []

    asyncio.run(_run())
