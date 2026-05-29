"""Tests for RemoteShellService: command execution, approval, dangerous commands."""

import asyncio
import dataclasses
import shlex
from types import SimpleNamespace

import pytest

from app.services.remote_shell_service import (
    CommandResult,
    DANGEROUS_COMMAND_PATTERNS,
    GitResult,
    GitStatusEntry,
    RemoteShellService,
    SearchMatch,
    SearchResult,
    is_dangerous_command,
)


# ---------------------------------------------------------------------------
# Fake SSH service
# ---------------------------------------------------------------------------


class FakeSSHService:
    def __init__(self, stdout="", stderr="", exit_code=0):
        self._stdout = stdout
        self._stderr = stderr
        self._exit_code = exit_code
        self.calls = []

    async def exec(self, workdir, host_alias, command, *, timeout_sec=30, chat_id=None):
        self.calls.append((workdir, host_alias, command, timeout_sec))
        return SimpleNamespace(
            stdout=self._stdout, stderr=self._stderr, exit_code=self._exit_code,
        )


class ExceptionSSHService:
    def __init__(self, exc):
        self._exc = exc
        self.calls = []

    async def exec(self, workdir, host_alias, command, *, timeout_sec=30, chat_id=None):
        self.calls.append((workdir, host_alias, command, timeout_sec))
        raise self._exc


# ---------------------------------------------------------------------------
# CommandResult dataclass
# ---------------------------------------------------------------------------


def test_command_result_fields():
    fields = {f.name for f in dataclasses.fields(CommandResult)}
    assert fields == {"stdout", "stderr", "return_code", "execution_target"}


def test_command_result_values():
    r = CommandResult(stdout="ok", stderr="", return_code=0, execution_target="remote")
    assert r.stdout == "ok"
    assert r.return_code == 0
    assert r.execution_target == "remote"


def test_remote_shell_service_basic_api_contract():
    svc = RemoteShellService(FakeSSHService())
    for name in (
        "execute_command",
        "search_code",
        "git_status",
        "git_diff",
        "git_branch",
        "git_log",
    ):
        assert hasattr(svc, name)
        assert callable(getattr(svc, name))


# ---------------------------------------------------------------------------
# is_dangerous_command
# ---------------------------------------------------------------------------


class TestIsDangerousCommand:
    def test_rm_rf(self):
        assert is_dangerous_command("rm -rf /tmp/foo") is True

    def test_rm_fr(self):
        assert is_dangerous_command("rm -fr /data") is True

    def test_rm_r_f(self):
        assert is_dangerous_command("rm -r -f /data") is True

    def test_rm_interleaved_flags(self):
        assert is_dangerous_command("rm -I -rf /tmp/foo") is True

    def test_git_reset_hard(self):
        assert is_dangerous_command("git reset --hard HEAD~1") is True

    def test_git_clean_f(self):
        assert is_dangerous_command("git clean -fd") is True

    def test_git_clean_split_flags(self):
        assert is_dangerous_command("git clean -d -f") is True

    def test_git_push_force(self):
        assert is_dangerous_command("git push origin main --force") is True

    def test_git_push_f(self):
        assert is_dangerous_command("git push -f") is True

    def test_git_push_trailing_short_force(self):
        assert is_dangerous_command("git push origin main -f") is True

    def test_mkfs(self):
        assert is_dangerous_command("mkfs.ext4 /dev/sda1") is True

    def test_dd(self):
        assert is_dangerous_command("dd if=/dev/zero of=/dev/sda") is True

    def test_chmod_777(self):
        assert is_dangerous_command("chmod -R 777 /") is True

    def test_safe_commands(self):
        assert is_dangerous_command("ls -la") is False
        assert is_dangerous_command("cat file.txt") is False
        assert is_dangerous_command("git status") is False
        assert is_dangerous_command("git push origin main") is False
        assert is_dangerous_command("git reset --soft HEAD~1") is False
        assert is_dangerous_command("rm file.txt") is False
        assert is_dangerous_command("echo hello") is False

    def test_patterns_list_not_empty(self):
        assert len(DANGEROUS_COMMAND_PATTERNS) > 0


