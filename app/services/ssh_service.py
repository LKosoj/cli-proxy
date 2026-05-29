"""SSH service: connection management, command execution and streaming."""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import os
import time
from typing import Awaitable, Callable, Dict, Optional, Tuple

import asyncssh

from app.services.ssh_config_loader import (
    load_ssh_config,
    load_ssh_secrets,
    resolve_ssh_secret,
)

logger = logging.getLogger(__name__)

_SUDO_PROMPT_PATTERNS = (
    "[sudo] password",
    "Password:",
    "password for ",
)


def _contains_sudo_prompt(text: str) -> bool:
    """Check whether *text* contains a recognisable sudo password prompt."""
    lower = text.lower()
    return any(p.lower() in lower for p in _SUDO_PROMPT_PATTERNS)


class SSHConnectionWrapper:
    """Wraps a single :class:`asyncssh.SSHClientConnection` with an idle timer.

    After *idle_timeout_sec* seconds without any command activity the
    connection is closed automatically.  Every call to :meth:`run` or
    :meth:`start_process` resets the timer.
    """

    def __init__(
        self,
        conn: asyncssh.SSHClientConnection,
        idle_timeout_sec: int = 1200,
    ) -> None:
        self._conn = conn
        self._idle_timeout_sec = max(1, int(idle_timeout_sec))
        self._idle_handle: Optional[asyncio.TimerHandle] = None
        self.on_idle_close: Optional[Callable[[], None]] = None
        self._active_process: Optional[asyncssh.SSHClientProcess] = None
        self._closed = False
        self._reset_idle_timer()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def is_open(self) -> bool:
        if self._closed:
            return False
        try:
            transport = self._conn.get_extra_info("transport")
            if transport is None:
                return False
        except Exception:
            return False
        return True

    async def run(
        self,
        command: str,
        timeout_sec: int = 30,
        sudo_password: Optional[str] = None,
    ) -> asyncssh.SSHCompletedProcess:
        """Execute *command* and return the completed result.

        When *sudo_password* is provided and the output contains a sudo
        prompt, the password is fed automatically.
        """
        self._reset_idle_timer()
        if sudo_password:
            return await self._run_with_sudo(command, timeout_sec, sudo_password)
        return await asyncio.wait_for(
            self._conn.run(command),
            timeout=max(1, timeout_sec),
        )

    async def start_process(self, command: str) -> asyncssh.SSHClientProcess:
        """Start a long-running remote process.

        The caller is responsible for reading output and terminating the
        process.  A reference is kept in :attr:`_active_process` so that
        :meth:`cancel_active` can send ``SIGINT``.
        """
        self._reset_idle_timer()
        proc = await self._conn.create_process(command)
        self._active_process = proc
        return proc

    def cancel_active(self) -> bool:
        """Send SIGINT to the active long-running process, if any.

        Returns ``True`` if a signal was sent.
        """
        proc = self._active_process
        if proc is None:
            return False
        try:
            proc.send_signal("INT")
            return True
        except Exception:
            logger.debug("cancel_active: failed to send SIGINT", exc_info=True)
            return False

    async def close(self) -> None:
        """Close the underlying SSH connection and cancel the idle timer."""
        if self._closed:
            return
        self._closed = True
        if self._idle_handle is not None:
            self._idle_handle.cancel()
            self._idle_handle = None
        try:
            self._conn.close()
            await self._conn.wait_closed()
        except Exception:
            logger.debug("SSHConnectionWrapper.close error", exc_info=True)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _reset_idle_timer(self) -> None:
        if self._idle_handle is not None:
            self._idle_handle.cancel()
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._idle_handle = loop.call_later(
            self._idle_timeout_sec,
            self._on_idle_timeout,
        )

    def _on_idle_timeout(self) -> None:
        logger.info("SSH connection idle timeout (%ds), closing", self._idle_timeout_sec)
        asyncio.ensure_future(self.close())
        if self.on_idle_close is not None:
            try:
                self.on_idle_close()
            except Exception:
                logger.debug("on_idle_close callback error", exc_info=True)

    async def _run_with_sudo(
        self,
        command: str,
        timeout_sec: int,
        sudo_password: str,
    ) -> asyncssh.SSHCompletedProcess:
        """Run a command that may prompt for a sudo password."""
        async def _inner() -> asyncssh.SSHCompletedProcess:
            proc = await self._conn.create_process(command, input=sudo_password + "\n")
            stdout_data = await proc.stdout.read()
            stderr_data = await proc.stderr.read()
            await proc.wait()
            return asyncssh.SSHCompletedProcess(
                env=None, command=command,
                subsystem=None, exit_status=proc.exit_status,
                exit_signal=None, returncode=proc.returncode,
                stdout=stdout_data, stderr=stderr_data,
            )
        return await asyncio.wait_for(_inner(), timeout=max(1, timeout_sec))


