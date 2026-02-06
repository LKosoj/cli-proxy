import asyncio
import logging
import os
import re
from typing import Dict, Optional, Tuple

import httpx
from openai import AsyncOpenAI, APIConnectionError, APITimeoutError, APIStatusError

from config import AppConfig
from utils import normalize_text

# ---------------------------------------------------------------------------
# Cached AsyncOpenAI clients — one per (api_key, base_url) pair.
# Avoids creating (and leaking) a new httpx client on every call.
# ---------------------------------------------------------------------------
_openai_clients: Dict[Tuple[str, str], AsyncOpenAI] = {}

_OPENAI_TIMEOUT = httpx.Timeout(connect=10, read=200, write=50, pool=10)


def _get_openai_client(api_key: str, base_url: str) -> AsyncOpenAI:
    key = (api_key, base_url)
    client = _openai_clients.get(key)
    if client is None:
        client = AsyncOpenAI(
            api_key=api_key,
            base_url=base_url,
            timeout=_OPENAI_TIMEOUT,
        )
        _openai_clients[key] = client
    return client


def _get_openai_config(config: Optional[AppConfig] = None):
    api_key = None
    model = None
    base_url = None
    if config:
        # Config должна быть источником правды, env — только fallback.
        api_key = config.defaults.openai_api_key
        model = config.defaults.openai_big_model or config.defaults.openai_model
        base_url = config.defaults.openai_base_url
    api_key = api_key or os.getenv("OPENAI_API_KEY")
    # Summaries are intentionally generated with the "big" model to improve quality/faithfulness.
    # Prefer OPENAI_BIG_MODEL; fall back to OPENAI_MODEL for backward-compat.
    model = model or os.getenv("OPENAI_BIG_MODEL") or os.getenv("OPENAI_MODEL")
    base_url = base_url or os.getenv("OPENAI_BASE_URL")
    if not base_url:
        base_url = "https://api.openai.com"
    if not api_key or not model:
        return None
    return api_key, model, base_url.rstrip("/")


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
        remainder = lines[user_idx + 1 :]
        return "\n".join(remainder).lstrip()
    return text


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


def _compact_reason(reason: str) -> str:
    clean = " ".join(reason.split()).strip()
    if len(clean) > 120:
        return f"{clean[:117]}..."
    return clean




async def _summarize_with_cfg(
    text: str, max_chars: int, cfg: Tuple[str, str, str]
) -> str:
    api_key, model, base_url = cfg

    tail_len = 4000
    head_len = min(6000, max(0, len(text) - tail_len))
    head = text[:head_len]
    tail = text[-tail_len:] if len(text) > tail_len else text
    client = _get_openai_client(api_key, base_url)
    resp = await client.chat.completions.create(
        model=model,
        messages=[
            {
                "role": "system",
                "content": (
                    "Сделай резюме на русском. Дай по делу, без воды. "
                    "Адаптируй длину под объём текста: "
                    "короткий → 2–4 пункта, средний → 4–6, длинный → 6–10. "
                    "В каждом пункте 1–2 предложения. Не повторяйся и не пиши лишнего. "
                    "Важно: обязательно учти ключевую информацию в конце текста и отрази её в резюме. "
                    "В конце добавь блок 'Ключевое в конце' (2–4 пункта): "
                    "либо итог/результат доработки, либо вопросы к пользователю. "
                    "Не включай служебные метрики (например, tokens used) и счетчики."
                ),
            },
            {
                "role": "user",
                "content": (
                    f"Длина текста: {_length_bucket(len(text))}.\n"
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
        summary = f"{summary}\n\nКлючевое в конце:\n{tail_digest}"
    if len(summary) > max_chars:
        suffix = "\n...(обрезано)..."
        if max_chars <= len(suffix) + 20:
            return summary[:max_chars]
        return summary[: max_chars - len(suffix)] + suffix
    return summary


async def summarize_text(text: str, max_chars: int = 3000, config: Optional[AppConfig] = None) -> Optional[str]:
    cfg = _get_openai_config(config)
    if not cfg:
        return None
    cleaned = _strip_cli_preamble(text)
    cleaned = normalize_text(cleaned, strip_ansi=True)
    if len(cleaned) < 3000:
        return cleaned
    return await _summarize_with_cfg(cleaned, max_chars, cfg)


async def summarize_text_with_reason(
    text: str, max_chars: int = 3000, config: Optional[AppConfig] = None
) -> Tuple[Optional[str], Optional[str]]:
    cfg = _get_openai_config(config)
    if not cfg:
        return None, "не настроены OPENAI_API_KEY/OPENAI_BIG_MODEL"
    cleaned = _strip_cli_preamble(text)
    cleaned = normalize_text(cleaned, strip_ansi=True)
    if len(cleaned) < 3000:
        return cleaned, None
    try:
        summary = await _summarize_with_cfg(cleaned, max_chars, cfg)
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
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    if not lines:
        return ""
    tail = lines[-12:]
    selected = []
    questions = []
    results = []
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
    config: AppConfig, system: str, user: str, max_tokens: int, temperature: float
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
    )
    content = resp.choices[0].message.content if resp.choices else ""
    return (content or "").strip()


async def suggest_commit_message_async(
    text: str, config: Optional[AppConfig] = None
) -> Optional[str]:
    if not config:
        return None
    content = await _chat_completion_async(
        config,
        (
            "Сформулируй краткое сообщение коммита по изменениям. "
            "Одна строка, без кавычек, без точки в конце, до ~80 символов. "
            "Пиши по-русски, отражай суть изменений."
        ),
        text[:12000],
        max_tokens=80,
        temperature=0.2,
    )
    return content or None


async def suggest_commit_message_detailed_async(
    text: str, config: Optional[AppConfig] = None
) -> Optional[Tuple[str, str]]:
    if not config:
        return None
    content = await _chat_completion_async(
        config,
        (
            "Сформируй сообщение коммита в двух частях:\n"
            "1) Краткий заголовок одной строкой (до ~80 символов), без точки в конце.\n"
            "2) Детальное описание в 3–6 пунктах, каждый пункт с новой строки, "
            "по делу, с упоминанием ключевых файлов/изменений и поведения. "
            "Если были тесты — укажи их, иначе напиши 'Тесты: не запускались'.\n"
            "Верни в формате:\n"
            "SUMMARY: <текст>\n"
            "BODY:\n"
            "- ...\n"
        ),
        text[:12000],
        max_tokens=220,
        temperature=0.2,
    )
    if not content:
        return None
    summary_line = ""
    body_lines: list[str] = []
    in_body = False
    for line in content.splitlines():
        if line.startswith("SUMMARY:"):
            summary_line = line.replace("SUMMARY:", "", 1).strip()
            continue
        if line.startswith("BODY:"):
            in_body = True
            continue
        if in_body:
            if line.strip():
                body_lines.append(line.rstrip())
    if not summary_line:
        return None
    body = "\n".join(body_lines).strip()
    if not body:
        return None
    return summary_line, body


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
