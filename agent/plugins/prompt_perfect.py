from __future__ import annotations

import logging
import os
from typing import Any, Dict, Optional

from openai import AsyncOpenAI

from agent.plugins.base import ToolPlugin
from agent.tooling.spec import ToolSpec


class PromptPerfectTool(ToolPlugin):
    def get_source_name(self) -> str:
        return "Prompt Perfect"

    def get_spec(self) -> ToolSpec:
        return ToolSpec(
            name="prompt_perfect",
            description="Оптимизировать пользовательский промпт для более точного ответа модели.",
            parameters={
                "type": "object",
                "properties": {
                    "original_prompt": {"type": "string", "description": "Исходный промпт пользователя"},
                    "context": {"type": "string", "description": "Дополнительный контекст (опционально)", "default": ""},
                },
                "required": ["original_prompt"],
            },
            parallelizable=True,
            timeout_ms=60_000,
        )

    async def execute(self, args: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
        original = (args.get("original_prompt") or "").strip()
        if not original:
            return {"success": False, "error": "original_prompt обязателен"}
        extra_ctx = (args.get("context") or "").strip()

        api_key = os.getenv("OPENAI_API_KEY") or getattr(getattr(self, "config", None), "defaults", None) and getattr(self.config.defaults, "openai_api_key", None)
        base_url = os.getenv("OPENAI_BASE_URL") or getattr(getattr(self, "config", None), "defaults", None) and getattr(self.config.defaults, "openai_base_url", None)
        model = os.getenv("OPENAI_MODEL") or getattr(getattr(self, "config", None), "defaults", None) and getattr(self.config.defaults, "openai_model", None) or "gpt-4o-mini"
        if not api_key:
            return {"success": False, "error": "Не задан OPENAI_API_KEY"}

        client = AsyncOpenAI(api_key=api_key, base_url=(base_url or None))

        system = (
            "Ты эксперт по составлению промптов. Преврати исходный текст в четкую, однозначную, "
            "полезную инструкцию для модели. Добавь формат вывода, ограничения и критерии качества, "
            "если это уместно. Не отвечай на сам запрос, верни только улучшенный промпт."
        )
        user = f"Исходный промпт:\n{original}\n\nКонтекст:\n{extra_ctx}\n\nУлучшенный промпт:"
        try:
            resp = await client.chat.completions.create(
                model=model,
                messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
                temperature=0.2,
                max_tokens=800,
            )
            optimized = (resp.choices[0].message.content or "").strip()
        except Exception as e:
            logging.exception(f"tool failed {str(e)}")
            return {"success": False, "error": f"Prompt optimization failed: {e}"}

        if not optimized:
            optimized = original
        return {"success": True, "output": optimized}
