from __future__ import annotations

import hashlib
import logging
import os
import re
import sqlite3
from datetime import UTC, datetime
from typing import Any, Dict, List

from modes.sdk.json_store import read_json_locked
from .memory_store import chat_workspace_root, parse_entries, read_memory

_log = logging.getLogger(__name__)

_WORD_RE = re.compile(r"[A-Za-zА-Яа-яЁё0-9_]+")
_TAG_IN_TEXT_RE = re.compile(r"^\[(\w+)\]")


def _db_path(cwd: str) -> str:
    return os.path.join(cwd, "MEMORY_FTS5.db")


def _mk_doc_id(source: str, ts: str, text: str) -> str:
    payload = f"{source}\n{ts}\n{text}".encode("utf-8", errors="ignore")
    return hashlib.sha1(payload).hexdigest()


def _extract_memory_docs(cwd: str) -> List[Dict[str, str]]:
    docs: List[Dict[str, str]] = []
    raw = read_memory(cwd)
    if not raw:
        return docs
    for entry in parse_entries(raw):
        ts = str(entry.get("ts") or "")
        tag = str(entry.get("tag") or "").strip().upper()
        layer = str(entry.get("layer") or "").strip().lower()
        verification_status = str(entry.get("verification_status") or "legacy").strip().lower()
        evidence_type = str(entry.get("evidence_type") or "legacy").strip().lower()
        evidence_ref = str(entry.get("evidence_ref") or "").strip()
        text = str(entry.get("text") or "").strip()
        if not text:
            continue
        tokens = [f"[{tag}]", f"[LAYER:{layer}]", f"[VER:{verification_status}]", f"[EVID:{evidence_type}]"]
        if evidence_ref:
            tokens.append(f"[REF:{evidence_ref}]")
        body = " ".join([*tokens, text]).strip()
        docs.append({"source": "memory", "ts": ts, "text": body})
    return docs


def _extract_session_docs(cwd: str) -> List[Dict[str, str]]:
    docs: List[Dict[str, str]] = []
    path = os.path.join(cwd, "SESSION.json")
    data = read_json_locked(path, default={"orchestrator_by_task": {}})
    tasks = data.get("orchestrator_by_task", {}) if isinstance(data, dict) else {}
    if not isinstance(tasks, dict):
        return docs
    for task_id, entries in tasks.items():
        if not isinstance(entries, list):
            continue
        tail = entries[-12:]
        for idx, item in enumerate(tail):
            if not isinstance(item, dict):
                continue
            date_str = str(item.get("date") or "")
            base_source = f"session:{task_id}"
            user_text = str(item.get("user") or "").strip()
            if user_text:
                docs.append(
                    {
                        "source": f"{base_source}:user:{idx}",
                        "ts": date_str,
                        "text": user_text,
                    }
                )
            final_text = str(item.get("final") or "").strip()
            if final_text:
                docs.append(
                    {
                        "source": f"{base_source}:final:{idx}",
                        "ts": date_str,
                        "text": final_text,
                    }
                )
            step_results = item.get("step_results") or []
            if not isinstance(step_results, list):
                continue
            for j, sr in enumerate(step_results[-8:]):
                if not isinstance(sr, dict):
                    continue
                summary = str(sr.get("summary") or "").strip()
                title = str(sr.get("title") or "").strip()
                if not summary and not title:
                    continue
                text = f"{title}. {summary}".strip(". ")
                docs.append(
                    {
                        "source": f"{base_source}:step:{idx}:{j}",
                        "ts": date_str,
                        "text": text,
                    }
                )
    return docs


def _prepare_query(query: str) -> str:
    terms = [w.lower() for w in _WORD_RE.findall(query or "") if len(w) >= 2]
    if not terms:
        return ""
    uniq: List[str] = []
    seen = set()
    for term in terms:
        if term in seen:
            continue
        seen.add(term)
        uniq.append(term)
        if len(uniq) >= 8:
            break
    return " OR ".join(f"{t}*" for t in uniq)


def _safe_ts_to_dt(ts: str) -> datetime:
    raw = str(ts or "").strip()
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw, fmt)
        except Exception:
            continue
    return datetime.utcfromtimestamp(0)


