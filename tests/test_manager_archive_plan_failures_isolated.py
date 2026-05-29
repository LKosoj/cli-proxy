import asyncio
import types
from unittest.mock import Mock

from modes.manager.mode import ManagerMode
from modes.sdk import (
    CallbackModel,
    DictStateService,
    MessageModel,
    MessagingService,
    ModePipelineService,
    SessionControlService,
    TaskService,
)
from modes.sdk.runtime.contracts import DevTask, ProjectPlan


class _FakeBotApp:
    def __init__(self) -> None:
        self.config = types.SimpleNamespace(
            defaults=types.SimpleNamespace(manager_auto_resume=False),
        )
        self.manager_resume_pending = {}
        self.manager = types.SimpleNamespace(_persist_sessions=lambda: None)
        self.sent = []
        self.edited = []

    async def _send_message(self, _context, *, chat_id: int, text: str, **_kwargs):
        self.sent.append((chat_id, text))
        return None

    async def _edit_message(self, _context, *, chat_id: int, message_id: int, text: str, **_kwargs):
        self.edited.append((chat_id, message_id, text))
        return True


def _build_mode(bot_app: _FakeBotApp, *, pipeline_calls: list[tuple[str, str, dict]]) -> ManagerMode:
    mode = ManagerMode()

    async def _run_mode_pipeline(_session, prompt, dest, _context, mode_id):
        pipeline_calls.append((str(mode_id), str(prompt), dict(dest)))

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


def _failed_plan() -> ProjectPlan:
    return ProjectPlan(
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
            )
        ],
        analysis=None,
        status="failed",
    )


def test_manager_archive_none_in_failed_archive_callback_returns_ui_error(monkeypatch, tmp_path) -> None:
    async def _run() -> None:
        pipeline_calls = []
        bot_app = _FakeBotApp()
        mode = _build_mode(bot_app, pipeline_calls=pipeline_calls)
        error_mock = Mock()
        mode._log.error = error_mock

        monkeypatch.setattr("modes.manager.mode.archive_plan", lambda _workdir, _status, **_kwargs: None)

        session = types.SimpleNamespace(
            id="s1",
            workdir=str(tmp_path),
            busy=False,
            run_lock=asyncio.Lock(),
        )
        query = types.SimpleNamespace(message=types.SimpleNamespace(chat_id=123, message_id=10))

        await mode.handle_callback(
            CallbackModel(action="failed_archive", chat_id=123, user_id=None, payload={}),
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
        assert bot_app.edited[-1][2] == "Не удалось перенести план в архив."
        error_mock.assert_called_once()

    asyncio.run(_run())


def test_manager_archive_none_in_failed_text_flow_returns_ui_error(monkeypatch, tmp_path) -> None:
    async def _run() -> None:
        pipeline_calls = []
        bot_app = _FakeBotApp()
        mode = _build_mode(bot_app, pipeline_calls=pipeline_calls)
        error_mock = Mock()
        mode._log.error = error_mock

        monkeypatch.setattr("modes.manager.mode.load_plan", lambda _workdir, **_kwargs: _failed_plan())
        monkeypatch.setattr("modes.manager.mode.archive_plan", lambda _workdir, _status, **_kwargs: None)

        session = types.SimpleNamespace(
            id="s1",
            workdir=str(tmp_path),
            busy=False,
            run_lock=asyncio.Lock(),
        )

        await mode.handle_input(
            MessageModel(text="сделай задачу", chat_id=123),
            {
                "bot_app": bot_app,
                "session": session,
                "context": object(),
                "dest": {"kind": "telegram", "chat_id": 123},
            },
        )
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        assert not pipeline_calls
        assert bot_app.sent
        assert bot_app.sent[-1][1] == "Не удалось перенести план в архив."
        error_mock.assert_called_once()

    asyncio.run(_run())


def test_manager_archive_none_then_success_allows_next_text_run(monkeypatch, tmp_path) -> None:
    async def _run() -> None:
        pipeline_calls = []
        bot_app = _FakeBotApp()
        mode = _build_mode(bot_app, pipeline_calls=pipeline_calls)

        monkeypatch.setattr("modes.manager.mode.load_plan", lambda _workdir, **_kwargs: _failed_plan())

        results = [None, str(tmp_path / "archive.json")]

        def _archive(_workdir, _status, **_kwargs):
            return results.pop(0)

        monkeypatch.setattr("modes.manager.mode.archive_plan", _archive)

        session = types.SimpleNamespace(
            id="s1",
            workdir=str(tmp_path),
            busy=False,
            run_lock=asyncio.Lock(),
        )

        await mode.handle_input(
            MessageModel(text="первый запуск", chat_id=123),
            {
                "bot_app": bot_app,
                "session": session,
                "context": object(),
                "dest": {"kind": "telegram", "chat_id": 123},
            },
        )
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        assert not pipeline_calls
        assert bot_app.sent
        assert bot_app.sent[-1][1] == "Не удалось перенести план в архив."

        await mode.handle_input(
            MessageModel(text="второй запуск", chat_id=123),
            {
                "bot_app": bot_app,
                "session": session,
                "context": object(),
                "dest": {"kind": "telegram", "chat_id": 123},
            },
        )
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        assert len(pipeline_calls) == 1
        assert pipeline_calls[-1][0] == "manager"
        assert pipeline_calls[-1][1] == "второй запуск"

    asyncio.run(_run())
