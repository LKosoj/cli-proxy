from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass
from typing import Mapping, Optional

logger = logging.getLogger(__name__)


class LocalTransportError(RuntimeError):
    """Raised when local subprocess execution cannot be performed."""


@dataclass(frozen=True)
class LocalCommandSpec:
    action_id: str
    argv: tuple[str, ...]
    cwd: Optional[str] = None
    timeout_sec: float = 30.0
    env: Optional[Mapping[str, str]] = None


@dataclass(frozen=True)
class LocalCommandResult:
    action_id: str
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool
    duration_ms: int


class LocalSubprocessTransport:
    @staticmethod
    def _close_pipe_transport(pipe: object, *, action_id: str, stream_name: str) -> None:
        transport = getattr(pipe, "_transport", None)
        if transport is None:
            return
        try:
            transport.close()
        except Exception:
            logger.debug(
                "best_effort_cleanup: failed to close local subprocess pipe action_id=%s stream=%s",
                action_id,
                stream_name,
                exc_info=True,
            )

    async def run(self, spec: LocalCommandSpec) -> LocalCommandResult:
        argv = tuple(str(item) for item in (spec.argv or ()))
        if not argv:
            raise LocalTransportError("empty argv for local command")
        action_id = str(spec.action_id or "").strip()

        cwd = str(spec.cwd).strip() if spec.cwd is not None else None
        timeout_sec = float(spec.timeout_sec or 0.0)
        if timeout_sec <= 0:
            raise LocalTransportError("timeout_sec must be > 0")

        env = None
        if spec.env is not None:
            env = dict(os.environ)
            env.update({str(k): str(v) for k, v in dict(spec.env).items()})

        started_at = time.perf_counter()
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                cwd=cwd or None,
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except Exception as exc:
            raise LocalTransportError(f"failed to start local command: {exc}") from exc

        timed_out = False
        try:
            out_b, err_b = await asyncio.wait_for(proc.communicate(), timeout=timeout_sec)
        except asyncio.TimeoutError:
            timed_out = True
            try:
                proc.kill()
            except Exception:
                logger.debug(
                    "best_effort_cleanup: failed to kill timed-out local subprocess action_id=%s pid=%s",
                    action_id,
                    getattr(proc, "pid", None),
                    exc_info=True,
                )
            out_b, err_b = await proc.communicate()
        except Exception as exc:
            raise LocalTransportError(f"failed to execute local command: {exc}") from exc
        finally:
            self._close_pipe_transport(
                getattr(proc, "stdout", None),
                action_id=action_id,
                stream_name="stdout",
            )
            self._close_pipe_transport(
                getattr(proc, "stderr", None),
                action_id=action_id,
                stream_name="stderr",
            )

        duration_ms = int((time.perf_counter() - started_at) * 1000)
        returncode = int(proc.returncode if proc.returncode is not None else -1)
        if timed_out and returncode == 0:
            returncode = -1

        stdout = (out_b or b"").decode("utf-8", errors="replace")
        stderr = (err_b or b"").decode("utf-8", errors="replace")
        return LocalCommandResult(
            action_id=action_id,
            returncode=returncode,
            stdout=stdout,
            stderr=stderr,
            timed_out=timed_out,
            duration_ms=duration_ms,
        )


__all__ = ["LocalCommandSpec", "LocalCommandResult", "LocalSubprocessTransport", "LocalTransportError"]
