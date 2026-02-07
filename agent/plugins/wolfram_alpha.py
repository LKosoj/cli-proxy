from __future__ import annotations

import asyncio
import os
import logging
from typing import Any, Dict

import requests

from agent.plugins.base import ToolPlugin
from agent.tooling.spec import ToolSpec


class WolframAlphaTool(ToolPlugin):
    def get_source_name(self) -> str:
        return "WolframAlpha"

    def get_spec(self) -> ToolSpec:
        return ToolSpec(
            name="wolfram_alpha",
            description="Ответ на вопрос через WolframAlpha. Ввод лучше давать на английском.",
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Запрос (желательно на английском)"},
                },
                "required": ["query"],
            },
            parallelizable=True,
            timeout_ms=60_000,
        )

    async def execute(self, args: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
        query = (args.get("query") or "").strip()
        if not query:
            return {"success": False, "error": "query обязателен"}
        app_id = os.getenv("WOLFRAM_APP_ID")
        if not app_id:
            return {"success": False, "error": "Не задан WOLFRAM_APP_ID в окружении"}

        try:
            text = await asyncio.to_thread(self._call_sync, app_id, query)
        except Exception as e:
            logging.exception(f"tool failed {str(e)}")
            return {"success": False, "error": f"WolframAlpha failed: {e}"}
        return {"success": True, "output": text or "Нет результата."}

    def _call_sync(self, app_id: str, query: str) -> str:
        # Быстрый endpoint возвращает plain text.
        url = "https://api.wolframalpha.com/v1/result"
        params = {"appid": app_id, "i": query}
        r = requests.get(url, params=params, timeout=30)
        if r.status_code == 501:
            return "WolframAlpha не смог ответить."
        if not r.ok:
            raise RuntimeError(f"HTTP {r.status_code}: {r.text[:200]}")
        return (r.text or "").strip()
