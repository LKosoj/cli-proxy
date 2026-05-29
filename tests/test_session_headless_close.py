import asyncio
import errno
import os
import sys
from types import SimpleNamespace

import pytest

from app.services.session_creation_service import SessionCreationService
from app.services.session_service import SessionService
from app.services.task_service import TaskService
from config import AppConfig, DefaultsConfig, MCPConfig, MiniAppConfig, TelegramConfig, ToolConfig
from session import SessionManager


def _build_config(tmp_path) -> AppConfig:
    return AppConfig(
        telegram=TelegramConfig(token="t", whitelist_chat_ids=[1], admlist_chat_ids=[1]),
        tools={
            "sleeper": ToolConfig(
                name="sleeper",
                mode="headless",
                cmd=[sys.executable, "-c", "import time; time.sleep(30)"],
            )
        },
        defaults=DefaultsConfig(
            workdir=str(tmp_path / "workdir"),
            state_path=str(tmp_path / "runtime" / "state.json"),
            toolhelp_path=str(tmp_path / "runtime" / "toolhelp.json"),
            log_path=str(tmp_path / "logs" / "bot.log"),
        ),
        mcp=MCPConfig(enabled=False),
        mcp_clients=[],
        presets=[],
        path=str(tmp_path / "config.yaml"),
        miniapp=MiniAppConfig(),
    )


async def _spawn_idle_process() -> asyncio.subprocess.Process:
    return await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        "import time; time.sleep(30)",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )


def _pid_exists(pid: int) -> bool:
    try:
        os.kill(int(pid), 0)
        return True
    except OSError as exc:
        return exc.errno != errno.ESRCH


async def _wait_pid_exit(pid: int, *, timeout: float = 3.0) -> None:
    deadline = asyncio.get_running_loop().time() + float(timeout)
    while asyncio.get_running_loop().time() < deadline:
        if not _pid_exists(pid):
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"headless pid still alive: {pid}")


@pytest.mark.asyncio
async def test_close_session_terminates_lingering_headless_process(tmp_path) -> None:
    cfg = _build_config(tmp_path)
    manager = SessionManager(cfg)
    tasks = TaskService()
    sessions = SessionService(manager, tasks)
    session = sessions.create_session(1, "sleeper", str(tmp_path))

    proc = await _spawn_idle_process()
    pid = int(proc.pid)
    session.current_proc = proc
    assert _pid_exists(pid) is True

    assert await sessions.close_session(1, session.id, cancel_timeout_s=0.1) is True

    await _wait_pid_exit(pid)
    assert session.current_proc is None
    assert manager.get(1, session.id) is None


@pytest.mark.asyncio
async def test_session_close_is_idempotent_for_headless_process(tmp_path) -> None:
    cfg = _build_config(tmp_path)
    manager = SessionManager(cfg)
    session = manager.create(1, "sleeper", str(tmp_path))

    proc = await _spawn_idle_process()
    pid = int(proc.pid)
    session.current_proc = proc

    session.close()
    session.close()

    await _wait_pid_exit(pid)
    assert session.current_proc is None


@pytest.mark.asyncio
async def test_session_creation_rollback_aborts_headless_process_before_manager_close(tmp_path) -> None:
    cfg = _build_config(tmp_path)
    workdir = tmp_path / "project"
    workdir.mkdir()
    session = SessionManager(cfg).create(1, "sleeper", str(workdir))

    proc = await _spawn_idle_process()
    pid = int(proc.pid)
    session.current_proc = proc

    class _FailingThreadManager:
        @staticmethod
        def is_enabled() -> bool:
            return True

        @staticmethod
        async def ensure_topic_for_session(**_kwargs) -> None:
            raise RuntimeError("topic bind failed")

    class _FakeManager:
        def __init__(self, prepared_session) -> None:
            self._session = prepared_session
            self.close_calls: list[tuple[int, str]] = []

        def create(self, chat_id: int, tool_name: str | None, workdir_value: str):
            _ = (chat_id, tool_name, workdir_value)
            return self._session

        def close(self, chat_id: int, session_id: str) -> bool:
            self.close_calls.append((int(chat_id), str(session_id)))
            return True

    fake_manager = _FakeManager(session)
    bot_app = SimpleNamespace(
        config=cfg,
        manager=fake_manager,
        session_thread_manager=_FailingThreadManager(),
        _is_tool_available=lambda _tool: True,
        _expected_tools=lambda: "sleeper",
        is_within_root=lambda workdir_value, root: True,
    )
    service = SessionCreationService(bot_app)

    created, error = await service.create_session(1, "sleeper", str(workdir))

    assert created is None
    assert error == (
        "Не удалось создать topic для сессии. "
        "Проверьте, что у бота есть право управлять темами форума."
    )
    assert fake_manager.close_calls == [(1, session.id)]
    await _wait_pid_exit(pid)
    assert session.current_proc is None