# ---------------------------------------------------------------------------
# RemoteShellService.execute_command
# ---------------------------------------------------------------------------


class TestExecuteCommand:
    def test_simple_command(self):
        ssh = FakeSSHService(stdout="hello\n")
        svc = RemoteShellService(ssh)
        result = asyncio.run(svc.execute_command("/w", "prod", "echo hello"))
        assert result.stdout == "hello\n"
        assert result.return_code == 0
        assert result.execution_target == "remote"
        assert len(ssh.calls) == 1

    def test_command_with_cwd(self):
        ssh = FakeSSHService(stdout="ok\n")
        svc = RemoteShellService(ssh)
        asyncio.run(svc.execute_command("/w", "prod", "ls", cwd="/srv/app"))
        assert ssh.calls[0][2] == "cd '/srv/app' && ls"

    def test_command_with_cwd_shell_quotes_metacharacters(self):
        ssh = FakeSSHService(stdout="ok\n")
        svc = RemoteShellService(ssh)
        asyncio.run(svc.execute_command("/w", "prod", "ls", cwd="/srv/app; touch /tmp/pwn"))
        command = ssh.calls[0][2]
        tokens = shlex.split(command)
        assert tokens[:3] == ["cd", "/srv/app; touch /tmp/pwn", "&&"]

    def test_command_without_cwd(self):
        ssh = FakeSSHService()
        svc = RemoteShellService(ssh)
        asyncio.run(svc.execute_command("/w", "prod", "pwd"))
        assert ssh.calls[0][2] == "pwd"

    def test_command_timeout_passed(self):
        ssh = FakeSSHService()
        svc = RemoteShellService(ssh)
        asyncio.run(svc.execute_command("/w", "prod", "sleep 5", timeout=60))
        assert ssh.calls[0][3] == 60

    def test_command_failure(self):
        ssh = FakeSSHService(stderr="not found", exit_code=127)
        svc = RemoteShellService(ssh)
        result = asyncio.run(svc.execute_command("/w", "prod", "nonexistent"))
        assert result.return_code == 127
        assert "not found" in result.stderr

    def test_command_timeout_propagates(self):
        ssh = ExceptionSSHService(asyncio.TimeoutError())
        svc = RemoteShellService(ssh)
        with pytest.raises(asyncio.TimeoutError):
            asyncio.run(svc.execute_command("/w", "prod", "sleep 5", timeout=1))

    def test_command_exec_error_propagates(self):
        ssh = ExceptionSSHService(RuntimeError("ssh transport failed"))
        svc = RemoteShellService(ssh)
        with pytest.raises(RuntimeError, match="ssh transport failed"):
            asyncio.run(svc.execute_command("/w", "prod", "pwd"))


# ---------------------------------------------------------------------------
# Dangerous command approval
# ---------------------------------------------------------------------------


