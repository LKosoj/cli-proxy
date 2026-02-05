from __future__ import annotations

import logging
import os
from typing import Any, Dict

from agent.plugins.base import ToolPlugin
from agent.tooling.spec import ToolSpec
from agent.tooling import helpers


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
            return {"success": False, "error": err}
        if helpers._is_other_user_workspace(full_path, cwd):
            return {"success": False, "error": "🚫 BLOCKED: Cannot access other user's workspace"}
        if helpers._is_sensitive_file(full_path):
            return {"success": False, "error": f"🚫 BLOCKED: Cannot read sensitive file ({os.path.basename(full_path)})"}
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
            logging.exception(f"tool failed {str(e)}")
            return {"success": False, "error": str(e)}
