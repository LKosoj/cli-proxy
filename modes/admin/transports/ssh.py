from __future__ import annotations

import asyncio
import logging
import os
import shlex
import time
from dataclasses import dataclass
from typing import Optional

import asyncssh


class SSHTransportError(RuntimeError):
    """Raised when SSH subprocess execution cannot be performed."""


_LOG = logging.getLogger(__name__)


@dataclass(frozen=True)
class SSHCommandSpec:
    action_id: str
    host: str
    argv: tuple[str, ...]
    key_path: str = ""
    user: Optional[str] = None
    port: int = 22
    timeout_sec: float = 30.0
    options: tuple[str, ...] = ()
    password: Optional[str] = None


@dataclass(frozen=True)
class SSHCommandResult:
    action_id: str
    host: str
    user: Optional[str]
    port: int
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool
    duration_ms: int


class SSHSubprocessTransport:
    @staticmethod
    def _close_pipe_transport(pipe: object) -> None:
        transport = getattr(pipe, "_transport", None)
        if transport is None:
            return
        try:
            transport.close()
        except Exception:
            pass

    async def run(self, spec: SSHCommandSpec) -> SSHCommandResult:
        host = str(spec.host or "").strip()
        if not host:
            raise SSHTransportError("empty host for ssh command")

        argv = tuple(str(item) for item in (spec.argv or ()))
        if not argv:
            raise SSHTransportError("empty argv for ssh command")

        timeout_sec = float(spec.timeout_sec or 0.0)
        if timeout_sec <= 0:
            raise SSHTransportError("timeout_sec must be > 0")

        port = int(spec.port or 0)
        if port <= 0:
            raise SSHTransportError("port must be > 0")

        user = str(spec.user or "").strip() or None
        password = str(spec.password or "")
        key_path = str(spec.key_path or "").strip()
        if not key_path and not password:
            raise SSHTransportError("empty key_path/password for ssh command")

        if not key_path:
            return await self._run_with_password(
                action_id=str(spec.action_id or "").strip(),
                host=host,
                user=user,
                port=port,
                argv=argv,
                password=password,
                timeout_sec=timeout_sec,
            )

        key_path_abs = os.path.abspath(key_path)
        if not os.path.isfile(key_path_abs):
            raise SSHTransportError(f"ssh private key not found: {key_path_abs}")

        target = f"{user}@{host}" if user else host
        remote_command = shlex.join(argv)

        connect_timeout = max(1, min(int(timeout_sec), 120))
        command = [
            "ssh",
            "-o",
            "BatchMode=yes",
            "-o",
            f"ConnectTimeout={connect_timeout}",
            "-i",
            key_path_abs,
            "-p",
            str(port),
        ]
        for option in tuple(str(opt).strip() for opt in (spec.options or ()) if str(opt).strip()):
            command.extend(["-o", option])
        command.extend([target, remote_command])

        started_at = time.perf_counter()
        try:
            proc = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except Exception as exc:
            raise SSHTransportError(f"failed to start ssh command: {exc}") from exc

        timed_out = False
        try:
            out_b, err_b = await asyncio.wait_for(proc.communicate(), timeout=timeout_sec)
        except asyncio.TimeoutError:
            timed_out = True
            try:
                proc.kill()
            except Exception:
                _LOG.exception(
                    "ssh transport: failed to kill timed out process action_id=%s target=%s",
                    str(spec.action_id or ""),
                    target,
                )
            out_b, err_b = await proc.communicate()
        except Exception as exc:
            raise SSHTransportError(f"failed to execute ssh command: {exc}") from exc
        finally:
            self._close_pipe_transport(getattr(proc, "stdout", None))
            self._close_pipe_transport(getattr(proc, "stderr", None))

        duration_ms = int((time.perf_counter() - started_at) * 1000)
        returncode = int(proc.returncode if proc.returncode is not None else -1)
        if timed_out and returncode == 0:
            returncode = -1

        stdout = (out_b or b"").decode("utf-8", errors="replace")
        stderr = (err_b or b"").decode("utf-8", errors="replace")
        return SSHCommandResult(
            action_id=str(spec.action_id or "").strip(),
            host=host,
            user=user,
            port=port,
            returncode=returncode,
            stdout=stdout,
            stderr=stderr,
            timed_out=timed_out,
            duration_ms=duration_ms,
        )

    async def _run_with_password(
        self,
        *,
        action_id: str,
        host: str,
        user: Optional[str],
        port: int,
        argv: tuple[str, ...],
        password: str,
        timeout_sec: float,
    ) -> SSHCommandResult:
        remote_command = shlex.join(argv)
        started_at = time.perf_counter()
        conn = None
        timed_out = False
        try:
            conn = await asyncio.wait_for(
                asyncssh.connect(
                    host=host,
                    port=port,
                    username=user,
                    password=password,
                    known_hosts=None,
                ),
                timeout=timeout_sec,
            )
            completed = await asyncio.wait_for(
                conn.run(remote_command, check=False),
                timeout=timeout_sec,
            )
            returncode = int(getattr(completed, "returncode", -1))
            stdout = str(getattr(completed, "stdout", "") or "")
            stderr = str(getattr(completed, "stderr", "") or "")
        except asyncio.TimeoutError:
            timed_out = True
            returncode = -1
            stdout = ""
            stderr = "ssh command timed out"
        except Exception as exc:
            _LOG.exception(
                "ssh transport: failed to execute password command action_id=%s host=%s",
                action_id,
                host,
            )
            raise SSHTransportError(f"failed to execute ssh password command: {exc}") from exc
        finally:
            if conn is not None:
                try:
                    conn.close()
                    await conn.wait_closed()
                except Exception:
                    _LOG.exception(
                        "ssh transport: failed to close password connection action_id=%s host=%s",
                        action_id,
                        host,
                    )

        duration_ms = int((time.perf_counter() - started_at) * 1000)
        return SSHCommandResult(
            action_id=action_id,
            host=host,
            user=user,
            port=port,
            returncode=returncode,
            stdout=stdout,
            stderr=stderr,
            timed_out=timed_out,
            duration_ms=duration_ms,
        )


__all__ = ["SSHCommandSpec", "SSHCommandResult", "SSHSubprocessTransport", "SSHTransportError"]
