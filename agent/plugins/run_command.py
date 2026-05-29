from __future__ import annotations

from typing import Any, Dict

from agent.plugins.base import ToolPlugin
from modes.sdk.runtime.tooling.spec import ToolSpec
from agent.tooling import helpers


class RunCommandTool(ToolPlugin):
    def get_spec(self) -> ToolSpec:
        return ToolSpec(
            name="run_command",
            description=(
                "Run a shell command. Use for: git, npm, pip, system operations. "
                "DANGEROUS commands (rm -rf, sudo, etc.) require user approval."
            ),
            parameters={
                "type": "object",
                "properties": {"command": {"type": "string", "description": "The shell command to execute"}},
                "required": ["command"],
            },
            risk_level="high",
            parallelizable=False,
        )

    async def execute(self, args: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
        cmd = (args.get("command") or "").strip()
        if not cmd:
            return {"success": False, "error": "Command required"}
        cwd = ctx["cwd"]
        session_id = ctx.get("session_id") or "default"
        chat_id = ctx.get("chat_id") or 0
        chat_type = ctx.get("chat_type")
        blocked_ws, reason_ws = helpers._check_workspace_isolation(cmd, cwd)
        if blocked_ws:
            return helpers.blocked_error(reason_ws or "Command blocked by workspace isolation", error=f"🚫 {reason_ws}")
        blocked_path, reason_path = helpers._check_command_path_escape(cmd, cwd)
        if blocked_path:
            return helpers.blocked_error(reason_path or "Command path escapes workspace", error=f"🚫 {reason_path}")
        dangerous, blocked, reason = helpers.check_command(cmd, chat_type)
        if blocked:
            block_reason = reason or "Command blocked by security policy"
            return helpers.blocked_error(
                block_reason,
                error=f"🚫 {block_reason}\n\nThis command is not allowed for security reasons.",
            )
        if dangerous:
            cb = helpers._APPROVAL_CALLBACK
            if not cb or not chat_id:
                return {
                    "success": False,
                    "error": "⚠️ APPROVAL REQUIRED, but approval channel is unavailable.",
                    "approval_required": True,
                }
            cmd_id = helpers._store_pending_command(session_id, chat_id, cmd, cwd, reason or "Dangerous")
            helpers.ensure_pending_command_waiter(cmd_id)
            cb(chat_id, cmd_id, cmd, reason or "Dangerous")
            approved = await helpers.wait_pending_command_decision(cmd_id, timeout_sec=1200)
            if approved is None:
                helpers.pop_pending_command(cmd_id)
                return {
                    "success": False,
                    "error": "Approval timeout: user did not confirm command in time.",
                    "approval_required": True,
                }
            if not approved:
                return {
                    "success": False,
                    "error": "Command denied by user.",
                    "approval_required": True,
                }
            return await helpers.execute_shell_command(cmd, cwd)
        return await helpers.execute_shell_command(cmd, cwd)
