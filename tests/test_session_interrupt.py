import asyncio
import logging
import signal
import subprocess
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import session as session_module
from config import AppConfig, DefaultsConfig, MCPConfig, TelegramConfig, ToolConfig
from session import Session


class _FakeNativePopen:
    def __init__(self, *, timeout_expired: bool = False) -> None:
        self.timeout_expired = bool(timeout_expired)
        self.wait_calls: list[float] = []

    def wait(self, timeout: float) -> int:
        self.wait_calls.append(float(timeout))
        if self.timeout_expired:
            raise subprocess.TimeoutExpired(cmd="claude", timeout=timeout)
        return -15


class _FakeInterruptProc:
    def __init__(self, *, popen: _FakeNativePopen | None) -> None:
        self.pid = 424242
        self.returncode = None
        self.terminate_calls = 0
        self.kill_calls = 0
        self._transport = SimpleNamespace(_proc=popen)

    def terminate(self) -> None:
        self.terminate_calls += 1

    def kill(self) -> None:
        self.kill_calls += 1
        self.returncode = -9


class _FakeHeadlessStream:
    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = list(chunks)

    async def read(self, _n: int) -> bytes:
        await asyncio.sleep(0)
        if self._chunks:
            return self._chunks.pop(0)
        return b""


class _FakeHeadlessEofProc:
    def __init__(self) -> None:
        self.pid = 434343
        self.returncode = None
        self.stdin = None
        self.stdout = _FakeHeadlessStream([b"partial output\n"])
        self.stderr = _FakeHeadlessStream([])
        self.terminate_calls = 0
        self.kill_calls = 0

    async def wait(self) -> int:
        while self.returncode is None:
            await asyncio.sleep(0)
        return int(self.returncode)

    def terminate(self) -> None:
        self.terminate_calls += 1

    def kill(self) -> None:
        self.kill_calls += 1
        self.returncode = -9


class _FakeBlockingHeadlessStream:
    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = list(chunks)
        self.close_calls = 0
        self._transport = SimpleNamespace(close=self._close)

    def _close(self) -> None:
        self.close_calls += 1

    async def read(self, _n: int) -> bytes:
        await asyncio.sleep(0)
        if self._chunks:
            return self._chunks.pop(0)
        await asyncio.Future()
        raise AssertionError("unreachable")


class _FakeNativePollPopen:
    def __init__(self, proc, *, returncode: int = 0) -> None:
        self._proc = proc
        self._returncode = int(returncode)
        self.poll_calls = 0

    def poll(self) -> int:
        self.poll_calls += 1
        self._proc.returncode = self._returncode
        return self._returncode


class _FakePendingWaitProc:
    def __init__(self) -> None:
        self.pid = 454545
        self.returncode = None
        self.stdin = None
        self.stdout = _FakeBlockingHeadlessStream([b"partial output\n"])
        self.stderr = _FakeHeadlessStream([])
        self._transport = SimpleNamespace(_proc=_FakeNativePollPopen(self))

    async def wait(self) -> int:
        await asyncio.Future()
        raise AssertionError("unreachable")

    def terminate(self) -> None:
        self.returncode = -15

    def kill(self) -> None:
        self.returncode = -9


def _build_session(tmp_path) -> Session:
    tool = ToolConfig(
        name="claude",
        mode="headless",
        cmd=["claude", "-p", "{prompt}"],
        headless_cmd=["claude", "-p", "{prompt}"],
    )
    cfg = AppConfig(
        telegram=TelegramConfig(token="", whitelist_chat_ids=[]),
        tools={"claude": tool},
        defaults=DefaultsConfig(workdir=str(tmp_path)),
        mcp=MCPConfig(enabled=False),
        mcp_clients=[],
        presets=[],
        path=str(tmp_path / "config.yaml"),
    )
    return Session(
        id="s1",
        tool=tool,
        workdir=str(tmp_path),
        idle_timeout_sec=10,
        config=cfg,
    )


def test_signal_headless_process_tree_signals_descendant_process_groups(tmp_path, monkeypatch) -> None:
    session = _build_session(tmp_path)
    group_signals: list[tuple[int, int]] = []
    pid_signals: list[tuple[int, int]] = []
    group_by_pid = {
        111: 111,
        222: 424242,
        333: 333,
        444: 777,
    }

    monkeypatch.setattr(session, "_headless_process_descendants", lambda _pid: [111, 222, 333, 444])
    monkeypatch.setattr(session, "_process_group_id", lambda pid: group_by_pid[pid])
    monkeypatch.setattr(session_module.os, "getpgrp", lambda: 777)
    monkeypatch.setattr(session_module.os, "killpg", lambda pid, sig: group_signals.append((pid, sig)))
    monkeypatch.setattr(session_module.os, "kill", lambda pid, sig: pid_signals.append((pid, sig)))

    assert session._signal_headless_process_tree(424242, signal.SIGTERM) is True

    assert group_signals == [
        (424242, signal.SIGTERM),
        (111, signal.SIGTERM),
        (333, signal.SIGTERM),
    ]
    assert pid_signals == [(444, signal.SIGTERM)]


