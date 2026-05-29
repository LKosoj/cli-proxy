import asyncio
import types
from unittest.mock import Mock

from app.services.project_prompts_service import ensure_project_prompts
from modes.sdk.runtime.contracts import DevTask, ProjectPlan
from modes.manager.mode import ManagerMode
from modes.sdk import DictStateService, MessageModel, MessagingService, ModePipelineService, SessionControlService, TaskService
from modes.sdk.services.tooling import ModeToolingService
from session import session_runtime_uid


def test_manager_mode_failed_resume_text_input_archives_plan(monkeypatch, tmp_path) -> None:
    ensure_project_prompts(str(tmp_path))
    plan = ProjectPlan(
        project_goal="Goal",
        tasks=[
            DevTask(
                id="t1",
                title="Task 1",
                description="",
                acceptance_criteria=["ok"],
                status="failed",
                attempt=1,
                max_attempts=3,
                review_comments="Падает проверка smoke",
            )
        ],
        analysis=None,
        status="failed",
    )

    monkeypatch.setattr("modes.manager.mode.load_plan", lambda _workdir, **_kwargs: plan)
    archived = {"called": False, "status": ""}

    def _fake_archive(_workdir, status, **_kwargs):
        archived["called"] = True
        archived["status"] = str(status)
        return "/tmp/archive.json"

    monkeypatch.setattr("modes.manager.mode.archive_plan", _fake_archive)

    sent_messages = []

    class _FakeBotApp:
        def __init__(self) -> None:
            self.config = types.SimpleNamespace(
                defaults=types.SimpleNamespace(manager_auto_resume=False),
            )
            self.manager_resume_pending = {}
            self.manager = types.SimpleNamespace(_persist_sessions=lambda: None)

        async def _send_message(self, _context, *, chat_id: int, text: str, **_kwargs) -> None:
            sent_messages.append((chat_id, text))

        async def _edit_message(self, *_args, **_kwargs):
            return True

    bot_app = _FakeBotApp()
    pipeline_calls = {"count": 0, "prompt": "", "dest": None}

    async def _run_mode_pipeline(_session, prompt, dest, _context, _mode_id):
        pipeline_calls["count"] += 1
        pipeline_calls["prompt"] = str(prompt)
        pipeline_calls["dest"] = dict(dest)

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
            "pipeline": ModePipelineService(
                run_mode_pipeline_fn=_run_mode_pipeline,
            ),
            "messaging_factory": (lambda ctx: MessagingService(
                send_message=bot_app._send_message,
                edit_message=bot_app._edit_message,
                transport_context=ctx,
            )),
            "tooling": ModeToolingService(
                execute_tool_fn=(lambda name, args, tool_ctx: bot_app._tool_registry.execute(name, args, tool_ctx)),
                registry_provider=(lambda: bot_app._tool_registry),
            ),
        },
    )
    session = types.SimpleNamespace(id="s1", workdir=str(tmp_path), busy=False, run_lock=asyncio.Lock())
    pending_key = session_runtime_uid(session)

    async def _run():
        await mode.handle_input(
            MessageModel(text="сделай задачу", chat_id=123),
            {
                "bot_app": bot_app,
                "session": session,
                "context": None,
                "dest": {"kind": "telegram", "chat_id": 123},
            },
        )
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    asyncio.run(_run())

    assert pending_key not in bot_app.manager_resume_pending
    assert archived["called"] is True
    assert archived["status"] == "failed"
    assert sent_messages
    text = sent_messages[0][1]
    assert "План перенесён в архив" in text
    assert "запускаю новый план" in text.lower()
    assert pipeline_calls["count"] == 1
    assert pipeline_calls["prompt"] == "сделай задачу"
    assert pipeline_calls["dest"] == {"kind": "telegram", "chat_id": 123}


