from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional

from agent.plugins.base import ToolPlugin
from agent.tooling.spec import ToolSpec


class DDGWebSearchTool(ToolPlugin):
    def get_source_name(self) -> str:
        return "DuckDuckGo"

    def get_spec(self) -> ToolSpec:
        return ToolSpec(
            name="ddg_web_search",
            description="Поиск в DuckDuckGo. Возвращает несколько результатов (title, url, snippet).",
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Поисковый запрос"},
                    "region": {"type": "string", "description": "Регион (например, 'wt-wt', 'ru-ru')", "default": "wt-wt"},
                    "max_results": {"type": "integer", "description": "Сколько результатов вернуть (1-10)", "default": 5},
                    "safesearch": {"type": "string", "enum": ["on", "moderate", "off"], "default": "moderate"},
                },
                "required": ["query"],
            },
            parallelizable=True,
        )

    async def execute(self, args: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
        query = (args.get("query") or "").strip()
        if not query:
            return {"success": False, "error": "query обязателен"}

        region = (args.get("region") or "wt-wt").strip() or "wt-wt"
        max_results = int(args.get("max_results") or 5)
        max_results = max(1, min(max_results, 10))
        safesearch = (args.get("safesearch") or "moderate").strip().lower()
        if safesearch not in ("on", "moderate", "off"):
            safesearch = "moderate"

        try:
            results = await asyncio.to_thread(self._search_sync, query, region, safesearch, max_results)
        except Exception as e:
            logging.exception(f"tool failed {str(e)}")
            return {"success": False, "error": f"DDG search failed: {e}"}

        if not results:
            return {"success": True, "output": "Ничего не найдено."}

        lines: List[str] = []
        for i, r in enumerate(results, start=1):
            title = (r.get("title") or "").strip()
            url = (r.get("href") or r.get("link") or "").strip()
            body = (r.get("body") or r.get("snippet") or "").strip()
            lines.append(f"{i}. {title}\n{url}\n{body}".strip())
        return {"success": True, "output": "\n\n".join(lines)}

    def _search_sync(self, query: str, region: str, safesearch: str, max_results: int) -> List[Dict[str, Any]]:
        # Lazy import: не ломаем загрузку плагинов при отсутствии зависимости.
        try:
            from duckduckgo_search import DDGS  # type: ignore
        except Exception as e:
            raise RuntimeError("Пакет 'duckduckgo-search' не установлен") from e

        with DDGS() as ddgs:
            gen = ddgs.text(query, region=region, safesearch=safesearch)
            out: List[Dict[str, Any]] = []
            for item in gen:
                out.append(item)
                if len(out) >= max_results:
                    break
            return out
