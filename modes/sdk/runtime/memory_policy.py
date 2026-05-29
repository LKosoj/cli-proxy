from __future__ import annotations

from typing import Any, Dict, Optional

from config import AppConfig
from .json_normalizer import loads_safe
from .openai_client import chat_completion


_DECIDER_SYSTEM = """Ты принимаешь решение, что сохранить в долговременную память проекта.
Сохраняй только устойчивые и полезные факты.
Используй категории:
- preference, decision, config, agreement (слой semantic, без TTL)
- task_state (слой task_state, TTL 14 дней)
Приоритет категорий: preference (самый важный) > decision > config > agreement > task_state.
Не сохраняй личные данные, чувствительную информацию и временные детали.
Запись должна быть атомарной: одно короткое утверждение, без списков и переносов строк.
Верни строго JSON:
{
  "save": true/false,
  "category": "preference|decision|config|agreement|task_state",
  "layer": "semantic|task_state",
  "content": "короткая запись (1-2 предложения)",
  "source": "agent|user|system",
  "confidence": 0.0-1.0,
  "ttl_days": 14
}
"""


_COMPRESS_SYSTEM = """Ты сжимаешь память проекта, чтобы она помещалась в заданный лимит.
Приоритет сохранения: preference > decision > config > agreement.
Удаляй повторы и мелкие детали. Сохраняй только устойчивые факты.
Сохраняй формат строк: "- YYYY-MM-DD HH:MM: [TAG] текст".
Верни только сжатый текст памяти без JSON."""


async def decide_memory_save(
    config: AppConfig, user_text: str, final_response: str, memory_text: str
) -> Optional[Dict[str, Any]]:
    prompt = (
        f"Текущая память:\n{memory_text}\n\n"
        f"Запрос пользователя:\n{user_text}\n\n"
        f"Итоговый ответ:\n{final_response}\n\n"
        "Нужно ли сохранять что-то новое?"
    )
    raw = await chat_completion(config, _DECIDER_SYSTEM, prompt, response_format={"type": "json_object"})
    if not raw:
        return None
    try:
        payload = loads_safe(raw, strict_first=False)
    except Exception:
        return None
    if not payload.get("save"):
        return None
    content = (payload.get("content") or "").strip()
    category = (payload.get("category") or "").strip().lower()
    layer = (payload.get("layer") or "").strip().lower()
    source = (payload.get("source") or "").strip().lower() or "agent"
    confidence_raw = payload.get("confidence")
    ttl_days_raw = payload.get("ttl_days")
    if not content:
        return None
    if category not in ("preference", "decision", "config", "agreement", "task_state"):
        return None
    if category == "task_state":
        layer = "task_state"
    if layer not in ("semantic", "task_state"):
        layer = "semantic"
    try:
        confidence = float(confidence_raw)
    except Exception:
        confidence = 0.8 if layer == "semantic" else 0.6
    confidence = max(0.0, min(1.0, confidence))
    try:
        ttl_days = int(ttl_days_raw) if ttl_days_raw is not None else None
    except Exception:
        ttl_days = None
    if layer == "task_state" and (ttl_days is None or ttl_days <= 0):
        ttl_days = 14
    if layer != "task_state":
        ttl_days = None
    tag = {
        "preference": "PREF",
        "decision": "DECISION",
        "config": "CONFIG",
        "agreement": "AGREEMENT",
        "task_state": "TASK",
    }[category]
    return {
        "tag": tag,
        "content": content,
        "layer": layer,
        "source": source if source in ("agent", "user", "system") else "agent",
        "confidence": confidence,
        "ttl_days": ttl_days,
    }


async def compress_memory(config: AppConfig, memory_text: str, max_chars: int) -> Optional[str]:
    if not memory_text:
        return ""
    prompt = f"Лимит: {max_chars} символов.\n\nПамять:\n{memory_text}"
    raw = await chat_completion(config, _COMPRESS_SYSTEM, prompt)
    return raw.strip() if raw else None
