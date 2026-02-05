from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Dict, List

import requests

from agent.plugins.base import ToolPlugin
from agent.tooling.spec import ToolSpec
from agent.tooling import helpers


class ChiefTool(ToolPlugin):
    def get_source_name(self) -> str:
        return "Edamam Recipes"

    def get_spec(self) -> ToolSpec:
        return ToolSpec(
            name="chief",
            description="Рецепты: поиск по ингредиентам/запросу через Edamam.",
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Запрос (ингредиенты/блюдо/ограничения)"},
                    "count": {"type": "integer", "description": "Сколько рецептов вернуть (1-10)", "default": 5},
                },
                "required": ["query"],
            },
            parallelizable=True,
            timeout_ms=60_000,
        )

    async def execute(self, args: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
        app_id = os.getenv("EDAMAM_APP_ID")
        app_key = os.getenv("EDAMAM_APP_KEY")
        if not app_id or not app_key:
            return {"success": False, "error": "Нужны EDAMAM_APP_ID и EDAMAM_APP_KEY в окружении"}

        query = (args.get("query") or "").strip()
        if not query:
            return {"success": False, "error": "query обязателен"}
        count = int(args.get("count") or 5)
        count = max(1, min(count, 10))

        try:
            out = await asyncio.to_thread(self._search_sync, app_id, app_key, query, count)
        except Exception as e:
            logging.exception(f"tool failed {str(e)}")
            return {"success": False, "error": f"Edamam failed: {e}"}

        return {"success": True, "output": out}

    def _search_sync(self, app_id: str, app_key: str, query: str, count: int) -> str:
        url = "https://api.edamam.com/api/recipes/v2"
        params = {
            "type": "public",
            "q": query,
            "app_id": app_id,
            "app_key": app_key,
            "random": "true",
        }
        r = requests.get(url, params=params, timeout=30)
        if not r.ok:
            raise RuntimeError(f"HTTP {r.status_code}: {r.text[:200]}")
        data = r.json() or {}
        hits = data.get("hits") or []
        if not hits:
            return "Рецепты не найдены."
        lines: List[str] = []
        for h in hits[:count]:
            rec = (h or {}).get("recipe") or {}
            label = rec.get("label") or ""
            link = rec.get("url") or ""
            source = rec.get("source") or ""
            lines.append(f"{label}\n{source}\n{link}".strip())
        return helpers._trim_output("\n\n".join(lines))
