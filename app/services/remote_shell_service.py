"""Remote shell service: execute commands on remote targets via SSH."""

from __future__ import annotations

import logging
import re
import shlex
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, List, Optional

from app.services.remote_control_service import ExecutionTarget

logger = logging.getLogger(__name__)

# Patterns for commands that require explicit approval before execution.
DANGEROUS_COMMAND_PATTERNS = [
    re.compile(r"\brm\s+.*-\w*[rf]\w*\s+.*-\w*[rf]"),  # rm -r -f (split flags)
    re.compile(r"\brm\s+-\w*r\w*f"),              # rm -rf, rm -rf
    re.compile(r"\brm\s+-\w*f\w*r"),              # rm -fr
    re.compile(r"\bgit\s+reset\s+--hard\b"),
    re.compile(r"\bgit\s+clean\s+-\w*f"),         # git clean -f, -fd, -fdx
    re.compile(r"\bgit\s+push\s+.*--force\b"),
    re.compile(r"\bgit\s+push\s+-f\b"),
    re.compile(r"\bmkfs\b"),
    re.compile(r"\bdd\s+"),
    re.compile(r"\b:()\s*\{"),                     # fork bomb
    re.compile(r"\bchmod\s+-R\s+777\b"),
    re.compile(r"\bchown\s+-R\b"),
    re.compile(r"\b>\s*/dev/sd"),                  # overwrite block device
]

# Type for approval callback: receives command string, returns True to approve.
ApprovalCallback = Callable[[str], Awaitable[bool]]


@dataclass
class CommandResult:
    """Result of a shell command execution."""

    stdout: str
    stderr: str
    return_code: int
    execution_target: str  # "local" or "remote"


@dataclass
class SearchMatch:
    """A single code search match."""

    file: str
    line: int
    text: str


@dataclass
class SearchResult:
    """Result of a code search operation."""

    matches: List[SearchMatch] = field(default_factory=list)
    truncated: bool = False
    error: Optional[str] = None


@dataclass
class GitStatusEntry:
    """A single file in git status output."""

    path: str
    status: str  # "M", "A", "D", "??" etc.


@dataclass
class GitResult:
    """Result of a git operation on remote target."""

    git_available: bool
    output: str = ""
    entries: List[Any] = field(default_factory=list)
    error: Optional[str] = None


def is_dangerous_command(command: str) -> bool:
    """Check if *command* matches any dangerous command pattern."""
    text = str(command or "")
    tokens = _split_command_tokens(text)
    if (
        _has_rm_recursive_force(tokens)
        or _has_git_clean_force(tokens)
        or _has_git_push_force(tokens)
    ):
        return True
    for pattern in DANGEROUS_COMMAND_PATTERNS:
        if pattern.search(text):
            return True
    return False


def _split_command_tokens(command: str) -> List[str]:
    try:
        return shlex.split(str(command or ""))
    except ValueError:
        return str(command or "").split()


def _command_name(token: str) -> str:
    return str(token or "").rsplit("/", 1)[-1]


def _short_option_has(token: str, flag: str) -> bool:
    item = str(token or "")
    return item.startswith("-") and not item.startswith("--") and flag in item[1:]


def _has_rm_recursive_force(tokens: List[str]) -> bool:
    for idx, token in enumerate(tokens):
        if _command_name(token) != "rm":
            continue
        recursive = False
        force = False
        for arg in tokens[idx + 1:]:
            if arg == "--":
                continue
            if arg == "--force":
                force = True
                continue
            if arg in {"--recursive", "--dir"}:
                recursive = True
                continue
            recursive = recursive or _short_option_has(arg, "r") or _short_option_has(arg, "R")
            force = force or _short_option_has(arg, "f")
        if recursive and force:
            return True
    return False


def _has_git_clean_force(tokens: List[str]) -> bool:
    for idx, token in enumerate(tokens):
        if _command_name(token) != "git":
            continue
        if idx + 1 >= len(tokens) or tokens[idx + 1] != "clean":
            continue
        for arg in tokens[idx + 2:]:
            if arg in {"-f", "--force"} or _short_option_has(arg, "f"):
                return True
    return False


def _has_git_push_force(tokens: List[str]) -> bool:
    for idx, token in enumerate(tokens):
        if _command_name(token) != "git":
            continue
        if idx + 1 >= len(tokens) or tokens[idx + 1] != "push":
            continue
        for arg in tokens[idx + 2:]:
            if arg == "-f" or arg.startswith("--force") or _short_option_has(arg, "f"):
                return True
    return False


