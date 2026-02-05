from __future__ import annotations

import logging
import os
from typing import Any, Dict

from agent.plugins.base import ToolPlugin
from agent.tooling.spec import ToolSpec
from agent.tooling import helpers


class EditFileTool(ToolPlugin):
    def get_spec(self) -> ToolSpec:
        return ToolSpec(
            name="edit_file",
            description="Edit a file by replacing text. The old_text must match exactly.",
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path to the file"},
                    "old_text": {"type": "string", "description": "Exact text to find and replace"},
                    "new_text": {"type": "string", "description": "New text to insert"},
                },
                "required": ["path", "old_text", "new_text"],
            },
        )

    async def execute(self, args: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
        path = args.get("path")
        old_text = args.get("old_text")
        new_text = args.get("new_text")
        if not path or old_text is None or new_text is None:
            return {"success": False, "error": "path, old_text, new_text required"}
        cwd = ctx["cwd"]
        full_path, err = helpers._resolve_within_workspace(path, cwd)
        if err:
            return {"success": False, "error": err}
        if helpers._is_other_user_workspace(full_path, cwd):
            return {"success": False, "error": "🚫 BLOCKED: Cannot access other user's workspace"}
        if helpers._is_sensitive_file(full_path):
            return {"success": False, "error": f"🚫 BLOCKED: Cannot edit sensitive file ({os.path.basename(full_path)})"}
        symlink_check = helpers._is_symlink_escape(full_path, cwd)
        if symlink_check[0]:
            return {"success": False, "error": f"🚫 BLOCKED: {symlink_check[1]}"}
        if not os.path.exists(full_path):
            return {"success": False, "error": f"File not found: {full_path}"}
        content_check = helpers._contains_dangerous_code(new_text)
        if content_check[0]:
            return {"success": False, "error": f"🚫 BLOCKED: Edit contains dangerous code ({content_check[1]})."}
        try:
            with open(full_path, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()
            if old_text not in content:
                preview = content[:2000]
                return {"success": False, "error": f"old_text not found.\n\nFile preview:\n{preview}"}
            new_content = content.replace(old_text, new_text)
            with open(full_path, "w", encoding="utf-8") as f:
                f.write(new_content)
            return {"success": True, "output": f"Edited {path}"}
        except Exception as e:
            logging.exception(f"tool failed {str(e)}")
            return {"success": False, "error": str(e)}
