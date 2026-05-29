from __future__ import annotations

from typing import Any, Dict, Optional


def _explicit_attr(obj: Any, name: str) -> tuple[bool, Any]:
    try:
        attrs = vars(obj)
    except Exception:
        attrs = {}
    if isinstance(attrs, dict) and name in attrs:
        return True, attrs.get(name)
    return False, None


def _explicit_modes(session: Any) -> tuple[bool, Any]:
    return _explicit_attr(session, "modes")


def _explicit_orchestrator(session: Any) -> tuple[bool, Any]:
    return _explicit_attr(session, "orchestrator")


def get_active_mode(session: Any, default: Optional[str] = None) -> Optional[str]:
    has_modes, modes = _explicit_modes(session)
    if has_modes and modes is not None and hasattr(modes, "active_mode"):
        value = getattr(modes, "active_mode", None)
    else:
        value = getattr(session, "active_mode", None)
    return default if value is None else value


def set_active_mode(session: Any, value: Optional[str]) -> None:
    normalized = str(value).strip() if value is not None else None
    has_modes, modes = _explicit_modes(session)
    if has_modes and modes is not None and hasattr(modes, "active_mode"):
        modes.active_mode = normalized
        return
    setattr(session, "active_mode", normalized)


def reset_session_runtime_state(session: Any, *, default_mode_id: Optional[str] = None) -> None:
    if hasattr(session, "reset_all_resume_tokens"):
        try:
            session.reset_all_resume_tokens()
        except Exception:
            session.resume_token = None
    else:
        session.resume_token = None
    session.state_summary = None
    session.state_updated_at = None
    queue = getattr(session, "queue", None)
    if hasattr(queue, "clear"):
        queue.clear()
    set_active_mode(session, default_mode_id)
    session.manager_quiet_mode = False
    session.agent_memory = {}
    set_orchestrator_pending_input(session, None)
    set_orchestrator_last_mode_output(session, None)
    set_orchestrator_last_mode_id(session, None)
    session.project_root = None


def get_analyst_mode(session: Any, default: str = "spec") -> str:
    has_modes, modes = _explicit_modes(session)
    if has_modes and modes is not None and hasattr(modes, "analyst_mode"):
        value = getattr(modes, "analyst_mode", default)
    else:
        value = getattr(session, "analyst_mode", default)
    text = str(value or default).strip()
    return text or default


def set_analyst_mode(session: Any, value: Optional[str]) -> None:
    normalized = str(value or "spec").strip() or "spec"
    has_modes, modes = _explicit_modes(session)
    if has_modes and modes is not None and hasattr(modes, "analyst_mode"):
        modes.analyst_mode = normalized
        return
    setattr(session, "analyst_mode", normalized)


def is_ssh_remote_enabled(session: Any, default: bool = False) -> bool:
    has_modes, modes = _explicit_modes(session)
    if has_modes and modes is not None and hasattr(modes, "ssh_remote_enabled"):
        value = getattr(modes, "ssh_remote_enabled", default)
    else:
        value = getattr(session, "ssh_remote_enabled", default)
    return bool(value)


def set_ssh_remote_enabled(session: Any, value: bool) -> None:
    normalized = bool(value)
    has_modes, modes = _explicit_modes(session)
    if has_modes and modes is not None and hasattr(modes, "ssh_remote_enabled"):
        modes.ssh_remote_enabled = normalized
        return
    setattr(session, "ssh_remote_enabled", normalized)


def is_remote_control_enabled(session: Any, default: bool = False) -> bool:
    has_modes, modes = _explicit_modes(session)
    if has_modes and modes is not None and hasattr(modes, "remote_control_enabled"):
        value = getattr(modes, "remote_control_enabled", default)
    else:
        value = getattr(session, "remote_control_enabled", default)
    return bool(value)


def set_remote_control_enabled(session: Any, value: bool) -> None:
    normalized = bool(value)
    has_modes, modes = _explicit_modes(session)
    if has_modes and modes is not None and hasattr(modes, "remote_control_enabled"):
        modes.remote_control_enabled = normalized
        return
    setattr(session, "remote_control_enabled", normalized)


