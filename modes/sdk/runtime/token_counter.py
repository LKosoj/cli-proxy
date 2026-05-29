"""Token counting with tiktoken and character-based fallback."""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List

_log = logging.getLogger(__name__)

_encoder_cache: Dict[str, Any] = {}

# Models that use cl100k_base encoding.
_CL100K_MODELS = frozenset({
    "gpt-4", "gpt-4-turbo", "gpt-4o", "gpt-4o-mini",
    "gpt-4-0125-preview", "gpt-4-1106-preview",
    "gpt-3.5-turbo", "gpt-3.5-turbo-16k",
})

CHARS_PER_TOKEN_FALLBACK = 2.5
MESSAGE_OVERHEAD_TOKENS = 4  # per-message overhead in chat format


def _get_encoder(model: str) -> Any:
    """Get tiktoken encoder, with caching. Returns None if tiktoken unavailable."""
    if model in _encoder_cache:
        return _encoder_cache[model]
    try:
        import tiktoken
        try:
            enc = tiktoken.encoding_for_model(model)
        except KeyError:
            enc = tiktoken.get_encoding("cl100k_base")
        _encoder_cache[model] = enc
        return enc
    except ImportError:
        _log.debug("tiktoken not installed, using character fallback")
        _encoder_cache[model] = None
        return None
    except Exception:
        _log.exception("tiktoken init failed for model=%s", model)
        _encoder_cache[model] = None
        return None


def count_tokens(text: str, model: str = "gpt-4o") -> int:
    """Count tokens in a text string."""
    if not text:
        return 0
    enc = _get_encoder(model)
    if enc is not None:
        return len(enc.encode(text))
    return int(len(text) / CHARS_PER_TOKEN_FALLBACK)


def count_messages_tokens(messages: List[Dict[str, Any]], model: str = "gpt-4o") -> int:
    """Count total tokens for a list of chat messages (OpenAI format)."""
    total = 0
    for msg in messages:
        total += MESSAGE_OVERHEAD_TOKENS
        content = msg.get("content")
        if isinstance(content, str):
            total += count_tokens(content, model)
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict):
                    text = part.get("text") or ""
                    if text:
                        total += count_tokens(str(text), model)
        # Count tool call arguments.
        tool_calls = msg.get("tool_calls")
        if isinstance(tool_calls, list):
            for tc in tool_calls:
                func = tc.get("function") or {}
                name = func.get("name") or ""
                args = func.get("arguments") or ""
                total += count_tokens(name, model)
                total += count_tokens(str(args), model)
    total += 2  # reply priming tokens
    return total


def estimate_tools_tokens(tools: List[Dict[str, Any]], model: str = "gpt-4o") -> int:
    """Estimate token count for tool/function definitions."""
    if not tools:
        return 0
    try:
        text = json.dumps(tools, ensure_ascii=False)
    except Exception:
        text = str(tools)
    return count_tokens(text, model)
