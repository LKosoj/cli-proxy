from __future__ import annotations

import logging
import os
import re
import subprocess
from typing import Any, Dict

from agent.plugins.base import ToolPlugin
from agent.tooling.spec import ToolSpec
from agent.tooling import helpers
from agent.tooling.constants import GREP_TIMEOUT_MS


class SearchTextTool(ToolPlugin):
    def get_spec(self) -> ToolSpec:
        return ToolSpec(
            name="search_text",
            description="Search for text/code in files using grep/ripgrep. Find definitions, usages, patterns.",
            parameters={
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "Text or regex pattern to search"},
                    "path": {"type": "string", "description": "Directory or file to search in (default: current)"},
                    "context_before": {"type": "number", "description": "Lines to show before match (like grep -B)"},
                    "context_after": {"type": "number", "description": "Lines to show after match (like grep -A)"},
                    "files_only": {"type": "boolean", "description": "Return only file paths, not content"},
                    "ignore_case": {"type": "boolean", "description": "Case insensitive search"},
                },
                "required": ["pattern"],
            },
        )

    async def execute(self, args: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
        pattern = (args.get("pattern") or "").strip()
        if not pattern:
            return {"success": False, "error": "Pattern required"}
        if re.search(r"password|secret|token|api.?key|credential|private.?key", pattern, re.I):
            return {"success": False, "error": "🚫 BLOCKED: Cannot search for secrets/credentials patterns"}
        cwd = ctx["cwd"]
        search_path = args.get("path") or cwd
        if not os.path.isabs(search_path):
            search_path = os.path.join(cwd, search_path)
        resolved_path, err = helpers._resolve_within_workspace(search_path, cwd)
        if err:
            return {"success": False, "error": err}
        search_path = resolved_path
        flags = ["-rn"]
        if args.get("ignore_case"):
            flags.append("-i")
        if args.get("files_only"):
            flags.append("-l")
        if args.get("context_before"):
            flags.append(f"-B{int(args.get('context_before'))}")
        if args.get("context_after"):
            flags.append(f"-A{int(args.get('context_after'))}")
        flags += ["--exclude-dir=node_modules", "--exclude-dir=.git", "--exclude-dir=dist"]
        flags += [
            "--exclude=*.env*", "--exclude=*credentials*", "--exclude=*secret*",
            "--exclude=*.pem", "--exclude=*.key", "--exclude=id_rsa*",
        ]
        escaped = pattern.replace('"', '\\"')
        cmd = f"grep {' '.join(flags)} \"{escaped}\" \"{search_path}\" 2>/dev/null | head -200"
        try:
            completed = subprocess.run(
                cmd,
                shell=True,
                cwd=cwd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                encoding="utf-8",
                timeout=GREP_TIMEOUT_MS / 1000,
            )
            output = completed.stdout or ""
            return {"success": True, "output": output or "(no matches)"}
        except Exception as e:
            logging.exception(f"tool failed {str(e)}")
            return {"success": True, "output": "(no matches)"}
