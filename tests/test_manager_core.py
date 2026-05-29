import asyncio
import logging
import os
import subprocess
from types import SimpleNamespace

import pytest

import agent.manager_core as manager_core
from agent.manager_core import ManagerOrchestrator
from config import load_config
from modes.manager.runner_service import ManagerModeRunnerService, ManagerRuntimeAdapter
from modes.sdk import (
    BaseMode,
    CallbackModel,
    DialogService,
    MessageModel,
    MessagingService,
    ModeToolingService,
    SessionControlService,
    TaskService,
    ToolResult,
    mode_runtime_context_from_legacy,
)
from modes.sdk.runtime.contracts import DevTask, ProjectPlan, ReviewResult


@pytest.fixture(autouse=True)
def _capture_agent_manager_logs():
    logger = logging.getLogger("agent")
    old_propagate = logger.propagate
    logger.propagate = True
    try:
        yield
    finally:
        logger.propagate = old_propagate


class _ProbeMode(BaseMode):
    mode_id = "manager_probe"

    async def handle_input(self, message: MessageModel, ctx):
        return ToolResult.ok(message.text)

    async def handle_callback(self, callback: CallbackModel, ctx):
        return ToolResult.ok(callback.action)


async def _execute_tool(_tool_name, _args, _ctx):
    return {"success": True, "output": {}}


def _make_config(tmp_path):
    cfg = load_config(os.path.join(os.path.dirname(__file__), "..", "config_example.yaml"))
    cfg.defaults.workdir = str(tmp_path)
    cfg.defaults.state_path = str(tmp_path / "state.json")
    cfg.defaults.manager_response_archive = False
    return cfg


def test_manager_run_git_retries_timeout(tmp_path, monkeypatch, caplog) -> None:
    calls = []

    def _fake_run(cmd, **kwargs):
        calls.append((cmd, dict(kwargs)))
        if len(calls) == 1:
            raise subprocess.TimeoutExpired(cmd=cmd, timeout=kwargs["timeout"])
        return subprocess.CompletedProcess(cmd, 0, stdout=" M file.py\n")

    monkeypatch.setattr(manager_core.subprocess, "run", _fake_run)
    monkeypatch.setattr(manager_core, "_GIT_COMMAND_ATTEMPTS", 2)
    monkeypatch.setattr(manager_core, "_GIT_COMMAND_RETRY_DELAY_SEC", 0.0)
    caplog.set_level(logging.WARNING, logger="agent.manager_core")

    code, output = asyncio.run(ManagerOrchestrator._run_git(str(tmp_path), ["status", "--porcelain"]))

    assert code == 0
    assert output == " M file.py\n"
    assert len(calls) == 2
    assert calls[0][1]["timeout"] == manager_core._GIT_COMMAND_TIMEOUT_SEC
    assert "manager git command timed out attempt=1/2" in caplog.text


def test_manager_run_git_returns_timeout_after_retries(tmp_path, monkeypatch, caplog) -> None:
    calls = []

    def _fake_run(cmd, **kwargs):
        calls.append((cmd, dict(kwargs)))
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=kwargs["timeout"])

    monkeypatch.setattr(manager_core.subprocess, "run", _fake_run)
    monkeypatch.setattr(manager_core, "_GIT_COMMAND_ATTEMPTS", 2)
    monkeypatch.setattr(manager_core, "_GIT_COMMAND_RETRY_DELAY_SEC", 0.0)
    caplog.set_level(logging.WARNING, logger="agent.manager_core")

    code, output = asyncio.run(ManagerOrchestrator._run_git(str(tmp_path), ["status", "--porcelain"]))

    assert code == 124
    assert "timed out after" in output
    assert "git status --porcelain" in output
    assert len(calls) == 2
    assert "manager git command timed out attempt=2/2" in caplog.text


def _build_probe_mode(config, send_message, runtime):
    mode = _ProbeMode()
    mode.initialize(
        config=config,
        services={
            "tasks": TaskService(),
            "dialogs": DialogService(),
            "session_control": SessionControlService(persist_sessions=lambda: None),
            "messaging_factory": (
                lambda transport_context: MessagingService(
                    send_message=send_message,
                    transport_context=transport_context,
                )
            ),
            "tooling": ModeToolingService(execute_tool_fn=_execute_tool),
            "runtime_by_capability": lambda capability: runtime if capability == "run_manager" else None,
        },
    )
    return mode


