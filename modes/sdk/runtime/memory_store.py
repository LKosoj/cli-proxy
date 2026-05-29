from __future__ import annotations

import hashlib
import os
import re
import time
from datetime import UTC, datetime, timedelta
from typing import Any, Dict, List, Optional

from utils.paths import sandbox_root

MEMORY_FILE = "MEMORY.md"
_LINE_RE = re.compile(r"^- (\d{4}-\d{2}-\d{2} \d{2}:\d{2}):\s*(.*)$")
_TOKEN_RE = re.compile(r"^\[([^\]]+)\]\s*")
_SEMANTIC_TAGS = {"PREF", "DECISION", "CONFIG", "AGREEMENT"}
_ALLOWED_LAYERS = {"semantic", "task_state"}


def _normalize_chat_id(chat_id: Any) -> int:
    try:
        return max(0, int(chat_id or 0))
    except Exception:
        return 0


def chat_workspace_root(workdir: str, chat_id: Any) -> str:
    cid = _normalize_chat_id(chat_id)
    return os.path.join(sandbox_root(str(workdir or "")), "chats", f"chat_{cid}")


def ensure_chat_workspace(workdir: str, chat_id: Any) -> str:
    path = chat_workspace_root(workdir, chat_id)
    os.makedirs(path, exist_ok=True)
    return path


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


def memory_size_bytes(content: str) -> int:
    return len((content or "").encode("utf-8"))