def _recency_bonus(ts: str) -> float:
    dt = _safe_ts_to_dt(ts)
    age_days = max(0.0, (datetime.now(UTC).replace(tzinfo=None) - dt).total_seconds() / 86400.0)
    if age_days <= 1:
        return 1.0
    if age_days <= 7:
        return 0.7
    if age_days <= 30:
        return 0.4
    if age_days <= 90:
        return 0.2
    return 0.0


def _tag_weight(text: str) -> float:
    s = str(text or "").strip()
    m = _TAG_IN_TEXT_RE.match(s)
    if not m:
        return 0.1
    tag = m.group(1).upper()
    if tag == "PREF":
        return 1.0
    if tag == "DECISION":
        return 0.8
    if tag == "CONFIG":
        return 0.6
    if tag == "AGREEMENT":
        return 0.4
    if tag == "TASK":
        return 0.2
    return 0.1


def _memory_status_from_text(text: str) -> str:
    match = re.search(r"\[VER:([^\]]+)\]", str(text or ""), flags=re.IGNORECASE)
    if not match:
        return "legacy"
    status = match.group(1).strip().lower()
    if status in {"verified", "unverified", "legacy"}:
        return status
    return "legacy"


def _memory_evidence_from_text(text: str) -> str:
    match = re.search(r"\[EVID:([^\]]+)\]", str(text or ""), flags=re.IGNORECASE)
    if not match:
        return "legacy"
    evidence_type = match.group(1).strip().lower()
    if evidence_type in {"user", "tool", "code", "config", "system", "none", "legacy"}:
        return evidence_type
    return "legacy"


def _verification_weight(status: str) -> float:
    token = str(status or "").strip().lower()
    if token == "verified":
        return 0.35
    if token == "unverified":
        return -0.2
    return 0.0


