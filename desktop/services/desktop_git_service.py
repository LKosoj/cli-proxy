from __future__ import annotations

import asyncio
import logging
import os
import shlex
import subprocess
import tempfile
from dataclasses import dataclass
from typing import Any, Dict, Iterable, Optional

from app.services.remote_control_service import ExecutionTarget, RemoteControlService
from app.services.remote_shell_service import RemoteShellService
from app.services.ssh_config_loader import load_ssh_config
from app.services.ssh_service import SSHService
from sessions.session_state_access import is_remote_control_enabled

#: Returned by merge/rebase when git exits with conflicts.
GitOpResult = Dict[str, Any]


@dataclass(frozen=True)
class _GitResult:
    code: int
    output: str


class DesktopGitService:
    """
    Минимальный Git-сервис для Desktop UI.

    - Все операции выполняются через subprocess с cwd=session.workdir.
    - Возвращает (returncode, output) и никогда не пробрасывает исключения наружу,
      чтобы не уронить UI-слой.
    - Методы merge/rebase/fetch/pull/push могут возвращать dict со статусом
      "conflict" при обнаружении git-конфликтов (см. GitOpResult).
    """

    def __init__(
        self,
        *,
        timeout_sec: float = 20.0,
        logger: Optional[logging.Logger] = None,
        ssh_service: Optional[SSHService] = None,
        remote_control_service: Optional[RemoteControlService] = None,
        github_token: Optional[str] = None,
    ):
        self._timeout_sec = float(timeout_sec)
        self._logger = logger or logging.getLogger(__name__)
        self._ssh_service = ssh_service or SSHService()
        self._remote_control_service = remote_control_service or RemoteControlService()
        self._github_token: Optional[str] = github_token
        self._askpass_path: Optional[str] = None

    async def status_text(self, session: Any) -> tuple[int, str]:
        remote_enabled, workdir, host_alias, remote_root = self._remote_context(session)
        if remote_enabled:
            if not workdir or not host_alias or not remote_root:
                return 2, "remote control is enabled but the remote target is unavailable"
            shell = RemoteShellService(self._ssh_service)
            result = await shell.git_status(
                workdir,
                host_alias,
                remote_root,
                timeout=max(1, int(self._timeout_sec)),
            )
            if not result.git_available:
                return 0, "git unavailable for this target"
            if result.error:
                return 1, result.error
            return 0, result.output
        return await self._run_git(session, ["status", "--porcelain=v1", "-b"])

    async def get_commit_context(self, session: Any) -> Optional[str]:
        """Контекст для генерации сообщения коммита (status + diff), как в боте."""
        code, status_out = await self._run_git(session, ["status", "--porcelain"])
        if code != 0:
            return None
        code, stat_out = await self._run_git(session, ["diff", "--stat"])
        if code != 0:
            stat_out = ""
        code, diff_out = await self._run_git(session, ["diff"])
        if code != 0:
            diff_out = ""
        text = (
            "git status --porcelain:\n"
            f"{(status_out or '').strip()}\n\n"
            "git diff --stat:\n"
            f"{(stat_out or '').strip()}\n\n"
            "git diff:\n"
            f"{(diff_out or '').strip()}"
        )
        return text.strip() or None

    async def commit(self, session: Any, message: str, body: Optional[str] = None) -> tuple[int, str]:
        msg = str(message or "").strip()
        if not msg:
            return 2, "commit message is empty"
        code, out = await self._run_git(session, ["add", "-A"])
        if code != 0:
            return code, out
        args: list[str] = ["commit", "-m", msg]
        if body and str(body).strip():
            args += ["-m", str(body).strip()[:2000]]
        return await self._run_git(session, args)

    # ------------------------------------------------------------------
    # GIT_ASKPASS helpers (token-based auth for push/pull/fetch)
    # ------------------------------------------------------------------

    def _ensure_askpass(self) -> Optional[str]:
        """Create a GIT_ASKPASS script that injects *github_token*."""
        token = self._github_token
        if not token:
            return None
        if self._askpass_path and os.path.isfile(self._askpass_path):
            return self._askpass_path
        fd, path = tempfile.mkstemp(prefix="dgt-askpass-", text=True)
        script = (
            "#!/bin/sh\n"
            "prompt=\"$1\"\n"
            "case \"$prompt\" in\n"
            "  *Username*) echo \"x-access-token\" ;;\n"
            "  *) echo \"$GIT_ASKPASS_TOKEN\" ;;\n"
            "esac\n"
        )
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(script)
        os.chmod(path, 0o700)
        self._askpass_path = path
        return path

    def _git_env(self) -> Dict[str, str]:
        """Build env dict with GIT_TERMINAL_PROMPT=0 and optional GIT_ASKPASS."""
        env = os.environ.copy()
        env["GIT_TERMINAL_PROMPT"] = "0"
        token = self._github_token
        if token:
            askpass = self._ensure_askpass()
            if askpass:
                env["GIT_ASKPASS"] = askpass
                env["GIT_ASKPASS_TOKEN"] = token
                env["GIT_USERNAME"] = "x-access-token"
        return env

    # ------------------------------------------------------------------
    # Remote-operations helpers
    # ------------------------------------------------------------------

    async def _current_branch(self, session: Any) -> Optional[str]:
        code, out = await self._run_git(session, ["rev-parse", "--abbrev-ref", "HEAD"])
        if code != 0:
            return None
        branch = out.strip()
        return branch if branch and branch != "HEAD" else None

    async def _upstream(self, session: Any) -> Optional[str]:
        code, out = await self._run_git(
            session,
            ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"],
        )
        return out.strip() if code == 0 and out.strip() else None

    # ------------------------------------------------------------------
    # pull / push (with token support + strategy)
    # ------------------------------------------------------------------

    async def pull(
        self,
        session: Any,
        *,
        strategy: str = "ff",
        remote: str = "origin",
    ) -> tuple[int, str]:
        """Pull from *remote*.

        strategy:
          - "ff"     — ``--ff-only`` (default, safe)
          - "merge"  — regular merge pull
          - "rebase" — ``--rebase``
        """
        strategy_flags: dict[str, list[str]] = {
            "ff": ["--ff-only"],
            "merge": [],
            "rebase": ["--rebase"],
        }
        flags = strategy_flags.get(strategy, ["--ff-only"])
        return await self._run_git_authed(session, ["pull", *flags])

    async def push(self, session: Any, *, remote: str = "origin") -> tuple[int, str]:
        """Push current branch; sets upstream automatically when not configured."""
        branch = await self._current_branch(session)
        upstream = await self._upstream(session)
        args = ["push"]
        if branch and not upstream:
            args += ["-u", remote, branch]
        return await self._run_git_authed(session, args)

    # ------------------------------------------------------------------
    # merge / rebase / fetch
    # ------------------------------------------------------------------

    async def fetch(self, session: Any, remote: str = "origin") -> tuple[int, str]:
        """Run ``git fetch --prune <remote>``."""
        return await self._run_git_authed(session, ["fetch", "--prune", remote])

    async def merge(
        self,
        session: Any,
        branch: str,
        *,
        strategy: str = "ff",
    ) -> GitOpResult:
        """Merge *branch* into HEAD.

        strategy:
          - "ff"     — ``--ff-only``
          - "merge"  — ``--no-ff``
          - "squash" — ``--squash``

        Returns::

            {"status": "ok",       "code": 0, "output": "..."}
            {"status": "conflict", "code": 1, "output": "...", "files": [...]}
            {"status": "error",    "code": N, "output": "..."}
        """
        strategy_flags: dict[str, list[str]] = {
            "ff": ["--ff-only"],
            "merge": ["--no-ff"],
            "squash": ["--squash"],
        }
        flags = strategy_flags.get(strategy, ["--ff-only"])
        code, output = await self._run_git(session, ["merge", *flags, str(branch)])
        return self._classify_git_result(code, output)

    async def rebase(self, session: Any, target_branch: str) -> GitOpResult:
        """Rebase HEAD onto *target_branch*.

        Returns same shape as :meth:`merge`.
        """
        code, output = await self._run_git(session, ["rebase", str(target_branch)])
        return self._classify_git_result(code, output)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _classify_git_result(code: int, output: str) -> GitOpResult:
        """Map (code, output) to a structured result dict."""
        if code == 0:
            return {"status": "ok", "code": 0, "output": output}
        lower = output.lower()
        if "conflict" in lower or "merge conflict" in lower:
            # Extract conflicted file names heuristically.
            files = [
                line.strip()
                for line in output.splitlines()
                if "conflict" in line.lower() and line.strip()
            ]
            return {"status": "conflict", "code": code, "output": output, "files": files}
        return {"status": "error", "code": code, "output": output}

    async def _run_git_authed(self, session: Any, args: Iterable[str]) -> tuple[int, str]:
        """Like _run_git but injects token env for local runs."""
        remote_enabled, workdir, host_alias, remote_root = self._remote_context(session)
        # For remote sessions token injection is not supported yet; fall through.
        if remote_enabled:
            return await self._run_git(session, args)

        cwd = self._resolve_cwd(session)
        argv = ["git", *[str(a) for a in args]]

        if not cwd:
            return 2, "session.workdir is not set"
        if not os.path.isdir(cwd):
            return 2, f"workdir is not a directory: {cwd}"

        env = self._git_env()

        def _sync_run() -> _GitResult:
            try:
                completed = subprocess.run(
                    argv,
                    cwd=cwd,
                    env=env,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    timeout=self._timeout_sec,
                )
                stdout = (completed.stdout or "").rstrip()
                stderr = (completed.stderr or "").rstrip()
                output = (stdout + "\n" + stderr).strip() if stdout and stderr else (stdout or stderr or "")
                return _GitResult(int(completed.returncode), output)
            except FileNotFoundError as e:
                return _GitResult(127, f"git is not available: {e}")
            except subprocess.TimeoutExpired:
                return _GitResult(124, f"git command timed out after {self._timeout_sec:.1f}s")
            except Exception as e:
                return _GitResult(1, f"{type(e).__name__}: {e}")

        res = await asyncio.to_thread(_sync_run)
        if res.code != 0:
            self._logger.info("git-authed failed code=%s cwd=%s argv=%s", res.code, cwd, argv)
        return res.code, res.output

    async def log(
        self,
        session: Any,
        max_count: int = 30,
        fmt: str = "%H|%an|%ad|%s",
    ) -> tuple[int, str]:
        """Возвращает историю коммитов в формате, удобном для парсинга."""
        remote_enabled, workdir, host_alias, remote_root = self._remote_context(session)
        if remote_enabled:
            if not workdir or not host_alias or not remote_root:
                return 2, "remote control is enabled but the remote target is unavailable"
            shell = RemoteShellService(self._ssh_service)
            result = await shell.git_log(
                workdir,
                host_alias,
                remote_root,
                max_count=max_count,
                timeout=max(1, int(self._timeout_sec)),
            )
            if not result.git_available:
                return 0, "git unavailable for this target"
            if result.error:
                return 1, result.error
            return 0, result.output
        return await self._run_git(
            session,
            ["log", f"--max-count={max_count}", f"--pretty=format:{fmt}", "--date=short"],
        )

    async def show(self, session: Any, commit: str) -> tuple[int, str]:
        """Показывает diff конкретного коммита."""
        return await self._run_git(session, ["show", "--stat", str(commit)])

    async def stash(self, session: Any) -> tuple[int, str]:
        return await self._run_git(session, ["stash"])

    async def stash_pop(self, session: Any) -> tuple[int, str]:
        return await self._run_git(session, ["stash", "pop"])

    async def branch_create(self, session: Any, branch_name: str) -> tuple[int, str]:
        return await self._run_git(session, ["checkout", "-b", str(branch_name)])

    async def checkout(self, session: Any, branch_name: str) -> tuple[int, str]:
        return await self._run_git(session, ["checkout", str(branch_name)])

    def _resolve_cwd(self, session: Any) -> Optional[str]:
        try:
            workdir = str(getattr(session, "workdir", "") or "").strip()
        except Exception:
            workdir = ""
        if not workdir:
            return None
        # Normalize to absolute to avoid surprises with relative cwd.
        abs_path = os.path.abspath(os.path.expanduser(workdir))
        if not os.path.isdir(abs_path):
            return abs_path  # keep for error message
        return abs_path

    def _remote_context(self, session: Any) -> tuple[bool, Optional[str], Optional[str], Optional[str]]:
        try:
            workdir = str(getattr(session, "workdir", "") or "").strip()
        except Exception:
            workdir = ""
        hosts = load_ssh_config(workdir) if workdir else {}
        effective = self._remote_control_service.compute_effective_state(session, hosts)
        if effective.execution_target == ExecutionTarget.REMOTE:
            return True, workdir, effective.host_alias, effective.remote_project_root
        if is_remote_control_enabled(session):
            return True, workdir, None, None
        return False, None, None, None

    async def _run_git(self, session: Any, args: Iterable[str]) -> tuple[int, str]:
        remote_enabled, workdir, host_alias, remote_root = self._remote_context(session)
        if remote_enabled:
            if not workdir or not host_alias or not remote_root:
                return 2, "remote control is enabled but the remote target is unavailable"

            shell = RemoteShellService(self._ssh_service)
            command = "git " + " ".join(shlex.quote(str(a)) for a in args)
            result = await shell.execute_command(
                workdir,
                host_alias,
                command,
                cwd=remote_root,
                timeout=max(1, int(self._timeout_sec)),
            )
            stdout = (result.stdout or "").rstrip()
            stderr = (result.stderr or "").rstrip()
            if stdout and stderr:
                output = stdout + "\n" + stderr
            else:
                output = stdout or stderr or ""
            if result.return_code != 0:
                self._logger.info(
                    "remote git failed code=%s host=%s cwd=%s argv=%s",
                    result.return_code,
                    host_alias,
                    remote_root,
                    list(args),
                )
            return int(result.return_code), output

        cwd = self._resolve_cwd(session)
        argv = ["git", *[str(a) for a in args]]

        if not cwd:
            return 2, "session.workdir is not set"
        if not os.path.isdir(cwd):
            return 2, f"workdir is not a directory: {cwd}"

        def _sync_run() -> _GitResult:
            env = os.environ.copy()
            # Never block on interactive auth in Desktop UI.
            env.setdefault("GIT_TERMINAL_PROMPT", "0")
            try:
                completed = subprocess.run(
                    argv,
                    cwd=cwd,
                    env=env,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    timeout=self._timeout_sec,
                )
                stdout = (completed.stdout or "").rstrip()
                stderr = (completed.stderr or "").rstrip()
                if stdout and stderr:
                    output = stdout + "\n" + stderr
                else:
                    output = stdout or stderr or ""
                return _GitResult(int(completed.returncode), output)
            except FileNotFoundError as e:
                return _GitResult(127, f"git is not available: {e}")
            except subprocess.TimeoutExpired:
                return _GitResult(124, f"git command timed out after {self._timeout_sec:.1f}s")
            except Exception as e:
                return _GitResult(1, f"{type(e).__name__}: {e}")

        res = await asyncio.to_thread(_sync_run)
        if res.code != 0:
            # Keep logging lightweight; UI gets output via return value.
            self._logger.info("git failed code=%s cwd=%s argv=%s", res.code, cwd, argv)
        return res.code, res.output
