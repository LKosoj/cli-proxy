import asyncio
import time
import types

from modes.manager.mode import ManagerMode
from modes.sdk import CallbackModel, DictStateService, MessagingService, ModePipelineService, SessionControlService, TaskService
from session import session_runtime_uid


def _build_mode(bot_app, *, pipeline_calls, runtime_by_capability=(lambda _cap: None)):
    mode = ManagerMode()

    async def _run_mode_pipeline(_session, _prompt, dest, _context, _mode_id):
        pipeline_calls.append((_mode_id, dict(dest)))

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
                cancel_mode_tasks=(lambda _sid, _mid, _timeout: asyncio.sleep(0, result=0)),
                cancel_session_tasks=(lambda _sid, _timeout: asyncio.sleep(0, result=0)),
            ),
            "manager_pending": DictStateService(bot_app.manager_resume_pending),
            "pipeline": ModePipelineService(run_mode_pipeline_fn=_run_mode_pipeline),
            "messaging_factory": (lambda ctx: MessagingService(
                send_message=bot_app._send_message,
                edit_message=bot_app._edit_message,
                transport_context=ctx,
            )),
            "runtime_by_capability": runtime_by_capability,
        },
    )
    return mode


def test_manager_resume_continue_uses_pending_dest_for_callback() -> None:
    async def _run() -> None:
        pipeline_calls = []

        class _FakeBotApp:
            def __init__(self):
                self.config = types.SimpleNamespace(defaults=types.SimpleNamespace())
                self.manager_resume_pending = {}
                self.manager = types.SimpleNamespace(_persist_sessions=lambda: None)

            async def _send_message(self, _context, *, chat_id: int, text: str, **_kwargs):
                return None

            async def _edit_message(self, *_args, **_kwargs):
                return True

        bot_app = _FakeBotApp()
        mode = _build_mode(bot_app, pipeline_calls=pipeline_calls)
        session = types.SimpleNamespace(id="s1", workdir="/tmp", busy=False, run_lock=asyncio.Lock())
        pending_key = session_runtime_uid(session)
        expected_dest = {
            "kind": "desktop",
            "chat_id": 123,
            "chat_type": "desktop",
            "user_id": 77,
        }
        bot_app.manager_resume_pending[pending_key] = {
            "prompt": "go",
            "dest": dict(expected_dest),
            "created_at": time.time(),
        }

        await mode.handle_callback(
            CallbackModel(action="resume_continue", chat_id=123, user_id=None, payload={}),
            {
                "bot_app": bot_app,
                "session": session,
                "chat_id": 123,
                "context": None,
                "query": None,
            },
        )
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        assert pending_key not in bot_app.manager_resume_pending
        assert pipeline_calls
        assert pipeline_calls[-1][0] == "manager"
        assert pipeline_calls[-1][1] == expected_dest

    asyncio.run(_run())


def test_manager_resume_paused_uses_desktop_dest_without_telegram_fallback(monkeypatch, tmp_path) -> None:
    async def _run() -> None:
        pipeline_calls = []

        class _FakePlan:
            status = "paused"

        class _FakeBotApp:
            def __init__(self):
                self.config = types.SimpleNamespace(defaults=types.SimpleNamespace())
                self.manager_resume_pending = {}
                self.manager = types.SimpleNamespace(_persist_sessions=lambda: None)

            async def _send_message(self, _context, *, chat_id: int, text: str, **_kwargs):
                return None

            async def _edit_message(self, *_args, **_kwargs):
                return True

        monkeypatch.setattr("modes.manager.mode.load_plan", lambda _workdir, **_kwargs: _FakePlan())
        monkeypatch.setattr("modes.manager.mode.save_plan", lambda _workdir, _plan, **_kwargs: None)

        bot_app = _FakeBotApp()
        mode = _build_mode(bot_app, pipeline_calls=pipeline_calls)
        session = types.SimpleNamespace(id="s2", workdir=str(tmp_path), busy=False, run_lock=asyncio.Lock())

        await mode.handle_callback(
            CallbackModel(action="resume_paused", chat_id=321, user_id=None, payload={}),
            {
                "bot_app": bot_app,
                "session": session,
                "chat_id": 321,
                "context": None,
                "query": None,
            },
        )
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        assert pipeline_calls
        assert pipeline_calls[-1][0] == "manager"
        assert pipeline_calls[-1][1] == {"kind": "desktop", "chat_id": 321}

    asyncio.run(_run())