def _connect(cwd: str) -> sqlite3.Connection:
    os.makedirs(cwd, exist_ok=True)
    conn = sqlite3.connect(_db_path(cwd))
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    return conn


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(
            doc_id UNINDEXED,
            source UNINDEXED,
            ts UNINDEXED,
            text,
            tokenize='unicode61'
        )
        """
    )


def _sync_index(conn: sqlite3.Connection, cwd: str) -> None:
    _ensure_schema(conn)
    docs = _extract_memory_docs(cwd) + _extract_session_docs(cwd)
    desired: Dict[str, tuple[str, str, str]] = {}
    for d in docs:
        text = str(d.get("text") or "").strip()
        if not text:
            continue
        source = str(d.get("source") or "unknown")
        ts = str(d.get("ts") or "")
        doc_id = _mk_doc_id(source, ts, text)
        desired[doc_id] = (source, ts, text)

    existing_rows = conn.execute(
        "SELECT rowid, doc_id, source, ts, text FROM memory_fts"
    ).fetchall()

    existing_by_doc: Dict[str, tuple[int, str, str, str]] = {}
    duplicate_rowids: List[int] = []
    for rowid, doc_id, source, ts, text in existing_rows:
        key = str(doc_id or "")
        if key in existing_by_doc:
            duplicate_rowids.append(int(rowid))
            continue
        existing_by_doc[key] = (int(rowid), str(source or ""), str(ts or ""), str(text or ""))

    if duplicate_rowids:
        conn.executemany("DELETE FROM memory_fts WHERE rowid = ?", [(rid,) for rid in duplicate_rowids])

    to_delete = [doc_id for doc_id in existing_by_doc.keys() if doc_id not in desired]
    if to_delete:
        conn.executemany("DELETE FROM memory_fts WHERE doc_id = ?", [(doc_id,) for doc_id in to_delete])

    to_insert = []
    for doc_id, (source, ts, text) in desired.items():
        prev = existing_by_doc.get(doc_id)
        if prev is None:
            to_insert.append((doc_id, source, ts, text))
            continue
        _, prev_source, prev_ts, prev_text = prev
        if prev_source != source or prev_ts != ts or prev_text != text:
            conn.execute("DELETE FROM memory_fts WHERE doc_id = ?", (doc_id,))
            to_insert.append((doc_id, source, ts, text))
    if to_insert:
        conn.executemany(
            "INSERT INTO memory_fts(doc_id, source, ts, text) VALUES (?, ?, ?, ?)",
            to_insert,
        )
    conn.commit()


def retrieve_relevant_context(cwd: str, query: str, limit: int = 6, *, verified_only: bool = False) -> List[Dict[str, Any]]:
    if not (query or "").strip():
        return []
    try:
        conn = _connect(cwd)
    except Exception as e:
        _log.exception("tool failed %s", e)
        return []
    try:
        try:
            _sync_index(conn, cwd)
        except Exception as e:
            _log.exception("tool failed %s", e)
            return []
        prepared = _prepare_query(query)
        if not prepared:
            _log.warning("memory_retrieval: query %r produced no FTS terms after preparation; returning empty", query)
            return []
        query_sql = """
            SELECT source, ts, text, snippet(memory_fts, 3, '[', ']', ' … ', 20), bm25(memory_fts)
            FROM memory_fts
            WHERE memory_fts MATCH ?
            ORDER BY bm25(memory_fts), ts DESC
            """
        if verified_only:
            rows = conn.execute(query_sql, (prepared,)).fetchall()
        else:
            candidate_limit = max(3, int(limit) * 3)
            rows = conn.execute(f"{query_sql} LIMIT ?", (prepared, candidate_limit)).fetchall()
        scored: List[Dict[str, Any]] = []
        for source, ts, full_text, snippet_text, score in rows:
            source_text = str(source or "")
            raw_bm25 = float(score or 0.0)
            memory_status = _memory_status_from_text(str(full_text or "")) if source_text == "memory" else ""
            evidence_type = _memory_evidence_from_text(str(full_text or "")) if source_text == "memory" else ""
            if verified_only and (
                source_text != "memory"
                or memory_status != "verified"
                or evidence_type in ("", "none", "legacy")
            ):
                continue
            # Higher final_score is better.
            final_score = (
                (-raw_bm25)
                + _recency_bonus(str(ts or ""))
                + _tag_weight(str(full_text or snippet_text or ""))
                + _verification_weight(memory_status)
            )
            scored.append(
                {
                    "source": str(source or ""),
                    "ts": str(ts or ""),
                    "text": str(snippet_text or ""),
                    "memory_status": memory_status,
                    "evidence_type": evidence_type,
                    "score": raw_bm25,
                    "rank_score": final_score,
                }
            )
        scored.sort(key=lambda x: float(x.get("rank_score") or 0.0), reverse=True)
        return scored[: max(1, int(limit))]
    except Exception as e:
        _log.exception("tool failed %s", e)
        return []
    finally:
        try:
            conn.close()
        except Exception as e:
            _log.exception("tool failed %s", e)


def format_retrieved_context(items: List[Dict[str, Any]], max_chars: int = 1600) -> str:
    if not items:
        return ""
    lines: List[str] = []
    for item in items:
        source = str(item.get("source") or "unknown")
        ts = str(item.get("ts") or "")
        text = str(item.get("text") or "").strip()
        if not text:
            continue
        head = f"- [{source}]"
        if ts:
            head += f" ({ts})"
        memory_status = str(item.get("memory_status") or "").strip()
        if memory_status:
            head += f" memory_status={memory_status}"
        lines.append(f"{head}: {text}")
    if not lines:
        return ""
    content = "\n".join(lines)
    if len(content) <= max_chars:
        return content
    marker = f"\n[degraded_retrieved_context_trimmed chars_removed={len(content) - max_chars}]\n"
    if max_chars <= len(marker) + 32:
        minimal_marker = f"[degraded_retrieved_context_trimmed chars_removed={len(content) - max_chars}]"
        return minimal_marker[:max_chars]
    head_len = max(16, (max_chars - len(marker)) // 2)
    tail_len = max(16, max_chars - len(marker) - head_len)
    return content[:head_len] + marker + content[-tail_len:]


def retrieve_relevant_context_for_chat(
    workdir: str,
    chat_id: Any,
    query: str,
    limit: int = 6,
    verified_only: bool = False,
) -> List[Dict[str, Any]]:
    cwd = chat_workspace_root(workdir, chat_id)
    return retrieve_relevant_context(cwd, query, limit=limit, verified_only=verified_only)
