from __future__ import annotations

import asyncio
import datetime as _dt
import logging
import os
import tempfile
from typing import Any, Dict

from agent.plugins.base import ToolPlugin
from agent.tooling.spec import ToolSpec


class GTTSTextToSpeechTool(ToolPlugin):
    def get_source_name(self) -> str:
        return "gTTS"

    def get_spec(self) -> ToolSpec:
        return ToolSpec(
            name="gtts_text_to_speech",
            description="Сгенерировать аудио (mp3) из текста через gTTS. Возвращает путь к файлу.",
            parameters={
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "Текст для озвучки"},
                    "lang": {"type": "string", "description": "Код языка (например, 'ru', 'en')", "default": "ru"},
                },
                "required": ["text"],
            },
            parallelizable=True,
            timeout_ms=90_000,
        )

    async def execute(self, args: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
        text = (args.get("text") or "").strip()
        if not text:
            return {"success": False, "error": "text обязателен"}
        lang = (args.get("lang") or "ru").strip() or "ru"

        try:
            path = await asyncio.to_thread(self._tts_sync, text, lang)
        except Exception as e:
            logging.exception(f"tool failed {str(e)}")
            return {"success": False, "error": f"gTTS failed: {e}"}

        return {"success": True, "output": path}

    def _tts_sync(self, text: str, lang: str) -> str:
        try:
            from gtts import gTTS  # type: ignore
        except Exception as e:
            raise RuntimeError("Пакет 'gTTS' не установлен") from e

        out_dir = os.path.join(tempfile.gettempdir(), "tts")
        os.makedirs(out_dir, exist_ok=True)
        out = os.path.join(out_dir, f"gtts_{_dt.datetime.now().timestamp():.0f}.mp3")
        tts = gTTS(text, lang=lang)
        tts.save(out)
        return out
