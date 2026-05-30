from __future__ import annotations

import logging
import os
from typing import Any, Dict

from agent.plugins.base import ToolPlugin
from modes.sdk.runtime.tooling.spec import ToolSpec
from agent.tooling import helpers

logger = logging.getLogger(__name__)


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
            blocked = helpers.blocked_from_error(err)
            if blocked:
                return blocked
            return {"success": False, "error": err}
        if helpers._is_other_user_workspace(full_path, cwd):
            reason = "Cannot access other user's workspace"
            return helpers.blocked_error(reason)
        if helpers._is_sensitive_file(full_path):
            reason = f"Cannot delete sensitive file ({os.path.basename(full_path)})"
            return helpers.blocked_error(reason)
        symlink_check = helpers._is_symlink_escape(full_path, cwd)
        if symlink_check[0]:
            reason = str(symlink_check[1])
            return helpers.blocked_error(reason)
        if not os.path.exists(full_path):
            return {"success": False, "error": f"File not found: {full_path}"}
        try:
            os.remove(full_path)
            return {"success": True, "output": f"Deleted: {path}"}
        except Exception as e:
            logger.exception("delete_file: failed to delete file path=%r", full_path)
            return {"success": False, "error": str(e)}
