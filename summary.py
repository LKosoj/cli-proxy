import asyncio
import logging
import re
from typing import Any, Dict, Optional, Tuple

import httpx
from openai import AsyncOpenAI, APIConnectionError, APITimeoutError, APIStatusError

from modes.sdk.runtime.openai_client import create_async_openai_client, resolve_openai_config
from modes.sdk.runtime.json_normalizer import parse_normalize_validate
from config import AppConfig
from utils.text import normalize_text

# ---------------------------------------------------------------------------
# Cached AsyncOpenAI clients — one per (api_key, base_url) pair.
# Avoids creating (and leaking) a new httpx client on every call.
# ---------------------------------------------------------------------------
_openai_clients: Dict[Tuple[str, str], AsyncOpenAI] = {}

_OPENAI_TIMEOUT = httpx.Timeout(connect=10, read=200, write=50, pool=10)
_COMMIT_MESSAGE_ATTEMPTS = 5
_COMMIT_MESSAGE_MAX_TOKENS = 8000
_COMMIT_MESSAGE_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {
            "type": "string",
            "minLength": 1,
        },
        "body": {
            "type": ["array", "string", "null"],
            "items": {
                "type": "string",
                "minLength": 1,
            },
            "default": [],
        },
    },
    "required": ["summary"],
}
logger = logging.getLogger(__name__)


def _get_openai_client(api_key: str, base_url: str) -> AsyncOpenAI:
    key = (api_key, base_url)
    client = _openai_clients.get(key)
    if client is None:
        client = create_async_openai_client(
            api_key=api_key,
            base_url=base_url,
            timeout=_OPENAI_TIMEOUT,
        )
        _openai_clients[key] = client
    return client


def _get_openai_config(config: Optional[AppConfig] = None) -> Optional[Tuple[str, str, str]]:
    return resolve_openai_config(config, model_key="openai_big_model", env_priority=False)


def _strip_cli_preamble(text: str) -> str:
    lines = text.splitlines()
    if not lines:
        return text
    scan_limit = min(len(lines), 80)
    user_idx = None
    for idx in range(scan_limit):
        label = lines[idx].strip().lower()
        if label in ("user", "user:"):
            user_idx = idx
            break
    if user_idx is None:
        return text
    header = lines[:user_idx]
    meta_lines = 0
    separators = 0
    for line in header:
        stripped = line.strip()
        if not stripped:
            continue
        if set(stripped) == {"-"} and len(stripped) >= 4:
            separators += 1
            continue
        if ":" in stripped:
            key = stripped.split(":", 1)[0].strip()
            if 1 <= len(key) <= 24:
                meta_lines += 1
    if meta_lines >= 3 or separators >= 1:
        remainder = lines[user_idx + 1:]
        return "\n".join(remainder).lstrip()
    return text


_LENGTH_BUCKET_LABELS: Dict[str, Tuple[str, str, str]] = {
    "ru": ("короткий", "средний", "длинный"),
    "en": ("short",    "medium",  "long"),
    "zh": ("短",       "中等",    "长"),
    "de": ("kurz",     "mittel",  "lang"),
}

