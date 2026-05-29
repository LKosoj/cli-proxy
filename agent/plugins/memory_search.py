from __future__ import annotations

from typing import Any, Dict

from modes.sdk.runtime.memory_retrieval import format_retrieved_context, retrieve_relevant_context
from agent.plugins.base import ToolPlugin
from modes.sdk.runtime.tooling.spec import ToolSpec


class MemorySearchTool(ToolPlugin):
    def get_spec(self) -> ToolSpec:
        return ToolSpec(
            name="memory_search",
            description=(
                "Search long-term memory and orchestrator session history. "
                "Use when task references prior decisions, configs, or past steps."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query for memory/history"},
                    "limit": {"type": "integer", "description": "Max snippets to return", "minimum": 1, "maximum": 20},
                },
                "required": ["query"],
            },
            parallelizable=True,
        )

    async def execute(self, args: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
        query = str(args.get("query") or "").strip()
        if not query:
            return {"success": False, "error": "query required"}
        try:
            limit = int(args.get("limit") or 6)
        except Exception:
            limit = 6
        limit = max(1, min(20, limit))
        cwd = str(ctx.get("state_root") or ctx.get("cwd") or "")
        if not cwd:
            return {"success": False, "error": "cwd/state_root is required"}
        items = retrieve_relevant_context(cwd, query, limit=limit)
        if not items:
            return {"success": True, "output": "(no relevant memory found)", "items": []}
        rendered = format_retrieved_context(items, max_chars=3000)
        return {"success": True, "output": rendered or "(no relevant memory found)", "items": items}