def test_manager_runtime_adapter_contract_paths(tmp_path) -> None:
    async def _run() -> None:
        calls = []
        config = _make_config(tmp_path)

        async def _send_message(context, **kwargs):
            calls.append(("message", context, dict(kwargs)))
            return SimpleNamespace(message_id=11)

        async def _send_output(session, dest, output, context, **kwargs):
            calls.append(("output", session, dict(dest), output, context, dict(kwargs)))
            return None

        adapter = ManagerRuntimeAdapter(
            config=config,
            send_message=_send_message,
            send_output=_send_output,
            is_admin=lambda chat_id: int(chat_id) == 123,
        )
        context = object()
        session = SimpleNamespace(id="s1")

        sent = await adapter.send_message(context, chat_id=123, text="hello", md2=True)
        await adapter.send_output(session, {"chat_id": 123}, "report", context, send_header=False)

        assert sent.message_id == 11
        assert adapter.config is config
        assert adapter.is_admin(123) is True
        assert adapter.is_admin(456) is False
        assert calls == [
            ("message", context, {"chat_id": 123, "text": "hello", "md2": True}),
            ("output", session, {"chat_id": 123}, "report", context, {"send_header": False}),
        ]

    asyncio.run(_run())


def test_manager_runner_run_passes_runtime_adapter_instead_of_bot_app(tmp_path) -> None:
    async def _run() -> None:
        config = _make_config(tmp_path)
        calls = []

        async def _send_message(context, **kwargs):
            calls.append(("message", context, dict(kwargs)))
            return SimpleNamespace(message_id=21)

        async def _send_output(session, dest, output, context, **kwargs):
            calls.append(("output", session, dict(dest), output, context, dict(kwargs)))
            return None

        bot_app = SimpleNamespace(
            config=config,
            _send_message=_send_message,
            send_output=_send_output,
            is_admin=lambda chat_id: int(chat_id) == 123,
        )

        class _Orchestrator:
            def __init__(self, config):
                self._config = config

            async def run(self, session, prompt, bot, context, dest):
                assert bot is not bot_app
                assert isinstance(bot, ManagerRuntimeAdapter)
                assert bot.config is config
                assert bot.is_admin(dest["chat_id"]) is True
                await bot.send_message(context, chat_id=dest["chat_id"], text=prompt)
                await bot.send_output(session, dest, "manager-output", context, send_header=False)
                return "manager-ok"

        runner = ManagerModeRunnerService(config)
        runner._orchestrator = _Orchestrator(config)
        session = SimpleNamespace(id="s1", workdir=str(tmp_path))
        context = object()
        dest = {"kind": "desktop", "chat_id": 123}

        result = await runner.run(session, "manager prompt", bot_app, context, dest)

        assert result == "manager-ok"
        assert calls == [
            ("message", context, {"chat_id": 123, "text": "manager prompt"}),
            ("output", session, dest, "manager-output", context, {"send_header": False}),
        ]

    asyncio.run(_run())


def test_manager_orchestrator_uses_adapter_send_message_without_private_method(tmp_path, caplog) -> None:
    async def _run() -> None:
        config = _make_config(tmp_path)
        calls = []

        async def _send_message(context, **kwargs):
            calls.append((context, dict(kwargs)))
            return SimpleNamespace(message_id=31)

        bot = SimpleNamespace(send_message=_send_message)
        orch = ManagerOrchestrator(config)
        session = SimpleNamespace(modes=SimpleNamespace(manager_quiet_mode=False))
        context = object()

        sent = await orch._send_adapter_message(bot, context, chat_id=123, text="hello")
        await orch._send_runtime_message(session, bot, context, chat_id=123, text="runtime")

        assert sent.message_id == 31
        assert calls == [
            (context, {"chat_id": 123, "text": "hello"}),
            (context, {"chat_id": 123, "text": "runtime"}),
        ]
        assert "legacy fallback used" not in caplog.text

    caplog.set_level(logging.WARNING, logger="agent.manager_core")
    asyncio.run(_run())