def test_manager_resume_continue_stale_pending_requires_new_prompt() -> None:
    async def _run() -> None:
        pipeline_calls = []
        edited = []

        class _FakeBotApp:
            def __init__(self):
                self.config = types.SimpleNamespace(defaults=types.SimpleNamespace())
                self.manager_resume_pending = {}
                self.manager = types.SimpleNamespace(_persist_sessions=lambda: None)

            async def _send_message(self, _context, *, chat_id: int, text: str, **_kwargs):
                return None

            async def _edit_message(self, _context, *, chat_id: int, message_id: int, text: str, **_kwargs):
                edited.append((chat_id, message_id, text))
                return True

        bot_app = _FakeBotApp()
        mode = _build_mode(bot_app, pipeline_calls=pipeline_calls)
        session = types.SimpleNamespace(id="s1", workdir="/tmp", busy=False, run_lock=asyncio.Lock())
        pending_key = session_runtime_uid(session)
        bot_app.manager_resume_pending[pending_key] = {
            "prompt": "go",
            "dest": {"kind": "telegram", "chat_id": 123},
            "created_at": time.time() - 99999,
        }

        await mode.handle_callback(
            CallbackModel(action="resume_continue", chat_id=123, user_id=None, payload={}),
            {
                "bot_app": bot_app,
                "session": session,
                "chat_id": 123,
                "context": object(),
                "query": types.SimpleNamespace(message=types.SimpleNamespace(chat_id=123, message_id=10)),
            },
        )
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        assert not pipeline_calls
        assert edited
        assert edited[-1][2] == "Выбор устарел. Пришлите задачу заново."

    asyncio.run(_run())


def test_manager_resume_continue_missing_pending_requires_new_prompt() -> None:
    async def _run() -> None:
        pipeline_calls = []
        edited = []

        class _FakeBotApp:
            def __init__(self):
                self.config = types.SimpleNamespace(defaults=types.SimpleNamespace())
                self.manager_resume_pending = {}
                self.manager = types.SimpleNamespace(_persist_sessions=lambda: None)

            async def _send_message(self, _context, *, chat_id: int, text: str, **_kwargs):
                return None

            async def _edit_message(self, _context, *, chat_id: int, message_id: int, text: str, **_kwargs):
                edited.append((chat_id, message_id, text))
                return True

        bot_app = _FakeBotApp()
        mode = _build_mode(bot_app, pipeline_calls=pipeline_calls)
        session = types.SimpleNamespace(id="s1", workdir="/tmp", busy=False, run_lock=asyncio.Lock())

        await mode.handle_callback(
            CallbackModel(action="resume_continue", chat_id=123, user_id=None, payload={}),
            {
                "bot_app": bot_app,
                "session": session,
                "chat_id": 123,
                "context": object(),
                "query": types.SimpleNamespace(message=types.SimpleNamespace(chat_id=123, message_id=10)),
            },
        )
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        assert not pipeline_calls
        assert edited
        assert edited[-1][2] == "Выбор устарел. Пришлите задачу заново."

    asyncio.run(_run())


