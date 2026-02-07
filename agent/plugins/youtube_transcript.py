from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List

from agent.plugins.base import ToolPlugin
from agent.tooling.spec import ToolSpec
from agent.tooling import helpers


class YouTubeTranscriptTool(ToolPlugin):
    def get_source_name(self) -> str:
        return "YouTube Transcript"

    def get_spec(self) -> ToolSpec:
        return ToolSpec(
            name="youtube_transcript",
            description="Получить текст субтитров YouTube по video_id.",
            parameters={
                "type": "object",
                "properties": {
                    "video_id": {"type": "string", "description": "YouTube video id (например, dQw4w9WgXcQ)"},
                    "languages": {"type": "array", "description": "Приоритет языков (например, ['ru','en'])"},
                },
                "required": ["video_id"],
            },
            parallelizable=True,
            timeout_ms=60_000,
        )

    async def execute(self, args: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
        video_id = (args.get("video_id") or "").strip()
        if not video_id:
            return {"success": False, "error": "video_id обязателен"}
        languages = args.get("languages") or ["ru", "en"]
        if not isinstance(languages, list) or not languages:
            languages = ["ru", "en"]

        try:
            text = await asyncio.to_thread(self._fetch_sync, video_id, languages)
        except Exception as e:
            logging.exception(f"tool failed {str(e)}")
            return {"success": False, "error": f"Transcript failed: {e}"}

        return {"success": True, "output": helpers._trim_output(text)}

    def _fetch_sync(self, video_id: str, languages: List[str]) -> str:
        try:
            from youtube_transcript_api import YouTubeTranscriptApi  # type: ignore
        except Exception as e:
            raise RuntimeError("Пакет 'youtube-transcript-api' не установлен") from e

        api = YouTubeTranscriptApi()
        transcript_list = api.list(video_id)
        tr = None
        try:
            tr = transcript_list.find_manually_created_transcript(languages)
        except Exception:
            tr = None
        if tr is None:
            try:
                tr = transcript_list.find_generated_transcript(languages)
            except Exception:
                tr = None
        if tr is None:
            try:
                tr = transcript_list.find_transcript(languages)
            except Exception:
                tr = None
        if tr is None:
            try:
                tr = next(iter(transcript_list))
            except StopIteration:
                return "Транскрипт не найден."

        fetched = tr.fetch()
        parts: List[str] = []
        for snippet in fetched:
            parts.append(getattr(snippet, "text", "") or "")
        lang_info = f"Язык: {getattr(tr, 'language', '')}, " + ("Автоген" if getattr(tr, "is_generated", False) else "Ручной")
        return f"{lang_info}\n\n" + " ".join([p.strip() for p in parts if p.strip()])