def test_manager_orchestrator_logs_legacy_send_message_fallback(tmp_path, caplog) -> None:
    class _CaptureHandler(logging.Handler):
        def __init__(self) -> None:
            super().__init__(logging.WARNING)
            self.messages = []

        def emit(self, record) -> None:
            self.messages.append(record.getMessage())

    async def _run() -> None:
        config = _make_config(tmp_path)
        calls = []

        async def _legacy_send_message(context, **kwargs):
            calls.append((context, dict(kwargs)))
            return SimpleNamespace(message_id=41)

        bot = SimpleNamespace(_send_message=_legacy_send_message)
        orch = ManagerOrchestrator(config)
        context = object()

        sent = await orch._send_adapter_message(bot, context, chat_id=456, text="legacy")

        assert sent.message_id == 41
        assert calls == [(context, {"chat_id": 456, "text": "legacy"})]

    logger = logging.getLogger("agent.manager_core")
    capture = _CaptureHandler()
    old_disabled = logger.disabled
    old_level = logger.level
    logger.disabled = False
    logger.setLevel(logging.WARNING)
    logger.addHandler(capture)
    try:
        caplog.set_level(logging.WARNING, logger="agent.manager_core")
        asyncio.run(_run())
    finally:
        logger.removeHandler(capture)
        logger.setLevel(old_level)
        logger.disabled = old_disabled

    log_text = "\n".join(capture.messages) or caplog.text
    assert "legacy fallback used" in log_text
    assert "bot_type=SimpleNamespace" in log_text
    assert "chat_id=456" in log_text


def test_manager_pauses_and_notifies_when_auto_commit_raises(tmp_path, monkeypatch, caplog) -> None:
    async def _run() -> None:
        config = _make_config(tmp_path)
        orch = ManagerOrchestrator(config)
        session = SimpleNamespace(id="s1", chat_id=123, workdir=str(tmp_path))
        sent_messages = []
        plan = ProjectPlan(
            project_goal="fix manager continuation",
            status="active",
            current_task_id="task_1",
            tasks=[
                DevTask(
                    id="task_1",
                    title="First task",
                    description="done",
                    acceptance_criteria=["approved"],
                    status="in_review",
                    attempt=1,
                    manager_change_audit="changed files",
                    manager_change_audit_has_changes=True,
                ),
            ],
        )

        async def _noop(*_args, **_kwargs):
            return None

        async def _send_runtime_message(*_args, **kwargs):
            sent_messages.append(dict(kwargs))
            return None

        async def _delegate_review(_session, _plan, _task, _bot, _context, _dest):
            return ReviewResult(approved=True, summary="ok", comments="ok")

        async def _make_decision(_task, _review, *, workdir):
            return "approved", []

        async def _auto_commit(_session, _task, _plan, _bot, _context, _dest):
            raise RuntimeError("post approval failed")

        monkeypatch.setattr(orch, "_auto_commit_baseline_before_first_step", _noop)
        monkeypatch.setattr(orch, "_send_runtime_message", _send_runtime_message)
        monkeypatch.setattr(orch, "_delegate_review", _delegate_review)
        monkeypatch.setattr(orch, "_make_decision", _make_decision)
        monkeypatch.setattr(orch, "_auto_commit", _auto_commit)

        await orch._run_loop(
            session,
            plan,
            SimpleNamespace(),
            object(),
            {"chat_id": 123, "message_thread_id": 456},
        )

        saved = orch._load_live_plan(session)
        assert saved is not None
        assert saved.status == "paused"
        assert saved.current_task_id is None
        assert saved.tasks[0].status == "approved"
        assert any("auto_commit упал" in str(message.get("text") or "") for message in sent_messages)
        assert any(message.get("message_thread_id") == 456 for message in sent_messages)
        assert "auto_commit failed task_id=task_1" in caplog.text

    caplog.set_level(logging.ERROR, logger="agent.manager_core")
    asyncio.run(_run())


