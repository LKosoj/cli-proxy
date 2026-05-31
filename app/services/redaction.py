from __future__ import annotations

import re
from typing import Any

SENSITIVE_KEY_RE = re.compile(r"(api[_-]?key|token|secret|password|authorization)", re.IGNORECASE)
SENSITIVE_QUOTED_ASSIGNMENT_RE = re.compile(
    r"(?i)(['\"]?[A-Za-z0-9_.-]*(?:api[_-]?key|token|secret|password|authorization)"
    r"[A-Za-z0-9_.-]*['\"]?)(\s*[:=]\s*)(['\"])(.*?)(\3)"
)
SENSITIVE_UNQUOTED_ASSIGNMENT_RE = re.compile(
    r"(?i)(\b[A-Za-z0-9_.-]*(?:api[_-]?key|token|secret|password|authorization)"
    r"[A-Za-z0-9_.-]*)(\s*[:=]\s*)([^\s,;}]+)"
)
AUTH_HEADER_RE = re.compile(r"(?i)\bauthorization(\s*[:=]\s*)([A-Za-z]+\s+)?[^\s,;]+")
BEARER_VALUE_RE = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}")
BASIC_VALUE_RE = re.compile(r"(?i)\bbasic\s+[A-Za-z0-9+/=-]{8,}")
BARE_SECRET_RE = re.compile(
    r"\b("
    r"sk-proj-[A-Za-z0-9_-]{12,}|"
    r"sk-[A-Za-z0-9_-]{12,}|"
    r"ghp_[A-Za-z0-9_]{12,}|"
    r"tvly-[A-Za-z0-9_-]{8,}|"
    r"jina_[A-Za-z0-9_-]{8,}"
    r")\b"
)
REDACTED = "[REDACTED]"


def redact_value(value: Any) -> Any:
    if isinstance(value, dict):
        cleaned: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            if SENSITIVE_KEY_RE.search(key_text):
                cleaned[key_text] = REDACTED
            else:
                cleaned[key_text] = redact_value(item)
        return cleaned
    if isinstance(value, list):
        return [redact_value(item) for item in value]
    if isinstance(value, str):
        return redact_text(value)
    return value


def redact_text(value: str) -> str:
    text = str(value or "")
    text = SENSITIVE_QUOTED_ASSIGNMENT_RE.sub(
        lambda m: f"{m.group(1)}{m.group(2)}{m.group(3)}{REDACTED}{m.group(3)}",
        text,
    )
    text = AUTH_HEADER_RE.sub(lambda m: f"authorization{m.group(1)}{REDACTED}", text)
    text = SENSITIVE_UNQUOTED_ASSIGNMENT_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}{REDACTED}", text)
    text = BEARER_VALUE_RE.sub(f"Bearer {REDACTED}", text)
    text = BASIC_VALUE_RE.sub(f"Basic {REDACTED}", text)
    return BARE_SECRET_RE.sub(REDACTED, text)
