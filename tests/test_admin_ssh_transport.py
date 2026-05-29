from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
_SSH_TRANSPORT_PATH = REPO_ROOT / "modes" / "admin" / "transports" / "ssh.py"
_SPEC = importlib.util.spec_from_file_location("modes_admin_ssh_transport_test", _SSH_TRANSPORT_PATH)
if _SPEC is None or _SPEC.loader is None:
    raise RuntimeError(f"failed to load admin ssh transport module from {_SSH_TRANSPORT_PATH}")
_MODULE = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _MODULE
_SPEC.loader.exec_module(_MODULE)
SSHCommandSpec = _MODULE.SSHCommandSpec
SSHSubprocessTransport = _MODULE.SSHSubprocessTransport
SSHTransportError = _MODULE.SSHTransportError


def test_ssh_subprocess_transport_executes_command_with_key(monkeypatch, tmp_path) -> None:
    async def _run() -> None:
        key_path = tmp_path / "id_rsa"
        key_path.write_text("private-key", encoding="utf-8")
        captured = {}

        class _FakeProc:
            returncode = 0

            async def communicate(self):
                return b"SSH_OK\n", b""

            def kill(self) -> None:
                return None

        async def _fake_create_subprocess_exec(*argv, **kwargs):
            captured["argv"] = list(argv)
            captured["kwargs"] = dict(kwargs)
            return _FakeProc()

        monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_create_subprocess_exec)
        transport = SSHSubprocessTransport()
        spec = SSHCommandSpec(
            action_id="restart_nginx",
            host="server.example",
            user="root",
            port=2222,
            key_path=str(key_path),
            argv=("echo", "SSH_OK"),
            timeout_sec=2.0,
            options=("StrictHostKeyChecking=no",),
        )

        result = await transport.run(spec)

        assert result.action_id == "restart_nginx"
        assert result.returncode == 0
        assert result.timed_out is False
        assert "SSH_OK" in result.stdout

        argv = captured["argv"]
        assert argv[0] == "ssh"
        assert "-i" in argv
        assert str(key_path) in argv
        assert "-p" in argv
        assert "2222" in argv
        assert "root@server.example" in argv
        assert "echo SSH_OK" in argv
        assert captured["kwargs"].get("stdout") == asyncio.subprocess.PIPE
        assert captured["kwargs"].get("stderr") == asyncio.subprocess.PIPE

    asyncio.run(_run())


def test_ssh_subprocess_transport_executes_command_with_password(monkeypatch) -> None:
    async def _run() -> None:
        captured = {}

        class _FakeCompleted:
            returncode = 0
            stdout = "SSH_OK\n"
            stderr = ""

        class _FakeConn:
            async def run(self, command, check=False):
                captured["command"] = command
                captured["check"] = check
                return _FakeCompleted()

            def close(self) -> None:
                captured["closed"] = True

            async def wait_closed(self) -> None:
                captured["wait_closed"] = True

        async def _fake_connect(**kwargs):
            captured["connect_kwargs"] = dict(kwargs)
            return _FakeConn()

        monkeypatch.setattr(_MODULE.asyncssh, "connect", _fake_connect)
        transport = SSHSubprocessTransport()
        spec = SSHCommandSpec(
            action_id="probe",
            host="server.example",
            user="root",
            port=2222,
            argv=("echo", "SSH_OK"),
            timeout_sec=2.0,
            password="secret",
        )

        result = await transport.run(spec)

        assert result.action_id == "probe"
        assert result.returncode == 0
        assert result.timed_out is False
        assert "SSH_OK" in result.stdout
        assert captured["connect_kwargs"]["host"] == "server.example"
        assert captured["connect_kwargs"]["username"] == "root"
        assert captured["connect_kwargs"]["password"] == "secret"
        assert captured["connect_kwargs"]["known_hosts"] is None
        assert captured["command"] == "echo SSH_OK"
        assert captured["check"] is False
        assert captured["closed"] is True
        assert captured["wait_closed"] is True

    asyncio.run(_run())


def test_ssh_subprocess_transport_marks_timeout(tmp_path) -> None:
    async def _run() -> None:
        key_path = tmp_path / "id_rsa"
        key_path.write_text("private-key", encoding="utf-8")

        class _SlowProc:
            def __init__(self) -> None:
                self.killed = False
                self.returncode = None

            async def communicate(self):
                if not self.killed:
                    await asyncio.sleep(0.3)
                    return b"", b""
                return b"", b"killed"

            def kill(self) -> None:
                self.killed = True
                self.returncode = 137

        transport = SSHSubprocessTransport()
        original = asyncio.create_subprocess_exec

        async def _fake_create_subprocess_exec(*_args, **_kwargs):
            return _SlowProc()

        asyncio.create_subprocess_exec = _fake_create_subprocess_exec
        try:
            spec = SSHCommandSpec(
                action_id="slow_cmd",
                host="server.example",
                key_path=str(key_path),
                argv=("sleep", "5"),
                timeout_sec=0.05,
            )
            result = await transport.run(spec)
            assert result.timed_out is True
            assert result.returncode != 0
        finally:
            asyncio.create_subprocess_exec = original

    asyncio.run(_run())


def test_ssh_subprocess_transport_requires_existing_key(tmp_path) -> None:
    async def _run() -> None:
        transport = SSHSubprocessTransport()
        spec = SSHCommandSpec(
            action_id="missing_key",
            host="server.example",
            key_path=str(tmp_path / "missing_key"),
            argv=("echo", "x"),
            timeout_sec=1.0,
        )
        try:
            await transport.run(spec)
        except SSHTransportError as exc:
            assert "private key not found" in str(exc)
            return
        raise AssertionError("expected SSHTransportError for missing key")

    asyncio.run(_run())


def test_ssh_subprocess_transport_rejects_empty_host(tmp_path) -> None:
    async def _run() -> None:
        key_path = tmp_path / "id_rsa"
        key_path.write_text("private-key", encoding="utf-8")
        transport = SSHSubprocessTransport()
        spec = SSHCommandSpec(
            action_id="empty_host",
            host="",
            key_path=str(key_path),
            argv=("echo", "x"),
            timeout_sec=1.0,
        )
        try:
            await transport.run(spec)
        except SSHTransportError as exc:
            assert "empty host" in str(exc)
            return
        raise AssertionError("expected SSHTransportError for empty host")

    asyncio.run(_run())


def test_ssh_subprocess_transport_closes_pipe_transports(monkeypatch, tmp_path) -> None:
    async def _run() -> None:
        key_path = tmp_path / "id_rsa"
        key_path.write_text("private-key", encoding="utf-8")
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
                return b"SSH_OK\n", b""

            def kill(self) -> None:
                return None

        async def _fake_create_subprocess_exec(*_argv, **_kwargs):
            return _FakeProc()

        monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_create_subprocess_exec)
        transport = SSHSubprocessTransport()
        spec = SSHCommandSpec(
            action_id="close_pipes",
            host="server.example",
            key_path=str(key_path),
            argv=("echo", "SSH_OK"),
            timeout_sec=1.0,
        )

        result = await transport.run(spec)

        assert result.returncode == 0
        assert sorted(closed) == ["stderr", "stdout"]

    asyncio.run(_run())
