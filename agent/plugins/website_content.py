from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict

import requests

from agent.plugins.base import ToolPlugin
from agent.tooling.spec import ToolSpec
from agent.tooling import helpers


class WebsiteContentTool(ToolPlugin):
    def get_source_name(self) -> str:
        return "Website Content"

    def get_spec(self) -> ToolSpec:
        return ToolSpec(
            name="website_content",
            description="Скачать и очистить основной текст страницы по URL. Использует r.jina.ai как ридер.",
            parameters={
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "URL страницы"},
                    "max_chars": {"type": "integer", "description": "Ограничение по длине ответа", "default": 6000},
                },
                "required": ["url"],
            },
            parallelizable=True,
            timeout_ms=60_000,
        )

    async def execute(self, args: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
        url = (args.get("url") or "").strip()
        if not url:
            return {"success": False, "error": "url обязателен"}
        max_chars = int(args.get("max_chars") or 6000)
        max_chars = max(500, min(max_chars, 20000))

        try:
            text = await asyncio.to_thread(self._fetch_sync, url)
        except Exception as e:
            logging.exception(f"tool failed {str(e)}")
            return {"success": False, "error": f"Fetch failed: {e}"}

        text = (text or "").strip()
        if not text:
            return {"success": True, "output": "Пустой ответ."}
        text = helpers._trim_output(text[:max_chars])
        return {"success": True, "output": text}

    def _fetch_sync(self, url: str) -> str:
        # Jina Reader умеет вытаскивать основной контент без тяжелых зависимостей.
        reader_url = f"https://r.jina.ai/{url}"
        r = requests.get(
            reader_url,
            headers={"Accept": "text/plain; charset=utf-8", "User-Agent": "cli-proxy/agent"},
            timeout=30,
        )
        if r.ok and (r.text or "").strip():
            return r.text

        # Fallback: обычный GET (может вернуть HTML, но иногда лучше чем ничего).
        r2 = requests.get(url, headers={"User-Agent": "cli-proxy/agent"}, timeout=30)
        if not r2.ok:
            raise RuntimeError(f"HTTP {r2.status_code}")
        return r2.text or ""
