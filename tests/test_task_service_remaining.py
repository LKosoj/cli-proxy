import asyncio

from app.services.task_service import TaskService


def test_task_service_remaining_scenarios() -> None:
    async def _run() -> None:
        svc = TaskService()

        async def long_runner(token):
            while not token.is_cancelled:
                await asyncio.sleep(0.01)

        async def short_runner(_token):
            await asyncio.sleep(0)
            return "ok"

        low = svc.create(name="low", runner=long_runner, session_id="s1", priority=1)
        high = svc.create(name="high", runner=long_runner, session_id="s2", priority=5)

        tasks = svc.list_active()
        assert [t.task_id for t in tasks][:2] == [high.task_id, low.task_id]

        assert svc.set_priority("missing-task", 10) is False
        assert svc.set_progress("missing-task", progress=0.3, stage="x") is False

        assert svc.set_progress(low.task_id, progress=2.0, stage="stage-a") is True
        rec = svc.get(low.task_id)
        assert rec is not None
        assert rec.progress == 1.0
        assert rec.stage == "stage-a"

        # Empty stage must not wipe current stage; invalid progress converts to 0.0.
        assert svc.set_progress(low.task_id, progress="bad", stage="") is True  # type: ignore[arg-type]
        rec = svc.get(low.task_id)
        assert rec is not None
        assert rec.progress == 0.0
        assert rec.stage == "stage-a"

        # Done task must be pruned by getters/listing.
        done = svc.create(name="done", runner=short_runner, session_id="s3", priority=0)
        await asyncio.sleep(0.05)
        assert svc.get(done.task_id) is None

        await svc.cancel_session("s1", timeout_s=0.2)
        await svc.cancel_session("s2", timeout_s=0.2)
        assert svc.list_active() == []

    asyncio.run(_run())
