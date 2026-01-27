import os
import requests
from typing import Optional

from config import AppConfig


def _get_openai_config(config: Optional[AppConfig] = None):
    api_key = os.getenv("OPENAI_API_KEY")
    model = os.getenv("OPENAI_MODEL")
    base_url = os.getenv("OPENAI_BASE_URL")
    if config:
        api_key = api_key or config.defaults.openai_api_key
        model = model or config.defaults.openai_model
        base_url = base_url or config.defaults.openai_base_url
    if not base_url:
        base_url = "https://api.openai.com"
    if not api_key or not model:
        return None
    return api_key, model, base_url.rstrip("/")


def _length_bucket(text_len: int) -> str:
    if text_len < 2000:
        return "короткий"
    if text_len < 12000:
        return "средний"
    return "длинный"


def _suggest_max_tokens(text: str, max_chars: int) -> int:
    # Aim for a summary that can fit within max_chars without hard truncation.
    # Roughly 4 chars per token for Russian; clamp to keep responses concise.
    rough = max(200, min(1200, max_chars // 4))
    # Allow more tokens for larger inputs, but keep an upper bound.
    size_hint = max(0, len(text) - 2000) // 8000
    return min(1200, rough + size_hint * 100)


def summarize_text(text: str, max_chars: int = 3000, config: Optional[AppConfig] = None) -> Optional[str]:
    cfg = _get_openai_config(config)
    if not cfg:
        return None
    api_key, model, base_url = cfg

    payload = {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": (
                    "Сделай резюме на русском. Дай по делу, без воды. "
                    "Адаптируй длину под объём текста: "
                    "короткий → 2–4 пункта, средний → 4–6, длинный → 6–10. "
                    "В каждом пункте 1–2 предложения. Не повторяйся и не пиши лишнего."
                ),
            },
            {
                "role": "user",
                "content": (
                    f"Длина текста: {_length_bucket(len(text))}.\n"
                    f"{text[:12000]}"
                ),
            },
        ],
        "max_tokens": _suggest_max_tokens(text, max_chars),
        "temperature": 0.2,
    }

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    resp = requests.post(
        f"{base_url}/v1/chat/completions", json=payload, headers=headers, timeout=20
    )
    resp.raise_for_status()
    data = resp.json()
    summary = data["choices"][0]["message"]["content"].strip()
    if len(summary) > max_chars:
        return summary[:max_chars]
    return summary
