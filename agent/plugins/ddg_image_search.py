from __future__ import annotations

import asyncio
import logging
import random
from typing import Any, Dict, List

from agent.plugins.base import ToolPlugin
from agent.tooling.spec import ToolSpec


class DDGImageSearchTool(ToolPlugin):
    def get_source_name(self) -> str:
        return "DuckDuckGo Images"

    def get_spec(self) -> ToolSpec:
        return ToolSpec(
            name="ddg_image_search",
            description="Поиск картинки или GIF в DuckDuckGo Images. Возвращает URL.",
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Поисковый запрос"},
                    "type": {"type": "string", "enum": ["photo", "gif"], "default": "photo"},
                    "region": {"type": "string", "default": "wt-wt"},
                    "max_results": {"type": "integer", "default": 10},
                },
                "required": ["query"],
            },
            parallelizable=True,
        )

    async def execute(self, args: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
        query = (args.get("query") or "").strip()
        if not query:
            return {"success": False, "error": "query обязателен"}
        image_type = (args.get("type") or "photo").strip().lower()
        if image_type not in ("photo", "gif"):
            image_type = "photo"
        region = (args.get("region") or "wt-wt").strip() or "wt-wt"
        max_results = int(args.get("max_results") or 10)
        max_results = max(1, min(max_results, 30))

        try:
            results = await asyncio.to_thread(self._search_sync, query, region, image_type, max_results)
        except Exception as e:
            logging.exception(f"tool failed {str(e)}")
            return {"success": False, "error": f"DDG image search failed: {e}"}

        if not results:
            return {"success": True, "output": "Ничего не найдено."}
        random.shuffle(results)
        url = (results[0].get("image") or results[0].get("thumbnail") or "").strip()
        if not url:
            return {"success": True, "output": "Ничего не найдено."}
        return {"success": True, "output": url}

    def _search_sync(self, query: str, region: str, image_type: str, max_results: int) -> List[Dict[str, Any]]:
        try:
            from duckduckgo_search import DDGS  # type: ignore
        except Exception as e:
            raise RuntimeError("Пакет 'duckduckgo-search' не установлен") from e

        with DDGS() as ddgs:
            gen = ddgs.images(query, region=region, safesearch="moderate", type_image=image_type)
            out: List[Dict[str, Any]] = []
            for item in gen:
                out.append(item)
                if len(out) >= max_results:
                    break
            return out
