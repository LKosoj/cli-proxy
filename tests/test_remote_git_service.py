"""Tests for RemoteGitService and RemoteShellService mutating git operations."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Tuple
from unittest.mock import MagicMock, patch

import pytest

from app.services.remote_git_service import RemoteGitError, RemoteGitService
from app.services.remote_shell_service import RemoteShellService


# ---------------------------------------------------------------------------
# Fake SSH infrastructure
# ---------------------------------------------------------------------------


class FakeSSH:
    """Minimal SSH fake that records calls and returns configured responses."""

    def __init__(
        self,
        is_git: bool = True,
        exit_code: int = 0,
        stdout: str = "",
        stderr: str = "",
    ) -> None:
        self._is_git = is_git
        self._exit_code = exit_code
        self._stdout = stdout
        self._stderr = stderr
        self.calls: List[Tuple[str, str, str, int]] = []

    async def exec(
        self, workdir: str, host_alias: str, command: str, *, timeout_sec: int = 30, **kwargs: Any
    ) -> SimpleNamespace:
        self.calls.append((workdir, host_alias, command, timeout_sec))
        if "rev-parse --is-inside-work-tree" in command:
            out = "true\n" if self._is_git else ""
            code = 0 if self._is_git else 128
            return SimpleNamespace(stdout=out, stderr="", exit_code=code)
        if "rev-parse --abbrev-ref HEAD" in command and "--symbolic-full-name" not in command:
            return SimpleNamespace(stdout="main\n", stderr="", exit_code=0)
        if "--symbolic-full-name" in command:
            # No upstream by default
            return SimpleNamespace(stdout="", stderr="", exit_code=1)
        return SimpleNamespace(stdout=self._stdout, stderr=self._stderr, exit_code=self._exit_code)


class FakeSSHError:
    """SSH fake that always raises an exception."""

    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    async def exec(self, *args: Any, **kwargs: Any) -> None:
        raise self._exc


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _hosts_config(alias: str = "prod", remote_root: Optional[str] = "/srv/app") -> Dict:
    """Return a fake ssh.yaml hosts dict."""
    cfg = MagicMock()
    cfg.remote_project_root = remote_root
    return {alias: cfg}


# ---------------------------------------------------------------------------
# RemoteShellService mutating git methods
# ---------------------------------------------------------------------------


class TestRemoteShellServiceMutating:
    """Tests for the new mutating git methods on RemoteShellService."""

    def test_api_contract(self) -> None:
        """All expected methods exist."""
        svc = RemoteShellService(FakeSSH())
        for name in ("git_fetch", "git_pull", "git_push", "git_checkout", "git_merge", "git_rebase"):
            assert hasattr(svc, name), f"RemoteShellService missing method: {name}"
            assert callable(getattr(svc, name))

    # -- git_fetch --

    def test_fetch_calls_git_fetch_prune(self) -> None:
        ssh = FakeSSH(is_git=True, stdout="", stderr="")
        svc = RemoteShellService(ssh)
        result = asyncio.run(svc.git_fetch("/w", "prod", "/srv/app"))
        assert result.git_available is True
        assert result.error is None
        cmds = [c for _, _, c, _ in ssh.calls if "git fetch" in c]
        assert len(cmds) == 1
        assert "--prune" in cmds[0]
        assert "origin" in cmds[0]

    def test_fetch_non_git_dir(self) -> None:
        ssh = FakeSSH(is_git=False)
        svc = RemoteShellService(ssh)
        result = asyncio.run(svc.git_fetch("/w", "prod", "/srv/app"))
        assert result.git_available is False
        assert "not a git repository" in (result.error or "")

    def test_fetch_ssh_error_propagates(self) -> None:
        result = asyncio.run(
            RemoteShellService(FakeSSH(is_git=True, exit_code=1, stderr="auth fail")).git_fetch(
                "/w", "prod", "/srv/app"
            )
        )
        assert result.error == "auth fail"

    def test_fetch_custom_remote(self) -> None:
        ssh = FakeSSH(is_git=True)
        svc = RemoteShellService(ssh)
        asyncio.run(svc.git_fetch("/w", "prod", "/srv/app", remote="upstream"))
        cmds = [c for _, _, c, _ in ssh.calls if "git fetch" in c]
        assert "upstream" in cmds[0]

    # -- git_pull --

    def test_pull_ff_only_by_default(self) -> None:
        ssh = FakeSSH(is_git=True, stdout="Already up to date.")
        svc = RemoteShellService(ssh)
        result = asyncio.run(svc.git_pull("/w", "prod", "/srv/app"))
        assert result.git_available is True
        cmds = [c for _, _, c, _ in ssh.calls if "git pull" in c]
        assert len(cmds) == 1
        assert "--ff-only" in cmds[0]

    def test_pull_rebase_strategy(self) -> None:
        ssh = FakeSSH(is_git=True, stdout="up to date")
        svc = RemoteShellService(ssh)
        asyncio.run(svc.git_pull("/w", "prod", "/srv/app", strategy="rebase"))
        cmds = [c for _, _, c, _ in ssh.calls if "git pull" in c]
        assert "--rebase" in cmds[0]

    def test_pull_merge_strategy(self) -> None:
        ssh = FakeSSH(is_git=True, stdout="done")
        svc = RemoteShellService(ssh)
        asyncio.run(svc.git_pull("/w", "prod", "/srv/app", strategy="merge"))
        cmds = [c for _, _, c, _ in ssh.calls if "git pull" in c]
        assert "--ff-only" not in cmds[0]
        assert "--rebase" not in cmds[0]

    def test_pull_non_zero_returns_error(self) -> None:
        ssh = FakeSSH(is_git=True, exit_code=1, stderr="conflict")
        svc = RemoteShellService(ssh)
        result = asyncio.run(svc.git_pull("/w", "prod", "/srv/app"))
        assert result.error == "conflict"

    # -- git_push --

    def test_push_calls_git_push(self) -> None:
        ssh = FakeSSH(is_git=True, stdout="Everything up-to-date")
        svc = RemoteShellService(ssh)
        result = asyncio.run(svc.git_push("/w", "prod", "/srv/app"))
        assert result.git_available is True
        cmds = [c for _, _, c, _ in ssh.calls if "git push" in c]
        assert len(cmds) == 1

    def test_push_sets_upstream_when_none(self) -> None:
        """Without upstream, push -u <remote> <branch> should be used."""
        ssh = FakeSSH(is_git=True, stdout="branch created")
        svc = RemoteShellService(ssh)
        result = asyncio.run(svc.git_push("/w", "prod", "/srv/app"))
        assert result.git_available is True
        push_cmds = [c for _, _, c, _ in ssh.calls if "git push" in c]
        assert len(push_cmds) == 1
        # Should include -u for setting upstream (since FakeSSH returns exit_code=1 for upstream)
        assert "-u" in push_cmds[0]
        assert "origin" in push_cmds[0]
        assert "main" in push_cmds[0]

    def test_push_error_captured(self) -> None:
        ssh = FakeSSH(is_git=True, exit_code=1, stderr="rejected")
        svc = RemoteShellService(ssh)
        result = asyncio.run(svc.git_push("/w", "prod", "/srv/app"))
        assert result.error == "rejected"

    # -- git_checkout --

    def test_checkout_passes_branch_name(self) -> None:
        ssh = FakeSSH(is_git=True, stdout="Switched to branch 'feat'")
        svc = RemoteShellService(ssh)
        result = asyncio.run(svc.git_checkout("/w", "prod", "/srv/app", "feat"))
        assert result.git_available is True
        cmds = [c for _, _, c, _ in ssh.calls if "git checkout" in c]
        assert len(cmds) == 1
        assert "feat" in cmds[0]

    def test_checkout_branch_name_is_quoted(self) -> None:
        """Branch names with spaces are single-quoted."""
        ssh = FakeSSH(is_git=True)
        svc = RemoteShellService(ssh)
        asyncio.run(svc.git_checkout("/w", "prod", "/srv/app", "feature/x"))
        cmds = [c for _, _, c, _ in ssh.calls if "git checkout" in c]
        assert "'feature/x'" in cmds[0]

    def test_checkout_non_git_dir(self) -> None:
        ssh = FakeSSH(is_git=False)
        svc = RemoteShellService(ssh)
        result = asyncio.run(svc.git_checkout("/w", "prod", "/srv/app", "main"))
        assert result.git_available is False

    # -- git_merge --

    def test_merge_calls_git_merge(self) -> None:
        ssh = FakeSSH(is_git=True, stdout="Merge made by recursive")
        svc = RemoteShellService(ssh)
        result = asyncio.run(svc.git_merge("/w", "prod", "/srv/app", "origin/feat"))
        assert result.git_available is True
        cmds = [c for _, _, c, _ in ssh.calls if "git merge" in c]
        assert len(cmds) == 1
        assert "origin/feat" in cmds[0]

    def test_merge_conflict_captured_in_error(self) -> None:
        ssh = FakeSSH(is_git=True, exit_code=1, stderr="CONFLICT")
        svc = RemoteShellService(ssh)
        result = asyncio.run(svc.git_merge("/w", "prod", "/srv/app", "feat"))
        assert result.error == "CONFLICT"

    # -- git_rebase --

    def test_rebase_calls_git_rebase(self) -> None:
        ssh = FakeSSH(is_git=True, stdout="Successfully rebased")
        svc = RemoteShellService(ssh)
        result = asyncio.run(svc.git_rebase("/w", "prod", "/srv/app", "main"))
        assert result.git_available is True
        cmds = [c for _, _, c, _ in ssh.calls if "git rebase" in c]
        assert len(cmds) == 1
        assert "main" in cmds[0]

    def test_rebase_error_propagates(self) -> None:
        ssh = FakeSSH(is_git=True, exit_code=1, stderr="conflict during rebase")
        svc = RemoteShellService(ssh)
        result = asyncio.run(svc.git_rebase("/w", "prod", "/srv/app", "main"))
        assert result.error == "conflict during rebase"


# ---------------------------------------------------------------------------
# RemoteGitService
# ---------------------------------------------------------------------------


def _make_service(ssh: FakeSSH, hosts: Optional[Dict] = None) -> RemoteGitService:
    """Build a RemoteGitService with mocked SSH config."""
    shell = RemoteShellService(ssh)
    svc = RemoteGitService(shell)
    if hosts is None:
        hosts = _hosts_config()
    with patch("app.services.remote_git_service.load_ssh_config", return_value=hosts):
        pass  # patch applied per-call in tests below
    return svc


class TestRemoteGitServiceResolution:
    """Test cwd resolution logic in RemoteGitService."""

    def test_resolves_remote_project_root_when_no_cwd(self) -> None:
        ssh = FakeSSH(is_git=True, stdout="Already up to date.")
        svc = RemoteGitService(RemoteShellService(ssh))
        hosts = _hosts_config("prod", "/srv/app")
        with patch("app.services.remote_git_service.load_ssh_config", return_value=hosts):
            result = asyncio.run(svc.fetch("/tmp/w", "prod"))
        assert result.git_available is True
        fetch_cmds = [c for _, _, c, _ in ssh.calls if "git fetch" in c]
        assert len(fetch_cmds) == 1
        assert "/srv/app" in fetch_cmds[0]

    def test_explicit_cwd_overrides_remote_root(self) -> None:
        ssh = FakeSSH(is_git=True)
        svc = RemoteGitService(RemoteShellService(ssh))
        # Should NOT call load_ssh_config at all when cwd is provided
        asyncio.run(svc.fetch("/tmp/w", "prod", "/custom/cwd"))
        fetch_cmds = [c for _, _, c, _ in ssh.calls if "git fetch" in c]
        assert "/custom/cwd" in fetch_cmds[0]

    def test_raises_when_no_root_and_no_cwd(self) -> None:
        ssh = FakeSSH(is_git=True)
        svc = RemoteGitService(RemoteShellService(ssh))
        hosts = _hosts_config("prod", None)  # no remote_project_root
        with patch("app.services.remote_git_service.load_ssh_config", return_value=hosts):
            with pytest.raises(RemoteGitError, match="remote_project_root"):
                asyncio.run(svc.fetch("/tmp/w", "prod"))

    def test_raises_on_unknown_host(self) -> None:
        ssh = FakeSSH(is_git=True)
        svc = RemoteGitService(RemoteShellService(ssh))
        with patch("app.services.remote_git_service.load_ssh_config", return_value={}):
            with pytest.raises(RemoteGitError, match="unknown_host"):
                asyncio.run(svc.pull("/tmp/w", "unknown_host"))


class TestRemoteGitServicePull:
    """Tests for RemoteGitService.pull."""

    def test_pull_ff_default(self) -> None:
        ssh = FakeSSH(is_git=True, stdout="up to date")
        svc = RemoteGitService(RemoteShellService(ssh))
        with patch("app.services.remote_git_service.load_ssh_config", return_value=_hosts_config()):
            result = asyncio.run(svc.pull("/tmp/w", "prod"))
        assert result.git_available is True
        cmds = [c for _, _, c, _ in ssh.calls if "git pull" in c]
        assert "--ff-only" in cmds[0]

    def test_pull_invalid_strategy_raises(self) -> None:
        ssh = FakeSSH(is_git=True)
        svc = RemoteGitService(RemoteShellService(ssh))
        with patch("app.services.remote_git_service.load_ssh_config", return_value=_hosts_config()):
            with pytest.raises(RemoteGitError, match="Unknown pull strategy"):
                asyncio.run(svc.pull("/tmp/w", "prod", strategy="bad"))

    def test_pull_rebase_forwarded(self) -> None:
        ssh = FakeSSH(is_git=True, stdout="rebased")
        svc = RemoteGitService(RemoteShellService(ssh))
        with patch("app.services.remote_git_service.load_ssh_config", return_value=_hosts_config()):
            result = asyncio.run(svc.pull("/tmp/w", "prod", strategy="rebase"))
        assert result.git_available is True
        cmds = [c for _, _, c, _ in ssh.calls if "git pull" in c]
        assert "--rebase" in cmds[0]


class TestRemoteGitServicePush:
    """Tests for RemoteGitService.push."""

    def test_push_delegates_to_shell(self) -> None:
        ssh = FakeSSH(is_git=True, stdout="Everything up-to-date")
        svc = RemoteGitService(RemoteShellService(ssh))
        with patch("app.services.remote_git_service.load_ssh_config", return_value=_hosts_config()):
            result = asyncio.run(svc.push("/tmp/w", "prod"))
        assert result.git_available is True

    def test_push_custom_cwd(self) -> None:
        ssh = FakeSSH(is_git=True)
        svc = RemoteGitService(RemoteShellService(ssh))
        asyncio.run(svc.push("/tmp/w", "prod", "/override/cwd"))
        cmds = [c for _, _, c, _ in ssh.calls if "git push" in c]
        assert "/override/cwd" in cmds[0]


class TestRemoteGitServiceFetch:
    """Tests for RemoteGitService.fetch."""

    def test_fetch_delegates_to_shell(self) -> None:
        ssh = FakeSSH(is_git=True)
        svc = RemoteGitService(RemoteShellService(ssh))
        with patch("app.services.remote_git_service.load_ssh_config", return_value=_hosts_config()):
            result = asyncio.run(svc.fetch("/tmp/w", "prod"))
        assert result.git_available is True


class TestRemoteGitServiceCheckout:
    """Tests for RemoteGitService.checkout."""

    def test_checkout_delegates_to_shell(self) -> None:
        ssh = FakeSSH(is_git=True, stdout="Switched to branch 'dev'")
        svc = RemoteGitService(RemoteShellService(ssh))
        with patch("app.services.remote_git_service.load_ssh_config", return_value=_hosts_config()):
            result = asyncio.run(svc.checkout("/tmp/w", "prod", "dev"))
        assert result.git_available is True
        cmds = [c for _, _, c, _ in ssh.calls if "git checkout" in c]
        assert "dev" in cmds[0]


class TestRemoteGitServiceMergeRebase:
    """Tests for RemoteGitService.merge and .rebase."""

    def test_merge_delegates(self) -> None:
        ssh = FakeSSH(is_git=True, stdout="merged")
        svc = RemoteGitService(RemoteShellService(ssh))
        with patch("app.services.remote_git_service.load_ssh_config", return_value=_hosts_config()):
            result = asyncio.run(svc.merge("/tmp/w", "prod", "origin/feat"))
        assert result.git_available is True

    def test_rebase_delegates(self) -> None:
        ssh = FakeSSH(is_git=True, stdout="rebased")
        svc = RemoteGitService(RemoteShellService(ssh))
        with patch("app.services.remote_git_service.load_ssh_config", return_value=_hosts_config()):
            result = asyncio.run(svc.rebase("/tmp/w", "prod", "main"))
        assert result.git_available is True


class TestRemoteGitServiceReadOnly:
    """Smoke tests for read-only operations exposed through RemoteGitService."""

    def test_status_delegates(self) -> None:
        ssh = FakeSSH(is_git=True, stdout=" M file.py\n")
        svc = RemoteGitService(RemoteShellService(ssh))
        with patch("app.services.remote_git_service.load_ssh_config", return_value=_hosts_config()):
            result = asyncio.run(svc.status("/tmp/w", "prod"))
        assert result.git_available is True
        assert len(result.entries) == 1

    def test_log_delegates(self) -> None:
        log_line = "abc123|Author|2024-01-01|commit msg"
        ssh = FakeSSH(is_git=True, stdout=log_line)
        svc = RemoteGitService(RemoteShellService(ssh))
        with patch("app.services.remote_git_service.load_ssh_config", return_value=_hosts_config()):
            result = asyncio.run(svc.log("/tmp/w", "prod"))
        assert result.git_available is True

    def test_diff_delegates(self) -> None:
        ssh = FakeSSH(is_git=True, stdout="diff --git a/f b/f\n")
        svc = RemoteGitService(RemoteShellService(ssh))
        with patch("app.services.remote_git_service.load_ssh_config", return_value=_hosts_config()):
            result = asyncio.run(svc.diff("/tmp/w", "prod"))
        assert result.git_available is True
