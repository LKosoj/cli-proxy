"""Write CanonicalSession into Grok Build session files."""

from __future__ import annotations

import json
import os
import urllib.parse
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

from ._user_paths import chown_path, home_for_user, mkdir_chain_with_chown
from .canonical import CanonicalMessage, CanonicalSession

GROK_SESSIONS_BASE = Path.home() / ".grok" / "sessions"
DEFAULT_GROK_MODEL = "grok-build"
DEFAULT_CONTEXT_WINDOW_TOKENS = 512_000


def _resolve_sessions_base(username: Optional[str]) -> Path:
    home = home_for_user(username)
    if home is None:
        return GROK_SESSIONS_BASE
    return home / ".grok" / "sessions"


def _workspace_key(workdir: str) -> str:
    raw = os.path.realpath(str(workdir or "")).rstrip(os.sep) or str(workdir or "")
    return urllib.parse.quote(raw, safe="") if raw else ""


def _iso_z(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _msg_dt(msg_ts: Optional[float], fallback: datetime) -> datetime:
    if not msg_ts:
        return fallback
    try:
        return datetime.fromtimestamp(msg_ts, tz=timezone.utc)
    except Exception:
        return fallback


def _title_from_messages(messages: Iterable[CanonicalMessage]) -> str:
    for msg in messages:
        if msg.role != "user":
            continue
        text = " ".join(str(msg.content or "").strip().split())
        if text:
            return text[:80]
    return "Transferred session"


def _build_chat_records(canonical: CanonicalSession) -> list[Dict[str, Any]]:
    records: list[Dict[str, Any]] = []
    for msg in canonical.messages:
        text = str(msg.content or "").strip()
        if not text:
            continue
        if msg.role == "user":
            records.append({"type": "user", "content": [{"type": "text", "text": text}]})
        elif msg.role == "assistant":
            records.append({"type": "assistant", "content": text, "model_id": DEFAULT_GROK_MODEL})
        elif msg.role == "tool":
            records.append({"type": "tool_result", "content": text, "tool_call_id": "transfer"})
    return records


def _build_update_records(
    canonical: CanonicalSession,
    *,
    session_id: str,
    started_at: datetime,
) -> list[Dict[str, Any]]:
    records: list[Dict[str, Any]] = []
    event_idx = 0
    for msg in canonical.messages:
        text = str(msg.content or "").strip()
        if not text or msg.role not in ("user", "assistant"):
            continue
        when = _msg_dt(msg.timestamp, started_at)
        timestamp = int(when.timestamp())
        update_type = "user_message_chunk" if msg.role == "user" else "agent_message_chunk"
        records.append(
            {
                "method": "session/update",
                "timestamp": timestamp,
                "params": {
                    "sessionId": session_id,
                    "update": {
                        "sessionUpdate": update_type,
                        "content": {"type": "text", "text": text},
                    },
                    "_meta": {
                        "eventId": f"{session_id}-{event_idx}",
                        "agentTimestampMs": int(when.timestamp() * 1000),
                    },
                },
            }
        )
        event_idx += 1
    return records


def _build_summary(
    canonical: CanonicalSession,
    *,
    session_id: str,
    workspace: str,
    started_at: datetime,
) -> Dict[str, Any]:
    title = str(canonical.summary or "").strip() or _title_from_messages(canonical.messages)
    user_count = sum(1 for msg in canonical.messages if msg.role == "user")
    assistant_count = sum(1 for msg in canonical.messages if msg.role == "assistant")
    now_iso = _iso_z(started_at)
    return {
        "session_summary": title,
        "created_at": now_iso,
        "updated_at": now_iso,
        "last_active_at": now_iso,
        "num_messages": len(canonical.messages),
        "num_chat_messages": user_count + assistant_count,
        "current_model_id": DEFAULT_GROK_MODEL,
        "chat_format_version": 1,
        "git_root_dir": os.path.realpath(workspace).rstrip(os.sep) + os.sep,
        "generated_title": title,
        "agent_name": DEFAULT_GROK_MODEL,
        "request_id": str(uuid.uuid4()),
        "info": {
            "id": session_id,
            "cwd": os.path.realpath(workspace),
            "transferred_from_cli": canonical.source_cli,
            "source_session_id": canonical.session_id,
            "target_session_id": session_id,
        },
    }


def _build_signals(canonical: CanonicalSession) -> Dict[str, Any]:
    user_count = sum(1 for msg in canonical.messages if msg.role == "user")
    assistant_count = sum(1 for msg in canonical.messages if msg.role == "assistant")
    return {
        "turnCount": user_count,
        "userMessageCount": user_count,
        "assistantMessageCount": assistant_count,
        "toolCallCount": sum(1 for msg in canonical.messages if msg.role == "tool"),
        "contextTokensUsed": 0,
        "contextWindowTokens": DEFAULT_CONTEXT_WINDOW_TOKENS,
        "contextWindowUsage": 0,
        "primaryModelId": DEFAULT_GROK_MODEL,
        "modelsUsed": [DEFAULT_GROK_MODEL],
        "toolsUsed": [],
    }


def _write_json(path: Path, payload: Dict[str, Any], username: Optional[str]) -> None:
    mkdir_chain_with_chown(path.parent, username)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
    chown_path(path, username)


def _write_jsonl(path: Path, records: Iterable[Dict[str, Any]], username: Optional[str]) -> None:
    mkdir_chain_with_chown(path.parent, username)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False))
            handle.write("\n")
    os.replace(tmp, path)
    chown_path(path, username)


def write_session(canonical: CanonicalSession, workspace: str, username: Optional[str] = None) -> Optional[str]:
    """Write canonical session as a Grok Build session. Returns the new session id."""
    if not canonical.messages:
        return None
    key = _workspace_key(workspace)
    if not key:
        return None

    new_sid = str(uuid.uuid4())
    started_at = datetime.now(timezone.utc)
    target = _resolve_sessions_base(username) / key / new_sid

    _write_json(
        target / "summary.json",
        _build_summary(canonical, session_id=new_sid, workspace=workspace, started_at=started_at),
        username,
    )
    _write_json(target / "signals.json", _build_signals(canonical), username)
    _write_json(
        target / "prompt_context.json",
        {
            "version": 1,
            "prompt_mode": "transfer",
            "working_directory": os.path.realpath(workspace),
            "memory_enabled": False,
            "is_non_interactive": False,
            "build_timestamp_utc": _iso_z(started_at),
        },
        username,
    )
    _write_jsonl(target / "chat_history.jsonl", _build_chat_records(canonical), username)
    _write_jsonl(
        target / "updates.jsonl",
        _build_update_records(canonical, session_id=new_sid, started_at=started_at),
        username,
    )
    # Keep files Grok expects optional but present for tooling/export compatibility.
    _write_jsonl(target / "events.jsonl", [], username)
    try:
        (target / "terminal").mkdir(exist_ok=True)
        (target / "videos").mkdir(exist_ok=True)
        chown_path(target / "terminal", username)
        chown_path(target / "videos", username)
    except Exception:
        pass
    return new_sid
