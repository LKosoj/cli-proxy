import asyncio
import types
from unittest.mock import AsyncMock
from unittest.mock import Mock

import shutil
import yaml

from tg.callbacks import CallbackHandler
from config import AppConfig, DefaultsConfig, MCPConfig, TelegramConfig, ToolConfig
from bot import BotApp
from modes.sdk.planning import load_plan, save_plan
from modes.sdk.runtime.contracts import DevTask, ProjectPlan
from session import session_runtime_uid


class _FakeMessage:
    def __init__(self, chat_id: int = 1, message_id: int = 10) -> None:
        self.chat_id = chat_id
        self.message_id = message_id


class _FakeQuery:
    def __init__(self, data: str) -> None:
        self.data = data
        self.message = _FakeMessage()
        self.from_user = types.SimpleNamespace(id=42)

    async def answer(self) -> None:
        return None


def _build_app(tmp_path) -> BotApp:
    cfg = AppConfig(
        telegram=TelegramConfig(token="", whitelist_chat_ids=[1], admlist_chat_ids=[1]),
        tools={
            "dummy": ToolConfig(
                name="dummy",
                mode="headless",
                cmd=["bash", "-lc", "cat"],
            )
        },
        defaults=DefaultsConfig(
            workdir=str(tmp_path),
            state_path=str(tmp_path / "state.json"),
            toolhelp_path=str(tmp_path / "toolhelp.json"),
            log_path=str(tmp_path / "bot.log"),
            openai_api_key="k",
            openai_model="m",
        ),
        mcp=MCPConfig(enabled=False),
        mcp_clients=[],
        presets=[],
        path=str(tmp_path / "config.yaml"),
    )
    return BotApp(cfg)


def _manager_plan(*, goal: str, status: str) -> ProjectPlan:
    return ProjectPlan(
        project_goal=goal,
        tasks=[
            DevTask(
                id="TASK-1",
                title="Task 1",
                description="desc",
                acceptance_criteria=["ok"],
                status="pending",
            )
        ],
        status=status,
    )


def _callback_tokens(markup) -> set[str]:
    rows = getattr(markup, "inline_keyboard", []) or []
    return {
        str(button.callback_data or "")
        for row in rows
        for button in (row or [])
        if str(getattr(button, "callback_data", "") or "").strip()
    }


def test_manager_mode_plugin_is_loaded(tmp_path) -> None:
    app = _build_app(tmp_path)
    assert app.mode_registry.get("manager") is not None


def test_manager_parallel_sessions_use_scoped_plans(tmp_path) -> None:
    app = _build_app(tmp_path)
    session_a = app.manager.create(1, "dummy", str(tmp_path))
    session_b = app.manager.create(2, "dummy", str(tmp_path))
    assert session_a.id == "s1"
    assert session_b.id == "s1"

    session_a.modes.active_mode = "manager"
    session_b.modes.active_mode = "manager"

    save_plan(str(tmp_path), _manager_plan(goal="goal-a", status="paused"), scoped_key=session_a.scoped_key)
    save_plan(str(tmp_path), _manager_plan(goal="goal-b", status="active"), scoped_key=session_b.scoped_key)

    mode = app.mode_registry.get("manager")
    assert mode is not None
    text_a, markup_a = mode.build_menu(session_a)
    text_b, markup_b = mode.build_menu(session_b)

    callbacks_a = _callback_tokens(markup_a)
    callbacks_b = _callback_tokens(markup_b)

    assert "(пауза)" in text_a
    assert any(item.startswith("ma:manager:resume_paused") for item in callbacks_a)
    assert not any(item.startswith("ma:manager:pause") for item in callbacks_a)
    assert "(пауза)" not in text_b
    assert any(item.startswith("ma:manager:pause") for item in callbacks_b)

    runtime = app.mode_runtime_registry.get("manager")
    assert runtime is not None
    runtime._orchestrator.pause(session_b)

    plan_a = load_plan(str(tmp_path), scoped_key=session_a.scoped_key)
    plan_b = load_plan(str(tmp_path), scoped_key=session_b.scoped_key)
    assert plan_a is not None
    assert plan_b is not None
    assert plan_a.project_goal == "goal-a"
    assert plan_a.status == "paused"
    assert plan_b.project_goal == "goal-b"
    assert plan_b.status == "paused"


