"""Read and write SSH host configuration and secrets for a project's .cli-proxy/ directory."""

from __future__ import annotations

import dataclasses
import logging
import os
import re
import stat
from typing import Dict, Optional

import yaml

from app.services.dotenv_loader import parse_dotenv
from config import SSHHostConfig

logger = logging.getLogger(__name__)

SSH_CONFIG_DIR = ".cli-proxy"
SSH_CONFIG_FILE = "ssh.yaml"
SSH_ENV_FILE = "ssh.env"
SSH_CONFIG_TEMPLATE = """# SSH hosts configuration for this project.
# Add hosts via MiniApp SSH settings or edit this file manually.
# Example:
# hosts:
#   prod:
#     host: 203.0.113.10
#     user: deploy
#     auth: key
#     port: 22
#     key_file: ~/.ssh/id_ed25519
hosts: {}
"""
_SSH_SECRET_ALIAS_RE = re.compile(r"[^A-Za-z0-9]+")


def _ssh_config_path(workdir: str) -> str:
    return os.path.join(workdir, SSH_CONFIG_DIR, SSH_CONFIG_FILE)


def _ssh_env_path(workdir: str) -> str:
    return os.path.join(workdir, SSH_CONFIG_DIR, SSH_ENV_FILE)


def ssh_config_exists(workdir: str) -> bool:
    return os.path.isfile(_ssh_config_path(workdir))


def ensure_ssh_config_template(workdir: str) -> bool:
    path = _ssh_config_path(workdir)
    if os.path.isfile(path):
        return True
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(SSH_CONFIG_TEMPLATE)
        return True
    except Exception:
        logger.exception("Failed to create SSH config template at %s", path)
        return False


def load_ssh_config(workdir: str) -> Dict[str, SSHHostConfig]:
    """Load SSH host definitions from ``{workdir}/.cli-proxy/ssh.yaml``.

    Returns an empty dict when the file is missing or unparseable.
    """
    path = _ssh_config_path(workdir)
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = yaml.safe_load(fh)
    except Exception:
        logger.exception("Failed to read SSH config from %s", path)
        return {}
    if not isinstance(raw, dict):
        return {}
    hosts_raw = raw.get("hosts")
    if not isinstance(hosts_raw, dict):
        return {}

    result: Dict[str, SSHHostConfig] = {}
    for alias, fields in hosts_raw.items():
        alias = str(alias).strip()
        if not alias or not isinstance(fields, dict):
            continue
        host = str(fields.get("host") or "").strip()
        user = str(fields.get("user") or "").strip()
        if not host or not user:
            logger.warning(
                "SSH host '%s' skipped: host and user are required", alias,
            )
            continue
        auth = str(fields.get("auth") or "key").strip()
        if auth not in ("key", "password"):
            logger.warning(
                "SSH host '%s': unknown auth '%s', defaulting to 'key'",
                alias, auth,
            )
            auth = "key"
        port = int(fields.get("port") or 22)
        if not (1 <= port <= 65535):
            logger.warning(
                "SSH host '%s': port %d out of range, defaulting to 22",
                alias, port,
            )
            port = 22

        allowed_raw = fields.get("allowed_chat_ids")
        allowed: Optional[list] = None
        if isinstance(allowed_raw, list):
            allowed = [int(x) for x in allowed_raw]

        roles_raw = fields.get("roles")
        roles = list(roles_raw) if isinstance(roles_raw, list) else []

        remote_project_root = fields.get("remote_project_root") or None
        if remote_project_root is not None:
            remote_project_root = str(remote_project_root).strip() or None
        if remote_project_root and not remote_project_root.startswith("/"):
            logger.warning(
                "SSH host '%s': remote_project_root '%s' is not absolute, ignoring",
                alias, remote_project_root,
            )
            remote_project_root = None

        result[alias] = SSHHostConfig(
            host=host,
            user=user,
            auth=auth,
            port=port,
            key_file=fields.get("key_file"),
            key_passphrase_env=fields.get("key_passphrase_env"),
            password_env=fields.get("password_env"),
            sudo=bool(fields.get("sudo", False)),
            sudo_password_env=fields.get("sudo_password_env"),
            idle_timeout_sec=int(fields.get("idle_timeout_sec") or 1200),
            allowed_chat_ids=allowed,
            roles=[str(r) for r in roles],
            description=str(fields.get("description") or ""),
            remote_project_root=remote_project_root,
        )
    return result


def ssh_remote_available(workdir: str) -> bool:
    """Check if SSH hosts are configured for the project."""
    return bool(load_ssh_config(workdir))


def load_ssh_secrets(workdir: str) -> Dict[str, str]:
    """Read secrets from ``{workdir}/.cli-proxy/ssh.env`` via parse_dotenv."""
    path = _ssh_env_path(workdir)
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            content = fh.read()
        return parse_dotenv(content)
    except Exception:
        logger.exception("Failed to read SSH secrets from %s", path)
        return {}


def resolve_ssh_secret(secrets: Dict[str, str], env_name: Optional[str]) -> Optional[str]:
    """Resolve secret value from provided secrets dict or os.environ."""
    if not env_name:
        return None
    # 1. Check provided secrets (from ssh.env)
    if env_name in secrets:
        return secrets[env_name]
    # 2. Check system environment
    return os.environ.get(env_name)


def build_ssh_secret_env_name(alias: str, *, sudo: bool = False) -> str:
    """Build a stable env name for project-scoped SSH secrets."""
    normalized = _SSH_SECRET_ALIAS_RE.sub("_", str(alias or "").strip()).strip("_").upper()
    if not normalized:
        normalized = "HOST"
    suffix = "SUDO_PASSWORD" if sudo else "PASSWORD"
    return f"SSH_{normalized}_{suffix}"


def save_ssh_config(workdir: str, hosts: Dict[str, SSHHostConfig]) -> None:
    """Save hosts dictionary to ``{workdir}/.cli-proxy/ssh.yaml``."""
    path = _ssh_config_path(workdir)
    os.makedirs(os.path.dirname(path), exist_ok=True)

    data = {"hosts": {}}
    for alias, cfg in hosts.items():
        data["hosts"][alias] = dataclasses.asdict(cfg)

    try:
        with open(path, "w", encoding="utf-8") as fh:
            yaml.safe_dump(data, fh, allow_unicode=True, sort_keys=False)
    except Exception:
        logger.exception("Failed to save SSH config to %s", path)


def save_ssh_secret(workdir: str, key: str, value: str) -> None:
    """Add or update a single variable in ``{workdir}/.cli-proxy/ssh.env``."""
    path = _ssh_env_path(workdir)
    os.makedirs(os.path.dirname(path), exist_ok=True)

    secrets = load_ssh_secrets(workdir)
    secrets[key] = value

    lines = []
    for k, v in secrets.items():
        lines.append(f"{k}={v}")

    try:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
        # Set 0o600 permissions
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    except Exception:
        logger.exception("Failed to save SSH secret %s to %s", key, path)


def delete_ssh_secret(workdir: str, key: str) -> None:
    """Remove a variable from ``ssh.env``."""
    path = _ssh_env_path(workdir)
    if not os.path.isfile(path):
        return

    secrets = load_ssh_secrets(workdir)
    if key in secrets:
        del secrets[key]
        lines = [f"{k}={v}" for k, v in secrets.items()]
        try:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("\n".join(lines) + "\n")
        except Exception:
            logger.exception("Failed to delete SSH secret %s from %s", key, path)
