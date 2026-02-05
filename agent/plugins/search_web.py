from __future__ import annotations

from typing import Any, Dict

from agent.plugins.base import ToolPlugin
from agent.tooling.spec import ToolSpec
from agent.tooling import helpers


class SearchWebTool(ToolPlugin):
    def get_spec(self) -> ToolSpec:
        return ToolSpec(
            name="search_web",
            description="Search the internet. USE IMMEDIATELY for: news, current events, external info, 'what is X?', prices, weather.",
            parameters={"type": "object", "properties": {"query": {"type": "string", "description": "Search query"}}, "required": ["query"]},
            risk_level="medium",
        )

    async def execute(self, args: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
        query = (args.get("query") or "").strip()
        return await helpers.search_web_impl(query, self.config)
