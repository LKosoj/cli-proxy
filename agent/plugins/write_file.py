from __future__ import annotations

import logging
import os
from typing import Any, Dict

from agent.plugins.base import ToolPlugin
from modes.sdk.runtime.tooling.spec import ToolSpec
from agent.tooling import helpers

logger = logging.getLogger(__name__)


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
            blocked = helpers.blocked_from_error(err)
            if blocked:
                return blocked
            return {"success": False, "error": err}
        if helpers._is_other_user_workspace(full_path, cwd):
            reason = "Cannot access other user's workspace"
            return helpers.blocked_error(reason)
        if helpers._is_sensitive_file(full_path):
            reason = f"Cannot write to sensitive file ({os.path.basename(full_path)})"
            return helpers.blocked_error(reason)
        symlink_check = helpers._is_symlink_escape(full_path, cwd)
        if symlink_check[0]:
            reason = str(symlink_check[1])
            return helpers.blocked_error(reason)
        content_check = helpers._contains_dangerous_code(content)
        if content_check[0]:
            reason = f"File contains dangerous code ({content_check[1]}). Cannot write files that may leak secrets."
            return helpers.blocked_error(reason)
        try:
            os.makedirs(os.path.dirname(full_path), exist_ok=True)
            with open(full_path, "w", encoding="utf-8") as f:
                f.write(content)
            return {"success": True, "output": f"Written {len(content)} bytes to {path}"}
        except Exception as e:
            logger.exception("write_file: failed to write file path=%r", full_path)
            return {"success": False, "error": str(e)}
