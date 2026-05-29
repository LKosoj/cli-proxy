"""Generate a Markdown SSH skill description for CLI agents from project config."""

from __future__ import annotations

import os
from typing import Any, Optional

from app.services.ssh_config_loader import load_ssh_config
from sessions.session_state_access import is_ssh_remote_enabled


def generate_ssh_skill_text(
    workdir: str,
    session: Any = None,
) -> Optional[str]:
    """Build a Markdown skill block describing available SSH hosts.

    Returns ``None`` when:
    * *session* has ``ssh_remote_enabled == False``
    * no hosts are configured in ``{workdir}/.cli-proxy/ssh.yaml``
    """
    if session is not None and not is_ssh_remote_enabled(session):
        return None
    hosts = load_ssh_config(workdir)
    if not hosts:
        return None

    sections: list[str] = [
        "# SSH Remote Access",
        "",
        "This project has remote servers configured. "
        "Use SSH to execute commands on them when the task requires it.",
        "",
        "## Available Servers",
    ]

    for alias, cfg in hosts.items():
        roles_str = ", ".join(cfg.roles) if cfg.roles else "no roles"
        header = f"### {alias} ({roles_str})"
        if cfg.description:
            header += f" — {cfg.description}"
        sections.append("")
        sections.append(header)

        if cfg.auth == "key" and cfg.key_file:
            key_path = cfg.key_file
            if not os.path.isabs(key_path):
                key_path = os.path.join(workdir, key_path)
            cmd = f"ssh -i {key_path} -p {cfg.port} {cfg.user}@{cfg.host} \"<command>\""
        elif cfg.auth == "password" and cfg.password_env:
            cmd = (
                f"sshpass -p \"${cfg.password_env}\" "
                f"ssh -p {cfg.port} {cfg.user}@{cfg.host} \"<command>\""
            )
        else:
            cmd = f"ssh -p {cfg.port} {cfg.user}@{cfg.host} \"<command>\""

        sections.append("```bash")
        sections.append(cmd)
        sections.append("```")

        if cfg.sudo and cfg.sudo_password_env:
            sudo_cmd = (
                f"echo \"${cfg.sudo_password_env}\" | "
                f"ssh -p {cfg.port} {cfg.user}@{cfg.host} \"sudo -S <command>\""
            )
            sections.append(f"For sudo: `{sudo_cmd}`")

    sections.append("")
    return "\n".join(sections)