def get_remote_control_host_alias(session: Any, default: Optional[str] = None) -> Optional[str]:
    has_modes, modes = _explicit_modes(session)
    if has_modes and modes is not None and hasattr(modes, "remote_control_host_alias"):
        value = getattr(modes, "remote_control_host_alias", default)
    else:
        value = getattr(session, "remote_control_host_alias", default)
    return default if value is None else value


def set_remote_control_host_alias(session: Any, value: Optional[str]) -> None:
    normalized = str(value).strip() if value is not None else None
    normalized = normalized or None
    has_modes, modes = _explicit_modes(session)
    if has_modes and modes is not None and hasattr(modes, "remote_control_host_alias"):
        modes.remote_control_host_alias = normalized
        return
    setattr(session, "remote_control_host_alias", normalized)


def is_orchestrator_enabled(session: Any, default: bool = False) -> bool:
    has_orchestrator, orchestrator = _explicit_orchestrator(session)
    if has_orchestrator and orchestrator is not None and hasattr(orchestrator, "enabled"):
        value = getattr(orchestrator, "enabled", default)
    else:
        value = getattr(session, "advanced_orchestrator_enabled", default)
    return bool(value)


def set_orchestrator_enabled(session: Any, value: bool) -> None:
    normalized = bool(value)
    has_orchestrator, orchestrator = _explicit_orchestrator(session)
    if has_orchestrator and orchestrator is not None and hasattr(orchestrator, "enabled"):
        orchestrator.enabled = normalized
        return
    setattr(session, "advanced_orchestrator_enabled", normalized)


def get_orchestrator_pending_input(
    session: Any, default: Optional[Dict[str, Any]] = None
) -> Optional[Dict[str, Any]]:
    has_orchestrator, orchestrator = _explicit_orchestrator(session)
    if has_orchestrator and orchestrator is not None and hasattr(orchestrator, "pending_input"):
        value = getattr(orchestrator, "pending_input", default)
    else:
        value = getattr(session, "orchestrator_pending_input", default)
    return default if value is None else value


def set_orchestrator_pending_input(session: Any, value: Optional[Dict[str, Any]]) -> None:
    has_orchestrator, orchestrator = _explicit_orchestrator(session)
    if has_orchestrator and orchestrator is not None and hasattr(orchestrator, "pending_input"):
        orchestrator.pending_input = value
        return
    setattr(session, "orchestrator_pending_input", value)


def get_orchestrator_last_mode_output(session: Any, default: Optional[str] = None) -> Optional[str]:
    has_orchestrator, orchestrator = _explicit_orchestrator(session)
    if has_orchestrator and orchestrator is not None and hasattr(orchestrator, "last_mode_output"):
        value = getattr(orchestrator, "last_mode_output", default)
    else:
        value = getattr(session, "orchestrator_last_mode_output", default)
    return default if value is None else value


def set_orchestrator_last_mode_output(session: Any, value: Optional[str]) -> None:
    normalized = str(value) if value is not None else None
    has_orchestrator, orchestrator = _explicit_orchestrator(session)
    if has_orchestrator and orchestrator is not None and hasattr(orchestrator, "last_mode_output"):
        orchestrator.last_mode_output = normalized
        return
    setattr(session, "orchestrator_last_mode_output", normalized)


def get_orchestrator_last_mode_id(session: Any, default: Optional[str] = None) -> Optional[str]:
    has_orchestrator, orchestrator = _explicit_orchestrator(session)
    if has_orchestrator and orchestrator is not None and hasattr(orchestrator, "last_mode_id"):
        value = getattr(orchestrator, "last_mode_id", default)
    else:
        value = getattr(session, "orchestrator_last_mode_id", default)
    return default if value is None else value


def set_orchestrator_last_mode_id(session: Any, value: Optional[str]) -> None:
    normalized = str(value).strip() if value is not None else None
    has_orchestrator, orchestrator = _explicit_orchestrator(session)
    if has_orchestrator and orchestrator is not None and hasattr(orchestrator, "last_mode_id"):
        orchestrator.last_mode_id = normalized
        return
    setattr(session, "orchestrator_last_mode_id", normalized)