# ======================================================================
# Result dataclasses
# ======================================================================

@dataclasses.dataclass
class SSHExecResult:
    stdout: str
    stderr: str
    exit_code: int


@dataclasses.dataclass
class SSHTestResult:
    ok: bool
    message: str
    server_info: Optional[str] = None


@dataclasses.dataclass
class SSHKeygenResult:
    private_path: str
    public_key_text: str


# ======================================================================
# SSHService — top-level service with connection pool
# ======================================================================

class SSHService:
    """Centralised SSH service managing a pool of per-host connections."""

    def __init__(self) -> None:
        self._connections: Dict[Tuple[str, str], SSHConnectionWrapper] = {}
        self._locks: Dict[Tuple[str, str], asyncio.Lock] = {}

    # ------------------------------------------------------------------
    # exec
    # ------------------------------------------------------------------

    async def exec(
        self,
        workdir: str,
        host_alias: str,
        command: str,
        *,
        timeout_sec: int = 30,
        chat_id: Optional[int] = None,
    ) -> SSHExecResult:
        """Execute *command* on a remote host and return the result."""
        wrapper = await self._get_or_connect(workdir, host_alias, chat_id)
        host_cfg = load_ssh_config(workdir).get(host_alias)
        sudo_password: Optional[str] = None
        if host_cfg and "sudo" in command and host_cfg.sudo and host_cfg.sudo_password_env:
            secrets = load_ssh_secrets(workdir)
            sudo_password = resolve_ssh_secret(secrets, host_cfg.sudo_password_env)
        try:
            result = await wrapper.run(command, timeout_sec=timeout_sec, sudo_password=sudo_password)
            return SSHExecResult(
                stdout=result.stdout or "",
                stderr=result.stderr or "",
                exit_code=result.exit_status if result.exit_status is not None else 0,
            )
        except asyncio.TimeoutError:
            return SSHExecResult(stdout="", stderr="Command timed out", exit_code=-1)

    # ------------------------------------------------------------------
    # stream
    # ------------------------------------------------------------------

    async def stream(
        self,
        workdir: str,
        host_alias: str,
        command: str,
        *,
        max_duration_sec: int = 300,
        tail_lines: int = 200,
        on_output: Optional[Callable[[str], Awaitable[None]]] = None,
        chat_id: Optional[int] = None,
    ) -> SSHExecResult:
        """Run a long command, streaming stdout via *on_output* callback.

        Returns the last *tail_lines* lines when the process finishes or
        *max_duration_sec* is exceeded.  When the command contains ``sudo``
        and the host is configured with ``sudo: true`` + ``sudo_password_env``,
        a sudo prompt in the output stream is detected and the password is
        fed automatically.
        """
        wrapper = await self._get_or_connect(workdir, host_alias, chat_id)

        sudo_password: Optional[str] = None
        host_cfg = load_ssh_config(workdir).get(host_alias)
        if host_cfg and "sudo" in command and host_cfg.sudo and host_cfg.sudo_password_env:
            secrets = load_ssh_secrets(workdir)
            sudo_password = resolve_ssh_secret(secrets, host_cfg.sudo_password_env)

        proc = await wrapper.start_process(command)
        buffer: list[str] = []
        sudo_sent = False
        start = time.monotonic()

        try:
            while True:
                elapsed = time.monotonic() - start
                remaining = max(0.1, max_duration_sec - elapsed)
                if elapsed >= max_duration_sec:
                    proc.send_signal("INT")
                    await asyncio.sleep(2.0)
                    break
                try:
                    chunk = await asyncio.wait_for(
                        proc.stdout.read(4096),
                        timeout=min(remaining, 2.0),
                    )
                except asyncio.TimeoutError:
                    continue
                if not chunk:
                    break
                buffer.append(chunk)

                if sudo_password and not sudo_sent:
                    tail_text = "".join(buffer)
                    if _contains_sudo_prompt(tail_text):
                        proc.stdin.write(sudo_password + "\n")
                        sudo_sent = True

                if on_output is not None:
                    tail_text = "".join(buffer)
                    lines = tail_text.splitlines()
                    preview = "\n".join(lines[-tail_lines:])
                    try:
                        await on_output(preview)
                    except Exception:
                        logger.debug("stream on_output callback error", exc_info=True)
        except Exception:
            logger.debug("stream read loop error", exc_info=True)

        try:
            await asyncio.wait_for(proc.wait(), timeout=3.0)
        except (asyncio.TimeoutError, Exception):
            pass

        full_output = "".join(buffer)
        lines = full_output.splitlines()
        tail = "\n".join(lines[-tail_lines:])
        exit_code = proc.exit_status if proc.exit_status is not None else -1
        wrapper._active_process = None
        return SSHExecResult(stdout=tail, stderr="", exit_code=exit_code)

    # ------------------------------------------------------------------
    # cancel
    # ------------------------------------------------------------------

    async def cancel(self, workdir: str, host_alias: str) -> bool:
        """Cancel the active long-running command on *host_alias*."""
        key = (os.path.realpath(workdir), host_alias)
        wrapper = self._connections.get(key)
        if wrapper is None:
            return False
        return wrapper.cancel_active()

    # ------------------------------------------------------------------
    # test_connection
    # ------------------------------------------------------------------

    async def test_connection(self, workdir: str, host_alias: str) -> SSHTestResult:
        """Verify connectivity to *host_alias*."""
        try:
            wrapper = await self._get_or_connect(workdir, host_alias, chat_id=None)
            result = await wrapper.run("uname -a", timeout_sec=10)
            return SSHTestResult(
                ok=True,
                message="Connection successful",
                server_info=(result.stdout or "").strip(),
            )
        except Exception as exc:
            return SSHTestResult(ok=False, message=str(exc))

    # ------------------------------------------------------------------
    # generate_key
    # ------------------------------------------------------------------

    async def generate_key(
        self,
        workdir: str,
        alias: str,
        key_type: str = "ssh-ed25519",
    ) -> SSHKeygenResult:
        """Generate an SSH key pair and store the private key in the project."""
        keys_dir = os.path.join(workdir, ".cli-proxy", "ssh_keys")
        os.makedirs(keys_dir, mode=0o700, exist_ok=True)
        private_path = os.path.join(keys_dir, f"{alias}.key")
        if os.path.exists(private_path):
            raise FileExistsError(f"Key already exists: {private_path}")
        key = asyncssh.generate_private_key(key_type)
        key.write_private_key(private_path)
        os.chmod(private_path, 0o600)
        public_key_text = key.export_public_key().decode("utf-8").strip()
        return SSHKeygenResult(private_path=private_path, public_key_text=public_key_text)

    # ------------------------------------------------------------------
    # close_all
    # ------------------------------------------------------------------

    async def close_all(self, workdir: Optional[str] = None) -> None:
        """Close connections, optionally filtered by *workdir*."""
        real_workdir = os.path.realpath(workdir) if workdir else None
        keys_to_close = [
            k for k in list(self._connections)
            if real_workdir is None or k[0] == real_workdir
        ]
        for key in keys_to_close:
            wrapper = self._connections.pop(key, None)
            if wrapper is not None:
                await wrapper.close()

    # ------------------------------------------------------------------
    # Connection pool internals
    # ------------------------------------------------------------------

    async def _get_or_connect(
        self,
        workdir: str,
        host_alias: str,
        chat_id: Optional[int] = None,
    ) -> SSHConnectionWrapper:
        real_workdir = os.path.realpath(workdir)
        key = (real_workdir, host_alias)

        existing = self._connections.get(key)
        if existing is not None and existing.is_open:
            return existing
        if existing is not None:
            self._connections.pop(key, None)

        if key not in self._locks:
            self._locks[key] = asyncio.Lock()

        async with self._locks[key]:
            existing = self._connections.get(key)
            if existing is not None and existing.is_open:
                return existing

            hosts = load_ssh_config(workdir)
            if host_alias not in hosts:
                raise ValueError(f"SSH host '{host_alias}' not found in {workdir}")
            host_cfg = hosts[host_alias]

            if host_cfg.allowed_chat_ids is not None and chat_id is not None:
                if chat_id not in host_cfg.allowed_chat_ids:
                    raise PermissionError(
                        f"Chat {chat_id} not allowed for SSH host '{host_alias}'"
                    )

            secrets = load_ssh_secrets(workdir)
            connect_kwargs: Dict[str, object] = {
                "host": host_cfg.host,
                "port": host_cfg.port,
                "username": host_cfg.user,
                "known_hosts": None,
            }
            if host_cfg.auth == "key" and host_cfg.key_file:
                key_path = host_cfg.key_file
                if not os.path.isabs(key_path):
                    key_path = os.path.join(workdir, key_path)
                passphrase = resolve_ssh_secret(secrets, host_cfg.key_passphrase_env)
                connect_kwargs["client_keys"] = [key_path]
                if passphrase:
                    connect_kwargs["passphrase"] = passphrase
            elif host_cfg.auth == "password":
                password = resolve_ssh_secret(secrets, host_cfg.password_env)
                if password:
                    connect_kwargs["password"] = password

            conn = await asyncssh.connect(**connect_kwargs)
            wrapper = SSHConnectionWrapper(conn, idle_timeout_sec=host_cfg.idle_timeout_sec)
            wrapper.on_idle_close = lambda: self._connections.pop(key, None)
            self._connections[key] = wrapper
            return wrapper
