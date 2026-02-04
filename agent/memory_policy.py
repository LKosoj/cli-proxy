from __future__ import annotations

import json
from typing import Optional, Tuple

from config import AppConfig
from .openai_client import chat_completion


_DECIDER_SYSTEM = """Ты принимаешь решение, что сохранить в долговременную память проекта.
Сохраняй только устойчивые и полезные факты в категориях: preference, decision, config, agreement.
Приоритет категорий: preference (самый важный) > decision > config > agreement.
Не сохраняй личные данные, чувствительную информацию и временные детали.
Верни строго JSON:
{"save": true/false, "category": "preference|decision|config|agreement", "content": "короткая запись (1-2 предложения)"}
"""


_COMPRESS_SYSTEM = """Ты сжимаешь память проекта, чтобы она помещалась в заданный лимит.
Приоритет сохранения: preference > decision > config > agreement.
Удаляй повторы и мелкие детали. Сохраняй только устойчивые факты.
Сохраняй формат строк: "- YYYY-MM-DD HH:MM: [TAG] текст".
Верни только сжатый текст памяти без JSON."""


async def decide_memory_save(
    config: AppConfig, user_text: str, final_response: str, memory_text: str
) -> Optional[Tuple[str, str]]:
    prompt = (
        f"Текущая память:\n{memory_text}\n\n"
        f"Запрос пользователя:\n{user_text}\n\n"
        f"Итоговый ответ:\n{final_response}\n\n"
        "Нужно ли сохранять что-то новое?"
    )
    raw = await chat_completion(config, _DECIDER_SYSTEM, prompt)
    if not raw:
        return None
    try:
        payload = json.loads(raw)
    except Exception:
        return None
    if not payload.get("save"):
        return None
    content = (payload.get("content") or "").strip()
    category = (payload.get("category") or "").strip().lower()
    if not content:
        return None
    if category not in ("preference", "decision", "config", "agreement"):
        return None
    tag = {
        "preference": "PREF",
        "decision": "DECISION",
        "config": "CONFIG",
        "agreement": "AGREEMENT",
    }[category]
    return tag, content


async def compress_memory(config: AppConfig, memory_text: str, max_chars: int) -> Optional[str]:
    if not memory_text:
        return ""
    prompt = f"Лимит: {max_chars} символов.\n\nПамять:\n{memory_text}"
    raw = await chat_completion(config, _COMPRESS_SYSTEM, prompt)
    return raw.strip() if raw else None
