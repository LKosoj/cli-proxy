import asyncio
import time
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
from session import session_runtime_uid


class _FakeBotApp:
    def __init__(self) -> None:
        self.config = types.SimpleNamespace(defaults=types.SimpleNamespace())
        self.manager_resume_pending = {}
        self.manager = types.SimpleNamespace(_persist_sessions=lambda: None)
        self.edited = []

    async def _send_message(self, _context, *, chat_id: int, text: str, **_kwargs):
        return None

    async def _edit_message(
        self,
        _context,
        *,
        chat_id: int,
        message_id: int,
        text: str,
        **_kwargs,
    ):
        self.edited.append((chat_id, message_id, text))
        return True


def _build_mode(bot_app: _FakeBotApp, *, pipeline_calls: list[tuple[str, dict]]) -> ManagerMode:
    mode = ManagerMode()

    async def _run_mode_pipeline(_session, _prompt, dest, _context, mode_id):
        pipeline_calls.append((str(mode_id), dict(dest)))

    mode.initialize(
        config=bot_app.config,
        services={
            "tasks": TaskService(),
            "dialogs": types.SimpleNamespace(
                is_active=(lambda **_k: False),
                start=(lambda **_k: None),
                end=(lambda **_k: None),
            ),
            "session_control": SessionControlService(
                persist_sessions=bot_app.manager._persist_sessions,
                cancel_mode_tasks=(
                    lambda _sid, _mid, _timeout: asyncio.sleep(0, result=0)
                ),
                cancel_session_tasks=(
                    lambda _sid, _timeout: asyncio.sleep(0, result=0)
                ),
            ),
            "manager_pending": DictStateService(bot_app.manager_resume_pending),
            "pipeline": ModePipelineService(run_mode_pipeline_fn=_run_mode_pipeline),
            "messaging_factory": (
                lambda ctx: MessagingService(
                    send_message=bot_app._send_message,
                    edit_message=bot_app._edit_message,
                    transport_context=ctx,
                )
            ),
            "runtime_by_capability": (lambda _cap: None),
        },
    )
    return mode


def test_manager_isolated_resume_continue_rejects_invalid_pending() -> None:
    async def _run() -> None:
        pipeline_calls = []
        bot_app = _FakeBotApp()
        mode = _build_mode(bot_app, pipeline_calls=pipeline_calls)
        session = types.SimpleNamespace(
            id="s1",
            workdir="/tmp",
            busy=False,
            run_lock=asyncio.Lock(),
        )
        pending_key = session_runtime_uid(session)
        query = types.SimpleNamespace(
            message=types.SimpleNamespace(chat_id=123, message_id=10)
        )

        bot_app.manager_resume_pending[pending_key] = "invalid_pending"
        await mode.handle_callback(
            CallbackModel(action="resume_continue", chat_id=123, user_id=None, payload={}),
            {
                "bot_app": bot_app,
                "session": session,
                "chat_id": 123,
                "context": object(),
                "query": query,
            },
        )
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        assert not pipeline_calls
        assert bot_app.edited
        assert bot_app.edited[-1][2] == "Выбор устарел. Пришлите задачу заново."

    asyncio.run(_run())


def test_manager_isolated_duplicate_run_blocked_when_session_busy() -> None:
    async def _run() -> None:
        pipeline_calls = []
        bot_app = _FakeBotApp()
        mode = _build_mode(bot_app, pipeline_calls=pipeline_calls)
        session = types.SimpleNamespace(
            id="s1",
            workdir="/tmp",
            busy=True,
            run_lock=asyncio.Lock(),
        )
        query = types.SimpleNamespace(
            message=types.SimpleNamespace(chat_id=123, message_id=10)
        )

        await mode.handle_callback(
            CallbackModel(action="failed_retry", chat_id=123, user_id=None, payload={}),
            {
                "bot_app": bot_app,
                "session": session,
                "chat_id": 123,
                "context": object(),
                "query": query,
            },
        )
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        assert not pipeline_calls
        assert bot_app.edited
        assert "уже выполняется" in str(bot_app.edited[-1][2] or "").lower()

    asyncio.run(_run())


def test_manager_isolated_duplicate_run_blocked_when_run_lock_is_locked() -> None:
    async def _run() -> None:
        pipeline_calls = []
        bot_app = _FakeBotApp()
        mode = _build_mode(bot_app, pipeline_calls=pipeline_calls)
        run_lock = asyncio.Lock()
        await run_lock.acquire()
        session = types.SimpleNamespace(
            id="s1",
            workdir="/tmp",
            busy=False,
            run_lock=run_lock,
        )
        query = types.SimpleNamespace(
            message=types.SimpleNamespace(chat_id=123, message_id=10)
        )

        await mode.handle_callback(
            CallbackModel(action="failed_retry", chat_id=123, user_id=None, payload={}),
            {
                "bot_app": bot_app,
                "session": session,
                "chat_id": 123,
                "context": object(),
                "query": query,
            },
        )
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        assert not pipeline_calls
        assert bot_app.edited
        assert "уже выполняется" in str(bot_app.edited[-1][2] or "").lower()

        run_lock.release()
        await mode.handle_callback(
            CallbackModel(action="failed_retry", chat_id=123, user_id=None, payload={}),
            {
                "bot_app": bot_app,
                "session": session,
                "chat_id": 123,
                "context": object(),
                "query": query,
            },
        )
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        assert len(pipeline_calls) == 1
        assert pipeline_calls[-1][0] == "manager"

    asyncio.run(_run())


def test_manager_isolated_stale_then_valid_pending_allows_second_run() -> None:
    async def _run() -> None:
        pipeline_calls = []
        bot_app = _FakeBotApp()
        mode = _build_mode(bot_app, pipeline_calls=pipeline_calls)
        session = types.SimpleNamespace(
            id="s1",
            workdir="/tmp",
            busy=False,
            run_lock=asyncio.Lock(),
        )
        pending_key = session_runtime_uid(session)
        query = types.SimpleNamespace(
            message=types.SimpleNamespace(chat_id=123, message_id=10)
        )

        bot_app.manager_resume_pending[pending_key] = {
            "prompt": "go",
            "dest": {"kind": "desktop", "chat_id": 123},
            "created_at": time.time() - 99999,
        }
        await mode.handle_callback(
            CallbackModel(action="resume_continue", chat_id=123, user_id=None, payload={}),
            {
                "bot_app": bot_app,
                "session": session,
                "chat_id": 123,
                "context": object(),
                "query": query,
            },
        )
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        assert not pipeline_calls
        assert bot_app.edited
        assert bot_app.edited[-1][2] == "Выбор устарел. Пришлите задачу заново."

        bot_app.manager_resume_pending[pending_key] = {
            "prompt": "go",
            "dest": {"kind": "desktop", "chat_id": 123, "chat_type": "desktop"},
            "created_at": time.time(),
        }
        await mode.handle_callback(
            CallbackModel(action="resume_continue", chat_id=123, user_id=None, payload={}),
            {
                "bot_app": bot_app,
                "session": session,
                "chat_id": 123,
                "context": object(),
                "query": query,
            },
        )
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        assert len(pipeline_calls) == 1
        assert pipeline_calls[-1][0] == "manager"

    asyncio.run(_run())
