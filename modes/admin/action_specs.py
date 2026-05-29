from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

from .transports import LocalCommandSpec, SSHCommandSpec


def resolve_exec_action_payload(
    *,
    config_payload: Dict[str, Any],
    target: str,
    action_id: str,
) -> Any:
    admin_cfg = config_payload.get("admin", {}) if isinstance(config_payload, dict) else {}
    if not isinstance(admin_cfg, dict):
        return None

    actions_cfg = admin_cfg.get("actions", {})
    if isinstance(actions_cfg, dict):
        actions = actions_cfg.get(str(target), {})
        if isinstance(actions, dict) and str(action_id) in actions:
            return actions.get(str(action_id))

    allowlist_cfg = admin_cfg.get("allowlist", {})
    if isinstance(allowlist_cfg, dict):
        allowlist = allowlist_cfg.get(str(target), {})
        if isinstance(allowlist, dict) and str(action_id) in allowlist:
            return allowlist.get(str(action_id))
    return None


def build_local_command_spec(
    *,
    session: Any,
    action_id: str,
    action_payload: Any,
) -> LocalCommandSpec:
    cwd_default = str(getattr(session, "workdir", "") or "").strip() or None
    argv: List[str]
    cwd = cwd_default
    timeout_sec = 30.0
    env: Optional[Dict[str, str]] = None

    if isinstance(action_payload, (list, tuple)):
        argv = [str(item) for item in action_payload if str(item).strip()]
    elif isinstance(action_payload, dict):
        raw_argv = action_payload.get("argv", [])
        if not isinstance(raw_argv, (list, tuple)):
            raise ValueError("local action `argv` must be a list")
        argv = [str(item) for item in raw_argv if str(item).strip()]
        raw_cwd = action_payload.get("cwd")
        if raw_cwd is not None:
            cwd = str(raw_cwd).strip() or cwd_default
        raw_timeout = action_payload.get("timeout_sec")
        if raw_timeout is not None:
            timeout_sec = float(raw_timeout)
        raw_env = action_payload.get("env")
        if raw_env is not None:
            if not isinstance(raw_env, dict):
                raise ValueError("local action `env` must be a mapping")
            env = {str(k): str(v) for k, v in raw_env.items()}
    else:
        raise ValueError("local action payload must be list or mapping")

    if not argv:
        raise ValueError("local action argv is empty")

    if cwd and cwd_default and not cwd.startswith("/"):
        cwd = os.path.abspath(os.path.join(cwd_default, cwd))

    if timeout_sec <= 0:
        raise ValueError("local action timeout_sec must be > 0")

    return LocalCommandSpec(
        action_id=str(action_id or "").strip(),
        argv=tuple(argv),
        cwd=cwd,
        timeout_sec=float(timeout_sec),
        env=env,
    )


def build_ssh_command_spec(
    *,
    session: Any,
    action_id: str,
    action_payload: Any,
) -> SSHCommandSpec:
    if not isinstance(action_payload, dict):
        raise ValueError("ssh action payload must be a mapping")

    host = str(action_payload.get("host") or "").strip()
    if not host:
        raise ValueError("ssh action `host` is required")

    raw_argv = action_payload.get("argv", [])
    if not isinstance(raw_argv, (list, tuple)):
        raise ValueError("ssh action `argv` must be a list")
    argv = [str(item) for item in raw_argv if str(item).strip()]
    if not argv:
        raise ValueError("ssh action argv is empty")

    key_path = str(action_payload.get("key_path") or "").strip()
    session_workdir = str(getattr(session, "workdir", "") or "").strip()
    password = str(action_payload.get("password") or "")
    password_env = str(action_payload.get("password_env") or "").strip()
    if not password and password_env and session_workdir:
        from app.services.ssh_config_loader import load_ssh_secrets, resolve_ssh_secret

        password = str(resolve_ssh_secret(load_ssh_secrets(session_workdir), password_env) or "")
    if not key_path and not password:
        raise ValueError("ssh action `key_path` or `password_env` is required")
    if key_path and session_workdir and not key_path.startswith("/"):
        key_path = os.path.abspath(os.path.join(session_workdir, key_path))

    user = str(action_payload.get("user") or "").strip() or None
    port = int(action_payload.get("port") or 22)
    if port <= 0:
        raise ValueError("ssh action `port` must be > 0")

    timeout_sec = float(action_payload.get("timeout_sec") or 30.0)
    if timeout_sec <= 0:
        raise ValueError("ssh action timeout_sec must be > 0")

    raw_options = action_payload.get("options") or []
    if not isinstance(raw_options, (list, tuple)):
        raise ValueError("ssh action `options` must be a list")
    options = tuple(str(item).strip() for item in raw_options if str(item).strip())

    return SSHCommandSpec(
        action_id=str(action_id or "").strip(),
        host=host,
        argv=tuple(argv),
        key_path=key_path,
        user=user,
        port=port,
        timeout_sec=timeout_sec,
        options=options,
        password=password or None,
    )


__all__ = [
    "resolve_exec_action_payload",
    "build_local_command_spec",
    "build_ssh_command_spec",
]
