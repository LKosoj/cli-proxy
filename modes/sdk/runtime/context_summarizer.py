"""Context compression for long-running ReAct sessions.

Preserves user messages verbatim and prefers compressing the growing assistant/tool
working tail first. Falls back to whole-message summarization only when needed.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Sequence, Tuple

from modes.sdk.runtime.token_counter import count_messages_tokens

_log = logging.getLogger(__name__)

PRESERVE_LAST_N_EXCHANGES = 4
PRESERVE_LAST_N_WORKING_MESSAGES = 8
MIN_MESSAGES_FOR_SUMMARIZATION = 10
MIN_WORKING_MESSAGES_FOR_SUMMARIZATION = 12
MAX_CHUNK_CHARS = 24_000
MAX_CHUNK_SUMMARY_TOKENS = 8000


async def summarize_context(
    messages: List[Dict[str, Any]],
    *,
    config: Any,
    model: str = "gpt-4o",
    max_tokens: int = 128_000,
    reserve_tokens: int = 4096,
    threshold: float = 0.75,
) -> Tuple[List[Dict[str, Any]], bool]:
    """Summarize older conversation exchanges when context is too large."""
    if len(messages) < MIN_MESSAGES_FOR_SUMMARIZATION:
        return messages, False

    current_tokens = count_messages_tokens(messages, model)
    limit = int(max_tokens * threshold)
    if current_tokens <= limit:
        return messages, False

    _log.info(
        "Context summarization triggered: %d tokens > %d limit (%.0f%% of %d)",
        current_tokens,
        limit,
        threshold * 100,
        max_tokens,
    )

    system_msgs: List[Dict[str, Any]] = []
    body_start = 0
    for body_start, msg in enumerate(messages):
        if msg.get("role") == "system":
            system_msgs.append(msg)
            continue
        break
    body = list(messages[body_start:])
    user_indices = [i for i, msg in enumerate(body) if msg.get("role") == "user"]
    if len(user_indices) <= PRESERVE_LAST_N_EXCHANGES:
        return messages, False

    tail_start = user_indices[-PRESERVE_LAST_N_EXCHANGES]
    to_summarize = body[:tail_start]
    tail = body[tail_start:]
    if not to_summarize:
        return messages, False

    summary_text = await _summarize_messages(to_summarize, config)
    if not summary_text:
        return messages, False

    summary_msg = {"role": "assistant", "content": f"[Контекст суммаризирован]\n{summary_text}"}
    result = system_msgs + [summary_msg] + tail
    new_tokens = count_messages_tokens(result, model)
    _log.info(
        "Context summarized: %d -> %d tokens (saved %d)",
        current_tokens,
        new_tokens,
        current_tokens - new_tokens,
    )
    return result, True


async def summarize_working_context(
    working: List[Dict[str, Any]],
    *,
    base_messages: Sequence[Dict[str, Any]],
    config: Any,
    model: str = "gpt-4o",
    max_tokens: int = 128_000,
    reserve_tokens: int = 4096,
    threshold: float = 0.75,
) -> Tuple[List[Dict[str, Any]], bool]:
    """Compress the ReAct working tail and return a replacement working list."""
    if len(working) < MIN_WORKING_MESSAGES_FOR_SUMMARIZATION:
        return working, False

    current_messages = list(base_messages) + list(working)
    current_tokens = count_messages_tokens(current_messages, model)
    limit = int(max_tokens * threshold)
    if current_tokens <= limit:
        return working, False

    summarizable_count = len(working) - PRESERVE_LAST_N_WORKING_MESSAGES
    if summarizable_count <= 0:
        return working, False

    head = working[:summarizable_count]
    tail = working[summarizable_count:]
    summary_text = await _summarize_messages(head, config)
    if not summary_text:
        return working, False

    summarized_working = [
        {
            "role": "assistant",
            "content": f"[Суммаризация рабочего контекста]\n{summary_text}",
        }
    ] + tail
    new_tokens = count_messages_tokens(list(base_messages) + summarized_working, model)
    _log.info(
        "Working context summarized: %d -> %d tokens (saved %d, working %d -> %d messages)",
        current_tokens,
        new_tokens,
        current_tokens - new_tokens,
        len(working),
        len(summarized_working),
    )
    return summarized_working, True


def _message_to_text(message: Dict[str, Any]) -> str:
    role = str(message.get("role") or "unknown").strip() or "unknown"
    parts: List[str] = [f"[{role}]"]
    if role == "assistant":
        tool_calls = message.get("tool_calls") or []
        if isinstance(tool_calls, list) and tool_calls:
            names = [
                str(item.get("function", {}).get("name") or "?").strip()
                for item in tool_calls
                if isinstance(item, dict)
            ]
            names = [name for name in names if name]
            if names:
                parts.append("tool_calls: " + ", ".join(names))
    if role == "tool":
        tool_call_id = str(message.get("tool_call_id") or "").strip()
        if tool_call_id:
            parts.append(f"tool_call_id: {tool_call_id}")
    content = str(message.get("content") or "").strip()
    if content:
        parts.append(content)
    return "\n".join(parts).strip()


def _chunk_texts(texts: Sequence[str], *, max_chars: int = MAX_CHUNK_CHARS) -> List[str]:
    chunks: List[str] = []
    current_parts: List[str] = []
    current_len = 0

    def _flush() -> None:
        nonlocal current_parts, current_len
        if current_parts:
            chunks.append("\n\n".join(current_parts).strip())
        current_parts = []
        current_len = 0

    for text in texts:
        normalized = str(text or "").strip()
        if not normalized:
            continue
        if len(normalized) > max_chars:
            paragraphs = [part.strip() for part in normalized.split("\n\n") if part.strip()]
            if not paragraphs:
                paragraphs = [normalized]
            for paragraph in paragraphs:
                if len(paragraph) <= max_chars:
                    if current_len and current_len + len(paragraph) + 2 > max_chars:
                        _flush()
                    current_parts.append(paragraph)
                    current_len += len(paragraph) + 2
                    continue
                start = 0
                while start < len(paragraph):
                    piece = paragraph[start:start + max_chars]
                    if current_len and current_len + len(piece) + 2 > max_chars:
                        _flush()
                    current_parts.append(piece)
                    current_len += len(piece) + 2
                    _flush()
                    start += max_chars
            continue
        if current_len and current_len + len(normalized) + 2 > max_chars:
            _flush()
        current_parts.append(normalized)
        current_len += len(normalized) + 2

    _flush()
    return chunks


async def _summarize_messages(messages: Sequence[Dict[str, Any]], config: Any) -> Optional[str]:
    texts = [_message_to_text(msg) for msg in messages]
    chunks = _chunk_texts(texts)
    return await _summarize_text_chunks(chunks, config)


async def _summarize_text_chunks(chunks: Sequence[str], config: Any) -> Optional[str]:
    if not chunks:
        return None

    chunk_summaries: List[str] = []
    for idx, chunk in enumerate(chunks, start=1):
        summary = await _summarize_chunk(chunk, config, chunk_index=idx, total_chunks=len(chunks))
        if summary:
            chunk_summaries.append(summary)
    if not chunk_summaries:
        return None
    if len(chunk_summaries) == 1:
        return chunk_summaries[0]

    combined = "\n\n".join(
        f"--- Chunk {idx} ---\n{summary}" for idx, summary in enumerate(chunk_summaries, start=1)
    )
    return await _summarize_chunk(combined, config, chunk_index=0, total_chunks=len(chunk_summaries), combine=True)


async def _summarize_chunk(
    text: str,
    config: Any,
    *,
    chunk_index: int,
    total_chunks: int,
    combine: bool = False,
) -> Optional[str]:
    from summary import _get_openai_config, _get_openai_client

    cfg = _get_openai_config(config)
    if not cfg:
        _log.warning("Cannot summarize context: OpenAI not configured")
        return None

    api_key, model, base_url = cfg
    client = _get_openai_client(api_key, base_url)
    if combine:
        system_prompt = (
            "Сведи несколько кратких суммаризаций рабочего хода агента в одну. "
            "Сохрани: подтвержденные факты, результаты инструментов, ошибки, ограничения, "
            "полученные ответы пользователя и незавершенные задачи. "
            "Пиши кратко, структурированными пунктами."
        )
    else:
        system_prompt = (
            "Суммаризируй рабочий ход агента. Сохрани ВСЕ ключевые факты:\n"
            "- подтвержденные findings и evidence\n"
            "- результаты вызовов инструментов\n"
            "- ошибки и policy blocks\n"
            "- ответы пользователя и уточнения\n"
            "- что осталось непроверенным или незавершенным\n"
            "Пиши кратко, структурированными пунктами, без воды и без дословного копирования."
        )
    try:
        resp = await client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {
                    "role": "user",
                    "content": (
                        f"Chunk {chunk_index or 1}/{max(total_chunks, 1)}\n\n"
                        f"{text}"
                    ),
                },
            ],
            max_tokens=MAX_CHUNK_SUMMARY_TOKENS,
            temperature=0.2,
        )
        return (resp.choices[0].message.content or "").strip()
    except Exception:
        _log.exception("Context summarization LLM call failed")
        return None
