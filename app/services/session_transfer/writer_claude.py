"""Write CanonicalSession into a Claude Code session file."""

from __future__ import annotations

import json
import logging
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from ._user_paths import chown_path, home_for_user, mkdir_chain_with_chown
from .canonical import CanonicalSession

logger = logging.getLogger(__name__)

CLAUDE_BASE = Path.home() / ".claude"
CLAUDE_PROJECTS_DIR = CLAUDE_BASE / "projects"
CLAUDE_HISTORY_FILE = CLAUDE_BASE / "history.jsonl"


def _resolve_paths(username: Optional[str]) -> tuple[Path, Path]:
    home = home_for_user(username)
    if home is None:
        return CLAUDE_PROJECTS_DIR, CLAUDE_HISTORY_FILE
    base = home / ".claude"
    return base / "projects", base / "history.jsonl"


def _project_key(workdir: str) -> str:
    raw = os.path.realpath(workdir).rstrip(os.sep) or workdir
    return re.sub(r"[^A-Za-z0-9-]", "-", raw)


def _iso_z(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _ms(dt: datetime) -> int:
    return int(dt.astimezone(timezone.utc).timestamp() * 1000)


def _msg_dt(msg_ts: Optional[float], fallback: datetime) -> datetime:
    if not msg_ts:
        return fallback
    try:
        return datetime.fromtimestamp(msg_ts, tz=timezone.utc)
    except Exception:
        return fallback


def _build_records(
    canonical: CanonicalSession,
    workspace: str,
    new_sid: str,
    started_at: datetime,
) -> List[Dict[str, Any]]:
    records: List[Dict[str, Any]] = [
        {
            "type": "permission-mode",
            "permissionMode": "default",
            "sessionId": new_sid,
        }
    ]
    parent_uuid: Optional[str] = None
    prompt_id = str(uuid.uuid4())
    for msg in canonical.messages:
        if msg.role not in ("user", "assistant"):
            continue
        msg_uuid = str(uuid.uuid4())
        when = _msg_dt(msg.timestamp, started_at)
        if msg.role == "user":
            records.append({
                "parentUuid": parent_uuid,
                "isSidechain": False,
                "promptId": prompt_id,
                "type": "user",
                "uuid": msg_uuid,
                "timestamp": _iso_z(when),
                "cwd": workspace,
                "sessionId": new_sid,
                "version": "2.1.116",
                "gitBranch": "main",
                "userType": "external",
                "entrypoint": "cli",
                "message": {
                    "role": "user",
                    "content": msg.content,
                },
            })
        else:
            records.append({
                "parentUuid": parent_uuid,
                "isSidechain": False,
                "type": "assistant",
                "uuid": msg_uuid,
                "timestamp": _iso_z(when),
                "cwd": workspace,
                "sessionId": new_sid,
                "version": "2.1.116",
                "gitBranch": "main",
                "message": {
                    "model": "claude-opus-4-7",
                    "role": "assistant",
                    "content": [{"type": "text", "text": msg.content}],
                    "stop_reason": "end_turn",
                },
            })
        parent_uuid = msg_uuid
    return records


def _atomic_write_jsonl(target: Path, records: List[Dict[str, Any]], username: Optional[str]) -> None:
    mkdir_chain_with_chown(target.parent, username)
    tmp = target.with_suffix(target.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec, ensure_ascii=False))
            f.write("\n")
    os.replace(tmp, target)
    chown_path(target, username)


def _append_history(history_file: Path, new_sid: str, canonical: CanonicalSession, fallback: datetime, username: Optional[str]) -> None:
    try:
        existed = history_file.exists()
        mkdir_chain_with_chown(history_file.parent, username)
        with open(history_file, "a", encoding="utf-8") as f:
            for msg in canonical.messages:
                if msg.role != "user":
                    continue
                when = _msg_dt(msg.timestamp, fallback)
                f.write(json.dumps({
                    "sessionId": new_sid,
                    "timestamp": _ms(when),
                    "display": msg.content,
                    "pastedContents": {},
                }, ensure_ascii=False))
                f.write("\n")
        if not existed:
            chown_path(history_file, username)
    except Exception:
        logger.exception("claude writer: failed to append history.jsonl")


def write_session(canonical: CanonicalSession, workspace: str, username: Optional[str] = None) -> Optional[str]:
    """Write canonical session as a Claude Code .jsonl. Returns the new sessionId."""
    if not canonical.messages:
        return None
    key = _project_key(workspace)
    if not key:
        return None

    projects_dir, history_file = _resolve_paths(username)
    new_sid = str(uuid.uuid4())
    started_at = datetime.now(timezone.utc)
    target = projects_dir / key / f"{new_sid}.jsonl"

    try:
        records = _build_records(canonical, workspace, new_sid, started_at)
        _atomic_write_jsonl(target, records, username)
    except Exception:
        logger.exception("claude writer: failed to write session file")
        return None

    _append_history(history_file, new_sid, canonical, started_at, username)
    return new_sid