def test_manager_resume_continue_rejects_invalid_pending_then_accepts_valid_pending() -> None:
    async def _run() -> None:
        pipeline_calls = []
        edited = []

        class _FakeBotApp:
            def __init__(self):
                self.config = types.SimpleNamespace(defaults=types.SimpleNamespace())
                self.manager_resume_pending = {}
                self.manager = types.SimpleNamespace(_persist_sessions=lambda: None)

            async def _send_message(self, _context, *, chat_id: int, text: str, **_kwargs):
                return None

            async def _edit_message(self, _context, *, chat_id: int, message_id: int, text: str, **_kwargs):
                edited.append((chat_id, message_id, text))
                return True

        bot_app = _FakeBotApp()
        mode = _build_mode(bot_app, pipeline_calls=pipeline_calls)
        session = types.SimpleNamespace(id="s1", workdir="/tmp", busy=False, run_lock=asyncio.Lock())
        pending_key = session_runtime_uid(session)
        query = types.SimpleNamespace(message=types.SimpleNamespace(chat_id=123, message_id=10))

        bot_app.manager_resume_pending[pending_key] = "invalid_pending_value"
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
        assert edited
        assert edited[-1][2] == "Выбор устарел. Пришлите задачу заново."

        expected_dest = {
            "kind": "desktop",
            "chat_id": 123,
            "chat_type": "desktop",
            "user_id": 77,
        }
        bot_app.manager_resume_pending[pending_key] = {
            "prompt": "go",
            "dest": dict(expected_dest),
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

        assert pipeline_calls
        assert pipeline_calls[-1][0] == "manager"
        assert pipeline_calls[-1][1] == expected_dest

    asyncio.run(_run())


def test_manager_resume_continue_blocks_by_all_busy_signals_and_runs_after_recovery() -> None:
    async def _run() -> None:
        pipeline_calls = []
        edited = []
        tick_state = {"active": False}

        class _FakeBotApp:
            def __init__(self):
                self.config = types.SimpleNamespace(defaults=types.SimpleNamespace())
                self.manager_resume_pending = {}
                self.manager = types.SimpleNamespace(_persist_sessions=lambda: None)

            async def _send_message(self, _context, *, chat_id: int, text: str, **_kwargs):
                return None

            async def _edit_message(self, _context, *, chat_id: int, message_id: int, text: str, **_kwargs):
                edited.append((chat_id, message_id, text))
                return True

        bot_app = _FakeBotApp()
        mode = _build_mode(bot_app, pipeline_calls=pipeline_calls)
        session = types.SimpleNamespace(
            id="s1",
            workdir="/tmp",
            busy=False,
            run_lock=asyncio.Lock(),
            is_active_by_tick=lambda: bool(tick_state["active"]),
        )
        pending_key = session_runtime_uid(session)
        query = types.SimpleNamespace(message=types.SimpleNamespace(chat_id=123, message_id=10))

        def _put_pending() -> None:
            bot_app.manager_resume_pending[pending_key] = {
                "prompt": "continue",
                "dest": {"kind": "desktop", "chat_id": 123},
                "created_at": time.time(),
            }

        # busy=true
        _put_pending()
        session.busy = True
        await mode.handle_callback(
            CallbackModel(action="resume_continue", chat_id=123, user_id=None, payload={}),
            {"bot_app": bot_app, "session": session, "chat_id": 123, "context": object(), "query": query},
        )
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert not pipeline_calls
        assert pending_key in bot_app.manager_resume_pending
        assert "уже выполняется" in str(edited[-1][2] or "").lower()

        session.busy = False
        await mode.handle_callback(
            CallbackModel(action="resume_continue", chat_id=123, user_id=None, payload={}),
            {"bot_app": bot_app, "session": session, "chat_id": 123, "context": object(), "query": query},
        )
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert len(pipeline_calls) == 1
        assert pending_key not in bot_app.manager_resume_pending

        # run_lock.locked()=true
        _put_pending()
        await session.run_lock.acquire()
        try:
            await mode.handle_callback(
                CallbackModel(action="resume_continue", chat_id=123, user_id=None, payload={}),
                {"bot_app": bot_app, "session": session, "chat_id": 123, "context": object(), "query": query},
            )
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            assert len(pipeline_calls) == 1
            assert pending_key in bot_app.manager_resume_pending
            assert "уже выполняется" in str(edited[-1][2] or "").lower()
        finally:
            session.run_lock.release()

        await mode.handle_callback(
            CallbackModel(action="resume_continue", chat_id=123, user_id=None, payload={}),
            {"bot_app": bot_app, "session": session, "chat_id": 123, "context": object(), "query": query},
        )
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert len(pipeline_calls) == 2
        assert pending_key not in bot_app.manager_resume_pending

        # tick-active=true
        _put_pending()
        tick_state["active"] = True
        await mode.handle_callback(
            CallbackModel(action="resume_continue", chat_id=123, user_id=None, payload={}),
            {"bot_app": bot_app, "session": session, "chat_id": 123, "context": object(), "query": query},
        )
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert len(pipeline_calls) == 2
        assert pending_key in bot_app.manager_resume_pending
        assert "уже выполняется" in str(edited[-1][2] or "").lower()

        tick_state["active"] = False
        await mode.handle_callback(
            CallbackModel(action="resume_continue", chat_id=123, user_id=None, payload={}),
            {"bot_app": bot_app, "session": session, "chat_id": 123, "context": object(), "query": query},
        )
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert len(pipeline_calls) == 3
        assert pending_key not in bot_app.manager_resume_pending

    asyncio.run(_run())


def test_manager_failed_retry_double_click_does_not_create_duplicate_runs() -> None:
    async def _run() -> None:
        pipeline_calls = []
        edited = []
        started = asyncio.Event()
        release = asyncio.Event()

        class _FakeBotApp:
            def __init__(self):
                self.config = types.SimpleNamespace(defaults=types.SimpleNamespace())
                self.manager_resume_pending = {}
                self.manager = types.SimpleNamespace(_persist_sessions=lambda: None)

            async def _send_message(self, _context, *, chat_id: int, text: str, **_kwargs):
                return None

            async def _edit_message(self, _context, *, chat_id: int, message_id: int, text: str, **_kwargs):
                edited.append((chat_id, message_id, text))
                return True

        async def _run_mode_pipeline(_session, _prompt, dest, _context, _mode_id):
            pipeline_calls.append((_mode_id, dict(dest)))
            started.set()
            await release.wait()

        bot_app = _FakeBotApp()
        mode = ManagerMode()
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
                    cancel_mode_tasks=(lambda _sid, _mid, _timeout: asyncio.sleep(0, result=0)),
                    cancel_session_tasks=(lambda _sid, _timeout: asyncio.sleep(0, result=0)),
                ),
                "manager_pending": DictStateService(bot_app.manager_resume_pending),
                "pipeline": ModePipelineService(run_mode_pipeline_fn=_run_mode_pipeline),
                "messaging_factory": (lambda ctx: MessagingService(
                    send_message=bot_app._send_message,
                    edit_message=bot_app._edit_message,
                    transport_context=ctx,
                )),
                "runtime_by_capability": (lambda _cap: None),
            },
        )
        session = types.SimpleNamespace(id="s1", workdir="/tmp", busy=False, run_lock=asyncio.Lock())
        query = types.SimpleNamespace(message=types.SimpleNamespace(chat_id=123, message_id=10))

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
        await started.wait()

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
        assert any("уже выполняется" in str(text or "").lower() for _, _, text in edited)

        release.set()
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    asyncio.run(_run())