def test_manager_mode_failed_resume_text_input_archive_none_notifies_failure(monkeypatch, tmp_path) -> None:
    ensure_project_prompts(str(tmp_path))
    plan = ProjectPlan(
        project_goal="Goal",
        tasks=[
            DevTask(
                id="t1",
                title="Task 1",
                description="",
                acceptance_criteria=["ok"],
                status="failed",
                attempt=1,
                max_attempts=3,
                review_comments="Падает проверка smoke",
            )
        ],
        analysis=None,
        status="failed",
    )

    monkeypatch.setattr("modes.manager.mode.load_plan", lambda _workdir, **_kwargs: plan)
    monkeypatch.setattr("modes.manager.mode.archive_plan", lambda _workdir, _status, **_kwargs: None)

    sent_messages = []

    class _FakeBotApp:
        def __init__(self) -> None:
            self.config = types.SimpleNamespace(
                defaults=types.SimpleNamespace(manager_auto_resume=False),
            )
            self.manager_resume_pending = {}
            self.manager = types.SimpleNamespace(_persist_sessions=lambda: None)

        async def _send_message(self, _context, *, chat_id: int, text: str, **_kwargs) -> None:
            sent_messages.append((chat_id, text))

        async def _edit_message(self, *_args, **_kwargs):
            return True

    bot_app = _FakeBotApp()
    pipeline_calls = {"count": 0}

    async def _run_mode_pipeline(_session, _prompt, _dest, _context, _mode_id):
        pipeline_calls["count"] += 1

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
            "pipeline": ModePipelineService(
                run_mode_pipeline_fn=_run_mode_pipeline,
            ),
            "messaging_factory": (lambda ctx: MessagingService(
                send_message=bot_app._send_message,
                edit_message=bot_app._edit_message,
                transport_context=ctx,
            )),
            "tooling": ModeToolingService(
                execute_tool_fn=(lambda name, args, tool_ctx: bot_app._tool_registry.execute(name, args, tool_ctx)),
                registry_provider=(lambda: bot_app._tool_registry),
            ),
        },
    )
    error_mock = Mock()
    mode._log.error = error_mock
    session = types.SimpleNamespace(id="s1", workdir=str(tmp_path), busy=False, run_lock=asyncio.Lock())

    async def _run():
        await mode.handle_input(
            MessageModel(text="сделай задачу", chat_id=123),
            {
                "bot_app": bot_app,
                "session": session,
                "context": None,
                "dest": {"kind": "telegram", "chat_id": 123},
            },
        )
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    asyncio.run(_run())

    assert sent_messages
    assert sent_messages[-1][1] == "Не удалось перенести план в архив."
    assert pipeline_calls["count"] == 0
    error_mock.assert_called_once()


def test_manager_resume_choice_uses_ask_user_for_non_telegram_dest(monkeypatch, tmp_path) -> None:
    ensure_project_prompts(str(tmp_path))
    plan = ProjectPlan(
        project_goal="Goal",
        tasks=[],
        analysis=None,
        status="active",
    )
    monkeypatch.setattr("modes.manager.mode.load_plan", lambda _workdir, **_kwargs: plan)

    called = {"execute": 0, "pipeline": 0}

    class _FakeRegistry:
        async def execute(self, name, args, ctx):
            called["execute"] += 1
            assert name == "ask_user"
            assert ctx.get("chat_id") == 123
            return {"success": True, "output": "User selected: Начать новый план"}

    class _FakeRuntime:
        def __init__(self) -> None:
            self.reset = Mock()

    runtime = _FakeRuntime()

    class _FakeBotApp:
        def __init__(self) -> None:
            self.config = types.SimpleNamespace(
                defaults=types.SimpleNamespace(manager_auto_resume=False),
            )
            self.manager_resume_pending = {}
            self.manager = types.SimpleNamespace(_persist_sessions=lambda: None)
            self._tool_registry = _FakeRegistry()

        async def _send_message(self, _context, *, chat_id: int, text: str, **_kwargs) -> None:
            return None

        async def _edit_message(self, *_args, **_kwargs):
            return True

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
            "runtime_by_capability": (lambda capability: runtime if capability == "manager_control" else None),
            "pipeline": ModePipelineService(
                run_mode_pipeline_fn=lambda _session, _prompt, _dest, _context, _mode_id: asyncio.sleep(
                    0, result=called.__setitem__("pipeline", called["pipeline"] + 1)
                ),
            ),
            "messaging_factory": (lambda ctx: MessagingService(
                send_message=bot_app._send_message,
                edit_message=bot_app._edit_message,
                transport_context=ctx,
            )),
            "tooling": ModeToolingService(
                execute_tool_fn=(lambda name, args, tool_ctx: bot_app._tool_registry.execute(name, args, tool_ctx)),
                registry_provider=(lambda: bot_app._tool_registry),
            ),
        },
    )
    session = types.SimpleNamespace(id="s1", workdir=str(tmp_path), busy=False, run_lock=asyncio.Lock())
    pending_key = session_runtime_uid(session)

    async def _run():
        await mode.handle_input(
            MessageModel(text="сделай задачу", chat_id=123),
            {
                "bot_app": bot_app,
                "session": session,
                "context": object(),
                "dest": {"kind": "desktop", "chat_id": 123},
            },
        )
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    asyncio.run(_run())
    assert called["execute"] == 1
    assert pending_key not in bot_app.manager_resume_pending
    runtime.reset.assert_called_once()
    assert called["pipeline"] == 1


