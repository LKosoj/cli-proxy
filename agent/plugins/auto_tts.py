from __future__ import annotations

import os
import tempfile
import logging
from typing import Any, Dict

from openai import AsyncOpenAI

from agent.plugins.base import ToolPlugin
from agent.tooling.spec import ToolSpec


class AutoTTSTool(ToolPlugin):
    def get_source_name(self) -> str:
        return "OpenAI TTS"

    def get_spec(self) -> ToolSpec:
        return ToolSpec(
            name="auto_tts",
            description="Сгенерировать аудио из текста через OpenAI TTS. Возвращает путь к файлу (mp3).",
            parameters={
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "Текст для озвучки"},
                    "voice": {
                        "type": "string",
                        "enum": ["alloy", "ash", "ballad", "coral", "echo", "sage", "shimmer", "verse", "marin", "cedar"],
                        "default": "alloy",
                    },
                    "model": {"type": "string", "description": "Модель TTS", "default": "gpt-4o-mini-tts"},
                    "response_format": {"type": "string", "enum": ["mp3", "opus", "aac", "flac", "wav", "pcm"], "default": "mp3"},
                    "instructions": {"type": "string", "description": "Инструкции для голоса (опционально)"},
                },
                "required": ["text"],
            },
            parallelizable=True,
            timeout_ms=120_000,
        )

    async def execute(self, args: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
        text = (args.get("text") or "").strip()
        if not text:
            return {"success": False, "error": "text обязателен"}
        voice = (args.get("voice") or "alloy").strip()
        model = (args.get("model") or "gpt-4o-mini-tts").strip()
        fmt = (args.get("response_format") or "mp3").strip()
        instructions = (args.get("instructions") or "").strip() or None

        api_key = os.getenv("OPENAI_API_KEY") or getattr(getattr(self, "config", None), "defaults", None) and getattr(self.config.defaults, "openai_api_key", None)
        base_url = os.getenv("OPENAI_BASE_URL") or getattr(getattr(self, "config", None), "defaults", None) and getattr(self.config.defaults, "openai_base_url", None)
        if not api_key:
            return {"success": False, "error": "Не задан OPENAI_API_KEY"}

        client = AsyncOpenAI(api_key=api_key, base_url=(base_url or None))
        out_dir = os.path.join(tempfile.gettempdir(), "tts")
        os.makedirs(out_dir, exist_ok=True)
        suffix = ".mp3" if fmt == "mp3" else f".{fmt}"
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix, dir=out_dir) as f:
            out_path = f.name

        try:
            audio = await client.audio.speech.create(
                model=model,
                voice=voice,
                input=text,
                response_format=fmt,
                instructions=instructions,
            )
            audio.write_to_file(out_path)
        except Exception as e:
            logging.exception(f"tool failed {str(e)}")
            return {"success": False, "error": f"OpenAI TTS failed: {e}"}

        return {"success": True, "output": out_path}
