from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List

from agent.plugins.base import ToolPlugin
from agent.tooling.spec import ToolSpec


class WebResearchTool(ToolPlugin):
    def get_source_name(self) -> str:
        return "Web Research"

    def get_spec(self) -> ToolSpec:
        return ToolSpec(
            name="web_research",
            description="Быстрый ресерч: ищет релевантные ссылки по теме и, опционально, подтягивает краткий текст.",
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Тема/вопрос для поиска"},
                    "max_results": {"type": "integer", "description": "Максимум ссылок (1-15)", "default": 8},
                    "include_snippets": {"type": "boolean", "description": "Подтянуть краткий текст по ссылкам", "default": True},
                },
                "required": ["query"],
            },
            parallelizable=False,
            timeout_ms=90_000,
        )

    async def execute(self, args: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
        query = (args.get("query") or "").strip()
        if not query:
            return {"success": False, "error": "query обязателен"}
        max_results = int(args.get("max_results") or 8)
        max_results = max(1, min(max_results, 15))
        include_snippets = bool(args.get("include_snippets", True))

        # Делаем несколько вариаций запроса без обращения к LLM: агент и так умеет формулировать.
        queries = [query, f"{query} обзор", f"{query} research"]
        urls: List[str] = []
        for q in queries:
            try:
                found = await asyncio.to_thread(self._ddg_search_sync, q, max_results)
            except Exception as e:
                logging.exception(f"tool failed {str(e)}")
                continue
            for u in found:
                if u not in urls:
                    urls.append(u)
                if len(urls) >= max_results:
                    break
            if len(urls) >= max_results:
                break

        if not urls:
            return {"success": True, "output": "Ничего не найдено."}

        if not include_snippets:
            return {"success": True, "output": "\n".join(urls)}

        # Подтягиваем короткие сниппеты через r.jina.ai (без тяжелых зависимостей).
        snippets = await asyncio.gather(*[asyncio.to_thread(self._fetch_snippet_sync, u) for u in urls[: min(len(urls), 5)]])
        blocks: List[str] = []
        for u, sn in zip(urls[: min(len(urls), 5)], snippets):
            blocks.append(f"{u}\n{sn}".strip())
        if len(urls) > 5:
            blocks.append("\nДополнительные ссылки:\n" + "\n".join(urls[5:]))
        return {"success": True, "output": "\n\n".join(blocks)}

    def _ddg_search_sync(self, query: str, max_results: int) -> List[str]:
        try:
            from duckduckgo_search import DDGS  # type: ignore
        except Exception as e:
            raise RuntimeError("Пакет 'duckduckgo-search' не установлен") from e

        urls: List[str] = []
        with DDGS() as ddgs:
            gen = ddgs.text(query, region="wt-wt", safesearch="moderate")
            for item in gen:
                href = (item.get("href") or item.get("link") or "").strip()
                if href and href not in urls:
                    urls.append(href)
                if len(urls) >= max_results:
                    break
        return urls

    def _fetch_snippet_sync(self, url: str) -> str:
        import requests

        reader_url = f"https://r.jina.ai/{url}"
        try:
            r = requests.get(reader_url, headers={"Accept": "text/plain; charset=utf-8", "User-Agent": "cli-proxy/agent"}, timeout=25)
            if not r.ok:
                return "(не удалось скачать контент)"
            text = (r.text or "").strip()
            if not text:
                return "(пустой контент)"
            # Сильно ограничиваем объем, чтобы не забивать контекст.
            return text[:1200]
        except Exception:
            return "(не удалось скачать контент)"
