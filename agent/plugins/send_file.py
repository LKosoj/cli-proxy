from __future__ import annotations

import logging
import os
from typing import Any, Dict

from agent.plugins.base import ToolPlugin
from agent.tooling.spec import ToolSpec
from agent.tooling import helpers


class SendFileTool(ToolPlugin):
    def get_spec(self) -> ToolSpec:
        return ToolSpec(
            name="send_file",
            description="Send a file from your workspace to the chat. Use this to share files you created or found with the user. Max file size: 50MB.",
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path to the file (relative to workspace or absolute)"},
                    "caption": {"type": "string", "description": "Optional caption/description for the file"},
                },
                "required": ["path"],
            },
            parallelizable=False,
        )

    async def execute(self, args: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
        path = args.get("path")
        if not path:
            return {"success": False, "error": "Path required"}
        cwd = ctx["cwd"]
        chat_id = ctx.get("chat_id") or 0
        bot = ctx.get("bot")
        context = ctx.get("context")
        if not bot or not context:
            return {"success": False, "error": "Send file callback not configured"}
        full_path, err = helpers._resolve_within_workspace(path, cwd)
        if err:
            return {"success": False, "error": err}
        resolved = os.path.realpath(full_path)
        filename = os.path.basename(resolved).lower()
        blocked = [".env", "credentials", "secrets", "password", "token", ".pem", "id_rsa", "id_ed25519", ".key", "serviceaccount"]
        for b in blocked:
            if b in filename or b in resolved.lower():
                return {"success": False, "error": "🚫 BLOCKED: Cannot send sensitive files (credentials, keys, etc)"}
        if not os.path.exists(resolved):
            return {"success": False, "error": f"File not found: {path}"}
        size = os.path.getsize(resolved)
        if size > 50 * 1024 * 1024:
            return {"success": False, "error": f"File too large ({round(size/1024/1024)}MB). Max: 50MB"}
        if size == 0:
            return {"success": False, "error": "File is empty"}
        try:
            caption = args.get("caption")
            with open(resolved, "rb") as f:
                await bot._send_document(context, chat_id=chat_id, document=f, caption=caption)
            return {"success": True, "output": f"Sent file: {os.path.basename(resolved)}"}
        except Exception as e:
            logging.exception(f"tool failed {str(e)}")
            msg = str(e)
            if "not enough rights" in msg or "CHAT_SEND_MEDIA_FORBIDDEN" in msg:
                return {"success": False, "error": "Cannot send files in this group (no permissions). Try: read the file and paste contents, or tell user to DM for files."}
            return {"success": False, "error": f"Failed to send file: {msg}"}