def test_interrupt_uses_native_subprocess_wait_in_sync_context(tmp_path, monkeypatch) -> None:
    session = _build_session(tmp_path)
    popen = _FakeNativePopen()
    proc = _FakeInterruptProc(popen=popen)
    session.current_proc = proc
    group_signals: list[tuple[int, int]] = []

    monkeypatch.setattr(asyncio, "get_event_loop", lambda: (_ for _ in ()).throw(AssertionError("unexpected get_event_loop")))
    monkeypatch.setattr(session_module.os, "killpg", lambda pid, sig: group_signals.append((pid, sig)))

    session.interrupt()

    assert proc.terminate_calls == 0
    assert proc.kill_calls == 0
    assert popen.wait_calls == [0.5]
    assert group_signals == [(proc.pid, signal.SIGTERM)]


def test_interrupt_logs_graceful_timeout_separately_from_degradation(tmp_path, caplog) -> None:
    session = _build_session(tmp_path)
    popen = _FakeNativePopen(timeout_expired=True)
    proc = _FakeInterruptProc(popen=popen)
    session.current_proc = proc
    group_signals: list[tuple[int, int]] = []

    caplog.set_level(logging.WARNING, logger="session")
    with patch.object(session_module.os, "killpg", side_effect=lambda pid, sig: group_signals.append((pid, sig))):
        session.interrupt()

    messages = [record.getMessage() for record in caplog.records]
    assert proc.terminate_calls == 0
    assert proc.kill_calls == 0
    assert popen.wait_calls == [0.5, 0.5]
    assert group_signals == [(proc.pid, signal.SIGTERM), (proc.pid, signal.SIGKILL)]
    assert any("headless interrupt graceful timeout" in message for message in messages)
    assert not any("headless interrupt degraded to direct kill" in message for message in messages)


def test_close_headless_process_uses_native_subprocess_wait_in_sync_context(tmp_path) -> None:
    session = _build_session(tmp_path)
    popen = _FakeNativePopen()
    proc = _FakeInterruptProc(popen=popen)
    session.current_proc = proc
    group_signals: list[tuple[int, int]] = []

    with patch.object(session_module.os, "killpg", side_effect=lambda pid, sig: group_signals.append((pid, sig))):
        session.close_headless_process(wait_timeout_s=0.5)

    assert proc.terminate_calls == 0
    assert proc.kill_calls == 0
    assert popen.wait_calls == [0.5]
    assert group_signals == [(proc.pid, signal.SIGTERM)]
    assert session.current_proc is None


@pytest.mark.asyncio
async def test_interrupt_inside_active_loop_degrades_to_direct_kill_without_blocking_wait(
    tmp_path,
    caplog,
) -> None:
    session = _build_session(tmp_path)
    popen = _FakeNativePopen()
    proc = _FakeInterruptProc(popen=popen)
    session.current_proc = proc
    group_signals: list[tuple[int, int]] = []

    caplog.set_level(logging.WARNING, logger="session")

    with patch.object(
        asyncio,
        "get_event_loop",
        lambda: (_ for _ in ()).throw(AssertionError("unexpected get_event_loop")),
    ), patch.object(session_module.os, "killpg", side_effect=lambda pid, sig: group_signals.append((pid, sig))):
        t0 = asyncio.get_running_loop().time()
        session.interrupt()
        dt = asyncio.get_running_loop().time() - t0

    messages = [record.getMessage() for record in caplog.records]
    assert proc.terminate_calls == 0
    assert proc.kill_calls == 0
    assert popen.wait_calls == []
    assert dt < 0.2
    assert group_signals == [(proc.pid, signal.SIGTERM), (proc.pid, signal.SIGKILL)]
    assert any("headless interrupt degraded to direct kill" in message for message in messages)
    assert not any("headless interrupt graceful timeout" in message for message in messages)


