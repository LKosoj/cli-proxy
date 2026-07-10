from __future__ import annotations

import asyncio
import os
import pwd
import signal
import shlex
import shutil
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional


class TmuxDriverError(RuntimeError):
    pass


@dataclass(frozen=True)
class TmuxCommandResult:
    returncode: int
    stdout: str
    stderr: str


def wrap_user_command(argv: list[str], *, user: Optional[str] = None) -> list[str]:
    if not user:
        return list(argv)
    return ["su", "-", user, "-c", shlex.join(argv)]


def resolve_user_identity(user: Optional[str]) -> tuple[int, int] | None:
    name = str(user or "").strip()
    if not name:
        return None
    try:
        record = pwd.getpwnam(name)
    except KeyError as exc:
        raise TmuxDriverError(f"tmux user not found: {name}") from exc
    return int(record.pw_uid), int(record.pw_gid)


class TmuxDriver:
    def __init__(self, *, user: Optional[str] = None, timeout_sec: float = 30.0):
        self.user = str(user or "").strip() or None
        self.timeout_sec = float(timeout_sec)

    @staticmethod
    def tmux_available() -> bool:
        return shutil.which("tmux") is not None

    def command(self, *args: str) -> list[str]:
        return wrap_user_command(["tmux", *[str(arg) for arg in args]], user=self.user)

    async def run(self, *args: str, check: bool = True, timeout_sec: Optional[float] = None) -> TmuxCommandResult:
        if not self.tmux_available():
            raise TmuxDriverError("tmux binary is not available")
        argv = self.command(*args)
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        try:
            stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=timeout_sec or self.timeout_sec)
        except asyncio.TimeoutError as exc:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (OSError, ProcessLookupError):
                proc.kill()
            await proc.communicate()
            raise TmuxDriverError(f"tmux command timed out: {' '.join(args)}") from exc
        result = TmuxCommandResult(
            returncode=int(proc.returncode or 0),
            stdout=(stdout_b or b"").decode("utf-8", errors="replace"),
            stderr=(stderr_b or b"").decode("utf-8", errors="replace"),
        )
        if check and result.returncode != 0:
            raise TmuxDriverError(result.stderr.strip() or f"tmux command failed: {' '.join(args)}")
        return result

    async def has_session(self, session_name: str) -> bool:
        result = await self.run("has-session", "-t", session_name, check=False)
        return result.returncode == 0

    async def new_session(self, session_name: str, *, workdir: str, command: Iterable[str]) -> None:
        await self.run(
            "new-session",
            "-d",
            "-x",
            "200",
            "-y",
            "50",
            "-s",
            session_name,
            "-c",
            str(workdir),
            *[str(part) for part in command],
        )

    async def pipe_pane(self, pane_target: str, log_path: str) -> None:
        Path(log_path).parent.mkdir(parents=True, exist_ok=True)
        shell_command = f"cat >> {shlex.quote(str(log_path))}"
        await self.run("pipe-pane", "-o", "-t", pane_target, shell_command)

    async def load_buffer(self, prompt_path: str, *, buffer_name: Optional[str] = None) -> None:
        args = ["load-buffer"]
        if buffer_name:
            args.extend(["-b", str(buffer_name)])
        args.append(str(prompt_path))
        await self.run(*args)

    async def paste_buffer(
        self,
        pane_target: str,
        *,
        buffer_name: Optional[str] = None,
        delete: bool = False,
    ) -> None:
        args = ["paste-buffer"]
        if delete:
            args.append("-d")
        if buffer_name:
            args.extend(["-b", str(buffer_name)])
        args.extend(["-t", pane_target])
        await self.run(*args)

    async def delete_buffer(self, *, buffer_name: Optional[str]) -> None:
        if not buffer_name:
            return
        await self.run("delete-buffer", "-b", str(buffer_name), check=False)

    async def send_enter(self, pane_target: str) -> None:
        await self.run("send-keys", "-t", pane_target, "Enter")

    async def send_ctrl_c(self, pane_target: str) -> bool:
        result = await self.run("send-keys", "-t", pane_target, "C-c", check=False)
        return result.returncode == 0

    async def capture_pane(self, pane_target: str) -> str:
        result = await self.run("capture-pane", "-p", "-t", pane_target)
        return result.stdout

    async def kill_session(self, session_name: str) -> bool:
        result = await self.run("kill-session", "-t", session_name, check=False)
        if result.returncode == 0:
            return True
        error = str(result.stderr or "").strip()
        lower = error.lower()
        if any(
            marker in lower
            for marker in (
                "can't find session:",
                "no server running on",
                "no sessions",
                "no such file or directory",
            )
        ):
            return False
        raise TmuxDriverError(error or f"tmux session could not be killed: {session_name}")


def write_prompt_temp(runtime_dir: str, content: str, *, owner_user: Optional[str] = None) -> str:
    Path(runtime_dir).mkdir(parents=True, exist_ok=True)
    fd, path = tempfile.mkstemp(prefix="prompt-", suffix=".txt", dir=runtime_dir, text=True)
    try:
        os.write(fd, content.encode("utf-8"))
    finally:
        os.close(fd)
    identity = resolve_user_identity(owner_user)
    if identity is not None:
        os.chown(path, identity[0], identity[1])
    os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    return path
