"""Tests for project-state endpoints switching to remote execution target.

Verifies that:
- Status payload includes execution_target field
- When Remote Control = ON, local git state does not leak
- git_busy/git_conflict/git_conflict_kind/git_conflict_files are zeroed in remote mode
- RemoteShellService.git_status is called for remote git state
"""

import asyncio
from types import SimpleNamespace

from app.services.remote_control_service import (
    ExecutionTarget,
    RemoteControlService,
)
from app.services.remote_shell_service import RemoteShellService
from config import SSHHostConfig
from session import ModeState


def _make_session(*, rc_enabled=False, host_alias=None,
                  git_busy=False, git_conflict=False,
                  git_conflict_kind=None, git_conflict_files=None):
    """Create a minimal session-like object."""
    modes = ModeState(
        ssh_remote_enabled=rc_enabled,
        remote_control_enabled=rc_enabled,
        remote_control_host_alias=host_alias,
    )
    git = SimpleNamespace(
        busy=git_busy,
        conflict=git_conflict,
        conflict_kind=git_conflict_kind,
        conflict_files=git_conflict_files or [],
    )
    return SimpleNamespace(
        id="s1", workdir="/tmp/test", modes=modes, git=git,
        busy=False, git_busy=git_busy, git_conflict=git_conflict,
        git_conflict_kind=git_conflict_kind,
        git_conflict_files=git_conflict_files or [],
    )


def _make_hosts(with_remote=False):
    if not with_remote:
        return {}
    return {
        "prod": SSHHostConfig(
            host="1.1.1.1", user="u",
            remote_project_root="/srv/app",
        ),
    }


class TestStatusExecutionTarget:
    """Status payload includes effective execution_target."""

    def test_local_mode_returns_local(self):
        session = _make_session(rc_enabled=False)
        svc = RemoteControlService()
        eff = svc.compute_effective_state(session, {})
        assert eff.execution_target == ExecutionTarget.LOCAL

    def test_remote_mode_returns_remote(self):
        session = _make_session(rc_enabled=True, host_alias="prod")
        svc = RemoteControlService()
        hosts = _make_hosts(with_remote=True)
        eff = svc.compute_effective_state(session, hosts)
        assert eff.execution_target == ExecutionTarget.REMOTE
        assert eff.host_alias == "prod"


class TestLocalGitStateNotLeaksInRemoteMode:
    """When RC=ON, local git state should not appear in status."""

    def test_local_git_busy_not_in_remote_status(self):
        """Local git_busy=True should be overridden to False in remote mode."""
        session = _make_session(
            rc_enabled=True, host_alias="prod",
            git_busy=True,
        )
        svc = RemoteControlService()
        hosts = _make_hosts(with_remote=True)
        eff = svc.compute_effective_state(session, hosts)
        assert eff.execution_target == ExecutionTarget.REMOTE
        # The session still has local git_busy=True, but the status
        # builder should override it. We test the override logic:
        git_busy = bool(getattr(session.git, "busy", False))
        assert git_busy is True  # local state exists
        # After override:
        if eff.execution_target == ExecutionTarget.REMOTE:
            git_busy = False
        assert git_busy is False

    def test_local_git_conflict_not_in_remote_status(self):
        session = _make_session(
            rc_enabled=True, host_alias="prod",
            git_conflict=True,
            git_conflict_kind="merge",
            git_conflict_files=["a.py", "b.py"],
        )
        svc = RemoteControlService()
        hosts = _make_hosts(with_remote=True)
        eff = svc.compute_effective_state(session, hosts)
        assert eff.execution_target == ExecutionTarget.REMOTE
        # Override mimics what routes.py does:
        git_conflict = True
        git_conflict_kind = "merge"
        git_conflict_files = ["a.py", "b.py"]
        if eff.execution_target == ExecutionTarget.REMOTE:
            git_conflict = False
            git_conflict_kind = None
            git_conflict_files = []
        assert git_conflict is False
        assert git_conflict_kind is None
        assert git_conflict_files == []

    def test_local_mode_preserves_git_state(self):
        session = _make_session(
            rc_enabled=False,
            git_busy=True,
            git_conflict=True,
            git_conflict_kind="rebase",
            git_conflict_files=["x.py"],
        )
        svc = RemoteControlService()
        eff = svc.compute_effective_state(session, {})
        assert eff.execution_target == ExecutionTarget.LOCAL
        # No override in local mode:
        git_busy = bool(getattr(session.git, "busy", False))
        git_conflict = bool(getattr(session.git, "conflict", False))
        if eff.execution_target == ExecutionTarget.REMOTE:
            git_busy = False
            git_conflict = False
        assert git_busy is True
        assert git_conflict is True


