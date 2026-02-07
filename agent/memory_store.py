from __future__ import annotations

import os
import time
import re
from typing import Dict, List

MEMORY_FILE = "MEMORY.md"
_ENTRY_RE = re.compile(r"^- (\d{4}-\d{2}-\d{2} \d{2}:\d{2}): \[(\w+)\] (.*)$")


def _memory_path(cwd: str) -> str:
    return os.path.join(cwd, MEMORY_FILE)


def read_memory(cwd: str) -> str:
    path = _memory_path(cwd)
    if not os.path.exists(path):
        return ""
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        return f.read()


def write_memory(cwd: str, content: str) -> None:
    path = _memory_path(cwd)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content or "")


def append_memory(cwd: str, content: str) -> None:
    if not content:
        return
    timestamp = time.strftime("%Y-%m-%d %H:%M")
    entry = f"- {timestamp}: {content.strip()}\n"
    path = _memory_path(cwd)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(entry)


def memory_size_bytes(content: str) -> int:
    return len((content or "").encode("utf-8"))


def trim_for_context(content: str, max_chars: int = 2000) -> str:
    if not content:
        return ""
    if len(content) <= max_chars:
        return content
    return content[: max_chars - 20] + "\n...(truncated)"


def parse_entries(content: str) -> List[Dict[str, str]]:
    entries: List[Dict[str, str]] = []
    for line in (content or "").splitlines():
        line = line.strip()
        if not line:
            continue
        match = _ENTRY_RE.match(line)
        if not match:
            continue
        ts, tag, text = match.groups()
        entries.append({"ts": ts, "tag": tag, "text": text, "raw": line})
    return entries


def _normalize_text(text: str) -> str:
    return " ".join((text or "").strip().lower().split())


def append_memory_tagged(cwd: str, tag: str, content: str) -> bool:
    if not content or not tag:
        return False
    existing = read_memory(cwd)
    norm_new = _normalize_text(content)
    for entry in parse_entries(existing):
        if entry["tag"].upper() == tag.upper() and _normalize_text(entry["text"]) == norm_new:
            return False
    timestamp = time.strftime("%Y-%m-%d %H:%M")
    entry = f"- {timestamp}: [{tag.upper()}] {content.strip()}\n"
    path = _memory_path(cwd)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(entry)
    return True


def compact_memory_by_priority(content: str, max_bytes: int, priority: List[str]) -> str:
    entries = parse_entries(content)
    if not entries:
        return ""
    priority_index = {tag.upper(): idx for idx, tag in enumerate(priority)}

    def _sort_key(entry: Dict[str, str]):
        return (priority_index.get(entry["tag"].upper(), 999), entry["ts"])
    entries_sorted = sorted(entries, key=_sort_key)
    result_lines: List[str] = []
    current_bytes = 0
    for entry in entries_sorted:
        line = entry["raw"]
        line_bytes = len((line + "\n").encode("utf-8"))
        if current_bytes + line_bytes > max_bytes:
            continue
        result_lines.append(line)
        current_bytes += line_bytes
    return "\n".join(result_lines)
