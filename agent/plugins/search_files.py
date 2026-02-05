from __future__ import annotations

import logging
import os
import re
from typing import Any, Dict, List

from agent.plugins.base import ToolPlugin
from agent.tooling.spec import ToolSpec


class SearchFilesTool(ToolPlugin):
    def get_spec(self) -> ToolSpec:
        return ToolSpec(
            name="search_files",
            description="Search for files by glob pattern. Use to discover project structure.",
            parameters={
                "type": "object",
                "properties": {"pattern": {"type": "string", "description": "Glob pattern (e.g. **/*.ts, src/**/*.js)"}},
                "required": ["pattern"],
            },
        )

    async def execute(self, args: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
        pattern = (args.get("pattern") or "").strip()
        if not pattern:
            return {"success": False, "error": "Pattern required"}
        if re.search(r"(?:^|/)\.\.(?:/|$)", pattern):
            return {"success": False, "error": "🚫 BLOCKED: Path traversal is not allowed"}
        cwd = ctx["cwd"]
        try:
            import glob
            matches = glob.glob(os.path.join(cwd, pattern), recursive=True)
            files: List[str] = []
            for p in matches:
                resolved = os.path.realpath(p)
                root = os.path.realpath(cwd)
                if not (resolved == root or resolved.startswith(root + os.sep)):
                    continue
                rel = os.path.relpath(p, cwd)
                if "/node_modules/" in rel or rel.startswith("node_modules/"):
                    continue
                if "/.git/" in rel or rel.startswith(".git/"):
                    continue
                if os.path.isdir(p):
                    continue
                files.append(rel)
                if len(files) >= 200:
                    break
            return {"success": True, "output": "\n".join(files) or "(no matches)"}
        except Exception as e:
            logging.exception(f"tool failed {str(e)}")
            return {"success": False, "error": str(e)}
