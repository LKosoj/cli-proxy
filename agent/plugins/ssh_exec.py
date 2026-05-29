"""SSH tool plugins for the Agent mode tool registry."""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from agent.plugins.base import ToolPlugin
from app.services.ssh_config_loader import load_ssh_config
from modes.sdk.runtime.tooling.spec import ToolSpec
from sessions.session_state_access import is_ssh_remote_enabled

logger = logging.getLogger(__name__)


def _build_hosts_description(workdir: Optional[str]) -> str:
    if not workdir:
        return "Available hosts are defined in project SSH config."
    hosts = load_ssh_config(workdir)
    if not hosts:
        return "No SSH hosts configured for this project."
    parts = []
    for alias, cfg in hosts.items():
        roles_str = ", ".join(cfg.roles) if cfg.roles else "no roles"
        parts.append(f"'{alias}' ({roles_str}) — {cfg.description or cfg.host}")
    return "Available hosts: " + "; ".join(parts) + "."


def _check_ssh_enabled(ctx: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Return an error dict if SSH remote is disabled for the session."""
    session = ctx.get("session")
    if session is not None and not is_ssh_remote_enabled(session):
        return {"success": False, "error": "SSH remote is disabled for this session"}
    return None


def _get_ssh_service(services: Optional[Dict[str, Any]]) -> Any:
    return (services or {}).get("ssh")


class SSHExecTool(ToolPlugin):
    plugin_id = "ssh_exec"

    def __init__(self) -> None:
        super().__init__()
        self._last_workdir: Optional[str] = None

    def get_spec(self) -> ToolSpec:
        desc = _build_hosts_description(self._last_workdir)
        return ToolSpec(
            name="ssh_exec",
            description=(
                "Execute a command on a remote server via SSH and return the result. "
                + desc
            ),
            parameters={
                "type": "object",
                "properties": {
                    "host": {
                        "type": "string",
                        "description": "Host alias from project SSH config",
                    },
                    "command": {
                        "type": "string",
                        "description": "Shell command to execute",
                    },
                    "timeout_sec": {
                        "type": "integer",
                        "description": "Timeout in seconds (default 30)",
                    },
                },
                "required": ["host", "command"],
            },
        )

    async def execute(self, args: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
        host = str(args.get("host") or "").strip()
        command = str(args.get("command") or "").strip()
        timeout_sec = int(args.get("timeout_sec") or 30)
        cwd = str(ctx.get("cwd") or "").strip()
        chat_id = ctx.get("chat_id")
        self._last_workdir = cwd or None

        if not host or not command:
            return {"success": False, "error": "host and command are required"}

        err = _check_ssh_enabled(ctx)
        if err:
            return err

        ssh = _get_ssh_service(self.services)
        if ssh is None:
            return {"success": False, "error": "SSH service not available"}

        try:
            result = await ssh.exec(
                workdir=cwd,
                host_alias=host,
                command=command,
                timeout_sec=timeout_sec,
                chat_id=int(chat_id) if chat_id is not None else None,
            )
            output_parts = []
            if result.stdout.strip():
                output_parts.append(result.stdout.strip())
            if result.stderr.strip():
                output_parts.append(f"STDERR:\n{result.stderr.strip()}")
            output_parts.append(f"Exit code: {result.exit_code}")
            return {"success": True, "output": "\n".join(output_parts)}
        except PermissionError as exc:
            return {"success": False, "error": str(exc)}
        except Exception as exc:
            logger.exception("ssh_exec failed")
            return {"success": False, "error": f"SSH error: {exc}"}


class SSHLongTool(ToolPlugin):
    plugin_id = "ssh_long"

    def __init__(self) -> None:
        super().__init__()
        self._last_workdir: Optional[str] = None

    def get_spec(self) -> ToolSpec:
        desc = _build_hosts_description(self._last_workdir)
        return ToolSpec(
            name="ssh_long",
            description=(
                "Run a long-running command on a remote server (deploy scripts, "
                "tail -f, etc). Output is streamed as live preview. "
                "Returns last N lines when done or timed out. " + desc
            ),
            parameters={
                "type": "object",
                "properties": {
                    "host": {
                        "type": "string",
                        "description": "Host alias",
                    },
                    "command": {
                        "type": "string",
                        "description": "Shell command to run",
                    },
                    "max_duration_sec": {
                        "type": "integer",
                        "description": "Max duration in seconds (default 300)",
                    },
                    "tail_lines": {
                        "type": "integer",
                        "description": "Number of last lines to return (default 200)",
                    },
                },
                "required": ["host", "command"],
            },
        )

    async def execute(self, args: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
        host = str(args.get("host") or "").strip()
        command = str(args.get("command") or "").strip()
        max_duration = int(args.get("max_duration_sec") or 300)
        tail_lines = int(args.get("tail_lines") or 200)
        cwd = str(ctx.get("cwd") or "").strip()
        chat_id = ctx.get("chat_id")
        self._last_workdir = cwd or None

        if not host or not command:
            return {"success": False, "error": "host and command are required"}

        err = _check_ssh_enabled(ctx)
        if err:
            return err

        ssh = _get_ssh_service(self.services)
        if ssh is None:
            return {"success": False, "error": "SSH service not available"}

        try:
            result = await ssh.stream(
                workdir=cwd,
                host_alias=host,
                command=command,
                max_duration_sec=max_duration,
                tail_lines=tail_lines,
                chat_id=int(chat_id) if chat_id is not None else None,
            )
            output = result.stdout.strip() or "(no output)"
            return {"success": True, "output": f"{output}\nExit code: {result.exit_code}"}
        except PermissionError as exc:
            return {"success": False, "error": str(exc)}
        except Exception as exc:
            logger.exception("ssh_long failed")
            return {"success": False, "error": f"SSH error: {exc}"}


class SSHCancelTool(ToolPlugin):
    plugin_id = "ssh_cancel"

    def get_spec(self) -> ToolSpec:
        return ToolSpec(
            name="ssh_cancel",
            description="Cancel a running long SSH command on a remote host.",
            parameters={
                "type": "object",
                "properties": {
                    "host": {
                        "type": "string",
                        "description": "Host alias",
                    },
                },
                "required": ["host"],
            },
        )

    async def execute(self, args: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
        host = str(args.get("host") or "").strip()
        cwd = str(ctx.get("cwd") or "").strip()

        if not host:
            return {"success": False, "error": "host is required"}

        err = _check_ssh_enabled(ctx)
        if err:
            return err

        ssh = _get_ssh_service(self.services)
        if ssh is None:
            return {"success": False, "error": "SSH service not available"}

        cancelled = await ssh.cancel(workdir=cwd, host_alias=host)
        if cancelled:
            return {"success": True, "output": f"Command on '{host}' cancelled"}
        return {"success": False, "error": f"No active command on '{host}'"}