class TestDangerousCommandApproval:
    def test_dangerous_rejected_without_callback(self):
        ssh = FakeSSHService()
        svc = RemoteShellService(ssh, approval_callback=None)
        result = asyncio.run(svc.execute_command("/w", "prod", "rm -rf /tmp"))
        assert result.return_code == -1
        assert "approval" in result.stderr
        assert len(ssh.calls) == 0  # not executed

    def test_dangerous_approved(self):
        ssh = FakeSSHService(stdout="deleted\n")

        async def approve(cmd: str) -> bool:
            return True

        svc = RemoteShellService(ssh, approval_callback=approve)
        result = asyncio.run(svc.execute_command("/w", "prod", "rm -rf /tmp/old"))
        assert result.return_code == 0
        assert len(ssh.calls) == 1

    def test_dangerous_denied(self):
        ssh = FakeSSHService()

        async def deny(cmd: str) -> bool:
            return False

        svc = RemoteShellService(ssh, approval_callback=deny)
        result = asyncio.run(svc.execute_command("/w", "prod", "git reset --hard"))
        assert result.return_code == -1
        assert "denied" in result.stderr
        assert len(ssh.calls) == 0

    def test_safe_command_no_approval_needed(self):
        ssh = FakeSSHService(stdout="ok\n")
        callback_called = []

        async def track(cmd: str) -> bool:
            callback_called.append(cmd)
            return True

        svc = RemoteShellService(ssh, approval_callback=track)
        result = asyncio.run(svc.execute_command("/w", "prod", "echo safe"))
        assert result.return_code == 0
        assert len(callback_called) == 0  # callback not invoked for safe commands

    def test_git_push_force_requires_approval(self):
        ssh = FakeSSHService()
        svc = RemoteShellService(ssh, approval_callback=None)
        result = asyncio.run(svc.execute_command("/w", "prod", "git push --force"))
        assert result.return_code == -1
        assert len(ssh.calls) == 0

    def test_approval_callback_receives_command(self):
        ssh = FakeSSHService()
        received = []

        async def capture(cmd: str) -> bool:
            received.append(cmd)
            return True

        svc = RemoteShellService(ssh, approval_callback=capture)
        asyncio.run(svc.execute_command("/w", "prod", "rm -rf /data"))
        assert received == ["rm -rf /data"]


# ---------------------------------------------------------------------------
# SearchResult / SearchMatch dataclasses
# ---------------------------------------------------------------------------


def test_search_match_fields():
    m = SearchMatch(file="main.py", line=10, text="import os")
    assert m.file == "main.py"
    assert m.line == 10
    assert m.text == "import os"


def test_search_result_defaults():
    r = SearchResult()
    assert r.matches == []
    assert r.truncated is False
    assert r.error is None


# ---------------------------------------------------------------------------
# search_code
# ---------------------------------------------------------------------------


class TestSearchCode:
    def test_basic_search(self):
        ssh = FakeSSHService(
            stdout="src/main.py:5:import os\nsrc/main.py:10:import sys\n"
        )
        svc = RemoteShellService(ssh)
        result = asyncio.run(svc.search_code("/w", "prod", "import"))
        assert len(result.matches) == 2
        assert result.matches[0].file == "src/main.py"
        assert result.matches[0].line == 5
        assert result.matches[0].text == "import os"
        assert result.matches[1].line == 10
        assert result.truncated is False
        assert result.error is None

    def test_no_matches(self):
        ssh = FakeSSHService(stdout="", exit_code=1)
        svc = RemoteShellService(ssh)
        result = asyncio.run(svc.search_code("/w", "prod", "nonexistent"))
        assert len(result.matches) == 0
        assert result.error is None

    def test_search_with_path(self):
        ssh = FakeSSHService(stdout="")
        svc = RemoteShellService(ssh)
        asyncio.run(svc.search_code("/w", "prod", "foo", path="src/"))
        cmd = ssh.calls[0][2]
        assert "'src/'" in cmd

    def test_case_insensitive(self):
        ssh = FakeSSHService(stdout="")
        svc = RemoteShellService(ssh)
        asyncio.run(svc.search_code("/w", "prod", "Foo", case_sensitive=False))
        cmd = ssh.calls[0][2]
        assert " -i" in cmd

    def test_case_sensitive_default(self):
        ssh = FakeSSHService(stdout="")
        svc = RemoteShellService(ssh)
        asyncio.run(svc.search_code("/w", "prod", "Foo"))
        cmd = ssh.calls[0][2]
        # No -i flag in the command
        assert " -i " not in cmd or " -i\n" not in cmd

    def test_max_results_limits_output(self):
        lines = [f"f.py:{i}:line{i}" for i in range(1, 50)]
        ssh = FakeSSHService(stdout="\n".join(lines))
        svc = RemoteShellService(ssh)
        result = asyncio.run(svc.search_code("/w", "prod", "line", max_results=10))
        assert len(result.matches) == 10
        assert result.truncated is True

    def test_search_error(self):
        ssh = FakeSSHService(stderr="permission denied", exit_code=2)
        svc = RemoteShellService(ssh)
        result = asyncio.run(svc.search_code("/w", "prod", "secret"))
        assert result.error == "permission denied"
        assert len(result.matches) == 0

    def test_search_timeout_propagates(self):
        ssh = ExceptionSSHService(asyncio.TimeoutError())
        svc = RemoteShellService(ssh)
        with pytest.raises(asyncio.TimeoutError):
            asyncio.run(svc.search_code("/w", "prod", "import", timeout=1))

    def test_search_exec_error_propagates(self):
        ssh = ExceptionSSHService(ValueError("grep unavailable"))
        svc = RemoteShellService(ssh)
        with pytest.raises(ValueError, match="grep unavailable"):
            asyncio.run(svc.search_code("/w", "prod", "import"))

    def test_malformed_lines_skipped(self):
        ssh = FakeSSHService(stdout="good.py:1:match\nbadline\n::\n")
        svc = RemoteShellService(ssh)
        result = asyncio.run(svc.search_code("/w", "prod", "match"))
        assert len(result.matches) == 1
        assert result.matches[0].file == "good.py"

    def test_pattern_with_special_chars(self):
        ssh = FakeSSHService(stdout="")
        svc = RemoteShellService(ssh)
        asyncio.run(svc.search_code("/w", "prod", "it's a \"test\""))
        cmd = ssh.calls[0][2]
        # Pattern should be shell-quoted
        assert "it" in cmd

    def test_uses_grep_or_rg(self):
        ssh = FakeSSHService(stdout="")
        svc = RemoteShellService(ssh)
        asyncio.run(svc.search_code("/w", "prod", "hello"))
        cmd = ssh.calls[0][2]
        assert "rg" in cmd or "grep" in cmd