_SUMMARY_SYSTEM_PROMPTS: Dict[str, str] = {
    "ru": (
        "Сделай резюме на русском. Дай по делу, без воды. "
        "Адаптируй длину под объём текста: "
        "короткий → 2–4 пункта, средний → 4–6, длинный → 6–10. "
        "В каждом пункте 1–2 предложения. Не повторяйся и не пиши лишнего. "
        "Важно: обязательно учти ключевую информацию в конце текста и отрази её в резюме. "
        "В конце добавь блок 'Ключевое в конце' (2–4 пункта): "
        "либо итог/результат доработки, либо вопросы к пользователю. "
        "Не включай служебные метрики (например, tokens used) и счетчики."
    ),
    "en": (
        "Write a summary in English. Be concise and focused. "
        "Adapt the length to the text volume: "
        "short → 2–4 points, medium → 4–6, long → 6–10. "
        "1–2 sentences per point. No repetition, no filler. "
        "Important: make sure to capture key information from the end of the text. "
        "End with a 'Key points at the end' block (2–4 points): "
        "either the final result/outcome or open questions for the user. "
        "Do not include service metrics (e.g., tokens used) or counters."
    ),
    "zh": (
        "用中文写摘要。简洁扼要。"
        "根据文本量调整长度：短文→2-4点，中等→4-6点，长文→6-10点。"
        "每点1-2句话。不重复，不废话。"
        "重要：确保摘要包含文本末尾的关键信息。"
        "最后添加'末尾关键点'模块（2-4点）：最终结果或用户问题。"
        "不包含服务指标（如tokens used）和计数器。"
    ),
    "de": (
        "Schreibe eine Zusammenfassung auf Deutsch. Präzise und fokussiert. "
        "Passe die Länge dem Textumfang an: "
        "kurz → 2–4 Punkte, mittel → 4–6, lang → 6–10. "
        "1–2 Sätze pro Punkt. Keine Wiederholungen, kein Fülltext. "
        "Wichtig: Berücksichtige unbedingt die Schlüsselinformationen am Ende des Textes. "
        "Füge am Ende einen Block 'Kernpunkte am Ende' hinzu (2–4 Punkte): "
        "entweder das Ergebnis oder offene Fragen an den Nutzer. "
        "Keine Servicemetriken (z.B. tokens used) oder Zähler."
    ),
}

_KEY_TAIL_LABEL: Dict[str, str] = {
    "ru": "Ключевое в конце",
    "en": "Key points at the end",
    "zh": "末尾关键点",
    "de": "Kernpunkte am Ende",
}

_TRUNCATED_LABEL: Dict[str, str] = {
    "ru": "\n...(обрезано)...",
    "en": "\n...(truncated)...",
    "zh": "\n...(已截断)...",
    "de": "\n...(abgeschnitten)...",
}


def _length_bucket(text_len: int, language: str = "ru") -> str:
    labels = _LENGTH_BUCKET_LABELS.get(language, _LENGTH_BUCKET_LABELS["ru"])
    if text_len < 2000:
        return labels[0]
    if text_len < 12000:
        return labels[1]
    return labels[2]


def _suggest_max_tokens(text: str, max_chars: int) -> int:
    # Aim for a summary that can fit within max_chars without hard truncation.
    # Roughly 4 chars per token for Russian; clamp to keep responses concise.
    rough = max(200, min(1200, max_chars // 4))
    # Allow more tokens for larger inputs, but keep an upper bound.
    size_hint = max(0, len(text) - 2000) // 8000
    return min(1200, rough + size_hint * 100)


def _compact_reason(reason: str) -> str:
    clean = " ".join(reason.split()).strip()
    if len(clean) > 120:
        return f"{clean[:117]}..."
    return clean


async def _summarize_with_cfg(
    text: str, max_chars: int, cfg: Tuple[str, str, str], *, language: str = "ru"
) -> str:
    api_key, model, base_url = cfg

    tail_len = 4000
    head_len = min(6000, max(0, len(text) - tail_len))
    head = text[:head_len]
    tail = text[-tail_len:] if len(text) > tail_len else text
    client = _get_openai_client(api_key, base_url)
    system_prompt = _SUMMARY_SYSTEM_PROMPTS.get(language, _SUMMARY_SYSTEM_PROMPTS["ru"])
    resp = await client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": (
                    f"Длина текста: {_length_bucket(len(text), language)}.\n"
                    "Фрагменты текста:\n"
                    f"НАЧАЛО:\n{head}\n\n"
                    f"КОНЕЦ:\n{tail}"
                ),
            },
        ],
        max_tokens=_suggest_max_tokens(text, max_chars),
        temperature=0.2,
    )
    summary = (resp.choices[0].message.content or "").strip()
    tail_digest = _tail_digest(text)
    if tail_digest:
        key_label = _KEY_TAIL_LABEL.get(language, _KEY_TAIL_LABEL["ru"])
        summary = f"{summary}\n\n{key_label}:\n{tail_digest}"
    if len(summary) > max_chars:
        suffix = _TRUNCATED_LABEL.get(language, _TRUNCATED_LABEL["ru"])
        if max_chars <= len(suffix) + 20:
            return summary[:max_chars]
        return summary[: max_chars - len(suffix)] + suffix
    return summary


