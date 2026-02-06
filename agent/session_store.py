from __future__ import annotations

import fcntl
import json
import os
from typing import Any, Callable, Dict, Optional


def _ensure_parent(path: str) -> None:
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)


def read_json_locked(path: str, default: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """
    Read JSON file under a shared lock.
    Returns default (or {}) on any error or if file is empty.
    """
    default = default or {}
    _ensure_parent(path)
    try:
        with open(path, "a+", encoding="utf-8") as f:
            fcntl.flock(f.fileno(), fcntl.LOCK_SH)
            try:
                f.seek(0)
                raw = f.read()
                if not raw.strip():
                    return dict(default)
                data = json.loads(raw)
                if isinstance(data, dict):
                    return data
                return dict(default)
            finally:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)
    except Exception:
        return dict(default)


def write_json_locked(path: str, data: Dict[str, Any]) -> None:
    """
    Write JSON file under an exclusive lock (in-place truncate+write).
    This avoids races and keeps the file path stable.
    """
    _ensure_parent(path)
    with open(path, "a+", encoding="utf-8") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            f.seek(0)
            f.truncate(0)
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)


def update_json_locked(
    path: str,
    updater: Callable[[Dict[str, Any]], Dict[str, Any]],
    default: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Read-modify-write under an exclusive lock.
    updater() must return the new dict to write.
    """
    default = default or {}
    _ensure_parent(path)
    with open(path, "a+", encoding="utf-8") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            f.seek(0)
            raw = f.read()
            if raw.strip():
                try:
                    current = json.loads(raw)
                except Exception:
                    current = dict(default)
            else:
                current = dict(default)
            if not isinstance(current, dict):
                current = dict(default)
            updated = updater(current)
            if not isinstance(updated, dict):
                updated = current
            f.seek(0)
            f.truncate(0)
            json.dump(updated, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
            return updated
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)