def test_manager_mode_enable_via_mode_action_callback(tmp_path) -> None:
    async def _run() -> None:
        app = _build_app(tmp_path)
        session = app.manager.create(1, "dummy", str(tmp_path))
        session.modes.active_mode = None

        edits = []

        async def _edit_message(_ctx, *, chat_id: int, message_id: int, text: str, reply_markup=None, **_kw):
            edits.append((chat_id, message_id, text, reply_markup))
            return True

        app._edit_message = _edit_message

        handler = CallbackHandler(app)
        update = types.SimpleNamespace(callback_query=_FakeQuery("ma:manager:enable"))
        await handler.handle_callback(update, context=object())
        assert session.modes.active_mode == "manager"
        assert edits

    asyncio.run(_run())


def test_manager_mode_enable_recreates_project_prompts_when_missing(tmp_path) -> None:
    async def _run() -> None:
        app = _build_app(tmp_path)
        session = app.manager.create(1, "dummy", str(tmp_path))
        session.modes.active_mode = None
        prompt_dir = tmp_path / ".cli-proxy" / ".manager" / "prompt"
        prompts_path = prompt_dir / "prompts.yaml"
        learning_path = prompt_dir / "learning.yaml"
        if prompts_path.exists():
            prompts_path.unlink()
        if learning_path.exists():
            learning_path.unlink()
        if prompt_dir.exists():
            shutil.rmtree(prompt_dir)

        edits = []

        async def _edit_message(_ctx, *, chat_id: int, message_id: int, text: str, reply_markup=None, **_kw):
            edits.append((chat_id, message_id, text, reply_markup))
            return True

        app._edit_message = _edit_message

        handler = CallbackHandler(app)
        update = types.SimpleNamespace(callback_query=_FakeQuery("ma:manager:enable"))
        await handler.handle_callback(update, context=object())

        assert session.modes.active_mode == "manager"
        assert prompts_path.exists()
        assert learning_path.exists()
        assert edits

    asyncio.run(_run())


def test_manager_mode_status_without_plan_uses_unified_status(tmp_path) -> None:
    async def _run() -> None:
        app = _build_app(tmp_path)
        session = app.manager.create(1, "dummy", str(tmp_path))
        session.modes.active_mode = "manager"

        edits = []

        async def _edit_message(_ctx, *, chat_id: int, message_id: int, text: str, reply_markup=None, **_kw):
            edits.append((chat_id, message_id, text, reply_markup))
            return True

        app._edit_message = _edit_message

        handler = CallbackHandler(app)
        update = types.SimpleNamespace(callback_query=_FakeQuery("ma:manager:status"))
        await handler.handle_callback(update, context=object())

        assert edits
        text = edits[-1][2]
        assert "🏗 Статус Менеджера" in text
        assert "План: не найден." in text
        assert "Режим: включен" in text

    asyncio.run(_run())


def test_manager_mode_status_truncated_sends_file_without_summary(tmp_path, monkeypatch) -> None:
    async def _run() -> None:
        app = _build_app(tmp_path)
        session = app.manager.create(1, "dummy", str(tmp_path))
        session.modes.active_mode = "manager"

        import sys

        mode_plugin = app.mode_registry.get("manager")
        assert mode_plugin is not None
        manager_mode_mod = sys.modules.get(mode_plugin.__class__.__module__)
        assert manager_mode_mod is not None

        monkeypatch.setattr(
            manager_mode_mod,
            "load_plan",
            lambda _workdir, **_kwargs: types.SimpleNamespace(status="active"),
        )
        monkeypatch.setattr(
            manager_mode_mod,
            "format_manager_status_brief",
            lambda _plan: "X" * 6000,
        )

        edits = []
        sent_output = []

        async def _edit_message(_ctx, *, chat_id: int, message_id: int, text: str, reply_markup=None, **_kw):
            edits.append((chat_id, message_id, text, reply_markup))
            return True

        async def _send_output(_session, _dest, _output, _context, **kwargs):
            sent_output.append(dict(kwargs))

        app._edit_message = _edit_message
        app.send_output = _send_output

        handler = CallbackHandler(app)
        update = types.SimpleNamespace(callback_query=_FakeQuery("ma:manager:status"))
        await handler.handle_callback(update, context=object())
        await asyncio.sleep(0)

        assert edits
        assert "Обрезано по лимиту Telegram" in edits[-1][2]
        assert sent_output
        assert sent_output[-1].get("force_html") is True
        assert sent_output[-1].get("send_summary") is False

    asyncio.run(_run())