def test_manager_pauses_and_notifies_when_reconcile_raises(tmp_path, monkeypatch, caplog) -> None:
    async def _run() -> None:
        config = _make_config(tmp_path)
        orch = ManagerOrchestrator(config)
        session = SimpleNamespace(id="s1", chat_id=123, workdir=str(tmp_path))
        sent_messages = []
        plan = ProjectPlan(
            project_goal="fix manager continuation",
            status="active",
            current_task_id="task_1",
            tasks=[
                DevTask(
                    id="task_1",
                    title="First task",
                    description="done",
                    acceptance_criteria=["approved"],
                    status="in_review",
                    attempt=1,
                ),
            ],
        )

        async def _noop(*_args, **_kwargs):
            return None

        async def _send_runtime_message(*_args, **kwargs):
            sent_messages.append(str(kwargs.get("text") or ""))
            return None

        async def _delegate_review(_session, _plan, _task, _bot, _context, _dest):
            return ReviewResult(approved=True, summary="ok", comments="ok")

        async def _make_decision(_task, _review, *, workdir):
            return "approved", []

        async def _auto_commit(_session, _task, _plan, _bot, _context, _dest):
            return True

        async def _reconcile_after_commit(_session, _task, _plan, _bot, _context, _dest):
            raise RuntimeError("reconcile failed")

        monkeypatch.setattr(orch, "_auto_commit_baseline_before_first_step", _noop)
        monkeypatch.setattr(orch, "_send_runtime_message", _send_runtime_message)
        monkeypatch.setattr(orch, "_delegate_review", _delegate_review)
        monkeypatch.setattr(orch, "_make_decision", _make_decision)
        monkeypatch.setattr(orch, "_auto_commit", _auto_commit)
        monkeypatch.setattr(orch, "_reconcile_plan_after_commit", _reconcile_after_commit)

        await orch._run_loop(session, plan, SimpleNamespace(), object(), {"chat_id": 123})

        saved = orch._load_live_plan(session)
        assert saved is not None
        assert saved.status == "paused"
        assert saved.current_task_id is None
        assert saved.tasks[0].status == "approved"
        assert any("reconcile after commit упал" in message for message in sent_messages)
        assert "reconcile after commit failed task_id=task_1" in caplog.text

    caplog.set_level(logging.ERROR, logger="agent.manager_core")
    asyncio.run(_run())


def test_manager_does_not_block_auto_commit_when_approved_notification_hangs(
    tmp_path,
    monkeypatch,
    caplog,
) -> None:
    async def _run() -> None:
        config = _make_config(tmp_path)
        orch = ManagerOrchestrator(config)
        monkeypatch.setattr(orch, "_MANAGER_NOTIFICATION_TIMEOUT_SEC", 0.01)
        session = SimpleNamespace(id="s1", chat_id=123, workdir=str(tmp_path))
        auto_commit_called = False
        plan = ProjectPlan(
            project_goal="keep manager moving after approve",
            status="active",
            current_task_id="task_1",
            tasks=[
                DevTask(
                    id="task_1",
                    title="First task",
                    description="done",
                    acceptance_criteria=["approved"],
                    status="in_review",
                    attempt=1,
                ),
            ],
        )

        async def _noop(*_args, **_kwargs):
            return None

        async def _send_runtime_message(*_args, **kwargs):
            if str(kwargs.get("text") or "").startswith("✅ Принято"):
                await asyncio.Future()
            return None

        async def _delegate_review(_session, _plan, _task, _bot, _context, _dest):
            return ReviewResult(approved=True, summary="ok", comments="ok")

        async def _make_decision(_task, _review, *, workdir):
            return "approved", []

        async def _auto_commit(_session, _task, _plan, _bot, _context, _dest):
            nonlocal auto_commit_called
            auto_commit_called = True
            return False

        monkeypatch.setattr(orch, "_auto_commit_baseline_before_first_step", _noop)
        monkeypatch.setattr(orch, "_send_runtime_message", _send_runtime_message)
        monkeypatch.setattr(orch, "_delegate_review", _delegate_review)
        monkeypatch.setattr(orch, "_make_decision", _make_decision)
        monkeypatch.setattr(orch, "_auto_commit", _auto_commit)

        await orch._run_loop(session, plan, SimpleNamespace(), object(), {"chat_id": 123})

        saved = orch._load_live_plan(session)
        assert saved is not None
        assert auto_commit_called is True
        assert saved.status == "completed"
        assert saved.current_task_id is None
        assert saved.tasks[0].status == "approved"
        assert "manager runtime message timed out stage=approved notification task_id=task_1" in caplog.text

    caplog.set_level(logging.WARNING, logger="agent.manager_core")
    asyncio.run(_run())


def test_manager_runtime_adapter_validates_required_callables(tmp_path) -> None:
    config = _make_config(tmp_path)

    async def _send_message(_context, **_kwargs):
        return None

    async def _send_output(_session, _dest, _output, _context, **_kwargs):
        return None

    with pytest.raises(RuntimeError, match="send_message"):
        ManagerRuntimeAdapter(
            config=config,
            send_message=None,
            send_output=_send_output,
            is_admin=lambda _chat_id: False,
        )
    with pytest.raises(RuntimeError, match="send_output"):
        ManagerRuntimeAdapter(
            config=config,
            send_message=_send_message,
            send_output=None,
            is_admin=lambda _chat_id: False,
        )
    with pytest.raises(RuntimeError, match="is_admin"):
        ManagerRuntimeAdapter(
            config=config,
            send_message=_send_message,
            send_output=_send_output,
            is_admin=None,
        )


