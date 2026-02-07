from __future__ import annotations

from typing import Any, Dict

from agent.plugins.base import ToolPlugin
from agent.tooling.spec import ToolSpec
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
            return {"success": False, "error": f"🚫 {reason_ws}"}
        blocked_path, reason_path = helpers._check_command_path_escape(cmd, cwd)
        if blocked_path:
            return {"success": False, "error": f"🚫 {reason_path}"}
        dangerous, blocked, reason = helpers.check_command(cmd, chat_type)
        if blocked:
            return {"success": False, "error": f"🚫 {reason}\n\nThis command is not allowed for security reasons."}
        if dangerous:
            cmd_id = helpers._store_pending_command(session_id, chat_id, cmd, cwd, reason or "Dangerous")
            cb = helpers._APPROVAL_CALLBACK
            if cb and chat_id:
                cb(chat_id, cmd_id, cmd, reason or "Dangerous")
            return {
                "success": False,
                "error": f"⚠️ APPROVAL REQUIRED: \"{reason}\"\n\nWaiting for user to click Approve/Deny button.",
                "approval_required": True,
            }
        return await helpers.execute_shell_command(cmd, cwd)
