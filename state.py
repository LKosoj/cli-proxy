import json
import os
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional


@dataclass
class SessionState:
    tool: str
    workdir: str
    resume_token: Optional[str]
    summary: Optional[str]
    updated_at: float
    name: Optional[str] = None


@dataclass
class ActiveState:
    tool: str
    workdir: str
    updated_at: float
    session_id: Optional[str] = None


def _load_raw(path: str) -> Dict[str, Any]:
    if not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as f:
        content = f.read().strip()
        if not content:
            return {}
        return json.loads(content)


def _save_raw(path: str, raw: Dict[str, Any]) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(raw, f, ensure_ascii=False, indent=2)


def load_state(path: str) -> Dict[str, SessionState]:
    raw = _load_raw(path)
    result: Dict[str, SessionState] = {}
    for key, val in raw.items():
        if key in ("_active", "_sessions"):
            continue
        result[key] = SessionState(
            tool=val.get("tool", ""),
            workdir=val.get("workdir", ""),
            resume_token=val.get("resume_token"),
            summary=val.get("summary"),
            updated_at=float(val.get("updated_at", 0)),
            name=val.get("name"),
        )
    return result


def save_state(path: str, data: Dict[str, SessionState]) -> None:
    raw: Dict[str, Any] = _load_raw(path)
    for key, val in data.items():
        raw[key] = {
            "tool": val.tool,
            "workdir": val.workdir,
            "resume_token": val.resume_token,
            "summary": val.summary,
            "updated_at": val.updated_at,
            "name": val.name,
        }
    _save_raw(path, raw)


def make_key(tool: str, workdir: str) -> str:
    return f"{tool}::{workdir}"


def update_state(path: str, tool: str, workdir: str, resume_token: Optional[str], summary: Optional[str], name: Optional[str] = None) -> None:
    data = load_state(path)
    key = make_key(tool, workdir)
    data[key] = SessionState(
        tool=tool,
        workdir=workdir,
        resume_token=resume_token,
        summary=summary,
        updated_at=time.time(),
        name=name,
    )
    save_state(path, data)


def get_state(path: str, tool: str, workdir: str) -> Optional[SessionState]:
    data = load_state(path)
    return data.get(make_key(tool, workdir))


def load_active_state(path: str) -> Optional[ActiveState]:
    raw = _load_raw(path)
    val = raw.get("_active")
    if not val:
        return None
    return ActiveState(
        tool=val.get("tool", ""),
        workdir=val.get("workdir", ""),
        updated_at=float(val.get("updated_at", 0)),
        session_id=val.get("session_id"),
    )


def set_active_state(path: str, tool: str, workdir: str, session_id: Optional[str] = None) -> None:
    data: Dict[str, Any] = _load_raw(path)
    data["_active"] = {
        "tool": tool,
        "workdir": workdir,
        "updated_at": time.time(),
        "session_id": session_id,
    }
    _save_raw(path, data)


def clear_active_state(path: str) -> None:
    data = _load_raw(path)
    if "_active" in data:
        del data["_active"]
        _save_raw(path, data)


def load_sessions(path: str) -> Dict[str, Any]:
    raw = _load_raw(path)
    return raw.get("_sessions", {})


def save_sessions(path: str, sessions: Dict[str, Any]) -> None:
    raw = _load_raw(path)
    raw["_sessions"] = sessions
    _save_raw(path, raw)
