import os
from typing import Optional, Tuple

import requests

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




def _summarize_with_cfg(
    text: str, max_chars: int, cfg: Tuple[str, str, str]
) -> str:
    api_key, model, base_url = cfg

    tail_len = 4000
    head_len = min(6000, max(0, len(text) - tail_len))
    head = text[:head_len]
    tail = text[-tail_len:] if len(text) > tail_len else text
    payload = {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": (
                    "Сделай резюме на русском. Дай по делу, без воды. "
                    "Адаптируй длину под объём текста: "
                    "короткий → 2–4 пункта, средний → 4–6, длинный → 6–10. "
                    "В каждом пункте 1–2 предложения. Не повторяйся и не пиши лишнего. "
                    "Важно: обязательно учти ключевую информацию в конце текста и отрази её в резюме."
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
        "max_tokens": _suggest_max_tokens(text, max_chars),
        "temperature": 0.2,
    }

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    resp = requests.post(
        f"{base_url}/v1/chat/completions", json=payload, headers=headers, timeout=60
    )
    resp.raise_for_status()
    data = resp.json()
    summary = data["choices"][0]["message"]["content"].strip()
    tail_digest = _tail_digest(text)
    if tail_digest:
        summary = f"{summary}\n\nКлючевое в конце:\n{tail_digest}"
    if len(summary) > max_chars:
        return summary[:max_chars]
    return summary


def summarize_text(text: str, max_chars: int = 3000, config: Optional[AppConfig] = None) -> Optional[str]:
    cfg = _get_openai_config(config)
    if not cfg:
        return None
    cleaned = _strip_cli_preamble(text)
    if len(cleaned) < 3000:
        return cleaned
    return _summarize_with_cfg(cleaned, max_chars, cfg)


def summarize_text_with_reason(
    text: str, max_chars: int = 3000, config: Optional[AppConfig] = None
) -> Tuple[Optional[str], Optional[str]]:
    cfg = _get_openai_config(config)
    if not cfg:
        return None, "не настроены OPENAI_API_KEY/OPENAI_MODEL"
    cleaned = _strip_cli_preamble(text)
    if len(cleaned) < 3000:
        return cleaned, None
    try:
        summary = _summarize_with_cfg(cleaned, max_chars, cfg)
        return summary, None
    except requests.Timeout:
        return None, "таймаут OpenAI"
    except requests.ConnectionError:
        return None, "нет соединения с OpenAI"
    except requests.HTTPError as err:
        code = err.response.status_code if err.response is not None else "?"
        return None, f"ошибка OpenAI HTTP {code}"
    except Exception:
        return None, "неожиданный ответ OpenAI"

def _tail_digest(text: str) -> str:
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    if not lines:
        return ""
    tail = lines[-6:]
    selected = []
    for line in reversed(tail):
        if line and line not in selected:
            selected.append(line)
        if len(selected) >= 2:
            break
    selected.reverse()
    bullets = []
    for line in selected:
        if len(line) > 240:
            line = line[:237] + "..."
        bullets.append(f"- {line}")
    return "\n".join(bullets)


def suggest_commit_message(text: str, config: Optional[AppConfig] = None) -> Optional[str]:
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
                    "Сформулируй краткое сообщение коммита по изменениям. "
                    "Одна строка, без кавычек, без точки в конце, до ~80 символов. "
                    "Пиши по-русски, отражай суть изменений."
                ),
            },
            {
                "role": "user",
                "content": text[:12000],
            },
        ],
        "max_tokens": 80,
        "temperature": 0.2,
    }

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    resp = requests.post(
        f"{base_url}/v1/chat/completions", json=payload, headers=headers, timeout=60
    )
    resp.raise_for_status()
    data = resp.json()
    message = data["choices"][0]["message"]["content"].strip()
    if not message:
        return None
    return message
