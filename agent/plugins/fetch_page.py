from __future__ import annotations

from typing import Any, Dict

from agent.plugins.base import ToolPlugin
from agent.tooling.spec import ToolSpec
from agent.tooling import helpers


class FetchPageTool(ToolPlugin):
    def get_spec(self) -> ToolSpec:
        return ToolSpec(
            name="fetch_page",
            description="Fetch and parse content from a URL. Returns clean markdown text.",
            parameters={"type": "object", "properties": {"url": {"type": "string", "description": "URL to fetch"}}, "required": ["url"]},
            risk_level="medium",
        )

    async def execute(self, args: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
        url = (args.get("url") or "").strip()
        return await helpers.fetch_page_impl(url, self.config)