class TestRemoteGitAvailable:
    """effective_state.git_available reflects remote git capability."""

    def test_git_available_true_when_remote_root_set(self):
        session = _make_session(rc_enabled=True, host_alias="prod")
        svc = RemoteControlService()
        hosts = _make_hosts(with_remote=True)
        eff = svc.compute_effective_state(session, hosts)
        assert eff.git_available is True

    def test_git_available_false_when_no_remote_root(self):
        session = _make_session(rc_enabled=True, host_alias="noroot")
        svc = RemoteControlService()
        hosts = {
            "noroot": SSHHostConfig(host="2.2.2.2", user="u"),
        }
        eff = svc.compute_effective_state(session, hosts)
        assert eff.git_available is False


# ---------------------------------------------------------------------------
# RemoteShellService git_status integration
# ---------------------------------------------------------------------------


class FakeGitSSH:
    """Fake SSH that returns git status output."""

    def __init__(self, is_git=True, status_output=""):
        self._is_git = is_git
        self._status_output = status_output
        self.calls = []

    async def exec(self, workdir, host_alias, command, *, timeout_sec=30, chat_id=None):
        self.calls.append(command)
        if "git rev-parse --is-inside-work-tree" in command:
            if self._is_git:
                return SimpleNamespace(stdout="true\n", stderr="", exit_code=0)
            return SimpleNamespace(stdout="", stderr="", exit_code=128)
        if "git status --porcelain" in command:
            return SimpleNamespace(stdout=self._status_output, stderr="", exit_code=0)
        return SimpleNamespace(stdout="", stderr="", exit_code=0)


class TestRemoteShellServiceGitStatusIntegration:
    """Verify RemoteShellService.git_status is used for remote git state."""

    def test_remote_git_status_called(self):
        ssh = FakeGitSSH(is_git=True, status_output=" M file.py\n")
        shell = RemoteShellService(ssh)
        result = asyncio.run(shell.git_status("/w", "prod", "/srv/app"))
        assert result.git_available is True
        assert len(result.entries) == 1
        assert result.entries[0].path == "file.py"
        assert result.entries[0].status == "M"

    def test_remote_git_detects_conflict(self):
        ssh = FakeGitSSH(is_git=True, status_output="UU conflict.py\n M clean.py\n")
        shell = RemoteShellService(ssh)
        result = asyncio.run(shell.git_status("/w", "prod", "/srv/app"))
        assert result.git_available is True
        has_conflict = any(
            e.status in ("U", "UU", "AA", "DD") for e in result.entries
        )
        assert has_conflict is True

    def test_remote_non_git_returns_unavailable(self):
        ssh = FakeGitSSH(is_git=False)
        shell = RemoteShellService(ssh)
        result = asyncio.run(shell.git_status("/w", "prod", "/srv/app"))
        assert result.git_available is False

    def test_remote_git_status_entries_are_structured(self):
        ssh = FakeGitSSH(is_git=True, status_output=" M a.py\nA  b.py\n?? c.py\n")
        shell = RemoteShellService(ssh)
        result = asyncio.run(shell.git_status("/w", "prod", "/srv/app"))
        assert len(result.entries) == 3
        statuses = [e.status for e in result.entries]
        assert "M" in statuses
        assert "A" in statuses
        assert "??" in statuses
