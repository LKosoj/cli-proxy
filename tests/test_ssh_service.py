"""Tests for SSHService exec/stream/cancel/close_all with mocked connections."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.ssh_service import SSHExecResult, SSHService


def _write_ssh_config(tmp_path, hosts_yaml):
    ssh_dir = tmp_path / ".cli-proxy"
    ssh_dir.mkdir(exist_ok=True)
    (ssh_dir / "ssh.yaml").write_text(hosts_yaml)


def _basic_host_yaml(alias="prod", host="10.0.0.1", user="deploy"):
    return (
        f"hosts:\n"
        f"  {alias}:\n"
        f"    host: {host}\n"
        f"    user: {user}\n"
        f"    auth: key\n"
        f"    idle_timeout_sec: 60\n"
    )


def _mock_asyncssh_conn():
    conn = MagicMock()
    conn.get_extra_info = MagicMock(return_value=MagicMock())
    conn.close = MagicMock()
    conn.wait_closed = AsyncMock()
    completed = SimpleNamespace(
        stdout="hello\n", stderr="", exit_status=0,
        returncode=0, env=None, command="echo hello",
        subsystem=None, exit_signal=None,
    )
    conn.run = AsyncMock(return_value=completed)
    return conn


@pytest.mark.asyncio
async def test_exec_returns_result(tmp_path):
    _write_ssh_config(tmp_path, _basic_host_yaml())
    mock_conn = _mock_asyncssh_conn()

    service = SSHService()
    with patch("app.services.ssh_service.asyncssh.connect", new=AsyncMock(return_value=mock_conn)):
        result = await service.exec(str(tmp_path), "prod", "echo hello")

    assert isinstance(result, SSHExecResult)
    assert result.stdout == "hello\n"
    assert result.exit_code == 0
    await service.close_all()


@pytest.mark.asyncio
async def test_exec_reuses_connection(tmp_path):
    _write_ssh_config(tmp_path, _basic_host_yaml())
    mock_conn = _mock_asyncssh_conn()

    service = SSHService()
    with patch("app.services.ssh_service.asyncssh.connect", new=AsyncMock(return_value=mock_conn)) as mock_connect:
        await service.exec(str(tmp_path), "prod", "cmd1")
        await service.exec(str(tmp_path), "prod", "cmd2")
        assert mock_connect.await_count == 1

    await service.close_all()


@pytest.mark.asyncio
async def test_exec_unknown_host_raises(tmp_path):
    _write_ssh_config(tmp_path, _basic_host_yaml())
    service = SSHService()
    with pytest.raises(ValueError, match="not found"):
        await service.exec(str(tmp_path), "nonexistent", "ls")


@pytest.mark.asyncio
async def test_exec_acl_blocks_unauthorized(tmp_path):
    yaml = (
        "hosts:\n"
        "  prod:\n"
        "    host: 10.0.0.1\n"
        "    user: deploy\n"
        "    allowed_chat_ids: [111]\n"
    )
    _write_ssh_config(tmp_path, yaml)
    service = SSHService()
    with pytest.raises(PermissionError, match="not allowed"):
        await service.exec(str(tmp_path), "prod", "ls", chat_id=999)


@pytest.mark.asyncio
async def test_exec_acl_allows_authorized(tmp_path):
    yaml = (
        "hosts:\n"
        "  prod:\n"
        "    host: 10.0.0.1\n"
        "    user: deploy\n"
        "    allowed_chat_ids: [111]\n"
    )
    _write_ssh_config(tmp_path, yaml)
    mock_conn = _mock_asyncssh_conn()
    service = SSHService()
    with patch("app.services.ssh_service.asyncssh.connect", new=AsyncMock(return_value=mock_conn)):
        result = await service.exec(str(tmp_path), "prod", "ls", chat_id=111)
    assert result.exit_code == 0
    await service.close_all()


@pytest.mark.asyncio
async def test_exec_timeout(tmp_path):
    _write_ssh_config(tmp_path, _basic_host_yaml())
    mock_conn = _mock_asyncssh_conn()
    mock_conn.run = AsyncMock(side_effect=asyncio.TimeoutError())

    service = SSHService()
    with patch("app.services.ssh_service.asyncssh.connect", new=AsyncMock(return_value=mock_conn)):
        result = await service.exec(str(tmp_path), "prod", "slow", timeout_sec=1)

    assert result.exit_code == -1
    assert "timed out" in result.stderr.lower()
    await service.close_all()


@pytest.mark.asyncio
async def test_stream_collects_output(tmp_path):
    _write_ssh_config(tmp_path, _basic_host_yaml())
    mock_conn = _mock_asyncssh_conn()

    chunks = ["line1\n", "line2\n", ""]
    chunk_iter = iter(chunks)

    mock_stdout = MagicMock()
    mock_stdout.read = AsyncMock(side_effect=lambda n: next(chunk_iter))
    mock_stderr = MagicMock()
    mock_stderr.read = AsyncMock(return_value="")

    mock_proc = MagicMock()
    mock_proc.stdout = mock_stdout
    mock_proc.stderr = mock_stderr
    mock_proc.exit_status = 0
    mock_proc.send_signal = MagicMock()
    mock_proc.wait = AsyncMock()
    mock_conn.create_process = AsyncMock(return_value=mock_proc)

    service = SSHService()
    previews = []

    async def on_output(text):
        previews.append(text)

    with patch("app.services.ssh_service.asyncssh.connect", new=AsyncMock(return_value=mock_conn)):
        result = await service.stream(
            str(tmp_path), "prod", "tail -f log",
            max_duration_sec=10, on_output=on_output,
        )

    assert "line1" in result.stdout
    assert "line2" in result.stdout
    assert result.exit_code == 0
    assert len(previews) >= 1
    await service.close_all()


@pytest.mark.asyncio
async def test_cancel_no_active(tmp_path):
    service = SSHService()
    result = await service.cancel(str(tmp_path), "prod")
    assert result is False


@pytest.mark.asyncio
async def test_close_all_by_workdir(tmp_path):
    _write_ssh_config(tmp_path, _basic_host_yaml())
    mock_conn = _mock_asyncssh_conn()

    service = SSHService()
    with patch("app.services.ssh_service.asyncssh.connect", new=AsyncMock(return_value=mock_conn)):
        await service.exec(str(tmp_path), "prod", "ls")

    assert len(service._connections) == 1
    await service.close_all(workdir=str(tmp_path))
    assert len(service._connections) == 0


@pytest.mark.asyncio
async def test_multiple_hosts_separate_connections(tmp_path):
    yaml = (
        "hosts:\n"
        "  prod:\n"
        "    host: 10.0.0.1\n"
        "    user: deploy\n"
        "  staging:\n"
        "    host: 10.0.0.2\n"
        "    user: admin\n"
    )
    _write_ssh_config(tmp_path, yaml)
    mock_conn1 = _mock_asyncssh_conn()
    mock_conn2 = _mock_asyncssh_conn()
    conns = iter([mock_conn1, mock_conn2])

    service = SSHService()
    with patch("app.services.ssh_service.asyncssh.connect", new=AsyncMock(side_effect=lambda **kw: next(conns))):
        await service.exec(str(tmp_path), "prod", "ls")
        await service.exec(str(tmp_path), "staging", "ls")

    assert len(service._connections) == 2
    await service.close_all()
    assert len(service._connections) == 0


@pytest.mark.asyncio
async def test_service_api_contract():
    service = SSHService()
    assert callable(getattr(service, "exec"))
    assert callable(getattr(service, "stream"))
    assert callable(getattr(service, "cancel"))
    assert callable(getattr(service, "close_all"))
    assert callable(getattr(service, "test_connection"))
    assert callable(getattr(service, "generate_key"))


# ---------------------------------------------------------------------------
# sudo prompt detection
# ---------------------------------------------------------------------------

def test_contains_sudo_prompt_patterns():
    from app.services.ssh_service import _contains_sudo_prompt

    assert _contains_sudo_prompt("[sudo] password for user:")
    assert _contains_sudo_prompt("Password:")
    assert _contains_sudo_prompt("password for deploy:")
    assert _contains_sudo_prompt("some output\n[sudo] password for root: ")
    assert not _contains_sudo_prompt("normal output\n")
    assert not _contains_sudo_prompt("")


@pytest.mark.asyncio
async def test_stream_sudo_prompt_sends_password(tmp_path):
    yaml = (
        "hosts:\n"
        "  prod:\n"
        "    host: 10.0.0.1\n"
        "    user: deploy\n"
        "    sudo: true\n"
        "    sudo_password_env: SUDO_PASS\n"
    )
    _write_ssh_config(tmp_path, yaml)
    ssh_dir = tmp_path / ".cli-proxy"
    (ssh_dir / "ssh.env").write_text("SUDO_PASS=mysecret\n")

    mock_conn = _mock_asyncssh_conn()
    chunks = ["[sudo] password for deploy: ", "Success\n", ""]
    chunk_iter = iter(chunks)

    mock_stdin = MagicMock()
    mock_stdin.write = MagicMock()
    mock_stdout = MagicMock()
    mock_stdout.read = AsyncMock(side_effect=lambda n: next(chunk_iter))
    mock_stderr = MagicMock()
    mock_stderr.read = AsyncMock(return_value="")

    mock_proc = MagicMock()
    mock_proc.stdin = mock_stdin
    mock_proc.stdout = mock_stdout
    mock_proc.stderr = mock_stderr
    mock_proc.exit_status = 0
    mock_proc.send_signal = MagicMock()
    mock_proc.wait = AsyncMock()
    mock_conn.create_process = AsyncMock(return_value=mock_proc)

    service = SSHService()
    with patch("app.services.ssh_service.asyncssh.connect", new=AsyncMock(return_value=mock_conn)):
        result = await service.stream(
            str(tmp_path), "prod", "sudo systemctl restart app",
            max_duration_sec=10,
        )

    mock_stdin.write.assert_called_once_with("mysecret\n")
    assert "Success" in result.stdout
    await service.close_all()


@pytest.mark.asyncio
async def test_stream_no_sudo_when_not_configured(tmp_path):
    _write_ssh_config(tmp_path, _basic_host_yaml())
    mock_conn = _mock_asyncssh_conn()

    chunks = ["[sudo] password for deploy: ", ""]
    chunk_iter = iter(chunks)

    mock_stdin = MagicMock()
    mock_stdin.write = MagicMock()
    mock_stdout = MagicMock()
    mock_stdout.read = AsyncMock(side_effect=lambda n: next(chunk_iter))

    mock_proc = MagicMock()
    mock_proc.stdin = mock_stdin
    mock_proc.stdout = mock_stdout
    mock_proc.exit_status = 0
    mock_proc.send_signal = MagicMock()
    mock_proc.wait = AsyncMock()
    mock_conn.create_process = AsyncMock(return_value=mock_proc)

    service = SSHService()
    with patch("app.services.ssh_service.asyncssh.connect", new=AsyncMock(return_value=mock_conn)):
        await service.stream(
            str(tmp_path), "prod", "sudo ls",
            max_duration_sec=10,
        )

    mock_stdin.write.assert_not_called()
    await service.close_all()
