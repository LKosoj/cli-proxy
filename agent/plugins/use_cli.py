from __future__ import annotations

import logging
from typing import Any, Dict

from agent.plugins.base import ToolPlugin
from agent.tooling.spec import ToolSpec
from agent.tooling import helpers
from utils import strip_ansi


class UseCliTool(ToolPlugin):
    def get_spec(self) -> ToolSpec:
        return ToolSpec(
            name="use_cli",
            description="Delegate a complex task to the selected CLI (codex/gemini/claude code). Use when the task is too complex for tools or requires full coding workflow.",
            parameters={
                "type": "object",
                "properties": {"task_text": {"type": "string", "description": "Task description for CLI"}},
                "required": ["task_text"],
            },
            parallelizable=False,
        )

    async def execute(self, args: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
        task_text = (args.get("task_text") or "").strip()
        if not task_text:
            return {"success": False, "error": "task_text required"}
        session = ctx.get("session")
        if not session:
            return {"success": False, "error": "CLI session not available"}
        try:
            output = await session.run_prompt(task_text)
            output = strip_ansi(output)
            return {"success": True, "output": helpers._trim_output(output)}
        except Exception as e:
            logging.exception(f"tool failed {str(e)}")
            return {"success": False, "error": str(e)}