def trim_for_context(content: str, max_chars: int = 2000) -> str:
    if not content:
        return ""
    if len(content) <= max_chars:
        return content
    marker = f"\n[degraded_context_trimmed chars_removed={len(content) - max_chars}]\n"
    if max_chars <= len(marker) + 32:
        minimal_marker = f"[degraded_context_trimmed chars_removed={len(content) - max_chars}]"
        return minimal_marker[:max_chars]
    head_len = max(16, (max_chars - len(marker)) // 2)
    tail_len = max(16, max_chars - len(marker) - head_len)
    return content[:head_len] + marker + content[-tail_len:]


def _normalize_text(text: str) -> str:
    return " ".join((text or "").strip().lower().split())


def _derive_layer(tag: str, layer: str) -> str:
    clean_layer = (layer or "").strip().lower()
    if clean_layer in _ALLOWED_LAYERS:
        return clean_layer
    clean_tag = (tag or "").strip().upper()
    if clean_tag in _SEMANTIC_TAGS:
        return "semantic"
    return "task_state"


def _now_ts() -> str:
    return time.strftime("%Y-%m-%d %H:%M")


def _mk_id(ts: str, tag: str, text: str) -> str:
    payload = f"{ts}|{tag}|{text}".encode("utf-8", errors="ignore")
    return hashlib.sha1(payload).hexdigest()[:12]


def _sanitize_atomic(content: str, *, max_len: int = 280) -> str:
    text = " ".join((content or "").replace("\n", " ").split())
    if not text:
        return ""
    if len(text) > max_len:
        return ""
    # One short factual statement: avoid long compound text blobs.
    if text.count(".") + text.count("!") + text.count("?") > 2:
        return ""
    return text


def _parse_line(line: str) -> Optional[Dict[str, Any]]:
    raw = (line or "").strip()
    if not raw:
        return None
    m = _LINE_RE.match(raw)
    if not m:
        return None
    ts = m.group(1)
    rest = m.group(2).strip()

    tokens: List[str] = []
    while True:
        tm = _TOKEN_RE.match(rest)
        if not tm:
            break
        token = tm.group(1).strip()
        if token:
            tokens.append(token)
        rest = rest[tm.end():].lstrip()
    text = rest.strip()
    if not text:
        return None

    tag = ""
    layer = ""
    source = ""
    confidence = None
    entry_id = ""
    expires_at = ""
    for token in tokens:
        if ":" in token:
            key, value = token.split(":", 1)
            key = key.strip().upper()
            value = value.strip()
            if key == "LAYER":
                layer = value.lower()
            elif key == "SRC":
                source = value
            elif key == "CONF":
                try:
                    confidence = float(value)
                except Exception:
                    confidence = None
            elif key == "ID":
                entry_id = value
            elif key == "EXP":
                expires_at = value
        else:
            tok_u = token.strip().upper()
            if not tag:
                tag = tok_u

    if not tag:
        return None
    layer = _derive_layer(tag, layer)
    if not entry_id:
        entry_id = _mk_id(ts, tag, text)
    return {
        "ts": ts,
        "tag": tag,
        "layer": layer,
        "source": source or "unknown",
        "confidence": confidence,
        "id": entry_id,
        "expires_at": expires_at,
        "text": text,
        "raw": raw,
    }


def parse_entries(content: str) -> List[Dict[str, Any]]:
    entries: List[Dict[str, Any]] = []
    for line in (content or "").splitlines():
        item = _parse_line(line)
        if item:
            entries.append(item)
    return entries


def _render_entry(entry: Dict[str, Any]) -> str:
    ts = str(entry.get("ts") or _now_ts())
    tag = str(entry.get("tag") or "AGREEMENT").upper()
    layer = _derive_layer(tag, str(entry.get("layer") or ""))
    source = str(entry.get("source") or "unknown").strip() or "unknown"
    entry_id = str(entry.get("id") or _mk_id(ts, tag, str(entry.get("text") or "")))
    text = str(entry.get("text") or "").strip()
    conf = entry.get("confidence")
    exp = str(entry.get("expires_at") or "").strip()
    parts = [
        f"[{tag}]",
        f"[LAYER:{layer}]",
        f"[SRC:{source}]",
        f"[ID:{entry_id}]",
    ]
    if isinstance(conf, (float, int)):
        val = max(0.0, min(1.0, float(conf)))
        parts.append(f"[CONF:{val:.2f}]")
    if exp:
        parts.append(f"[EXP:{exp}]")
    meta = " ".join(parts)
    return f"- {ts}: {meta} {text}".rstrip()


def _is_expired(entry: Dict[str, Any], now: Optional[datetime] = None) -> bool:
    exp = str(entry.get("expires_at") or "").strip()
    if not exp:
        return False
    try:
        now_dt = now or datetime.now(UTC).replace(tzinfo=None)
        exp_dt = datetime.strptime(exp, "%Y-%m-%d")
        return exp_dt.date() < now_dt.date()
    except Exception:
        return False


def _ts_to_dt(ts: str) -> datetime:
    try:
        return datetime.strptime(ts, "%Y-%m-%d %H:%M")
    except Exception:
        return datetime.fromtimestamp(0, tz=UTC).replace(tzinfo=None)


def append_memory(cwd: str, content: str) -> None:
    append_memory_structured(
        cwd,
        tag="AGREEMENT",
        content=content,
        layer="semantic",
        source="agent",
        confidence=0.6,
        ttl_days=None,
    )


def append_memory_structured(
    cwd: str,
    *,
    tag: str,
    content: str,
    layer: Optional[str] = None,
    source: str = "agent",
    confidence: Optional[float] = 0.8,
    ttl_days: Optional[int] = None,
) -> bool:
    clean_tag = str(tag or "").strip().upper()
    if not clean_tag:
        return False
    clean_text = _sanitize_atomic(content)
    if not clean_text:
        return False
    entry_layer = _derive_layer(clean_tag, str(layer or ""))
    now_ts = _now_ts()
    exp = ""
    if entry_layer == "task_state" and isinstance(ttl_days, int) and ttl_days > 0:
        exp = (datetime.now(UTC).replace(tzinfo=None) + timedelta(days=ttl_days)).strftime("%Y-%m-%d")
    existing = parse_entries(read_memory(cwd))
    norm_new = _normalize_text(clean_text)
    for entry in existing:
        if _is_expired(entry):
            continue
        if entry.get("tag", "").upper() == clean_tag and _normalize_text(entry.get("text", "")) == norm_new:
            return False
    entry = {
        "ts": now_ts,
        "tag": clean_tag,
        "layer": entry_layer,
        "source": source or "agent",
        "confidence": confidence,
        "id": _mk_id(now_ts, clean_tag, clean_text),
        "expires_at": exp,
        "text": clean_text,
    }
    path = _memory_path(cwd)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(_render_entry(entry) + "\n")
    return True


def append_memory_tagged(cwd: str, tag: str, content: str) -> bool:
    return append_memory_structured(
        cwd,
        tag=tag,
        content=content,
        layer="semantic",
        source="agent",
        confidence=0.8,
        ttl_days=None,
    )


def update_memory_entry(
    cwd: str,
    *,
    entry_id: str,
    content: str,
    source: str = "agent",
    confidence: Optional[float] = 0.8,
) -> bool:
    target = str(entry_id or "").strip()
    if not target:
        return False
    clean_text = _sanitize_atomic(content)
    if not clean_text:
        return False
    entries = parse_entries(read_memory(cwd))
    changed = False
    for entry in entries:
        if str(entry.get("id") or "").strip() != target:
            continue
        entry["text"] = clean_text
        entry["source"] = source or entry.get("source") or "agent"
        entry["confidence"] = confidence
        entry["ts"] = _now_ts()
        changed = True
        break
    if not changed:
        return False
    write_memory(cwd, "\n".join(_render_entry(e) for e in entries) + ("\n" if entries else ""))
    return True


def forget_memory_entry(cwd: str, *, entry_id: str) -> bool:
    target = str(entry_id or "").strip()
    if not target:
        return False
    entries = parse_entries(read_memory(cwd))
    kept = [e for e in entries if str(e.get("id") or "").strip() != target]
    if len(kept) == len(entries):
        return False
    write_memory(cwd, "\n".join(_render_entry(e) for e in kept) + ("\n" if kept else ""))
    return True


def forget_memory_by_query(cwd: str, *, query: str) -> int:
    needle = _normalize_text(query)
    if not needle:
        return 0
    entries = parse_entries(read_memory(cwd))
    kept: List[Dict[str, Any]] = []
    removed = 0
    for e in entries:
        if needle in _normalize_text(str(e.get("text") or "")):
            removed += 1
            continue
        kept.append(e)
    if removed:
        write_memory(cwd, "\n".join(_render_entry(e) for e in kept) + ("\n" if kept else ""))
    return removed


def remove_expired_entries(content: str) -> str:
    entries = parse_entries(content)
    alive = [e for e in entries if not _is_expired(e)]
    return "\n".join(_render_entry(e) for e in alive)


def compact_memory_by_priority(content: str, max_bytes: int, priority: List[str]) -> str:
    entries = [e for e in parse_entries(content) if not _is_expired(e)]
    if not entries:
        return ""
    priority_index = {tag.upper(): idx for idx, tag in enumerate(priority)}

    def _sort_key(entry: Dict[str, Any]):
        layer = str(entry.get("layer") or "")
        layer_rank = 0 if layer == "semantic" else 1
        tag_rank = priority_index.get(str(entry.get("tag") or "").upper(), 999)
        # Keep newer entries first within same rank.
        dt = _ts_to_dt(str(entry.get("ts") or ""))
        return (layer_rank, tag_rank, -int(dt.timestamp()))

    entries_sorted = sorted(entries, key=_sort_key)
    result_lines: List[str] = []
    current_bytes = 0
    for entry in entries_sorted:
        line = _render_entry(entry)
        line_bytes = len((line + "\n").encode("utf-8"))
        if current_bytes + line_bytes > max_bytes:
            continue
        result_lines.append(line)
        current_bytes += line_bytes
    return "\n".join(result_lines)