def test_manager_mode_status_truncated_task_is_tracked_and_cancelable(tmp_path, monkeypatch) -> None:
    async def _run() -> None:
        app = _build_app(tmp_path)
        session = app.manager.create(1, "dummy", str(tmp_path))
        session.modes.active_mode = "manager"

        import sys

        mode_plugin = app.mode_registry.get("manager")
        assert mode_plugin is not None
        manager_mode_mod = sys.modules.get(mode_plugin.__class__.__module__)
        assert manager_mode_mod is not None

        monkeypatch.setattr(
            manager_mode_mod,
            "load_plan",
            lambda _workdir, **_kwargs: types.SimpleNamespace(status="active"),
        )
        monkeypatch.setattr(
            manager_mode_mod,
            "format_manager_status_brief",
            lambda _plan: "X" * 6000,
        )

        async def _edit_message(_ctx, *, chat_id: int, message_id: int, text: str, reply_markup=None, **_kw):
            return True

        app._edit_message = _edit_message

        canceled = {"value": False}
        blocker = asyncio.Event()

        async def _send_output(_session, _dest, _output, _context, **kwargs):
            _ = kwargs
            try:
                await blocker.wait()
            except asyncio.CancelledError:
                canceled["value"] = True
                raise

        app.send_output = _send_output

        handler = CallbackHandler(app)
        update = types.SimpleNamespace(callback_query=_FakeQuery("ma:manager:status"))
        await handler.handle_callback(update, context=object())
        await asyncio.sleep(0)

        session_uid = session_runtime_uid(session)
        task_names = app.mode_tasks.list(session_uid=session_uid, mode_id="manager")
        assert "status_send_output" in task_names

        cancelled_count = await app.mode_session_control.cancel_mode(
            session_id=session_uid,
            mode_id="manager",
            timeout_s=0.2,
        )
        assert cancelled_count >= 1
        assert canceled["value"] is True
        assert app.mode_tasks.list(session_uid=session_uid, mode_id="manager") == []

    asyncio.run(_run())


def test_manager_enable_does_not_cancel_other_modes_tasks(tmp_path) -> None:
    async def _run() -> None:
        app = _build_app(tmp_path)
        session = app.manager.create(1, "dummy", str(tmp_path))
        session.modes.active_mode = None

        cancel_mode = AsyncMock(return_value=0)
        app.mode_session_control.cancel_mode_tasks = cancel_mode

        edits = []

        async def _edit_message(_ctx, *, chat_id: int, message_id: int, text: str, reply_markup=None, **_kw):
            edits.append((chat_id, message_id, text, reply_markup))
            return True

        app._edit_message = _edit_message

        handler = CallbackHandler(app)
        update = types.SimpleNamespace(callback_query=_FakeQuery("ma:manager:enable"))
        await handler.handle_callback(update, context=object())

        assert session.modes.active_mode == "manager"
        assert edits
        cancel_mode.assert_not_awaited()

    asyncio.run(_run())


def test_manager_resume_cancel_clears_manager_pending_for_active_session(tmp_path) -> None:
    async def _run() -> None:
        app = _build_app(tmp_path)
        session = app.manager.create(1, "dummy", str(tmp_path))
        session_uid = session_runtime_uid(session)
        app.manager_resume_pending[session_uid] = {"prompt": "p", "dest": {"kind": "telegram", "chat_id": 1}}

        edits = []

        async def _edit_message(_ctx, *, chat_id: int, message_id: int, text: str, reply_markup=None, **_kw):
            edits.append((chat_id, message_id, text, reply_markup))
            return True

        app._edit_message = _edit_message

        handler = CallbackHandler(app)
        update = types.SimpleNamespace(callback_query=_FakeQuery("ma:manager:resume_cancel"))
        await handler.handle_callback(update, context=object())

        assert session_uid not in app.manager_resume_pending
        assert edits
        assert edits[-1][2] == "Отменено."

    asyncio.run(_run())


def test_manager_resume_new_without_pending_prompts_to_resend_task(tmp_path) -> None:
    async def _run() -> None:
        app = _build_app(tmp_path)
        session = app.manager.create(1, "dummy", str(tmp_path))
        session.modes.active_mode = "manager"
        app.manager_resume_pending.pop(session.id, None)

        edits = []

        async def _edit_message(_ctx, *, chat_id: int, message_id: int, text: str, reply_markup=None, **_kw):
            edits.append((chat_id, message_id, text, reply_markup))
            return True

        app._edit_message = _edit_message

        handler = CallbackHandler(app)
        update = types.SimpleNamespace(callback_query=_FakeQuery("ma:manager:resume_new"))
        await handler.handle_callback(update, context=object())

        assert edits
        assert edits[-1][2] == "Выбор устарел. Пришлите задачу заново."

    asyncio.run(_run())


