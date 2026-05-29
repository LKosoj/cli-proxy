"""Write CanonicalSession into a Qwen Code chat file."""

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

QWEN_PROJECTS_BASE = Path.home() / ".qwen" / "projects"


def _resolve_projects_base(username: Optional[str]) -> Path:
    home = home_for_user(username)
    if home is None:
        return QWEN_PROJECTS_BASE
    return home / ".qwen" / "projects"


def _project_key(workdir: str) -> str:
    raw = os.path.realpath(workdir).rstrip(os.sep) or workdir
    return re.sub(r"[^A-Za-z0-9-]", "-", raw)


def _iso_z(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


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
    records: List[Dict[str, Any]] = []
    parent_uuid: Optional[str] = None
    for msg in canonical.messages:
        if msg.role not in ("user", "assistant"):
            continue
        msg_uuid = str(uuid.uuid4())
        when = _msg_dt(msg.timestamp, started_at)
        base = {
            "uuid": msg_uuid,
            "parentUuid": parent_uuid,
            "sessionId": new_sid,
            "timestamp": _iso_z(when),
            "cwd": workspace,
            "version": "0.13.1",
        }
        if msg.role == "user":
            records.append({
                **base,
                "type": "user",
                "message": {"role": "user", "parts": [{"text": msg.content}]},
            })
        else:
            records.append({
                **base,
                "type": "assistant",
                "model": "coder-model",
                "message": {"role": "model", "parts": [{"text": msg.content}]},
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


def write_session(canonical: CanonicalSession, workspace: str, username: Optional[str] = None) -> Optional[str]:
    """Write canonical session as a Qwen .jsonl chat. Returns the new sessionId."""
    if not canonical.messages:
        return None
    key = _project_key(workspace)
    if not key:
        return None

    new_sid = str(uuid.uuid4())
    started_at = datetime.now(timezone.utc)
    target = _resolve_projects_base(username) / key / "chats" / f"{new_sid}.jsonl"

    try:
        records = _build_records(canonical, workspace, new_sid, started_at)
        _atomic_write_jsonl(target, records, username)
    except Exception:
        logger.exception("qwen writer: failed to write chat file")
        return None
    return new_sid