async def summarize_text(
    text: str, max_chars: int = 3000, config: Optional[AppConfig] = None, *, language: str = "ru"
) -> Optional[str]:
    cfg = _get_openai_config(config)
    if not cfg:
        return None
    # normalize_text() can be CPU-heavy on large inputs; avoid blocking the event loop.
    cleaned = await asyncio.to_thread(_strip_cli_preamble, text)
    cleaned = await asyncio.to_thread(normalize_text, cleaned, True)
    if len(cleaned) < 3000:
        return cleaned
    return await _summarize_with_cfg(cleaned, max_chars, cfg, language=language)


async def summarize_text_with_reason(
    text: str, max_chars: int = 3000, config: Optional[AppConfig] = None, *, language: str = "ru"
) -> Tuple[Optional[str], Optional[str]]:
    cfg = _get_openai_config(config)
    if not cfg:
        return None, "не настроены OPENAI_API_KEY/OPENAI_BIG_MODEL"
    cleaned = await asyncio.to_thread(_strip_cli_preamble, text)
    cleaned = await asyncio.to_thread(normalize_text, cleaned, True)
    if len(cleaned) < 3000:
        return cleaned, None
    try:
        summary = await _summarize_with_cfg(cleaned, max_chars, cfg, language=language)
        return summary, None
    except APITimeoutError:
        logging.getLogger(__name__).exception("OpenAI timeout")
        return None, "таймаут OpenAI"
    except APIConnectionError:
        logging.getLogger(__name__).exception("OpenAI connection error")
        return None, "нет соединения с OpenAI"
    except APIStatusError as err:
        logging.getLogger(__name__).exception("OpenAI status error")
        return None, f"ошибка OpenAI HTTP {err.status_code}"
    except Exception:
        logging.getLogger(__name__).exception("OpenAI summary error")
        return None, "неожиданный ответ OpenAI"


def _tail_digest(text: str) -> str:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return ""
    tail = lines[-12:]
    selected = []
    questions = []
    results = []
    # TODO T2-tail-digest: result_markers are Russian-only; non-ru languages skip tail digest.
    result_markers = (
        "готово", "сделано", "исправил", "исправлено", "обновил", "обновлено",
        "добавил", "добавлено", "внес", "внесено", "реализовал", "реализовано",
        "настроил", "настроено", "поправил", "поправлено", "исправляю",
    )
    for line in reversed(tail):
        lower = line.lower()
        if "tokens used" in lower or lower.startswith("tokens used"):
            continue
        if re.fullmatch(r"[\d,\s.]+", line):
            continue
        if "?" in line:
            if line not in questions:
                questions.append(line)
            continue
        if any(marker in lower for marker in result_markers):
            if line not in results:
                results.append(line)
            continue
        if line and line not in selected:
            selected.append(line)
    picked: list[str] = []
    for line in results[:3]:
        picked.append(line)
    for line in questions[:3]:
        if line not in picked:
            picked.append(line)
    if len(picked) < 2:
        for line in selected:
            if line not in picked:
                picked.append(line)
            if len(picked) >= 2:
                break
    bullets = []
    for line in picked[:4]:
        if len(line) > 240:
            line = line[:237] + "..."
        bullets.append(f"- {line}")
    return "\n".join(bullets)


async def _chat_completion_async(
    config: AppConfig,
    system: str,
    user: str,
    max_tokens: int,
    temperature: float,
    *,
    response_format: Optional[dict[str, Any]] = None,
) -> str:
    cfg = _get_openai_config(config)
    if not cfg:
        return ""
    api_key, model, base_url = cfg
    client = _get_openai_client(api_key, base_url)
    resp = await client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        max_tokens=max_tokens,
        temperature=temperature,
        response_format=response_format,
    )
    choice = resp.choices[0] if resp.choices else None
    content = choice.message.content if choice and getattr(choice, "message", None) else ""
    finish_reason = getattr(choice, "finish_reason", None)
    cleaned = (content or "").strip()
    if finish_reason == "length":
        logger.warning(
            "chat_completion truncated response model=%s max_tokens=%s response_format=%s content_len=%d preview=%r",
            model,
            max_tokens,
            response_format,
            len(cleaned),
            cleaned[:300],
        )
    return cleaned


