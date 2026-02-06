import json
import logging
import os
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional


@dataclass
class SessionState:
    session_id: Optional[str]
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
    try:
        with open(path, "r", encoding="utf-8") as f:
            content = f.read().strip()
            if not content:
                return {}
            return json.loads(content)
    except Exception as e:
        logging.exception(f"tool failed {str(e)}")
        return {}


def _save_raw(path: str, raw: Dict[str, Any]) -> None:
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(raw, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logging.exception(f"tool failed {str(e)}")


def load_state(path: str) -> Dict[str, SessionState]:
    """
    Returns per-session state stored in state.json under the "_sessions" section.

    Previously, state was stored at top-level keys "{tool}::{workdir}", which collides when
    multiple sessions share the same tool/workdir. We keep reading legacy entries as a fallback
    only when we can't derive session_id.
    """
    raw = _load_raw(path)
    result: Dict[str, SessionState] = {}

    sessions = raw.get("_sessions", {}) or {}
    if isinstance(sessions, dict):
        for sid, val in sessions.items():
            if not isinstance(val, dict):
                continue
            tool = str(val.get("tool", "") or "")
            workdir = str(val.get("workdir", "") or "")
            if not tool or not workdir:
                continue
            updated_at = val.get("updated_at")
            try:
                updated_ts = float(updated_at) if updated_at is not None else 0.0
            except Exception:
                updated_ts = 0.0
            result[str(sid)] = SessionState(
                session_id=str(sid),
                tool=tool,
                workdir=workdir,
                resume_token=val.get("resume_token"),
                summary=val.get("summary"),
                updated_at=updated_ts,
                name=val.get("name"),
            )

    # Legacy top-level entries (tool::workdir). Keep them only if we don't have per-session state.
    if not result:
        for key, val in raw.items():
            if key in ("_active", "_sessions"):
                continue
            if not isinstance(val, dict):
                continue
            result[str(key)] = SessionState(
                session_id=None,
                tool=str(val.get("tool", "") or ""),
                workdir=str(val.get("workdir", "") or ""),
                resume_token=val.get("resume_token"),
                summary=val.get("summary"),
                updated_at=float(val.get("updated_at", 0) or 0),
                name=val.get("name"),
            )

    return result


def save_state(path: str, data: Dict[str, SessionState]) -> None:
    """
    Save per-session state into "_sessions".
    """
    raw: Dict[str, Any] = _load_raw(path)
    sessions = raw.get("_sessions")
    if not isinstance(sessions, dict):
        sessions = {}
    for sid, val in data.items():
        sessions[str(sid)] = {
            "tool": val.tool,
            "workdir": val.workdir,
            "resume_token": val.resume_token,
            "summary": val.summary,
            "updated_at": val.updated_at,
            "name": val.name,
        }
    raw["_sessions"] = sessions
    _save_raw(path, raw)

def delete_state(path: str, tool: str, workdir: str) -> None:
    # Legacy helper: previously deleted the top-level "{tool}::{workdir}" entry.
    raw = _load_raw(path)
    key = make_key(tool, workdir)
    if key in raw:
        del raw[key]
        _save_raw(path, raw)


def make_key(tool: str, workdir: str) -> str:
    return f"{tool}::{workdir}"


def update_state(path: str, tool: str, workdir: str, resume_token: Optional[str], summary: Optional[str], name: Optional[str] = None) -> None:
    # Legacy helper kept for backward compatibility. Prefer storing state per session in "_sessions"
    # via SessionManager._persist_sessions().
    data = load_state(path)
    key = make_key(tool, workdir)
    data[key] = SessionState(
        session_id=None,
        tool=tool,
        workdir=workdir,
        resume_token=resume_token,
        summary=summary,
        updated_at=time.time(),
        name=name,
    )
    # Only write legacy entry if we don't have per-session state at all.
    raw = _load_raw(path)
    if raw.get("_sessions"):
        # Avoid re-introducing ambiguous state when sessions exist.
        return
    raw[key] = {
        "tool": tool,
        "workdir": workdir,
        "resume_token": resume_token,
        "summary": summary,
        "updated_at": time.time(),
        "name": name,
    }
    _save_raw(path, raw)


def get_state(path: str, tool: str, workdir: str, session_id: Optional[str] = None) -> Optional[SessionState]:
    """
    Get state for a specific session (preferred) or by (tool, workdir) only when unique.
    """
    data = load_state(path)
    if session_id:
        st = data.get(str(session_id))
        if st:
            return st
    # Fallback: find unique match by tool/workdir among sessions.
    matches = [st for st in data.values() if st.tool == tool and st.workdir == workdir]
    if len(matches) == 1:
        return matches[0]
    # If legacy-only load_state returned tool::workdir keys, try direct key lookup.
    legacy = data.get(make_key(tool, workdir))
    return legacy


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
