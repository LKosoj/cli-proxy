from __future__ import annotations

import re
from typing import List


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[mK]")
_LOOSE_ANSI_RE = re.compile(r"\[(?:\d{1,3};)*\d{1,3}m")
_TICK_OR_TIME_RE = re.compile(
    r"\b\d{1,2}:\d{2}(?::\d{2})?\b|"
    r"\b\d{1,6}\s*(?:s|sec|сек)\b|"
    r"\[\s*\d{1,6}\s*s\s*\]|"
    r"✓\s*\d{1,6}\s*(?:s|sec|сек)?\b|"
    r"\b(?:tick|step|шаг)\s*[:#]?\s*\d{1,6}\b|"
    r"\b\d{1,3}\s*%",
    re.IGNORECASE,
)
_TIME_ONLY_RE = re.compile(r"^\d{1,2}:\d{2}(?::\d{2})?$")
_MCP_LINE_RE = re.compile(r"^mcp:\s+", re.IGNORECASE)


def strip_ansi(text: str) -> str:
    text = _ANSI_RE.sub("", text)
    return _LOOSE_ANSI_RE.sub("", text)


def has_ansi(text: str) -> bool:
    return _ANSI_RE.search(text) is not None


def extract_tick_tokens(text: str) -> List[str]:
    cleaned = strip_ansi(text)
    return [match.group(0) for match in _TICK_OR_TIME_RE.finditer(cleaned)]


def is_time_only_text(text: str) -> bool:
    cleaned = " ".join(strip_ansi(str(text or "")).split())
    if not cleaned:
        return False
    return _TIME_ONLY_RE.fullmatch(cleaned) is not None


def normalize_text(text: str, strip_ansi: bool = True) -> str:
    if not text:
        return text
    if strip_ansi:
        text = strip_ansi_codes(text)
    text = _remove_mcp_lines(text)
    return _dedupe_repeated_blocks(text)


def strip_ansi_codes(text: str) -> str:
    return strip_ansi(text)


def _remove_mcp_lines(text: str) -> str:
    lines = text.splitlines()
    first_mcp_idx = None
    startup_idx = None
    for idx, line in enumerate(lines):
        stripped = line.strip()
        if first_mcp_idx is None and _MCP_LINE_RE.match(stripped):
            first_mcp_idx = idx
        if first_mcp_idx is not None and stripped.lower().startswith("mcp startup:"):
            startup_idx = idx
            break
    if first_mcp_idx is None or startup_idx is None:
        return text
    filtered: list[str] = []
    for idx, line in enumerate(lines):
        if first_mcp_idx <= idx < startup_idx and _MCP_LINE_RE.match(line.strip()):
            continue
        filtered.append(line)
    return "\n".join(filtered)


def _dedupe_repeated_blocks(text: str) -> str:
    lines = text.splitlines()
    total = len(lines)
    if total == 0:
        return text
    min_block = 1
    changed = True
    while changed:
        changed = False
        total = len(lines)
        for i in range(total - min_block):
            if lines[i].strip() == "":
                continue
            j = i + 1
            while j <= total - min_block:
                if lines[j].strip() == "":
                    j += 1
                    continue
                k = 0
                while i + k < total and j + k < total and lines[i + k] == lines[j + k]:
                    k += 1
                if k >= min_block:
                    del lines[j:j + k]
                    changed = True
                    total = len(lines)
                    break
                j += 1
            if changed:
                break
    return "\n".join(lines)


def build_preview(text: str, max_chars: int) -> str:
    plain = strip_ansi(text)
    if len(plain) <= max_chars:
        return plain
    suffix = "\n...(обрезано)..."
    if max_chars <= len(suffix) + 20:
        return plain[:max_chars]
    return plain[: max_chars - len(suffix)] + suffix
