"""Integration test: execute 'echo hello' through a real asyncssh SSH tunnel.

Uses asyncssh.create_server() to spin up an in-process SSH server on a
random local port, then verifies SSHConnectionWrapper.run() returns the
expected output through an actual SSH connection.

Marked with ``@pytest.mark.ssh_integration`` so it can be filtered in CI.
"""

import asyncio

import asyncssh
import pytest

from app.services.ssh_service import SSHConnectionWrapper


class _TestSSHServer(asyncssh.SSHServer):
    def begin_auth(self, username: str) -> bool:
        return False  # no auth required

    def session_requested(self) -> bool:
        return True


async def _handle_client(process: asyncssh.SSHServerProcess) -> None:
    cmd = process.command
    if not cmd:
        process.stdout.write("no command\n")
        process.exit(1)
        return
    proc = await asyncio.create_subprocess_shell(
        cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout_data, stderr_data = await proc.communicate()
    if stdout_data:
        process.stdout.write(stdout_data.decode())
    if stderr_data:
        process.stderr.write(stderr_data.decode())
    process.exit(proc.returncode or 0)


async def _start_test_server(tmp_path):
    key = asyncssh.generate_private_key("ssh-ed25519")
    key_path = str(tmp_path / "host_key")
    key.write_private_key(key_path)

    server = await asyncssh.create_server(
        _TestSSHServer,
        host="127.0.0.1",
        port=0,
        server_host_keys=[key_path],
        process_factory=_handle_client,
    )
    port = server.sockets[0].getsockname()[1]
    return server, port


@pytest.mark.ssh_integration
@pytest.mark.asyncio
async def test_echo_hello_through_ssh_tunnel(tmp_path):
    """Execute 'echo hello' on a local SSH server and verify output."""
    server, port = await _start_test_server(tmp_path)
    try:
        conn = await asyncssh.connect(
            host="127.0.0.1",
            port=port,
            known_hosts=None,
            username="testuser",
        )
        wrapper = SSHConnectionWrapper(conn, idle_timeout_sec=30)
        result = await wrapper.run("echo hello", timeout_sec=10)

        assert result.stdout.strip() == "hello"
        assert result.exit_status == 0

        await wrapper.close()
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.ssh_integration
@pytest.mark.asyncio
async def test_command_exit_code_through_ssh_tunnel(tmp_path):
    """Verify non-zero exit code is propagated."""
    server, port = await _start_test_server(tmp_path)
    try:
        conn = await asyncssh.connect(
            host="127.0.0.1",
            port=port,
            known_hosts=None,
            username="testuser",
        )
        wrapper = SSHConnectionWrapper(conn, idle_timeout_sec=30)
        result = await wrapper.run("exit 42", timeout_sec=10)

        assert result.exit_status == 42

        await wrapper.close()
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.ssh_integration
@pytest.mark.asyncio
async def test_multiline_output_through_ssh_tunnel(tmp_path):
    """Verify multi-line output is captured correctly."""
    server, port = await _start_test_server(tmp_path)
    try:
        conn = await asyncssh.connect(
            host="127.0.0.1",
            port=port,
            known_hosts=None,
            username="testuser",
        )
        wrapper = SSHConnectionWrapper(conn, idle_timeout_sec=30)
        result = await wrapper.run(
            "echo line1; echo line2", timeout_sec=10,
        )

        lines = result.stdout.strip().splitlines()
        assert lines == ["line1", "line2"]

        await wrapper.close()
    finally:
        server.close()
        await server.wait_closed()
