"""Remote Control service: compute effective execution target from session state and SSH hosts."""

from __future__ import annotations

import enum
import logging
import posixpath
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

from config import SSHHostConfig
from app.services.logging_service import build_session_log_context

logger = logging.getLogger(__name__)

# Default preflight cache TTL in seconds.
PREFLIGHT_CACHE_TTL_SEC = 300.0


def _shell_quote(value: str) -> str:
    """Quote *value* for safe use in POSIX shell commands."""
    return "'" + str(value or "").replace("'", "'\\''") + "'"


class ExecutionTarget(enum.Enum):
    """Where CLI commands should be executed."""

    LOCAL = "local"
    REMOTE = "remote"


@dataclass
class EffectiveState:
    """Resolved execution context derived from session flags and SSH host config."""

    execution_target: ExecutionTarget
    host_alias: Optional[str] = None
    remote_project_root: Optional[str] = None
    git_available: bool = True


@dataclass
class PreflightResult:
    """Result of a remote-host preflight check."""

    ok: bool
    host_alias: Optional[str] = None
    remote_project_root: Optional[str] = None
    checked_at: Optional[float] = None
    error: Optional[str] = None


@dataclass
class TransitionRequest:
    """Request to transition execution target for a session."""

    enable: bool
    host_alias: Optional[str] = None


@dataclass
class ValidationResult:
    """Result of a transition validation check."""

    ok: bool
    error: Optional[str] = None


def build_remote_control_audit_extra(
    *,
    session: Any,
    actor: str,
    surface: str,
    action: str,
    host_alias: Optional[str],
    host_cfg: Optional[SSHHostConfig],
    result: str,
    reason: Optional[str] = None,
) -> Dict[str, Any]:
    """Build a normalized audit payload for Remote Control events.

    The payload is intentionally local-log friendly: it includes canonical
    session context for ``LogsService`` filtering and an explicit ``provider``
    marker to make it clear the audit trail remains local.
    """
    status = "ok" if str(result or "").strip().lower() == "ok" else "error"
    extra: Dict[str, Any] = build_session_log_context(session=session)
    extra.update({
        "actor": str(actor or ""),
        "surface": str(surface or ""),
        "provider": "local",
        "host_alias": str(host_alias or ""),
        "host": str(getattr(host_cfg, "host", "") or ""),
        "remote_project_root": str(getattr(host_cfg, "remote_project_root", "") or ""),
        "result": status,
        "reason": str(reason or ""),
        "error": str(reason or ""),
        "action": str(action or ""),
        "status": status,
        "path": str(getattr(host_cfg, "host", "") or host_alias or ""),
    })
    return extra


def build_remote_file_audit_extra(
    *,
    actor: Any,
    session_uid: str,
    surface: str,
    action: str,
    path: str,
    result: str,
    provider: str = "",
    host: str = "",
    remote_project_root: str = "",
    old_revision: Optional[str] = None,
    new_revision: Optional[str] = None,
    expected_revision: Optional[str] = None,
    current_revision: Optional[str] = None,
    reason: Optional[str] = None,
    chat_id: Any = None,
    user_id: Any = None,
) -> Dict[str, Any]:
    """Build a normalized audit payload for remote file conflict/force-save events."""
    status = str(result or "").strip().lower() or "error"
    extra: Dict[str, Any] = {}
    if chat_id is not None:
        extra["chat_id"] = chat_id
    if user_id is not None:
        extra["user_id"] = user_id
    extra.update({
        "actor": actor,
        "session_uid": str(session_uid or ""),
        "surface": str(surface or ""),
        "provider": str(provider or ""),
        "host": str(host or ""),
        "remote_project_root": str(remote_project_root or ""),
        "path": str(path or ""),
        "old_revision": str(old_revision or ""),
        "new_revision": str(new_revision or ""),
        "expected_revision": str(expected_revision or ""),
        "current_revision": str(current_revision or ""),
        "action": str(action or ""),
        "status": status,
        "result": status,
        "reason": str(reason or ""),
        "error": str(reason or ""),
    })
    return extra


