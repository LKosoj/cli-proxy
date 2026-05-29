from __future__ import annotations

from typing import Any, Dict

from agent.plugins.base import ToolPlugin
from modes.admin.plugin_tools import (
    AdminToolError,
    resolve_workdir,
    run_allowlisted_action,
)
from modes.sdk.runtime.tooling.spec import ToolSpec


class AdminExecuteActionTool(ToolPlugin):
    plugin_id = "admin_execute_action"

    def get_spec(self) -> ToolSpec:
        return ToolSpec(
            name="admin_execute_action",
            description=(
                "Execute an allowlisted admin action against a local or remote server. "
                "Actions must be pre-declared in the admin config (admin.allowlist.local or admin.allowlist.ssh). "
                "Defaults to dry-run (preview only); pass dry_run=false to actually apply. "
                "Writes an audit note into the target server's memory."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "action_id": {
                        "type": "string",
                        "description": "ID of the allowlisted action.",
                    },
                    "server_id": {
                        "type": "string",
                        "description": "Target server ID from admin config. Optional for logical/local actions.",
                    },
                    "target_hint": {
                        "type": "string",
                        "enum": ["local", "ssh"],
                        "description": "Restrict search to a specific allowlist section.",
                    },
                    "dry_run": {
                        "type": "boolean",
                        "description": "If true (default), return a preview without executing.",
                        "default": True,
                    },
                },
                "required": ["action_id"],
            },
            risk_level="high",
            requires_approval=False,
            tags=["admin", "server", "action"],
            parallelizable=False,
            category="admin",
        )

    async def execute(self, args: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
        try:
            workdir = resolve_workdir(ctx)
        except AdminToolError as exc:
            return {"success": False, "error": str(exc)}

        action_id = str(args.get("action_id") or "").strip()
        server_id = args.get("server_id")
        target_hint = args.get("target_hint")
        dry_run_raw = args.get("dry_run", True)
        dry_run = True if dry_run_raw is None else bool(dry_run_raw)

        try:
            result = await run_allowlisted_action(
                workdir=workdir,
                action_id=action_id,
                server_id=str(server_id) if server_id else None,
                target_hint=str(target_hint) if target_hint else None,
                dry_run=dry_run,
            )
        except AdminToolError as exc:
            return {"success": False, "error": str(exc)}

        payload = result.to_dict()
        payload["output"] = _format_output(result)
        return payload


def _format_output(result) -> str:
    if result.dry_run:
        return (
            f"[DRY-RUN] action={result.action_id} target={result.target} "
            f"cmd={result.command_preview}"
        )
    head = (
        f"action={result.action_id} target={result.target} "
        f"rc={result.exit_code} duration_ms={result.duration_ms}"
    )
    tail = ""
    if result.stdout:
        tail += f"\nSTDOUT:\n{result.stdout.strip()[:2000]}"
    if result.stderr:
        tail += f"\nSTDERR:\n{result.stderr.strip()[:2000]}"
    if result.error:
        tail += f"\nERROR: {result.error}"
    return head + tail
