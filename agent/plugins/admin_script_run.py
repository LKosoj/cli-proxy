from __future__ import annotations

from typing import Any, Dict

from agent.plugins.base import ToolPlugin
from modes.admin.plugin_tools import AdminToolError, resolve_workdir
from modes.admin.script_runner import ScriptRunnerError, run_runbook_step
from modes.sdk.runtime.tooling.spec import ToolSpec


class AdminScriptRunTool(ToolPlugin):
    plugin_id = "admin_script_run"

    def get_spec(self) -> ToolSpec:
        return ToolSpec(
            name="admin_script_run",
            description=(
                "Execute a script step from a script-based runbook. "
                "The script must be committed via the admin runbook builder (trust-on-write) "
                "and the target server must be listed in runbook.servers (promote gate). "
                "SHA1 checksum is verified before execution unless verify_checksum=false. "
                "Defaults to dry-run preview; pass dry_run=false to actually execute. "
                "Writes an audit note into the server's memory."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "rb_id": {
                        "type": "string",
                        "description": "Runbook id.",
                    },
                    "step_name": {
                        "type": "string",
                        "description": "Name of the step (frontmatter.steps[*].name).",
                    },
                    "server_id": {
                        "type": "string",
                        "description": "Target server id. Must match runbook.servers.",
                    },
                    "dry_run": {
                        "type": "boolean",
                        "description": "If true (default), return a preview without executing.",
                        "default": True,
                    },
                    "verify_checksum": {
                        "type": "boolean",
                        "description": "If true (default), reject execution when script SHA1 doesn't match frontmatter.",
                        "default": True,
                    },
                    "timeout_sec": {
                        "type": "number",
                        "description": "Override execution timeout in seconds (default 120).",
                    },
                },
                "required": ["rb_id", "step_name", "server_id"],
            },
            risk_level="high",
            requires_approval=True,
            tags=["admin", "server", "runbook", "script"],
            parallelizable=False,
            category="admin",
        )

    async def execute(self, args: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
        try:
            workdir = resolve_workdir(ctx)
        except AdminToolError as exc:
            return {"success": False, "error": str(exc)}

        rb_id = str(args.get("rb_id") or "").strip()
        step_name = str(args.get("step_name") or "").strip()
        server_id = str(args.get("server_id") or "").strip()
        if not rb_id or not step_name or not server_id:
            return {"success": False, "error": "rb_id, step_name and server_id are required"}

        dry_run_raw = args.get("dry_run", True)
        dry_run = True if dry_run_raw is None else bool(dry_run_raw)
        verify_raw = args.get("verify_checksum", True)
        verify_checksum = True if verify_raw is None else bool(verify_raw)
        timeout_raw = args.get("timeout_sec")
        timeout_sec = float(timeout_raw) if timeout_raw else None

        try:
            result = await run_runbook_step(
                workdir=workdir,
                rb_id=rb_id,
                step_name=step_name,
                server_id=server_id,
                dry_run=dry_run,
                verify_checksum=verify_checksum,
                timeout_sec=timeout_sec,
            )
        except ScriptRunnerError as exc:
            return {"success": False, "error": str(exc)}

        payload = result.to_dict()
        payload["output"] = _format_output(result)
        return payload


def _format_output(result) -> str:
    if result.dry_run:
        return (
            f"[DRY-RUN] rb={result.rb_id} step={result.step} target={result.target} "
            f"cmd={result.command_preview} checksum_ok={result.checksum_ok}"
        )
    head = (
        f"rb={result.rb_id} step={result.step} target={result.target} "
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