def test_manager_failed_retry_blocks_when_busy_then_runs_after_busy_cleared() -> None:
    async def _run() -> None:
        pipeline_calls = []
        edited = []

        class _FakeBotApp:
            def __init__(self):
                self.config = types.SimpleNamespace(defaults=types.SimpleNamespace())
                self.manager_resume_pending = {}
                self.manager = types.SimpleNamespace(_persist_sessions=lambda: None)

            async def _send_message(self, _context, *, chat_id: int, text: str, **_kwargs):
                return None

            async def _edit_message(self, _context, *, chat_id: int, message_id: int, text: str, **_kwargs):
                edited.append((chat_id, message_id, text))
                return True

        bot_app = _FakeBotApp()
        mode = _build_mode(bot_app, pipeline_calls=pipeline_calls)
        session = types.SimpleNamespace(id="s1", workdir="/tmp", busy=True, run_lock=asyncio.Lock())
        query = types.SimpleNamespace(message=types.SimpleNamespace(chat_id=123, message_id=10))

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
        assert edited
        assert "уже выполняется" in str(edited[-1][2] or "").lower()

        session.busy = False
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

        assert pipeline_calls
        assert pipeline_calls[-1][0] == "manager"

    asyncio.run(_run())


def test_manager_failed_retry_blocks_when_run_lock_locked_then_runs_after_unlock() -> None:
    async def _run() -> None:
        pipeline_calls = []
        edited = []

        class _FakeBotApp:
            def __init__(self):
                self.config = types.SimpleNamespace(defaults=types.SimpleNamespace())
                self.manager_resume_pending = {}
                self.manager = types.SimpleNamespace(_persist_sessions=lambda: None)

            async def _send_message(self, _context, *, chat_id: int, text: str, **_kwargs):
                return None

            async def _edit_message(self, _context, *, chat_id: int, message_id: int, text: str, **_kwargs):
                edited.append((chat_id, message_id, text))
                return True

        bot_app = _FakeBotApp()
        mode = _build_mode(bot_app, pipeline_calls=pipeline_calls)
        lock = asyncio.Lock()
        await lock.acquire()
        session = types.SimpleNamespace(id="s1", workdir="/tmp", busy=False, run_lock=lock)
        query = types.SimpleNamespace(message=types.SimpleNamespace(chat_id=123, message_id=10))

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
        assert edited
        assert "уже выполняется" in str(edited[-1][2] or "").lower()

        lock.release()
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

        assert pipeline_calls
        assert pipeline_calls[-1][0] == "manager"

    asyncio.run(_run())


