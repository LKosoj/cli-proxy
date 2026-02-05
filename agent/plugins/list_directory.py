from __future__ import annotations

import logging
import os
import subprocess
from typing import Any, Dict

from agent.plugins.base import ToolPlugin
from agent.tooling.spec import ToolSpec
from agent.tooling import helpers


class ListDirectoryTool(ToolPlugin):
    def get_spec(self) -> ToolSpec:
        return ToolSpec(
            name="list_directory",
            description="List contents of a directory.",
            parameters={"type": "object", "properties": {"path": {"type": "string", "description": "Directory path (default: current)"}}},
        )

    async def execute(self, args: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
        cwd = ctx["cwd"]
        path = args.get("path")
        dir_path = path if path else cwd
        if not os.path.isabs(dir_path):
            dir_path = os.path.join(cwd, dir_path)
        resolved_path, err = helpers._resolve_within_workspace(dir_path, cwd)
        if err:
            return {"success": False, "error": err}
        dir_path = resolved_path
        if helpers._is_other_user_workspace(dir_path, cwd):
            return {"success": False, "error": "🚫 BLOCKED: Cannot access other user's workspace"}
        blocked_dirs = ["/etc", "/root", "/.ssh", "/proc", "/sys", "/dev", "/boot", "/var/log", "/var/run"]
        resolved = os.path.realpath(dir_path).lower()
        for b in blocked_dirs:
            if resolved == b or resolved.startswith(b + "/"):
                return {"success": False, "error": f"🚫 BLOCKED: Cannot list directory {b} for security reasons"}
        if "/.ssh" in resolved:
            return {"success": False, "error": "🚫 BLOCKED: Cannot list .ssh directory"}
        try:
            completed = subprocess.run(
                f"ls -la \"{dir_path}\"",
                shell=True,
                cwd=cwd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                encoding="utf-8",
            )
            if completed.returncode == 0:
                return {"success": True, "output": completed.stdout}
            return {"success": False, "error": completed.stderr or "list failed"}
        except Exception as e:
            logging.exception(f"tool failed {str(e)}")
            return {"success": False, "error": str(e)}
