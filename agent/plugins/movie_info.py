from __future__ import annotations

import asyncio
import os
import logging
from typing import Any, Dict, List, Optional

import requests

from agent.plugins.base import ToolPlugin
from agent.tooling.spec import ToolSpec
from agent.tooling import helpers


class MovieInfoTool(ToolPlugin):
    TMDB_BASE_URL = "https://api.themoviedb.org/3"

    def get_source_name(self) -> str:
        return "TMDb"

    def get_spec(self) -> ToolSpec:
        return ToolSpec(
            name="movie_info",
            description="Информация о фильмах через TMDb: сейчас в прокате и поиск по discover.",
            parameters={
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["now_playing", "discover"]},
                    "genre_id": {"type": "integer", "description": "Фильтр по жанру (TMDb genre id)"},
                    "count": {"type": "integer", "description": "Сколько фильмов вернуть (1-30)", "default": 10},
                    "language": {"type": "string", "description": "Язык TMDb (например, ru-RU)", "default": "ru-RU"},
                    "region": {"type": "string", "description": "Регион (например, RU)", "default": "RU"},
                },
                "required": ["action"],
            },
            parallelizable=True,
            timeout_ms=60_000,
        )

    async def execute(self, args: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
        key = os.getenv("TMDB_API_KEY")
        if not key:
            return {"success": False, "error": "Не задан TMDB_API_KEY в окружении"}
        action = args.get("action")
        count = int(args.get("count") or 10)
        count = max(1, min(count, 30))
        language = (args.get("language") or "ru-RU").strip() or "ru-RU"
        region = (args.get("region") or "RU").strip() or "RU"
        genre_id = args.get("genre_id")
        if genre_id is not None:
            try:
                genre_id = int(genre_id)
            except Exception:
                genre_id = None

        try:
            movies = await asyncio.to_thread(self._fetch_sync, key, action, language, region, genre_id)
        except Exception as e:
            logging.exception(f"tool failed {str(e)}")
            return {"success": False, "error": f"TMDb failed: {e}"}

        if not movies:
            return {"success": True, "output": "Ничего не найдено."}

        lines: List[str] = []
        for m in movies[:count]:
            title = m.get("title") or m.get("name") or ""
            date = m.get("release_date") or ""
            rating = m.get("vote_average")
            overview = (m.get("overview") or "").strip()
            if overview:
                overview = overview.replace("\n", " ")
                overview = overview[:200] + ("..." if len(overview) > 200 else "")
            lines.append(f"{title} ({date}) рейтинг {rating}\n{overview}".strip())
        return {"success": True, "output": helpers._trim_output("\n\n".join(lines))}

    def _fetch_sync(
        self,
        key: str,
        action: str,
        language: str,
        region: str,
        genre_id: Optional[int],
    ) -> List[Dict[str, Any]]:
        if action == "now_playing":
            path = "/movie/now_playing"
            params: Dict[str, Any] = {"api_key": key, "language": language, "region": region}
        elif action == "discover":
            path = "/discover/movie"
            params = {
                "api_key": key,
                "language": language,
                "region": region,
                "sort_by": "popularity.desc",
                "include_adult": "false",
                "include_video": "false",
            }
            if genre_id:
                params["with_genres"] = genre_id
        else:
            raise ValueError(f"Unknown action: {action}")

        r = requests.get(self.TMDB_BASE_URL + path, params=params, timeout=30)
        if not r.ok:
            raise RuntimeError(f"HTTP {r.status_code}: {r.text[:200]}")
        return (r.json() or {}).get("results") or []