def test_manager_failed_retry_blocks_when_tick_active_then_runs_after_tick_cleared() -> None:
    async def _run() -> None:
        pipeline_calls = []
        edited = []
        tick_state = {"active": True}

        class _FakeBotApp:
            def __init__(self):
                self.config = types.SimpleNamespace(defaults=types.SimpleNamespace())
                self.manager_resume_pending = {}
                self.manager = types.SimpleNamespace(_persist_sessions=lambda: None)

            async def _send_message(self, _context, *, chat_id: int, text: str, **_kwargs):
                return None

            async def _edit_message(self, _context, *, chat_id: int, message_id: int, text: str, **_kwargs):
                edited.append((chat_id, message_id, text))
                return True

        bot_app = _FakeBotApp()
        mode = _build_mode(bot_app, pipeline_calls=pipeline_calls)
        session = types.SimpleNamespace(
            id="s1",
            workdir="/tmp",
            busy=False,
            run_lock=asyncio.Lock(),
            is_active_by_tick=lambda: bool(tick_state["active"]),
        )
        query = types.SimpleNamespace(message=types.SimpleNamespace(chat_id=123, message_id=10))

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
        assert edited
        assert "уже выполняется" in str(edited[-1][2] or "").lower()

        tick_state["active"] = False
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

        assert pipeline_calls
        assert pipeline_calls[-1][0] == "manager"

    asyncio.run(_run())