@pytest.mark.asyncio
async def test_close_headless_process_inside_active_loop_degrades_to_direct_kill_without_wait(
    tmp_path,
    caplog,
) -> None:
    session = _build_session(tmp_path)
    popen = _FakeNativePopen()
    proc = _FakeInterruptProc(popen=popen)
    session.current_proc = proc
    group_signals: list[tuple[int, int]] = []

    caplog.set_level(logging.WARNING, logger="session")

    t0 = asyncio.get_running_loop().time()
    with patch.object(session_module.os, "killpg", side_effect=lambda pid, sig: group_signals.append((pid, sig))):
        session.close_headless_process(wait_timeout_s=0.5)
    dt = asyncio.get_running_loop().time() - t0

    messages = [record.getMessage() for record in caplog.records]
    assert proc.terminate_calls == 0
    assert proc.kill_calls == 0
    assert popen.wait_calls == []
    assert dt < 0.2
    assert group_signals == [(proc.pid, signal.SIGTERM), (proc.pid, signal.SIGKILL)]
    assert session.current_proc is None
    assert any("headless close degraded to direct kill" in message for message in messages)


def test_interrupt_uses_active_backend_when_headless_mode_falls_back_to_interactive(tmp_path) -> None:
    session = _build_session(tmp_path)
    sent_controls: list[str] = []

    class _FakeChild:
        def isalive(self) -> bool:
            return True

        def sendcontrol(self, key: str) -> None:
            sent_controls.append(str(key))

    proc = _FakeInterruptProc(popen=_FakeNativePopen())
    session.child = _FakeChild()
    session.current_proc = proc
    session._active_execution_backend = "interactive"

    session.interrupt()

    assert sent_controls == ["c"]
    assert proc.terminate_calls == 0
    assert proc.kill_calls == 0


@pytest.mark.asyncio
async def test_run_headless_stops_process_when_eof_does_not_lead_to_exit(
    tmp_path,
    monkeypatch,
    caplog,
) -> None:
    session = _build_session(tmp_path)
    proc = _FakeHeadlessEofProc()
    group_signals: list[tuple[int, int]] = []

    async def _fake_create_subprocess_exec(*_args, **_kwargs):
        return proc

    def _fake_killpg(pid: int, sig: int) -> None:
        group_signals.append((pid, sig))
        proc.returncode = -15

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_create_subprocess_exec)
    monkeypatch.setattr(session_module.os, "kill", lambda _pid, _sig: None)
    monkeypatch.setattr(session_module.os, "killpg", _fake_killpg)
    monkeypatch.setattr(session_module, "_HEADLESS_WAIT_POLL_SEC", 0.01)
    monkeypatch.setattr(session_module, "_HEADLESS_EOF_EXIT_TIMEOUT_SEC", 0.01)
    monkeypatch.setattr(session_module, "_HEADLESS_EOF_STOP_GRACE_SEC", 0.01)
    monkeypatch.setattr(session, "_start_claude_monitor", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(session, "_stop_claude_monitor", lambda: None)
    caplog.set_level(logging.WARNING, logger="session.headless")

    out = await session._run_headless("hello")

    messages = [record.getMessage() for record in caplog.records]
    assert out == "partial output\n"
    assert proc.terminate_calls == 0
    assert proc.kill_calls == 0
    assert group_signals == [(proc.pid, signal.SIGTERM)]
    assert session.headless_forced_stop == "процесс не завершился в течение 0.0s после EOF stdout"
    assert any("stdout EOF получен до завершения процесса" in message for message in messages)
    assert any("после EOF stdout" in message for message in messages)


@pytest.mark.asyncio
async def test_run_headless_uses_native_poll_when_wait_task_stays_pending(
    tmp_path,
    monkeypatch,
    caplog,
) -> None:
    session = _build_session(tmp_path)
    proc = _FakePendingWaitProc()

    async def _fake_create_subprocess_exec(*_args, **_kwargs):
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_create_subprocess_exec)
    monkeypatch.setattr(session_module.os, "kill", lambda _pid, _sig: None)
    monkeypatch.setattr(session_module, "_HEADLESS_WAIT_POLL_SEC", 0.01)
    monkeypatch.setattr(session, "_start_claude_monitor", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(session, "_stop_claude_monitor", lambda: None)
    caplog.set_level(logging.WARNING, logger="session.headless")

    out = await session._run_headless("hello")

    messages = [record.getMessage() for record in caplog.records]
    assert out == "partial output\n"
    assert proc.returncode == 0
    assert proc._transport._proc.poll_calls >= 1
    assert proc.stdout.close_calls == 1
    assert session.headless_forced_stop == "returncode=0 есть, но wait() не завершился (stdout pipe удерживается)"
    assert any("native poll detected exited process" in message for message in messages)
