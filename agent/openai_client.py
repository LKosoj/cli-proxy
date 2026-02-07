from __future__ import annotations

from typing import Any, Optional, Tuple

from openai import AsyncOpenAI

from config import AppConfig

OPENAI_SDK_MAX_RETRIES = 4


def create_async_openai_client(
    api_key: str,
    base_url: Optional[str] = None,
    *,
    timeout: Optional[Any] = None,
) -> AsyncOpenAI:
    kwargs: dict[str, Any] = {
        "api_key": api_key,
        "max_retries": OPENAI_SDK_MAX_RETRIES,
    }
    if base_url:
        kwargs["base_url"] = base_url
    if timeout is not None:
        kwargs["timeout"] = timeout
    return AsyncOpenAI(**kwargs)


def get_openai_config(config: AppConfig) -> Optional[Tuple[str, str, str]]:
    api_key = config.defaults.openai_api_key
    model = config.defaults.openai_model
    base_url = config.defaults.openai_base_url or "https://api.openai.com"
    if not api_key or not model:
        return None
    return api_key, model, base_url.rstrip("/")


def build_client(config: AppConfig) -> Optional[Tuple[AsyncOpenAI, str]]:
    cfg = get_openai_config(config)
    if not cfg:
        return None
    api_key, model, base_url = cfg
    client = create_async_openai_client(api_key=api_key, base_url=base_url)
    return client, model


async def chat_completion(config: AppConfig, system: str, user: str, response_format=None) -> str:
    client_info = build_client(config)
    if not client_info:
        return ""
    client, model = client_info
    resp = await client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        temperature=0.2,
        response_format=response_format
    )
    content = resp.choices[0].message.content if resp.choices else ""
    return (content or "").strip()