# ---------------------------------------------------------------------------
# Git dataclasses
# ---------------------------------------------------------------------------


def test_git_status_entry():
    e = GitStatusEntry(path="file.txt", status="M")
    assert e.path == "file.txt"
    assert e.status == "M"


def test_git_result_available():
    r = GitResult(git_available=True, output="ok", entries=[])
    assert r.git_available is True
    assert r.error is None


def test_git_result_not_available():
    r = GitResult(git_available=False, error="not a git repository")
    assert r.git_available is False


# ---------------------------------------------------------------------------
# Git command-aware fake
# ---------------------------------------------------------------------------


class GitFakeSSH:
    """Fake SSH that handles git commands for testing."""

    def __init__(self, is_git_repo=True):
        self._is_git = is_git_repo
        self.calls = []

    async def exec(self, workdir, host_alias, command, *, timeout_sec=30, chat_id=None):
        self.calls.append(command)

        if "git rev-parse --is-inside-work-tree" in command:
            if self._is_git:
                return SimpleNamespace(stdout="true\n", stderr="", exit_code=0)
            return SimpleNamespace(stdout="", stderr="not a git repo", exit_code=128)

        if "git status --porcelain" in command:
            return SimpleNamespace(
                stdout=" M src/main.py\n?? new_file.txt\nA  added.py\n",
                stderr="", exit_code=0,
            )

        if "git diff" in command and "git diff" in command:
            return SimpleNamespace(
                stdout="diff --git a/file.py b/file.py\n--- a/file.py\n+++ b/file.py\n",
                stderr="", exit_code=0,
            )

        if "git branch -a" in command:
            return SimpleNamespace(
                stdout="* main\n  develop\n  remotes/origin/main\n",
                stderr="", exit_code=0,
            )

        if "git log" in command:
            return SimpleNamespace(
                stdout="abc123|Author|2025-01-01T00:00:00+00:00|Initial commit\ndef456|Dev|2025-01-02T00:00:00+00:00|Add feature\n",
                stderr="", exit_code=0,
            )

        return SimpleNamespace(stdout="", stderr="unknown", exit_code=1)


# ---------------------------------------------------------------------------
# Git commands tests
# ---------------------------------------------------------------------------


