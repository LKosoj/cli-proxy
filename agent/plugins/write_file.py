from __future__ import annotations

import logging
import os
from typing import Any, Dict

from agent.plugins.base import ToolPlugin
from agent.tooling.spec import ToolSpec
from agent.tooling import helpers


class WriteFileTool(ToolPlugin):
    def get_spec(self) -> ToolSpec:
        return ToolSpec(
            name="write_file",
            description="Write/create files. Use to create new files or overwrite existing ones.",
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path to the file"},
                    "content": {"type": "string", "description": "Full file content"},
                },
                "required": ["path", "content"],
            },
        )

    async def execute(self, args: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
        path = args.get("path")
        content = args.get("content") or ""
        if not path:
            return {"success": False, "error": "Path required"}
        cwd = ctx["cwd"]
        full_path, err = helpers._resolve_within_workspace(path, cwd)
        if err:
            return {"success": False, "error": err}
        if helpers._is_other_user_workspace(full_path, cwd):
            return {"success": False, "error": "🚫 BLOCKED: Cannot access other user's workspace"}
        if helpers._is_sensitive_file(full_path):
            return {"success": False, "error": f"🚫 BLOCKED: Cannot write to sensitive file ({os.path.basename(full_path)})"}
        symlink_check = helpers._is_symlink_escape(full_path, cwd)
        if symlink_check[0]:
            return {"success": False, "error": f"🚫 BLOCKED: {symlink_check[1]}"}
        content_check = helpers._contains_dangerous_code(content)
        if content_check[0]:
            return {"success": False, "error": (
                f"🚫 BLOCKED: File contains dangerous code ({content_check[1]}). "
                "Cannot write files that may leak secrets."
            )}
        try:
            os.makedirs(os.path.dirname(full_path), exist_ok=True)
            with open(full_path, "w", encoding="utf-8") as f:
                f.write(content)
            return {"success": True, "output": f"Written {len(content)} bytes to {path}"}
        except Exception as e:
            logging.exception(f"tool failed {str(e)}")
            return {"success": False, "error": str(e)}