class RemoteControlService:
    """Computes effective execution state from session mode-state and SSH host configuration."""

    def __init__(self, cache_ttl_sec: float = PREFLIGHT_CACHE_TTL_SEC) -> None:
        self._cache_ttl_sec = cache_ttl_sec
        # Cache key: (workdir, host_alias) → PreflightResult
        self._preflight_cache: Dict[Tuple[str, str], PreflightResult] = {}

    # ------------------------------------------------------------------
    # Settings normalization
    # ------------------------------------------------------------------

    def normalize_setting_change(
        self,
        session: Any,
        key: str,
        value: Any,
        ssh_hosts: Dict[str, SSHHostConfig],
        workdir: Optional[str] = None,
    ) -> EffectiveState:
        """Normalize dependent toggles after a setting change and return new effective state.

        Handles cascading rules:
        * ``ssh_remote_enabled`` set to False → forces ``remote_control_enabled`` to False.
        * ``remote_control_host_alias`` changed → invalidates preflight cache for old alias.
        * ``remote_control_enabled`` changed → recomputes effective execution target.

        Always returns the current :class:`EffectiveState` after normalization.
        """
        from sessions.session_state_access import (
            get_remote_control_host_alias,
            is_remote_control_enabled,
            is_ssh_remote_enabled,
            set_remote_control_enabled,
            set_remote_control_host_alias,
        )

        if key == "ssh_remote_enabled" and not bool(value):
            if is_remote_control_enabled(session):
                set_remote_control_enabled(session, False)
                logger.info("Disabled remote_control because ssh_remote_enabled was turned off")

        elif key == "remote_control_host_alias":
            old_alias = get_remote_control_host_alias(session)
            new_alias = str(value).strip() if value else None
            new_alias = new_alias or None
            set_remote_control_host_alias(session, new_alias)
            if old_alias and old_alias != new_alias and workdir:
                self.invalidate_preflight(workdir, old_alias)

        elif key == "remote_control_enabled":
            enabled = bool(value)
            if enabled and not is_ssh_remote_enabled(session):
                enabled = False
                logger.info("Cannot enable remote_control without ssh_remote_enabled")
            set_remote_control_enabled(session, enabled)

        return self.compute_effective_state(session, ssh_hosts)

    # ------------------------------------------------------------------
    # Transition validation
    # ------------------------------------------------------------------

    def validate_transition(
        self,
        session: Any,
        request: TransitionRequest,
        ssh_hosts: Dict[str, SSHHostConfig],
        *,
        chat_id: Optional[int] = None,
        is_admin: bool = False,
    ) -> ValidationResult:
        """Validate whether a remote-control transition is allowed.

        Checks (in order):
        1. **Idle-only (REQ-12)**: session must not be busy, run-locked, or tick-active.
        2. **ACL (REQ-11)**: if enabling with a host_alias, the host's
           ``allowed_chat_ids`` must include *chat_id* (unless *is_admin*).
        """
        # 1. Idle-only guard
        idle_check = self.validate_idle(session)
        if not idle_check.ok:
            return idle_check

        # 2. Enabling prerequisites + ACL
        from sessions.session_state_access import is_ssh_remote_enabled

        if request.enable and not is_ssh_remote_enabled(session):
            return ValidationResult(
                ok=False,
                error="Cannot enable remote control while SSH Remote is disabled",
            )

        if request.enable and not str(request.host_alias or "").strip():
            return ValidationResult(
                ok=False,
                error="remote_control_host_alias is required when enabling remote control",
            )

        # 3. ACL check (only when enabling with a specific host)
        if request.enable and request.host_alias:
            host_cfg = ssh_hosts.get(request.host_alias)
            if host_cfg is None:
                return ValidationResult(
                    ok=False,
                    error=f"Host '{request.host_alias}' not found in SSH hosts",
                )
            remote_root = str(getattr(host_cfg, "remote_project_root", "") or "").strip()
            if not remote_root:
                return ValidationResult(
                    ok=False,
                    error=f"Host '{request.host_alias}' is not eligible for remote control: remote_project_root is not configured",
                )
            if not posixpath.isabs(remote_root):
                return ValidationResult(
                    ok=False,
                    error=f"Host '{request.host_alias}' has invalid remote_project_root: must be an absolute path",
                )
            if not is_admin:
                acl = host_cfg.allowed_chat_ids
                if acl is not None and chat_id not in acl:
                    return ValidationResult(
                        ok=False,
                        error=f"Chat {chat_id} is not allowed to use host '{request.host_alias}'",
                    )

        return ValidationResult(ok=True)

    def validate_idle(self, session: Any) -> ValidationResult:
        """Validate the session is idle for Remote Control settings changes."""
        busy = getattr(session, "busy", False)
        run_lock = getattr(session, "run_lock", None)
        locked = run_lock.locked() if run_lock is not None else False
        tick_active = (
            session.is_active_by_tick()
            if hasattr(session, "is_active_by_tick")
            else False
        )
        if busy or locked or tick_active:
            return ValidationResult(
                ok=False,
                error="Cannot change remote control settings while session is busy",
            )
        return ValidationResult(ok=True)

    async def validate_and_preflight(
        self,
        session: Any,
        request: TransitionRequest,
        ssh_hosts: Dict[str, SSHHostConfig],
        ssh_service: Any,
        workdir: str,
        *,
        chat_id: Optional[int] = None,
        is_admin: bool = False,
    ) -> Tuple[ValidationResult, Optional[PreflightResult]]:
        """Validate transition, then run preflight only if validation passed and enabling.

        Returns a (validation, preflight) tuple.  If validation fails, preflight is None.
        If disabling, preflight is skipped (None).
        """
        vr = self.validate_transition(
            session, request, ssh_hosts, chat_id=chat_id, is_admin=is_admin,
        )
        if not vr.ok:
            return vr, None

        if not request.enable or not request.host_alias:
            return vr, None

        host_cfg = ssh_hosts.get(request.host_alias)
        if host_cfg is None:
            return ValidationResult(ok=False, error=f"Host '{request.host_alias}' not found"), None

        pf = await self.run_preflight(ssh_service, workdir, request.host_alias, host_cfg)
        return vr, pf

    # ------------------------------------------------------------------
    # Preflight
    # ------------------------------------------------------------------

    async def run_preflight(
        self,
        ssh_service: Any,
        workdir: str,
        host_alias: str,
        host_cfg: SSHHostConfig,
    ) -> PreflightResult:
        """Run preflight checks against *host_alias* and cache the result.

        Steps:
        1. SSH connectivity — execute ``echo ok`` to verify host reachability and shell.
        2. If *host_cfg.remote_project_root* is set, verify the directory exists
           and is readable + writable via ``test -d … -a -r … -a -w …``.
        """
        remote_root = host_cfg.remote_project_root
        now = time.time()

        cached = self.get_cached_preflight(workdir, host_alias)
        if cached is not None and cached.remote_project_root == remote_root:
            return cached

        if not remote_root:
            pf = PreflightResult(
                ok=False,
                host_alias=host_alias,
                remote_project_root=None,
                checked_at=now,
                error=f"Host '{host_alias}' is not eligible for remote control: remote_project_root is not configured",
            )
            self._preflight_cache[(workdir, host_alias)] = pf
            return pf

        if not posixpath.isabs(remote_root):
            pf = PreflightResult(
                ok=False,
                host_alias=host_alias,
                remote_project_root=remote_root,
                checked_at=now,
                error=f"remote_project_root '{remote_root}' must be an absolute path",
            )
            self._preflight_cache[(workdir, host_alias)] = pf
            return pf

        # 1. SSH connectivity + remote shell
        try:
            result = await ssh_service.exec(
                workdir, host_alias, "echo ok", timeout_sec=15,
            )
        except Exception as exc:
            pf = PreflightResult(
                ok=False,
                host_alias=host_alias,
                remote_project_root=remote_root,
                checked_at=now,
                error=f"SSH connection failed: {exc}",
            )
            self._preflight_cache[(workdir, host_alias)] = pf
            return pf

        if result.exit_code != 0 or (result.stdout or "").strip() != "ok":
            pf = PreflightResult(
                ok=False,
                host_alias=host_alias,
                remote_project_root=remote_root,
                checked_at=now,
                error=f"Remote shell check failed (exit={result.exit_code}): {result.stderr}",
            )
            self._preflight_cache[(workdir, host_alias)] = pf
            return pf

        # 2. remote_project_root checks (if configured)
        try:
            quoted_root = _shell_quote(remote_root)
            dir_result = await ssh_service.exec(
                workdir,
                host_alias,
                (
                    f"test -d {quoted_root} -a -r {quoted_root} -a -w {quoted_root} "
                    "&& echo ok"
                ),
                timeout_sec=10,
            )
        except Exception as exc:
            pf = PreflightResult(
                ok=False,
                host_alias=host_alias,
                remote_project_root=remote_root,
                checked_at=now,
                error=f"Directory check command failed: {exc}",
            )
            self._preflight_cache[(workdir, host_alias)] = pf
            return pf

        if dir_result.exit_code != 0 or (dir_result.stdout or "").strip() != "ok":
            pf = PreflightResult(
                ok=False,
                host_alias=host_alias,
                remote_project_root=remote_root,
                checked_at=now,
                error=f"remote_project_root '{remote_root}' is not accessible (not a directory, or not readable/writable)",
            )
            self._preflight_cache[(workdir, host_alias)] = pf
            return pf

        # 3. Remote file operations within remote_project_root.
        preflight_path = posixpath.join(
            remote_root,
            f".cli-proxy-preflight-{int(now * 1000)}",
        )
        quoted_preflight_path = _shell_quote(preflight_path)
        try:
            file_result = await ssh_service.exec(
                workdir,
                host_alias,
                (
                    f"printf ok > {quoted_preflight_path} "
                    f"&& test -f {quoted_preflight_path} -a -r {quoted_preflight_path} -a -w {quoted_preflight_path} "
                    f"&& cat {quoted_preflight_path} "
                    f"&& rm -f {quoted_preflight_path}"
                ),
                timeout_sec=10,
            )
        except Exception as exc:
            pf = PreflightResult(
                ok=False,
                host_alias=host_alias,
                remote_project_root=remote_root,
                checked_at=now,
                error=f"Remote file operations check failed: {exc}",
            )
            self._preflight_cache[(workdir, host_alias)] = pf
            return pf

        if file_result.exit_code != 0 or "ok" not in str(file_result.stdout or ""):
            pf = PreflightResult(
                ok=False,
                host_alias=host_alias,
                remote_project_root=remote_root,
                checked_at=now,
                error="remote file operations are not available for this target",
            )
            self._preflight_cache[(workdir, host_alias)] = pf
            return pf

        pf = PreflightResult(
            ok=True,
            host_alias=host_alias,
            remote_project_root=remote_root,
            checked_at=now,
        )
        self._preflight_cache[(workdir, host_alias)] = pf
        return pf

    def get_cached_preflight(
        self, workdir: str, host_alias: str,
    ) -> Optional[PreflightResult]:
        """Return cached preflight if still valid (within TTL), else None."""
        key = (workdir, host_alias)
        cached = self._preflight_cache.get(key)
        if cached is None or cached.checked_at is None:
            return None
        age = time.time() - cached.checked_at
        if age > self._cache_ttl_sec:
            self._preflight_cache.pop(key, None)
            return None
        return cached

    def invalidate_preflight(
        self, workdir: Optional[str] = None, host_alias: Optional[str] = None,
    ) -> None:
        """Drop cached preflight entries.

        * Both *workdir* and *host_alias* → drop that specific entry.
        * Only *workdir* → drop all entries for that workdir.
        * Neither → drop everything.
        """
        if workdir and host_alias:
            self._preflight_cache.pop((workdir, host_alias), None)
        elif workdir:
            keys = [k for k in self._preflight_cache if k[0] == workdir]
            for k in keys:
                del self._preflight_cache[k]
        else:
            self._preflight_cache.clear()

    # ------------------------------------------------------------------
    # compute_effective_state
    # ------------------------------------------------------------------

    def compute_effective_state(
        self,
        session_state: Any,
        ssh_hosts: Dict[str, SSHHostConfig],
    ) -> EffectiveState:
        """Derive the effective execution target from session mode-state and available hosts.

        ``session_state`` is expected to expose ``remote_control_enabled`` (bool)
        and ``remote_control_host_alias`` (Optional[str]) — either directly or via
        a ``modes`` sub-object (ModeState).

        Rules:
        * If remote_control_enabled is falsy → LOCAL.
        * If remote_control_host_alias is not set or not found in *ssh_hosts* → LOCAL.
        * Otherwise → REMOTE with resolved host_alias and remote_project_root.
        """
        modes = getattr(session_state, "modes", None)
        source = modes if modes is not None else session_state

        enabled = bool(getattr(source, "remote_control_enabled", False))
        alias: Optional[str] = getattr(source, "remote_control_host_alias", None)

        if not enabled or not alias:
            return EffectiveState(execution_target=ExecutionTarget.LOCAL)

        host_cfg = ssh_hosts.get(alias)
        if host_cfg is None:
            logger.warning(
                "Remote control alias '%s' not found in ssh_hosts, falling back to LOCAL",
                alias,
            )
            return EffectiveState(execution_target=ExecutionTarget.LOCAL)

        return EffectiveState(
            execution_target=ExecutionTarget.REMOTE,
            host_alias=alias,
            remote_project_root=host_cfg.remote_project_root,
            git_available=host_cfg.remote_project_root is not None,
        )
