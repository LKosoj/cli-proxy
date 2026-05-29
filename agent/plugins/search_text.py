from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
from typing import Any, Dict

from agent.plugins.base import ToolPlugin
from modes.sdk.runtime.tooling.spec import ToolSpec
from agent.tooling import helpers
from modes.sdk.runtime.tooling.constants import GREP_TIMEOUT_MS

logger = logging.getLogger(__name__)
_SEARCH_OUTPUT_LINE_LIMIT = 200
_SEARCH_EXCLUDE_DIRS = ("node_modules", ".git", "dist")
_SEARCH_EXCLUDE_FILES = (
    "*.env*",
    "*credentials*",
    "*secret*",
    "*.pem",
    "*.key",
    "id_rsa*",
)


def _build_grep_command(pattern: str, search_path: str, args: Dict[str, Any]) -> list[str]:
    grep_path = shutil.which("grep")
    if not grep_path:
        raise FileNotFoundError("grep not available")
    command = [grep_path, "-rn"]
    if args.get("ignore_case"):
        command.append("-i")
    if args.get("files_only"):
        command.append("-l")
    if args.get("context_before"):
        command.extend(["-B", str(int(args.get("context_before")))])
    if args.get("context_after"):
        command.extend(["-A", str(int(args.get("context_after")))])
    for excluded_dir in _SEARCH_EXCLUDE_DIRS:
        command.append(f"--exclude-dir={excluded_dir}")
    for excluded_file in _SEARCH_EXCLUDE_FILES:
        command.append(f"--exclude={excluded_file}")
    command.extend(["-e", pattern, "--", search_path])
    return command


def _build_rg_command(pattern: str, search_path: str, args: Dict[str, Any]) -> list[str]:
    rg_path = shutil.which("rg")
    if not rg_path:
        raise FileNotFoundError("rg not available")
    command = [rg_path, "-n", "--color", "never", "--no-messages"]
    if args.get("ignore_case"):
        command.append("-i")
    if args.get("files_only"):
        command.append("-l")
    if args.get("context_before"):
        command.extend(["-B", str(int(args.get("context_before")))])
    if args.get("context_after"):
        command.extend(["-A", str(int(args.get("context_after")))])
    for excluded_dir in _SEARCH_EXCLUDE_DIRS:
        command.extend(["--glob", f"!{excluded_dir}/**"])
    for excluded_file in _SEARCH_EXCLUDE_FILES:
        command.extend(["--glob", f"!{excluded_file}"])
    command.extend(["-e", pattern, search_path])
    return command


def _build_search_command(pattern: str, search_path: str, args: Dict[str, Any]) -> list[str]:
    try:
        return _build_grep_command(pattern, search_path, args)
    except FileNotFoundError:
        return _build_rg_command(pattern, search_path, args)


def _truncate_search_output(output: str) -> str:
    if not output:
        return ""
    lines = output.splitlines()
    limited = lines[:_SEARCH_OUTPUT_LINE_LIMIT]
    if not limited:
        return ""
    return "\n".join(limited) + "\n"


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
            reason = "Cannot search for secrets/credentials patterns"
            return helpers.blocked_error(reason)
        cwd = ctx["cwd"]
        search_path = args.get("path") or cwd
        if not os.path.isabs(search_path):
            search_path = os.path.join(cwd, search_path)
        resolved_path, err = helpers._resolve_within_workspace(search_path, cwd)
        if err:
            blocked = helpers.blocked_from_error(err)
            if blocked:
                return blocked
            return {"success": False, "error": err}
        search_path = resolved_path
        try:
            cmd = _build_search_command(pattern, search_path, args)
            completed = subprocess.run(
                cmd,
                shell=False,
                cwd=cwd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                encoding="utf-8",
                timeout=GREP_TIMEOUT_MS / 1000,
            )
            output = _truncate_search_output(completed.stdout or "")
            return {"success": True, "output": output or "(no matches)"}
        except Exception as e:
            logger.exception("tool failed %s", e)
            return {"success": True, "output": "(no matches)"}
