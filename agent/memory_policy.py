from __future__ import annotations

import json
from typing import Optional, Tuple

import requests

from config import AppConfig


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


def _get_openai_config(config: AppConfig) -> Optional[Tuple[str, str, str]]:
    api_key = config.defaults.openai_api_key
    model = config.defaults.openai_model
    base_url = config.defaults.openai_base_url or "https://api.openai.com"
    if not api_key or not model:
        return None
    return api_key, model, base_url.rstrip("/")


def _call_llm(config: AppConfig, system: str, user: str) -> str:
    cfg = _get_openai_config(config)
    if not cfg:
        return ""
    api_key, model, base_url = cfg
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
    payload = {"model": model, "messages": messages, "temperature": 0.2}
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    resp = requests.post(f"{base_url}/v1/chat/completions", json=payload, headers=headers, timeout=60)
    resp.raise_for_status()
    data = resp.json()
    return (data["choices"][0]["message"].get("content") or "").strip()


def decide_memory_save(
    config: AppConfig, user_text: str, final_response: str, memory_text: str
) -> Optional[Tuple[str, str]]:
    prompt = (
        f"Текущая память:\n{memory_text}\n\n"
        f"Запрос пользователя:\n{user_text}\n\n"
        f"Итоговый ответ:\n{final_response}\n\n"
        "Нужно ли сохранять что-то новое?"
    )
    raw = _call_llm(config, _DECIDER_SYSTEM, prompt)
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


def compress_memory(config: AppConfig, memory_text: str, max_chars: int) -> Optional[str]:
    if not memory_text:
        return ""
    prompt = f"Лимит: {max_chars} символов.\n\nПамять:\n{memory_text}"
    raw = _call_llm(config, _COMPRESS_SYSTEM, prompt)
    return raw.strip() if raw else None
