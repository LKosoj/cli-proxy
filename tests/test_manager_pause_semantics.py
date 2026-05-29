import asyncio
import types

from modes.manager.mode import ManagerMode
from modes.sdk import (
    CallbackModel,
    DictStateService,
    MessagingService,
    ModePipelineService,
    SessionControlService,
    TaskService,
)
from modes.sdk.planning import load_plan, save_plan
from modes.sdk.runtime.contracts import DevTask, ProjectPlan
from session import session_runtime_uid


def _build_mode(bot_app, *, task_service: TaskService, run_mode_pipeline_fn, runtime_by_capability) -> ManagerMode:
    mode = ManagerMode()
    mode.initialize(
        config=bot_app.config,
        services={
            "tasks": task_service,
            "dialogs": types.SimpleNamespace(
                is_active=(lambda **_k: False),
                start=(lambda **_k: None),
                end=(lambda **_k: None),
            ),
            "session_control": SessionControlService(
                persist_sessions=bot_app.manager._persist_sessions,
                cancel_mode_tasks=(lambda sid, mid, timeout: task_service.cancel_all(
                    session_id=sid,
                    mode_id=mid,
                    timeout_s=timeout,
                )),
                cancel_session_tasks=(lambda sid, timeout: task_service.cancel_session(
                    session_id=sid,
                    timeout_s=timeout,
                )),
            ),
            "manager_pending": DictStateService(bot_app.manager_resume_pending),
            "pipeline": ModePipelineService(run_mode_pipeline_fn=run_mode_pipeline_fn),
            "messaging_factory": (lambda ctx: MessagingService(
                send_message=bot_app._send_message,
                edit_message=bot_app._edit_message,
                transport_context=ctx,
            )),
            "runtime_by_capability": runtime_by_capability,
        },
    )
    return mode


def test_manager_pause_synchronously_cancels_running_task_and_persists_paused_status(tmp_path) -> None:
    async def _run() -> None:
        started = asyncio.Event()
        cancelled = asyncio.Event()
        pipeline_calls = []

        class _FakeRuntime:
            def pause(self, session) -> None:
                plan = load_plan(session.workdir)
                assert plan is not None
                plan.set_status("paused")
                save_plan(session.workdir, plan)

        class _FakeBotApp:
            def __init__(self):
                self.config = types.SimpleNamespace(defaults=types.SimpleNamespace())
                self.manager_resume_pending = {}
                self.manager = types.SimpleNamespace(_persist_sessions=lambda: None)

            async def _send_message(self, _context, *, chat_id: int, text: str, **_kwargs):
                return None

            async def _edit_message(self, _context, *, chat_id: int, message_id: int, text: str, **_kwargs):
                return True

        async def _run_mode_pipeline(_session, _prompt, _dest, _context, _mode_id):
            pipeline_calls.append(_mode_id)
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise

        bot_app = _FakeBotApp()
        tasks = TaskService()
        runtime = _FakeRuntime()
        mode = _build_mode(
            bot_app,
            task_service=tasks,
            run_mode_pipeline_fn=_run_mode_pipeline,
            runtime_by_capability=(lambda cap: runtime if cap == "manager_control" else None),
        )

        plan = ProjectPlan(
            project_goal="g",
            tasks=[DevTask(id="t1", title="t1", description="", acceptance_criteria=[], status="in_progress")],
            status="active",
        )
        save_plan(str(tmp_path), plan)

        session = types.SimpleNamespace(
            id="s1",
            workdir=str(tmp_path),
            busy=False,
            run_lock=asyncio.Lock(),
            active_mode="manager",
            manager_quiet_mode=False,
            queue=[],
        )
        query = types.SimpleNamespace(message=types.SimpleNamespace(chat_id=123, message_id=10))
        ctx = {
            "bot_app": bot_app,
            "session": session,
            "chat_id": 123,
            "context": object(),
            "query": query,
        }

        await mode.handle_callback(CallbackModel(action="failed_retry", chat_id=123, user_id=None, payload={}), ctx)
        await started.wait()
        session_uid = session_runtime_uid(session)
        assert tasks.list(session_uid=session_uid, mode_id="manager")

        await mode.handle_callback(CallbackModel(action="pause", chat_id=123, user_id=None, payload={}), ctx)

        assert cancelled.is_set()
        assert tasks.list(session_uid=session_uid, mode_id="manager") == []
        updated_plan = load_plan(str(tmp_path))
        assert updated_plan is not None
        assert updated_plan.status == "paused"
        assert len(pipeline_calls) == 1

    asyncio.run(_run())


def test_manager_pause_allows_next_run_after_sync_cancel(tmp_path) -> None:
    async def _run() -> None:
        started = asyncio.Event()
        cancelled = asyncio.Event()
        release_first = asyncio.Event()
        pipeline_calls = []

        class _FakeRuntime:
            def pause(self, session) -> None:
                plan = load_plan(session.workdir)
                assert plan is not None
                plan.set_status("paused")
                save_plan(session.workdir, plan)

        class _FakeBotApp:
            def __init__(self):
                self.config = types.SimpleNamespace(defaults=types.SimpleNamespace())
                self.manager_resume_pending = {}
                self.manager = types.SimpleNamespace(_persist_sessions=lambda: None)

            async def _send_message(self, _context, *, chat_id: int, text: str, **_kwargs):
                return None

            async def _edit_message(self, _context, *, chat_id: int, message_id: int, text: str, **_kwargs):
                return True

        async def _run_mode_pipeline(_session, _prompt, _dest, _context, _mode_id):
            pipeline_calls.append(_mode_id)
            if len(pipeline_calls) == 1:
                started.set()
                try:
                    await release_first.wait()
                except asyncio.CancelledError:
                    cancelled.set()
                    raise
                return None
            return None

        bot_app = _FakeBotApp()
        tasks = TaskService()
        runtime = _FakeRuntime()
        mode = _build_mode(
            bot_app,
            task_service=tasks,
            run_mode_pipeline_fn=_run_mode_pipeline,
            runtime_by_capability=(lambda cap: runtime if cap == "manager_control" else None),
        )

        plan = ProjectPlan(
            project_goal="g",
            tasks=[DevTask(id="t1", title="t1", description="", acceptance_criteria=[], status="in_progress")],
            status="active",
        )
        save_plan(str(tmp_path), plan)

        session = types.SimpleNamespace(
            id="s1",
            workdir=str(tmp_path),
            busy=False,
            run_lock=asyncio.Lock(),
            active_mode="manager",
            manager_quiet_mode=False,
            queue=[],
        )
        query = types.SimpleNamespace(message=types.SimpleNamespace(chat_id=123, message_id=10))
        ctx = {
            "bot_app": bot_app,
            "session": session,
            "chat_id": 123,
            "context": object(),
            "query": query,
        }

        await mode.handle_callback(CallbackModel(action="failed_retry", chat_id=123, user_id=None, payload={}), ctx)
        await started.wait()
        session_uid = session_runtime_uid(session)

        await mode.handle_callback(CallbackModel(action="pause", chat_id=123, user_id=None, payload={}), ctx)
        assert cancelled.is_set()
        assert tasks.list(session_uid=session_uid, mode_id="manager") == []

        await mode.handle_callback(CallbackModel(action="failed_retry", chat_id=123, user_id=None, payload={}), ctx)
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        assert len(pipeline_calls) == 2
        release_first.set()

    asyncio.run(_run())
