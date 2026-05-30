from __future__ import annotations

import json
import logging
import os
import re
from typing import Any, Callable, Optional, Tuple

from openai import AsyncOpenAI

from config import AppConfig
from .json_normalizer import loads_safe

OPENAI_SDK_MAX_RETRIES = 4
CHAT_COMPLETION_ATTEMPTS = 3

logger = logging.getLogger(__name__)
_JSON_BLOCK_RE = re.compile(r"```(?:json)?\s*\n?(.*?)\n?\s*```", re.DOTALL | re.IGNORECASE)


def _requires_json_response(response_format: Any) -> bool:
    if not isinstance(response_format, dict):
        return False
    return str(response_format.get("type") or "").strip().lower() in {"json_object", "json_schema"}


def _requires_json_object(response_format: Any) -> bool:
    if not isinstance(response_format, dict):
        return False
    return str(response_format.get("type") or "").strip().lower() == "json_object"


def _extract_json_payload(raw: str) -> str:
    s = (raw or "").strip()
    if not s:
        return s
    m = _JSON_BLOCK_RE.search(s)
    if m:
        inner = (m.group(1) or "").strip()
        if inner:
            return inner
    if s.startswith("{") or s.startswith("["):
        return s
    obj_start = s.find("{")
    obj_end = s.rfind("}")
    if obj_start >= 0 and obj_end > obj_start:
        return s[obj_start: obj_end + 1]
    arr_start = s.find("[")
    arr_end = s.rfind("]")
    if arr_start >= 0 and arr_end > arr_start:
        return s[arr_start: arr_end + 1]
    return s


def _normalize_response_content(content: str, response_format: Any) -> str:
    text = (content or "").strip()
    if not _requires_json_response(response_format):
        return text
    payload_text = _extract_json_payload(text)
    payload = loads_safe(payload_text, strict_first=False)
    if _requires_json_object(response_format) and not isinstance(payload, dict):
        raise ValueError("LLM returned non-object JSON for response_format=json_object")
    return json.dumps(payload, ensure_ascii=False)


async def _normalize_response_content_with_flags(config: AppConfig, content: str, response_format: Any) -> str:
    _ = config
    text = (content or "").strip()
    if not _requires_json_response(response_format):
        return text

    payload_text = _extract_json_payload(text)
    payload = loads_safe(payload_text, strict_first=False)
    if _requires_json_object(response_format) and not isinstance(payload, dict):
        raise ValueError("LLM returned non-object JSON for response_format=json_object")
    return json.dumps(payload, ensure_ascii=False)


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


_MODEL_ENV_VARS: dict[str, str] = {
    "openai_model": "OPENAI_MODEL",
    "openai_big_model": "OPENAI_BIG_MODEL",
}


def resolve_openai_config(
    config: Optional[Any] = None,
    *,
    model_key: str = "openai_model",
    env_priority: bool = True,
) -> Optional[Tuple[str, str, str]]:
    """Return (api_key, model, base_url) merging config.defaults and environment variables.

    When *env_priority* is True the environment overrides config (agent_core behaviour).
    When *env_priority* is False config takes precedence, env is used only as fallback (summary behaviour).
    """
    cfg_api_key: Optional[str] = None
    cfg_model: Optional[str] = None
    cfg_base_url: Optional[str] = None
    if config is not None:
        defaults = getattr(config, "defaults", None)
        if defaults is not None:
            cfg_api_key = getattr(defaults, "openai_api_key", None) or None
            cfg_model = getattr(defaults, model_key, None) or None
            cfg_base_url = getattr(defaults, "openai_base_url", None) or None

    env_api_key: Optional[str] = os.getenv("OPENAI_API_KEY") or None
    model_env_var = _MODEL_ENV_VARS.get(model_key, "OPENAI_MODEL")
    env_model: Optional[str] = os.getenv(model_env_var) or None
    env_base_url: Optional[str] = os.getenv("OPENAI_BASE_URL") or None

    if env_priority:
        api_key = env_api_key or cfg_api_key
        model = env_model or cfg_model
        base_url = env_base_url or cfg_base_url
    else:
        api_key = cfg_api_key or env_api_key
        model = cfg_model or env_model
        base_url = cfg_base_url or env_base_url

    if not base_url:
        base_url = "https://api.openai.com"
    if not api_key or not model:
        return None
    return api_key, model, base_url.rstrip("/")


def get_openai_config(config: AppConfig) -> Optional[Tuple[str, str, str]]:
    # Сохраняем историческое поведение build_client: только config.defaults, без env-fallback.
    # (Дедупликация env-логики из M8 применяется к agent_core/summary через resolve_openai_config.)
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


def _build_client_for_request(
    config: AppConfig,
    *,
    model_override: Optional[str] = None,
    client_factory: Optional[Callable[[], Any]] = None,
) -> Optional[Tuple[Any, str]]:
    chosen_model = str(model_override or "").strip()
    if callable(client_factory):
        cfg = get_openai_config(config)
        if not cfg and not chosen_model:
            return None
        if not chosen_model and cfg:
            _, chosen_model, _ = cfg
        if not chosen_model:
            return None
        return client_factory(), chosen_model

    if not chosen_model:
        return build_client(config)

    defaults = getattr(config, "defaults", None)
    api_key = getattr(defaults, "openai_api_key", None) if defaults else None
    base_url = (getattr(defaults, "openai_base_url", None) if defaults else None) or "https://api.openai.com"
    if not api_key:
        return None
    client = create_async_openai_client(api_key=api_key, base_url=base_url.rstrip("/"))
    return client, chosen_model


async def chat_completion(
    config: AppConfig,
    system: str,
    user: str,
    response_format=None,
    *,
    model: Optional[str] = None,
    temperature: float = 0.2,
    max_tokens: Optional[int] = None,
    client_factory: Optional[Callable[[], Any]] = None,
    normalize_error_handler: Optional[Callable[[str, Exception], Optional[str]]] = None,
) -> str:
    client_info = _build_client_for_request(
        config,
        model_override=model,
        client_factory=client_factory,
    )
    if not client_info:
        return ""
    client, resolved_model = client_info
    for attempt in range(1, CHAT_COMPLETION_ATTEMPTS + 1):
        try:
            kwargs: dict[str, Any] = {
                "model": resolved_model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "temperature": temperature,
                "response_format": response_format,
            }
            if max_tokens is not None:
                kwargs["max_tokens"] = max_tokens
            resp = await client.chat.completions.create(**kwargs)
            content = resp.choices[0].message.content if resp.choices else ""
            try:
                return await _normalize_response_content_with_flags(config, content or "", response_format)
            except Exception as exc:
                if callable(normalize_error_handler):
                    recovered = normalize_error_handler(content or "", exc)
                    if recovered is not None:
                        return recovered
                raise
        except Exception:
            logger.exception(
                "chat_completion failed (attempt %d/%d)",
                attempt,
                CHAT_COMPLETION_ATTEMPTS,
            )
            if attempt >= CHAT_COMPLETION_ATTEMPTS:
                raise
    return ""


chat_completion._supports_strict_json_contract = True