class RemoteShellService:
    """Execute shell commands on a remote target via SSH.

    Uses :class:`SSHService` for actual command execution.
    Optionally requires approval for dangerous commands via an async callback.
    """

    def __init__(
        self,
        ssh_service: Any,
        approval_callback: Optional[ApprovalCallback] = None,
    ) -> None:
        self._ssh = ssh_service
        self._approval_callback = approval_callback

    async def execute_command(
        self,
        workdir: str,
        host_alias: str,
        command: str,
        *,
        cwd: Optional[str] = None,
        timeout: int = 30,
    ) -> CommandResult:
        """Execute *command* on the remote host.

        If *cwd* is provided, the command is run inside ``cd <cwd> && ...``.
        Dangerous commands are checked against :data:`DANGEROUS_COMMAND_PATTERNS`
        and require approval via the configured callback.  If no callback is set
        or the callback denies the command, execution is refused.
        """
        if is_dangerous_command(command):
            if self._approval_callback is None:
                logger.warning("Dangerous command rejected (no approval callback): %s", command)
                return CommandResult(
                    stdout="",
                    stderr="command requires approval but no approval callback is configured",
                    return_code=-1,
                    execution_target=ExecutionTarget.REMOTE.value,
                )
            approved = await self._approval_callback(command)
            if not approved:
                logger.info("Dangerous command denied by approval callback: %s", command)
                return CommandResult(
                    stdout="",
                    stderr="command denied by user",
                    return_code=-1,
                    execution_target=ExecutionTarget.REMOTE.value,
                )

        full_command = command
        if cwd:
            full_command = f"cd {_shell_quote(cwd)} && {command}"

        result = await self._ssh.exec(
            workdir, host_alias, full_command, timeout_sec=timeout,
        )
        return CommandResult(
            stdout=result.stdout or "",
            stderr=result.stderr or "",
            return_code=result.exit_code if hasattr(result, "exit_code") else 0,
            execution_target=ExecutionTarget.REMOTE.value,
        )

    async def search_code(
        self,
        workdir: str,
        host_alias: str,
        pattern: str,
        *,
        path: Optional[str] = None,
        cwd: Optional[str] = None,
        case_sensitive: bool = True,
        max_results: int = 200,
        timeout: int = 30,
    ) -> SearchResult:
        """Search for *pattern* in files on the remote host using grep.

        Uses ``grep -rn`` (or ``rg`` if available) to search.  Results are
        parsed into :class:`SearchMatch` objects with file paths, line numbers,
        and matching text.

        *path* restricts the search to a subdirectory (relative to cwd or
        absolute).  *max_results* caps the number of returned matches.
        """
        search_path = path or "."
        case_flag = "" if case_sensitive else " -i"

        # Try ripgrep first, fall back to grep
        search_cmd = (
            f"(command -v rg >/dev/null 2>&1 && "
            f"rg -n --no-heading --max-count {max_results}{case_flag} "
            f"-- {_shell_quote(pattern)} {_shell_quote(search_path)}) || "
            f"grep -rn{case_flag} -- {_shell_quote(pattern)} {_shell_quote(search_path)} "
            f"| head -n {max_results}"
        )
        cmd = f"cd {_shell_quote(cwd)} && {search_cmd}" if cwd else search_cmd

        result = await self._ssh.exec(
            workdir, host_alias, cmd, timeout_sec=timeout,
        )

        if result.exit_code not in (0, 1) and result.stderr:
            return SearchResult(error=result.stderr.strip())

        matches: List[SearchMatch] = []
        truncated = False
        for line in (result.stdout or "").strip().splitlines():
            if not line:
                continue
            parts = line.split(":", 2)
            if len(parts) < 3:
                continue
            file_path = parts[0]
            try:
                line_no = int(parts[1])
            except ValueError:
                continue
            text = parts[2]
            matches.append(SearchMatch(file=file_path, line=line_no, text=text))
            if len(matches) >= max_results:
                truncated = True
                break

        return SearchResult(matches=matches, truncated=truncated)

    # ------------------------------------------------------------------
    # Git commands
    # ------------------------------------------------------------------

    async def _git_check(self, workdir: str, host_alias: str, cwd: str) -> bool:
        """Check if *cwd* is inside a git repository on the remote host."""
        r = await self._ssh.exec(
            workdir, host_alias,
            f"cd {_shell_quote(cwd)} && git rev-parse --is-inside-work-tree 2>/dev/null",
            timeout_sec=10,
        )
        return (r.stdout or "").strip() == "true"

    async def git_status(
        self, workdir: str, host_alias: str, cwd: str, *, timeout: int = 15,
    ) -> GitResult:
        """Run ``git status --porcelain`` on the remote target."""
        if not await self._git_check(workdir, host_alias, cwd):
            return GitResult(git_available=False, error="not a git repository")

        r = await self._ssh.exec(
            workdir, host_alias,
            f"cd {_shell_quote(cwd)} && git status --porcelain",
            timeout_sec=timeout,
        )
        if r.exit_code != 0:
            return GitResult(git_available=True, error=(r.stderr or "").strip())

        entries = []
        for line in (r.stdout or "").splitlines():
            if len(line) < 4:
                continue
            status = line[:2].strip()
            path = line[3:]
            entries.append(GitStatusEntry(path=path, status=status))

        return GitResult(git_available=True, output=r.stdout or "", entries=entries)

    async def git_diff(
        self, workdir: str, host_alias: str, cwd: str, *, timeout: int = 30,
    ) -> GitResult:
        """Run ``git diff`` on the remote target."""
        if not await self._git_check(workdir, host_alias, cwd):
            return GitResult(git_available=False, error="not a git repository")

        r = await self._ssh.exec(
            workdir, host_alias,
            f"cd {_shell_quote(cwd)} && git diff",
            timeout_sec=timeout,
        )
        if r.exit_code != 0:
            return GitResult(git_available=True, error=(r.stderr or "").strip())

        return GitResult(git_available=True, output=r.stdout or "")

    async def git_branch(
        self, workdir: str, host_alias: str, cwd: str, *, timeout: int = 15,
    ) -> GitResult:
        """Run ``git branch -a`` on the remote target.

        Returns branches as entries with ``name`` and ``current`` fields.
        """
        if not await self._git_check(workdir, host_alias, cwd):
            return GitResult(git_available=False, error="not a git repository")

        r = await self._ssh.exec(
            workdir, host_alias,
            f"cd {_shell_quote(cwd)} && git branch -a",
            timeout_sec=timeout,
        )
        if r.exit_code != 0:
            return GitResult(git_available=True, error=(r.stderr or "").strip())

        entries = []
        for line in (r.stdout or "").splitlines():
            line = line.rstrip()
            if not line:
                continue
            current = line.startswith("*")
            name = line.lstrip("* ").strip()
            entries.append({"name": name, "current": current})

        return GitResult(git_available=True, output=r.stdout or "", entries=entries)

    async def git_log(
        self,
        workdir: str,
        host_alias: str,
        cwd: str,
        *,
        max_count: int = 20,
        timeout: int = 15,
    ) -> GitResult:
        """Run ``git log`` on the remote target.

        Returns log entries with ``hash``, ``author``, ``date``, ``message``.
        """
        if not await self._git_check(workdir, host_alias, cwd):
            return GitResult(git_available=False, error="not a git repository")

        fmt = "%H|%an|%aI|%s"
        r = await self._ssh.exec(
            workdir, host_alias,
            f"cd {_shell_quote(cwd)} && git log --format='{fmt}' -n {max_count}",
            timeout_sec=timeout,
        )
        if r.exit_code != 0:
            return GitResult(git_available=True, error=(r.stderr or "").strip())

        entries = []
        for line in (r.stdout or "").splitlines():
            parts = line.split("|", 3)
            if len(parts) < 4:
                continue
            entries.append({
                "hash": parts[0],
                "author": parts[1],
                "date": parts[2],
                "message": parts[3],
            })

        return GitResult(git_available=True, output=r.stdout or "", entries=entries)

    async def git_fetch(
        self,
        workdir: str,
        host_alias: str,
        cwd: str,
        *,
        remote: str = "origin",
        timeout: int = 60,
    ) -> GitResult:
        """Run ``git fetch --prune <remote>`` on the remote target."""
        if not await self._git_check(workdir, host_alias, cwd):
            return GitResult(git_available=False, error="not a git repository")

        r = await self._ssh.exec(
            workdir, host_alias,
            f"cd {_shell_quote(cwd)} && git fetch --prune {_shell_quote(remote)}",
            timeout_sec=timeout,
        )
        if r.exit_code != 0:
            return GitResult(git_available=True, error=(r.stderr or "").strip())
        return GitResult(git_available=True, output=(r.stdout or "") + (r.stderr or ""))

    async def git_pull(
        self,
        workdir: str,
        host_alias: str,
        cwd: str,
        *,
        strategy: str = "ff",
        remote: str = "origin",
        timeout: int = 60,
    ) -> GitResult:
        """Run ``git pull`` on the remote target.

        strategy:
          - "ff"     — ``--ff-only`` (default, safe)
          - "merge"  — regular merge pull
          - "rebase" — ``--rebase``
        """
        if not await self._git_check(workdir, host_alias, cwd):
            return GitResult(git_available=False, error="not a git repository")

        strategy_flags: dict[str, list[str]] = {
            "ff": ["--ff-only"],
            "merge": [],
            "rebase": ["--rebase"],
        }
        flags = strategy_flags.get(strategy, ["--ff-only"])
        flags_str = " ".join(flags)
        cmd = f"cd {_shell_quote(cwd)} && git pull {flags_str} {_shell_quote(remote)}".strip()
        r = await self._ssh.exec(workdir, host_alias, cmd, timeout_sec=timeout)
        if r.exit_code != 0:
            return GitResult(
                git_available=True,
                output=(r.stdout or "").strip(),
                error=(r.stderr or "").strip(),
            )
        return GitResult(git_available=True, output=(r.stdout or "") + (r.stderr or ""))

    async def git_push(
        self,
        workdir: str,
        host_alias: str,
        cwd: str,
        *,
        remote: str = "origin",
        timeout: int = 60,
    ) -> GitResult:
        """Run ``git push`` on the remote target, setting upstream when needed."""
        if not await self._git_check(workdir, host_alias, cwd):
            return GitResult(git_available=False, error="not a git repository")

        # Determine current branch and upstream
        rb = await self._ssh.exec(
            workdir, host_alias,
            f"cd {_shell_quote(cwd)} && git rev-parse --abbrev-ref HEAD",
            timeout_sec=10,
        )
        branch = (rb.stdout or "").strip() if rb.exit_code == 0 else ""
        ru = await self._ssh.exec(
            workdir, host_alias,
            f"cd {_shell_quote(cwd)} && "
            "git rev-parse --abbrev-ref --symbolic-full-name '@{u}' 2>/dev/null",
            timeout_sec=10,
        )
        has_upstream = ru.exit_code == 0 and (ru.stdout or "").strip()
        if branch and not has_upstream:
            cmd = (
                f"cd {_shell_quote(cwd)} && "
                f"git push -u {_shell_quote(remote)} {_shell_quote(branch)}"
            )
        else:
            cmd = f"cd {_shell_quote(cwd)} && git push {_shell_quote(remote)}"

        r = await self._ssh.exec(workdir, host_alias, cmd, timeout_sec=timeout)
        if r.exit_code != 0:
            return GitResult(
                git_available=True,
                output=(r.stdout or "").strip(),
                error=(r.stderr or "").strip(),
            )
        return GitResult(git_available=True, output=(r.stdout or "") + (r.stderr or ""))

    async def git_checkout(
        self,
        workdir: str,
        host_alias: str,
        cwd: str,
        branch: str,
        *,
        timeout: int = 30,
    ) -> GitResult:
        """Run ``git checkout <branch>`` on the remote target."""
        if not await self._git_check(workdir, host_alias, cwd):
            return GitResult(git_available=False, error="not a git repository")

        r = await self._ssh.exec(
            workdir, host_alias,
            f"cd {_shell_quote(cwd)} && git checkout {_shell_quote(branch)}",
            timeout_sec=timeout,
        )
        if r.exit_code != 0:
            return GitResult(git_available=True, error=(r.stderr or "").strip())
        return GitResult(git_available=True, output=(r.stdout or "") + (r.stderr or ""))

    async def git_merge(
        self,
        workdir: str,
        host_alias: str,
        cwd: str,
        branch: str,
        *,
        timeout: int = 60,
    ) -> GitResult:
        """Run ``git merge <branch>`` on the remote target."""
        if not await self._git_check(workdir, host_alias, cwd):
            return GitResult(git_available=False, error="not a git repository")

        r = await self._ssh.exec(
            workdir, host_alias,
            f"cd {_shell_quote(cwd)} && git merge {_shell_quote(branch)}",
            timeout_sec=timeout,
        )
        if r.exit_code != 0:
            return GitResult(
                git_available=True,
                output=(r.stdout or "").strip(),
                error=(r.stderr or "").strip(),
            )
        return GitResult(git_available=True, output=(r.stdout or "") + (r.stderr or ""))

    async def git_rebase(
        self,
        workdir: str,
        host_alias: str,
        cwd: str,
        branch: str,
        *,
        timeout: int = 60,
    ) -> GitResult:
        """Run ``git rebase <branch>`` on the remote target."""
        if not await self._git_check(workdir, host_alias, cwd):
            return GitResult(git_available=False, error="not a git repository")

        r = await self._ssh.exec(
            workdir, host_alias,
            f"cd {_shell_quote(cwd)} && git rebase {_shell_quote(branch)}",
            timeout_sec=timeout,
        )
        if r.exit_code != 0:
            return GitResult(
                git_available=True,
                output=(r.stdout or "").strip(),
                error=(r.stderr or "").strip(),
            )
        return GitResult(git_available=True, output=(r.stdout or "") + (r.stderr or ""))


def _shell_quote(s: str) -> str:
    """Quote a string for safe use in a shell command."""
    return "'" + s.replace("'", "'\\''") + "'"