def test_manager_runtime_adapter_normalizes_is_admin_result(tmp_path) -> None:
    config = _make_config(tmp_path)

    async def _send_message(_context, **_kwargs):
        return None

    async def _send_output(_session, _dest, _output, _context, **_kwargs):
        return None

    adapter = ManagerRuntimeAdapter(
        config=config,
        send_message=_send_message,
        send_output=_send_output,
        is_admin=lambda _chat_id: "admin",
    )

    assert adapter.is_admin(123) is True


def test_manager_runtime_adapter_from_mode_runtime_context_drives_runner(tmp_path) -> None:
    async def _run() -> None:
        calls = []
        config = _make_config(tmp_path)

        async def _send_message(context, **kwargs):
            calls.append(("message", context, dict(kwargs)))
            return SimpleNamespace(message_id=17)

        async def _send_output(session, dest, output, context, **kwargs):
            calls.append(("output", session, dict(dest), output, context, dict(kwargs)))
            return None

        class _Orchestrator:
            def __init__(self, config):
                self._config = config

            async def run(self, session, prompt, bot, context, dest):
                assert isinstance(bot, ManagerRuntimeAdapter)
                assert bot.config is config
                assert bot.is_admin(dest["chat_id"]) is True
                await bot.send_message(context, chat_id=dest["chat_id"], text=prompt)
                await bot.send_output(session, dest, "manager-output", context, send_header=False)
                return "manager-ok"

        runner = ManagerModeRunnerService(config)
        runner._orchestrator = _Orchestrator(config)
        session = SimpleNamespace(id="s1", workdir=str(tmp_path))
        transport_context = object()
        dest = {"kind": "telegram", "chat_id": 123}
        mode = _build_probe_mode(config, _send_message, runtime=runner)
        runtime_context = mode_runtime_context_from_legacy(
            {
                "session": session,
                "chat_id": 123,
                "user_id": 123,
                "context": transport_context,
                "dest": dest,
            },
            mode,
        )

        result = await runner.run_with_runtime_context(
            runtime_context,
            "build plan",
            send_output=_send_output,
            is_admin=lambda chat_id: int(chat_id) == 123,
        )

        assert result == "manager-ok"
        assert calls == [
            ("message", transport_context, {"chat_id": 123, "text": "build plan"}),
            ("output", session, dest, "manager-output", transport_context, {"send_header": False}),
        ]

    asyncio.run(_run())


def test_manager_runtime_adapter_from_mode_runtime_context_validates_required_callables(tmp_path) -> None:
    config = _make_config(tmp_path)

    async def _send_message(_context, **_kwargs):
        return None

    async def _send_output(_session, _dest, _output, _context, **_kwargs):
        return None

    runner = ManagerModeRunnerService(config)
    session = SimpleNamespace(id="s1", workdir=str(tmp_path))
    mode = _build_probe_mode(config, _send_message, runtime=runner)
    runtime_context = mode_runtime_context_from_legacy(
        {
            "session": session,
            "chat_id": 123,
            "user_id": 123,
            "context": object(),
            "dest": {"kind": "telegram", "chat_id": 123},
        },
        mode,
    )

    with pytest.raises(RuntimeError, match="send_output"):
        ManagerRuntimeAdapter.from_runtime_context(
            runtime_context,
            is_admin=lambda _chat_id: True,
        )
    with pytest.raises(RuntimeError, match="is_admin"):
        ManagerRuntimeAdapter.from_runtime_context(
            runtime_context,
            send_output=_send_output,
        )

    missing_send_mode = _build_probe_mode(config, None, runtime=runner)
    missing_send_context = mode_runtime_context_from_legacy(
        {
            "session": session,
            "chat_id": 123,
            "user_id": 123,
            "context": object(),
            "dest": {"kind": "telegram", "chat_id": 123},
        },
        missing_send_mode,
    )
    with pytest.raises(RuntimeError, match="send_message"):
        ManagerRuntimeAdapter.from_runtime_context(
            missing_send_context,
            send_output=_send_output,
            is_admin=lambda _chat_id: True,
        )
