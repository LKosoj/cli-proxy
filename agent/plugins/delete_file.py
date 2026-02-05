from __future__ import annotations

import logging
import os
from typing import Any, Dict

from agent.plugins.base import ToolPlugin
from agent.tooling.spec import ToolSpec
from agent.tooling import helpers


class DeleteFileTool(ToolPlugin):
    def get_spec(self) -> ToolSpec:
        return ToolSpec(
            name="delete_file",
            description="Delete a file. Only works within workspace directory.",
            parameters={
                "type": "object",
                "properties": {"path": {"type": "string", "description": "Path to the file to delete"}},
                "required": ["path"],
            },
        )

    async def execute(self, args: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
        path = args.get("path")
        if not path:
            return {"success": False, "error": "Path required"}
        cwd = ctx["cwd"]
        full_path, err = helpers._resolve_within_workspace(path, cwd)
        if err:
            return {"success": False, "error": err}
        if helpers._is_other_user_workspace(full_path, cwd):
            return {"success": False, "error": "🚫 BLOCKED: Cannot access other user's workspace"}
        if not os.path.exists(full_path):
            return {"success": False, "error": f"File not found: {full_path}"}
        try:
            os.remove(full_path)
            return {"success": True, "output": f"Deleted: {path}"}
        except Exception as e:
            logging.exception(f"tool failed {str(e)}")
            return {"success": False, "error": str(e)}