def test_manager_failed_archive_none_returns_ui_error(tmp_path, monkeypatch) -> None:
    async def _run() -> None:
        app = _build_app(tmp_path)
        session = app.manager.create(1, "dummy", str(tmp_path))
        session.modes.active_mode = "manager"
        monkeypatch.setattr("modes.manager.mode.archive_plan", lambda _workdir, _status, **_kwargs: None)
        mode = app.mode_registry.get("manager")
        assert mode is not None
        error_mock = Mock()
        mode._log.error = error_mock

        edits = []

        async def _edit_message(_ctx, *, chat_id: int, message_id: int, text: str, reply_markup=None, **_kw):
            edits.append((chat_id, message_id, text, reply_markup))
            return True

        app._edit_message = _edit_message

        handler = CallbackHandler(app)
        update = types.SimpleNamespace(callback_query=_FakeQuery("ma:manager:failed_archive"))
        await handler.handle_callback(update, context=object())

        assert edits
        assert edits[-1][2] == "Не удалось перенести план в архив."
        error_mock.assert_called_once()

    asyncio.run(_run())


def test_manager_mode_enable_fails_on_invalid_project_prompts_yaml(tmp_path) -> None:
    async def _run() -> None:
        app = _build_app(tmp_path)
        session = app.manager.create(1, "dummy", str(tmp_path))
        session.modes.active_mode = None
        prompts_path = tmp_path / ".cli-proxy" / ".manager" / "prompt" / "prompts.yaml"
        prompts_path.write_text("prompts: [broken", encoding="utf-8")
        mode = app.mode_registry.get("manager")
        exception_mock = Mock()
        mode._log.exception = exception_mock

        edits = []

        async def _edit_message(_ctx, *, chat_id: int, message_id: int, text: str, reply_markup=None, **_kw):
            edits.append((chat_id, message_id, text, reply_markup))
            return True

        app._edit_message = _edit_message

        handler = CallbackHandler(app)
        update = types.SimpleNamespace(callback_query=_FakeQuery("ma:manager:enable"))
        await handler.handle_callback(update, context=object())

        assert session.modes.active_mode is None
        assert edits
        assert "Не удалось включить Manager" in edits[-1][2]
        assert exception_mock.called

    asyncio.run(_run())


def test_manager_mode_uses_updated_project_prompts_after_mode_restart(tmp_path) -> None:
    async def _run() -> None:
        app = _build_app(tmp_path)
        session = app.manager.create(1, "dummy", str(tmp_path))
        session.modes.active_mode = None
        prompts_path = tmp_path / ".cli-proxy" / ".manager" / "prompt" / "prompts.yaml"

        async def _edit_message(_ctx, *, chat_id: int, message_id: int, text: str, reply_markup=None, **_kw):
            return True

        app._edit_message = _edit_message
        mode = app.mode_registry.get("manager")

        with open(prompts_path, "r", encoding="utf-8") as f:
            payload = yaml.safe_load(f) or {}
        prompts = payload.get("prompts") if isinstance(payload, dict) else {}
        if not isinstance(prompts, dict):
            prompts = {}
        prompts["decision_system"] = "MARKER_DECISION_PROMPT_V1"
        with open(prompts_path, "w", encoding="utf-8") as f:
            yaml.safe_dump({"prompts": prompts}, f, allow_unicode=True, sort_keys=False)

        handler = CallbackHandler(app)
        await handler.handle_callback(
            types.SimpleNamespace(callback_query=_FakeQuery("ma:manager:enable")),
            context=object(),
        )
        first = mode._load_prompts(session=session).get("decision_system")
        await handler.handle_callback(
            types.SimpleNamespace(callback_query=_FakeQuery("ma:manager:disable")),
            context=object(),
        )

        prompts["decision_system"] = "MARKER_DECISION_PROMPT_V2"
        with open(prompts_path, "w", encoding="utf-8") as f:
            yaml.safe_dump({"prompts": prompts}, f, allow_unicode=True, sort_keys=False)

        await handler.handle_callback(
            types.SimpleNamespace(callback_query=_FakeQuery("ma:manager:enable")),
            context=object(),
        )
        second = mode._load_prompts(session=session).get("decision_system")

        assert first == "MARKER_DECISION_PROMPT_V1"
        assert second == "MARKER_DECISION_PROMPT_V2"

    asyncio.run(_run())
