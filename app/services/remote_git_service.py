"""Remote git service: high-level git operations on SSH hosts.

Provides a session-aware facade over :class:`RemoteShellService` that resolves
host configuration and the project root directory automatically.
"""

from __future__ import annotations

import logging
from typing import Optional

from app.services.remote_shell_service import GitResult, RemoteShellService
from app.services.ssh_config_loader import load_ssh_config

logger = logging.getLogger(__name__)

_PULL_STRATEGIES = {"ff", "merge", "rebase"}


class RemoteGitError(Exception):
    """Raised when a remote git operation cannot be started."""


class RemoteGitService:
    """Perform git operations on a configured SSH host.

    The service resolves the host's *remote_project_root* from the project's
    ``ssh.yaml`` and delegates command execution to :class:`RemoteShellService`.

    All mutating operations (pull / push / checkout / merge / rebase / fetch)
    accept ``host_alias`` plus an optional ``cwd`` override.  When *cwd* is
    ``None`` the value of ``remote_project_root`` from the host config is used.
    If neither is available, :class:`RemoteGitError` is raised.
    """

    def __init__(self, remote_shell: RemoteShellService) -> None:
        self._shell = remote_shell

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _resolve_cwd(
        self,
        workdir: str,
        host_alias: str,
        cwd: Optional[str],
    ) -> str:
        """Return effective working directory, falling back to remote_project_root."""
        if cwd:
            return cwd
        hosts = load_ssh_config(workdir)
        cfg = hosts.get(host_alias)
        if cfg and cfg.remote_project_root:
            return cfg.remote_project_root
        raise RemoteGitError(
            f"No cwd provided and remote_project_root not set for host '{host_alias}'"
        )

    # ------------------------------------------------------------------
    # Read-only operations (already in RemoteShellService, exposed here
    # for a consistent API)
    # ------------------------------------------------------------------

    async def status(
        self,
        workdir: str,
        host_alias: str,
        cwd: Optional[str] = None,
    ) -> GitResult:
        """Return ``git status --porcelain`` on the remote host."""
        effective_cwd = self._resolve_cwd(workdir, host_alias, cwd)
        return await self._shell.git_status(workdir, host_alias, effective_cwd)

    async def log(
        self,
        workdir: str,
        host_alias: str,
        cwd: Optional[str] = None,
        *,
        max_count: int = 20,
    ) -> GitResult:
        """Return ``git log`` entries from the remote host."""
        effective_cwd = self._resolve_cwd(workdir, host_alias, cwd)
        return await self._shell.git_log(workdir, host_alias, effective_cwd, max_count=max_count)

    async def diff(
        self,
        workdir: str,
        host_alias: str,
        cwd: Optional[str] = None,
    ) -> GitResult:
        """Return ``git diff`` output from the remote host."""
        effective_cwd = self._resolve_cwd(workdir, host_alias, cwd)
        return await self._shell.git_diff(workdir, host_alias, effective_cwd)

    # ------------------------------------------------------------------
    # Mutating operations
    # ------------------------------------------------------------------

    async def fetch(
        self,
        workdir: str,
        host_alias: str,
        cwd: Optional[str] = None,
        *,
        remote: str = "origin",
    ) -> GitResult:
        """Run ``git fetch --prune`` on the remote host."""
        effective_cwd = self._resolve_cwd(workdir, host_alias, cwd)
        return await self._shell.git_fetch(workdir, host_alias, effective_cwd, remote=remote)

    async def pull(
        self,
        workdir: str,
        host_alias: str,
        cwd: Optional[str] = None,
        *,
        strategy: str = "ff",
        remote: str = "origin",
    ) -> GitResult:
        """Run ``git pull`` on the remote host.

        *strategy*: ``"ff"`` (--ff-only, default), ``"merge"``, or ``"rebase"``.
        """
        if strategy not in _PULL_STRATEGIES:
            raise RemoteGitError(
                f"Unknown pull strategy '{strategy}'. Allowed: {sorted(_PULL_STRATEGIES)}"
            )
        effective_cwd = self._resolve_cwd(workdir, host_alias, cwd)
        return await self._shell.git_pull(
            workdir, host_alias, effective_cwd, strategy=strategy, remote=remote,
        )

    async def push(
        self,
        workdir: str,
        host_alias: str,
        cwd: Optional[str] = None,
        *,
        remote: str = "origin",
    ) -> GitResult:
        """Run ``git push`` on the remote host."""
        effective_cwd = self._resolve_cwd(workdir, host_alias, cwd)
        return await self._shell.git_push(workdir, host_alias, effective_cwd, remote=remote)

    async def checkout(
        self,
        workdir: str,
        host_alias: str,
        branch: str,
        cwd: Optional[str] = None,
    ) -> GitResult:
        """Run ``git checkout <branch>`` on the remote host."""
        effective_cwd = self._resolve_cwd(workdir, host_alias, cwd)
        return await self._shell.git_checkout(workdir, host_alias, effective_cwd, branch)

    async def merge(
        self,
        workdir: str,
        host_alias: str,
        branch: str,
        cwd: Optional[str] = None,
    ) -> GitResult:
        """Run ``git merge <branch>`` on the remote host."""
        effective_cwd = self._resolve_cwd(workdir, host_alias, cwd)
        return await self._shell.git_merge(workdir, host_alias, effective_cwd, branch)

    async def rebase(
        self,
        workdir: str,
        host_alias: str,
        branch: str,
        cwd: Optional[str] = None,
    ) -> GitResult:
        """Run ``git rebase <branch>`` on the remote host."""
        effective_cwd = self._resolve_cwd(workdir, host_alias, cwd)
        return await self._shell.git_rebase(workdir, host_alias, effective_cwd, branch)