def test_manager_resume_paused_blocks_by_all_busy_signals_and_runs_after_recovery(monkeypatch, tmp_path) -> None:
    async def _run() -> None:
        pipeline_calls = []
        edited = []
        tick_state = {"active": False}

        class _FakePlan:
            status = "paused"

        class _FakeBotApp:
            def __init__(self):
                self.config = types.SimpleNamespace(defaults=types.SimpleNamespace())
                self.manager_resume_pending = {}
                self.manager = types.SimpleNamespace(_persist_sessions=lambda: None)

            async def _send_message(self, _context, *, chat_id: int, text: str, **_kwargs):
                return None

            async def _edit_message(self, _context, *, chat_id: int, message_id: int, text: str, **_kwargs):
                edited.append((chat_id, message_id, text))
                return True

        monkeypatch.setattr("modes.manager.mode.load_plan", lambda _workdir, **_kwargs: _FakePlan())
        monkeypatch.setattr("modes.manager.mode.save_plan", lambda _workdir, _plan, **_kwargs: None)

        bot_app = _FakeBotApp()
        mode = _build_mode(bot_app, pipeline_calls=pipeline_calls)
        session = types.SimpleNamespace(
            id="s2",
            workdir=str(tmp_path),
            busy=False,
            run_lock=asyncio.Lock(),
            is_active_by_tick=lambda: bool(tick_state["active"]),
        )
        query = types.SimpleNamespace(message=types.SimpleNamespace(chat_id=123, message_id=10))

        # busy=true
        session.busy = True
        await mode.handle_callback(
            CallbackModel(action="resume_paused", chat_id=123, user_id=None, payload={}),
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
        assert edited
        assert "уже выполняется" in str(edited[-1][2] or "").lower()

        session.busy = False
        await mode.handle_callback(
            CallbackModel(action="resume_paused", chat_id=123, user_id=None, payload={}),
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

        assert pipeline_calls
        assert pipeline_calls[-1][0] == "manager"

        # run_lock.locked()=true
        await session.run_lock.acquire()
        try:
            await mode.handle_callback(
                CallbackModel(action="resume_paused", chat_id=123, user_id=None, payload={}),
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
            assert "уже выполняется" in str(edited[-1][2] or "").lower()
        finally:
            session.run_lock.release()

        await mode.handle_callback(
            CallbackModel(action="resume_paused", chat_id=123, user_id=None, payload={}),
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
        assert len(pipeline_calls) == 2

        # tick-active=true
        tick_state["active"] = True
        await mode.handle_callback(
            CallbackModel(action="resume_paused", chat_id=123, user_id=None, payload={}),
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
        assert len(pipeline_calls) == 2
        assert "уже выполняется" in str(edited[-1][2] or "").lower()

        tick_state["active"] = False
        await mode.handle_callback(
            CallbackModel(action="resume_paused", chat_id=123, user_id=None, payload={}),
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
        assert len(pipeline_calls) == 3

    asyncio.run(_run())


def test_manager_resume_new_blocks_by_all_busy_signals_and_runs_after_recovery() -> None:
    async def _run() -> None:
        pipeline_calls = []
        edited = []
        tick_state = {"active": False}

        class _Runtime:
            @staticmethod
            def reset(_session) -> None:
                return None

        class _FakeBotApp:
            def __init__(self):
                self.config = types.SimpleNamespace(defaults=types.SimpleNamespace())
                self.manager_resume_pending = {}
                self.manager = types.SimpleNamespace(_persist_sessions=lambda: None)

            async def _send_message(self, _context, *, chat_id: int, text: str, **_kwargs):
                return None

            async def _edit_message(self, _context, *, chat_id: int, message_id: int, text: str, **_kwargs):
                edited.append((chat_id, message_id, text))
                return True

        bot_app = _FakeBotApp()
        mode = _build_mode(
            bot_app,
            pipeline_calls=pipeline_calls,
            runtime_by_capability=(lambda cap: _Runtime() if cap == "manager_control" else None),
        )
        session = types.SimpleNamespace(
            id="s3",
            workdir="/tmp",
            busy=False,
            run_lock=asyncio.Lock(),
            is_active_by_tick=lambda: bool(tick_state["active"]),
        )
        pending_key = session_runtime_uid(session)
        query = types.SimpleNamespace(message=types.SimpleNamespace(chat_id=123, message_id=10))

        def _put_pending() -> None:
            bot_app.manager_resume_pending[pending_key] = {
                "prompt": "run manager",
                "dest": {"kind": "desktop", "chat_id": 123},
                "created_at": time.time(),
            }

        # busy=true -> blocked, pending must be preserved
        _put_pending()
        session.busy = True
        await mode.handle_callback(
            CallbackModel(action="resume_new", chat_id=123, user_id=None, payload={}),
            {"bot_app": bot_app, "session": session, "chat_id": 123, "context": object(), "query": query},
        )
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert not pipeline_calls
        assert pending_key in bot_app.manager_resume_pending
        assert "уже выполняется" in str(edited[-1][2] or "").lower()

        session.busy = False
        await mode.handle_callback(
            CallbackModel(action="resume_new", chat_id=123, user_id=None, payload={}),
            {"bot_app": bot_app, "session": session, "chat_id": 123, "context": object(), "query": query},
        )
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert len(pipeline_calls) == 1
        assert pending_key not in bot_app.manager_resume_pending

        # run_lock.locked() -> blocked, then recovered
        _put_pending()
        await session.run_lock.acquire()
        try:
            await mode.handle_callback(
                CallbackModel(action="resume_new", chat_id=123, user_id=None, payload={}),
                {"bot_app": bot_app, "session": session, "chat_id": 123, "context": object(), "query": query},
            )
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            assert len(pipeline_calls) == 1
            assert pending_key in bot_app.manager_resume_pending
            assert "уже выполняется" in str(edited[-1][2] or "").lower()
        finally:
            session.run_lock.release()

        await mode.handle_callback(
            CallbackModel(action="resume_new", chat_id=123, user_id=None, payload={}),
            {"bot_app": bot_app, "session": session, "chat_id": 123, "context": object(), "query": query},
        )
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert len(pipeline_calls) == 2
        assert pending_key not in bot_app.manager_resume_pending

        # tick-active=true -> blocked, then recovered
        _put_pending()
        tick_state["active"] = True
        await mode.handle_callback(
            CallbackModel(action="resume_new", chat_id=123, user_id=None, payload={}),
            {"bot_app": bot_app, "session": session, "chat_id": 123, "context": object(), "query": query},
        )
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert len(pipeline_calls) == 2
        assert pending_key in bot_app.manager_resume_pending
        assert "уже выполняется" in str(edited[-1][2] or "").lower()

        tick_state["active"] = False
        await mode.handle_callback(
            CallbackModel(action="resume_new", chat_id=123, user_id=None, payload={}),
            {"bot_app": bot_app, "session": session, "chat_id": 123, "context": object(), "query": query},
        )
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert len(pipeline_calls) == 3
        assert pending_key not in bot_app.manager_resume_pending

    asyncio.run(_run())