async def suggest_commit_message_async(
    text: str, config: Optional[AppConfig] = None, *, language: str = "ru"
) -> Optional[str]:
    if not config:
        return None
    from i18n.language_names import LANGUAGE_NAMES
    lang_name = LANGUAGE_NAMES.get(language, "Russian")
    content = await _chat_completion_async(
        config,
        (
            f"Write a short git commit message for the changes. "
            f"One line, no quotes, no trailing period, up to ~80 characters. "
            f"Write in {lang_name}, reflect the essence of the changes."
        ),
        text[:12000],
        max_tokens=8000,
        temperature=0.2,
    )
    return content or None


async def suggest_commit_message_detailed_async(
    text: str, config: Optional[AppConfig] = None, *, language: str = "ru"
) -> Optional[Tuple[str, str]]:
    if not config:
        return None
    from i18n.language_names import LANGUAGE_NAMES
    lang_name = LANGUAGE_NAMES.get(language, "Russian")
    base_system_prompt = (
        "Generate a git commit message and return strictly a JSON object.\n"
        "Response format:\n"
        "{\n"
        '  "summary": "short title up to 80 characters, no trailing period",\n'
        '  "body": ["point 1", "point 2", "point 3"]\n'
        "}\n"
        "Rules:\n"
        f"- Write strictly in {lang_name}.\n"
        "- summary: one line, capturing the essence of the change.\n"
        "- body: 3-5 very short points, maximum 12 words per point.\n"
        "- If tests were not run, one of the body points must say 'Tests: not run'.\n"
        "- If you need to reference a literal or string inside a value, use single quotes, not double.\n"
        "- Do not add markdown, explanations, or text outside JSON."
    )
    for attempt in range(1, _COMMIT_MESSAGE_ATTEMPTS + 1):
        system_prompt = base_system_prompt
        if attempt > 1:
            system_prompt += (
                "\nПРЕДЫДУЩАЯ ПОПЫТКА НЕ ДАЛА ВАЛИДНЫЙ JSON. "
                "Верни ТОЛЬКО валидный JSON-объект без текста вокруг. "
                "Если нужны кавычки внутри строковых значений, замени их на одинарные."
            )
        content = await _chat_completion_async(
            config,
            system_prompt,
            text[:12000],
            max_tokens=_COMMIT_MESSAGE_MAX_TOKENS,
            temperature=0.2,
            response_format={"type": "json_object"},
        )
        if not content:
            logger.warning(
                "commit message generation returned empty response attempt=%d/%d",
                attempt,
                _COMMIT_MESSAGE_ATTEMPTS,
            )
            continue
        try:
            payload = parse_normalize_validate(content, _COMMIT_MESSAGE_RESPONSE_SCHEMA)
        except Exception:
            logger.warning(
                "commit message generation parse failed attempt=%d/%d content_len=%d preview=%r",
                attempt,
                _COMMIT_MESSAGE_ATTEMPTS,
                len(content),
                content[:300],
                exc_info=True,
            )
            continue
        summary_line = str(payload.get("summary") or "").strip()
        body_value = payload.get("body")
        if isinstance(body_value, str):
            body_lines = [line.strip() for line in body_value.splitlines() if line.strip()]
        elif isinstance(body_value, list):
            body_lines = [str(line).strip() for line in body_value if str(line).strip()]
        else:
            body_lines = []
        if not summary_line:
            logger.warning(
                "commit message generation returned empty summary attempt=%d/%d payload=%r",
                attempt,
                _COMMIT_MESSAGE_ATTEMPTS,
                payload,
            )
            continue
        body = "\n".join(body_lines).strip()
        if not body:
            logger.warning(
                "commit message generation returned empty body attempt=%d/%d payload=%r",
                attempt,
                _COMMIT_MESSAGE_ATTEMPTS,
                payload,
            )
            continue
        return summary_line, body
    return None


def suggest_commit_message(text: str, config: Optional[AppConfig] = None) -> Optional[str]:
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop:
        return None
    return asyncio.run(suggest_commit_message_async(text, config))


def suggest_commit_message_detailed(
    text: str, config: Optional[AppConfig] = None
) -> Optional[Tuple[str, str]]:
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop:
        return None
    return asyncio.run(suggest_commit_message_detailed_async(text, config))
