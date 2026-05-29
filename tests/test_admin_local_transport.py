from __future__ import annotations

import asyncio
import importlib.util
import logging
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
_LOCAL_TRANSPORT_PATH = REPO_ROOT / "modes" / "admin" / "transports" / "local.py"
_SPEC = importlib.util.spec_from_file_location("modes_admin_local_transport_test", _LOCAL_TRANSPORT_PATH)
if _SPEC is None or _SPEC.loader is None:
    raise RuntimeError(f"failed to load admin local transport module from {_LOCAL_TRANSPORT_PATH}")
_MODULE = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _MODULE
_SPEC.loader.exec_module(_MODULE)
LocalCommandSpec = _MODULE.LocalCommandSpec
LocalSubprocessTransport = _MODULE.LocalSubprocessTransport
LocalTransportError = _MODULE.LocalTransportError


def test_local_subprocess_transport_executes_command_and_captures_stdout(tmp_path) -> None:
    async def _run() -> None:
        transport = LocalSubprocessTransport()
        spec = LocalCommandSpec(
            action_id="echo_test",
            argv=(sys.executable, "-c", "print('HELLO_LOCAL')"),
            cwd=str(tmp_path),
            timeout_sec=5.0,
        )
        result = await transport.run(spec)

        assert result.action_id == "echo_test"
        assert result.returncode == 0
        assert result.timed_out is False
        assert "HELLO_LOCAL" in result.stdout

    asyncio.run(_run())


def test_local_subprocess_transport_times_out_long_running_command(tmp_path) -> None:
    async def _run() -> None:
        transport = LocalSubprocessTransport()
        spec = LocalCommandSpec(
            action_id="timeout_test",
            argv=(sys.executable, "-c", "import time; time.sleep(0.5)"),
            cwd=str(tmp_path),
            timeout_sec=0.05,
        )
        result = await transport.run(spec)

        assert result.action_id == "timeout_test"
        assert result.timed_out is True
        assert result.returncode != 0

    asyncio.run(_run())


def test_local_subprocess_transport_rejects_empty_argv() -> None:
    async def _run() -> None:
        transport = LocalSubprocessTransport()
        spec = LocalCommandSpec(action_id="empty", argv=(), timeout_sec=1.0)
        try:
            await transport.run(spec)
        except LocalTransportError as exc:
            assert "empty argv" in str(exc)
            return
        raise AssertionError("expected LocalTransportError for empty argv")

    asyncio.run(_run())


def test_local_subprocess_transport_rejects_non_positive_timeout(tmp_path) -> None:
    async def _run() -> None:
        transport = LocalSubprocessTransport()
        spec = LocalCommandSpec(
            action_id="bad_timeout",
            argv=(sys.executable, "-c", "print('x')"),
            cwd=str(tmp_path),
            timeout_sec=0.0,
        )
        try:
            await transport.run(spec)
        except LocalTransportError as exc:
            assert "timeout_sec must be > 0" in str(exc)
            return
        raise AssertionError("expected LocalTransportError for timeout_sec <= 0")

    asyncio.run(_run())


def test_local_subprocess_transport_closes_pipe_transports(monkeypatch) -> None:
    async def _run() -> None:
        closed: list[str] = []

        class _FakeTransport:
            def __init__(self, label: str) -> None:
                self._label = label

            def close(self) -> None:
                closed.append(self._label)

        class _FakePipe:
            def __init__(self, label: str) -> None:
                self._transport = _FakeTransport(label)

        class _FakeProc:
            returncode = 0

            def __init__(self) -> None:
                self.stdout = _FakePipe("stdout")
                self.stderr = _FakePipe("stderr")

            async def communicate(self):
                return b"OK\n", b""

        async def _fake_create_subprocess_exec(*_argv, **_kwargs):
            return _FakeProc()

        monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_create_subprocess_exec)
        transport = LocalSubprocessTransport()
        spec = LocalCommandSpec(action_id="close_pipes", argv=(sys.executable, "-c", "print('x')"), timeout_sec=1.0)

        result = await transport.run(spec)

        assert result.returncode == 0
        assert sorted(closed) == ["stderr", "stdout"]

    asyncio.run(_run())


def test_local_subprocess_transport_logs_pipe_close_cleanup_failure(caplog) -> None:
    class _FailingTransport:
        def close(self) -> None:
            raise RuntimeError("close denied")

    class _FakePipe:
        _transport = _FailingTransport()

    caplog.set_level(logging.DEBUG, logger=_MODULE.__name__)

    LocalSubprocessTransport._close_pipe_transport(
        _FakePipe(),
        action_id="close_failure",
        stream_name="stdout",
    )

    assert "best_effort_cleanup: failed to close local subprocess pipe action_id=close_failure" in caplog.text
    assert "stream=stdout" in caplog.text


def test_local_subprocess_transport_logs_kill_cleanup_failure(monkeypatch, caplog) -> None:
    async def _run() -> None:
        class _FakeProc:
            returncode = -9
            stdout = None
            stderr = None
            pid = 4321

            def __init__(self) -> None:
                self.communicate_calls = 0

            def kill(self) -> None:
                raise RuntimeError("kill denied")

            async def communicate(self):
                self.communicate_calls += 1
                if self.communicate_calls == 1:
                    await asyncio.sleep(1.0)
                return b"", b"killed late"

        fake_proc = _FakeProc()

        async def _fake_create_subprocess_exec(*_argv, **_kwargs):
            return fake_proc

        monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_create_subprocess_exec)
        caplog.set_level(logging.DEBUG, logger=_MODULE.__name__)

        transport = LocalSubprocessTransport()
        result = await transport.run(
            LocalCommandSpec(
                action_id="kill_failure",
                argv=(sys.executable, "-c", "print('x')"),
                timeout_sec=0.01,
            )
        )

        assert result.action_id == "kill_failure"
        assert result.timed_out is True
        assert result.returncode == -9

    asyncio.run(_run())

    assert "best_effort_cleanup: failed to kill timed-out local subprocess action_id=kill_failure" in caplog.text
    assert "pid=4321" in caplog.text
