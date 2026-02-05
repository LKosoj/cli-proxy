from __future__ import annotations

import asyncio
import os
import logging
from typing import Any, Dict

import requests

from agent.plugins.base import ToolPlugin
from agent.tooling.spec import ToolSpec


class DeeplTool(ToolPlugin):
    def get_source_name(self) -> str:
        return "DeepL Translate"

    def get_spec(self) -> ToolSpec:
        return ToolSpec(
            name="deepl",
            description="Перевод текста через DeepL.",
            parameters={
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "Текст для перевода"},
                    "to_language": {"type": "string", "description": "Язык назначения (например, 'EN', 'RU', 'DE')"},
                },
                "required": ["text", "to_language"],
            },
            parallelizable=True,
            timeout_ms=60_000,
        )

    async def execute(self, args: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
        api_key = os.getenv("DEEPL_API_KEY")
        if not api_key:
            return {"success": False, "error": "Не задан DEEPL_API_KEY в окружении"}
        text = (args.get("text") or "").strip()
        to_language = (args.get("to_language") or "").strip()
        if not text or not to_language:
            return {"success": False, "error": "text и to_language обязательны"}

        try:
            translated = await asyncio.to_thread(self._translate_sync, api_key, text, to_language)
        except Exception as e:
            logging.exception(f"tool failed {str(e)}")
            return {"success": False, "error": f"DeepL failed: {e}"}

        return {"success": True, "output": translated}

    def _translate_sync(self, api_key: str, text: str, to_language: str) -> str:
        url = "https://api-free.deepl.com/v2/translate" if api_key.endswith(":fx") else "https://api.deepl.com/v2/translate"
        headers = {
            "Authorization": f"DeepL-Auth-Key {api_key}",
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        }
        data = {"text": text, "target_lang": to_language}
        r = requests.post(url, headers=headers, data=data, timeout=30)
        if not r.ok:
            raise RuntimeError(f"HTTP {r.status_code}: {r.text[:200]}")
        j = r.json()
        translations = j.get("translations") or []
        if not translations:
            return ""
        return str(translations[0].get("text") or "").strip()