def test_manager_resume_choice_ask_user_failure_keeps_pending_and_notifies_retry(monkeypatch, tmp_path) -> None:
    ensure_project_prompts(str(tmp_path))
    plan = ProjectPlan(
        project_goal="Goal",
        tasks=[],
        analysis=None,
        status="active",
    )
    monkeypatch.setattr("modes.manager.mode.load_plan", lambda _workdir, **_kwargs: plan)

    called = {"execute": 0, "pipeline": 0, "messages": []}

    class _FailRegistry:
        async def execute(self, name, args, ctx):
            called["execute"] += 1
            assert name == "ask_user"
            raise RuntimeError("ask_user failed")

    class _FakeBotApp:
        def __init__(self) -> None:
            self.config = types.SimpleNamespace(
                defaults=types.SimpleNamespace(manager_auto_resume=False),
            )
            self.manager_resume_pending = {}
            self.manager = types.SimpleNamespace(_persist_sessions=lambda: None)
            self._tool_registry = _FailRegistry()

        async def _send_message(self, _context, *, chat_id: int, text: str, **_kwargs) -> None:
            called["messages"].append((chat_id, text))

        async def _edit_message(self, *_args, **_kwargs):
            return True

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
            "pipeline": ModePipelineService(
                run_mode_pipeline_fn=lambda _session, _prompt, _dest, _context, _mode_id: asyncio.sleep(
                    0, result=called.__setitem__("pipeline", called["pipeline"] + 1)
                ),
            ),
            "messaging_factory": (lambda ctx: MessagingService(
                send_message=bot_app._send_message,
                edit_message=bot_app._edit_message,
                transport_context=ctx,
            )),
            "tooling": ModeToolingService(
                execute_tool_fn=(lambda name, args, tool_ctx: bot_app._tool_registry.execute(name, args, tool_ctx)),
                registry_provider=(lambda: bot_app._tool_registry),
            ),
        },
    )
    log_mock = Mock()
    mode._log.exception = log_mock
    session = types.SimpleNamespace(id="s1", workdir=str(tmp_path), busy=False, run_lock=asyncio.Lock())
    pending_key = session_runtime_uid(session)

    async def _run():
        await mode.handle_input(
            MessageModel(text="сделай задачу", chat_id=123),
            {
                "bot_app": bot_app,
                "session": session,
                "context": object(),
                "dest": {"kind": "desktop", "chat_id": 123},
            },
        )
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    asyncio.run(_run())

    assert called["execute"] == 1
    assert called["pipeline"] == 0
    assert bot_app.manager_resume_pending.get(pending_key, {}).get("prompt") == "сделай задачу"
    assert called["messages"]
    assert called["messages"][-1][1] == "Не удалось обработать выбор продолжения плана. Повторите выбор."
    log_mock.assert_called_once_with("manager ask_user resume choice failed")


def test_manager_resume_choice_cancel_via_ask_user_clears_pending(monkeypatch, tmp_path) -> None:
    ensure_project_prompts(str(tmp_path))
    plan = ProjectPlan(
        project_goal="Goal",
        tasks=[],
        analysis=None,
        status="active",
    )
    monkeypatch.setattr("modes.manager.mode.load_plan", lambda _workdir, **_kwargs: plan)

    called = {"execute": 0, "pipeline": 0, "messages": []}

    class _Registry:
        async def execute(self, name, args, ctx):
            called["execute"] += 1
            assert name == "ask_user"
            assert ctx.get("chat_id") == 123
            return {"success": True, "output": "User selected: Отмена"}

    class _FakeBotApp:
        def __init__(self) -> None:
            self.config = types.SimpleNamespace(
                defaults=types.SimpleNamespace(manager_auto_resume=False),
            )
            self.manager_resume_pending = {}
            self.manager = types.SimpleNamespace(_persist_sessions=lambda: None)
            self._tool_registry = _Registry()

        async def _send_message(self, _context, *, chat_id: int, text: str, **_kwargs) -> None:
            called["messages"].append((chat_id, text))

        async def _edit_message(self, *_args, **_kwargs):
            return True

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
            "pipeline": ModePipelineService(
                run_mode_pipeline_fn=lambda _session, _prompt, _dest, _context, _mode_id: asyncio.sleep(
                    0, result=called.__setitem__("pipeline", called["pipeline"] + 1)
                ),
            ),
            "messaging_factory": (lambda ctx: MessagingService(
                send_message=bot_app._send_message,
                edit_message=bot_app._edit_message,
                transport_context=ctx,
            )),
            "tooling": ModeToolingService(
                execute_tool_fn=(lambda name, args, tool_ctx: bot_app._tool_registry.execute(name, args, tool_ctx)),
                registry_provider=(lambda: bot_app._tool_registry),
            ),
        },
    )
    session = types.SimpleNamespace(id="s1", workdir=str(tmp_path), busy=False, run_lock=asyncio.Lock())
    pending_key = session_runtime_uid(session)

    async def _run():
        await mode.handle_input(
            MessageModel(text="сделай задачу", chat_id=123),
            {
                "bot_app": bot_app,
                "session": session,
                "context": object(),
                "dest": {"kind": "desktop", "chat_id": 123},
            },
        )
        await asyncio.sleep(0)

    asyncio.run(_run())

    assert called["execute"] == 1
    assert called["pipeline"] == 0
    assert pending_key not in bot_app.manager_resume_pending
    assert called["messages"]
    assert called["messages"][-1][1] == "Ок, оставляю текущий план без изменений."