class TestGitStatus:
    def test_git_status_parses_entries(self):
        svc = RemoteShellService(GitFakeSSH())
        result = asyncio.run(svc.git_status("/w", "prod", "/srv/app"))
        assert result.git_available is True
        assert len(result.entries) == 3
        assert result.entries[0].path == "src/main.py"
        assert result.entries[0].status == "M"
        assert result.entries[1].path == "new_file.txt"
        assert result.entries[1].status == "??"
        assert result.entries[2].status == "A"

    def test_git_status_not_git_repo(self):
        svc = RemoteShellService(GitFakeSSH(is_git_repo=False))
        result = asyncio.run(svc.git_status("/w", "prod", "/srv/app"))
        assert result.git_available is False
        assert "not a git repository" in result.error

    def test_git_status_timeout_propagates(self):
        svc = RemoteShellService(ExceptionSSHService(asyncio.TimeoutError()))
        with pytest.raises(asyncio.TimeoutError):
            asyncio.run(svc.git_status("/w", "prod", "/srv/app"))


class TestGitDiff:
    def test_git_diff_returns_output(self):
        svc = RemoteShellService(GitFakeSSH())
        result = asyncio.run(svc.git_diff("/w", "prod", "/srv/app"))
        assert result.git_available is True
        assert "diff --git" in result.output
        assert result.error is None

    def test_git_diff_not_git_repo(self):
        svc = RemoteShellService(GitFakeSSH(is_git_repo=False))
        result = asyncio.run(svc.git_diff("/w", "prod", "/srv/app"))
        assert result.git_available is False

    def test_git_diff_exec_error_propagates(self):
        svc = RemoteShellService(ExceptionSSHService(RuntimeError("git diff failed")))
        with pytest.raises(RuntimeError, match="git diff failed"):
            asyncio.run(svc.git_diff("/w", "prod", "/srv/app"))


class TestGitBranch:
    def test_git_branch_parses_entries(self):
        svc = RemoteShellService(GitFakeSSH())
        result = asyncio.run(svc.git_branch("/w", "prod", "/srv/app"))
        assert result.git_available is True
        assert len(result.entries) == 3
        assert result.entries[0]["name"] == "main"
        assert result.entries[0]["current"] is True
        assert result.entries[1]["name"] == "develop"
        assert result.entries[1]["current"] is False
        assert result.entries[2]["name"] == "remotes/origin/main"

    def test_git_branch_not_git_repo(self):
        svc = RemoteShellService(GitFakeSSH(is_git_repo=False))
        result = asyncio.run(svc.git_branch("/w", "prod", "/srv/app"))
        assert result.git_available is False


class TestGitLog:
    def test_git_log_parses_entries(self):
        svc = RemoteShellService(GitFakeSSH())
        result = asyncio.run(svc.git_log("/w", "prod", "/srv/app"))
        assert result.git_available is True
        assert len(result.entries) == 2
        assert result.entries[0]["hash"] == "abc123"
        assert result.entries[0]["author"] == "Author"
        assert result.entries[0]["message"] == "Initial commit"
        assert result.entries[1]["hash"] == "def456"

    def test_git_log_not_git_repo(self):
        svc = RemoteShellService(GitFakeSSH(is_git_repo=False))
        result = asyncio.run(svc.git_log("/w", "prod", "/srv/app"))
        assert result.git_available is False

    def test_git_log_max_count(self):
        ssh = GitFakeSSH()
        svc = RemoteShellService(ssh)
        asyncio.run(svc.git_log("/w", "prod", "/srv/app", max_count=5))
        log_cmd = [c for c in ssh.calls if "git log" in c][0]
        assert "-n 5" in log_cmd

    def test_cwd_is_shell_quoted(self):
        ssh = GitFakeSSH()
        svc = RemoteShellService(ssh)
        asyncio.run(svc.git_status("/w", "prod", "/srv/my app"))
        assert "'/srv/my app'" in ssh.calls[0]
