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


def summarize_text(text: str, max_chars: int = 1200, config: Optional[AppConfig] = None) -> Optional[str]:
    cfg = _get_openai_config(config)
    if not cfg:
        return None
    api_key, model, base_url = cfg

    payload = {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": "Сделай краткое резюме на русском, 2-5 пунктов.",
            },
            {"role": "user", "content": text[:8000]},
        ],
        "max_tokens": 350,
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
