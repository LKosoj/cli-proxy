from __future__ import annotations

import logging
import os
from typing import Any, Dict

from agent.plugins.base import ToolPlugin
from modes.sdk.runtime.tooling.spec import ToolSpec
from agent.tooling import helpers

logger = logging.getLogger(__name__)


class ReadFileTool(ToolPlugin):
    def get_spec(self) -> ToolSpec:
        return ToolSpec(
            name="read_file",
            description="Read file contents. Always read before editing a file.",
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path to the file"},
                    "offset": {"type": "number", "description": "Starting line number (1-based)"},
                    "limit": {"type": "number", "description": "Number of lines to read"},
                },
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
            reason = f"Cannot read sensitive file ({os.path.basename(full_path)})"
            return helpers.blocked_error(reason)
        if not os.path.exists(full_path):
            return {"success": False, "error": f"File not found: {full_path}"}
        try:
            with open(full_path, "r", encoding="utf-8", errors="replace") as f:
                lines = f.read().splitlines()
            offset = int(args.get("offset") or 1)
            limit = args.get("limit")
            if offset < 1:
                offset = 1
            start = offset - 1
            end = start + int(limit) if limit else None
            slice_lines = lines[start:end]
            content = "\n".join(slice_lines)
            return {"success": True, "output": content if content else "(empty file)"}
        except Exception as e:
            logger.exception("read_file: failed to read file path=%r", full_path)
            return {"success": False, "error": str(e)}
