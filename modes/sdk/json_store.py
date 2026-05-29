from __future__ import annotations

import json
import os
from typing import Any, Callable, Dict, Optional

from modes.sdk.file_lock import lock_file, unlock_file
from modes.sdk.runtime.json_normalizer import loads_safe


def _ensure_parent(path: str) -> None:
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)


def read_json_locked(path: str, default: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    default = default or {}
    _ensure_parent(path)
    try:
        with open(path, "a+", encoding="utf-8") as f:
            lock_file(f, shared=True)
            try:
                f.seek(0)
                raw = f.read()
                if not raw.strip():
                    return dict(default)
                data = loads_safe(raw, strict_first=True)
                if isinstance(data, dict):
                    return data
                return dict(default)
            finally:
                unlock_file(f)
    except Exception:
        return dict(default)


def read_json_locked_if_exists(path: str, default: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    if not os.path.exists(path):
        return dict(default or {})
    return read_json_locked(path, default=default)


def write_json_locked(path: str, data: Dict[str, Any]) -> None:
    _ensure_parent(path)
    with open(path, "a+", encoding="utf-8") as f:
        lock_file(f, shared=False)
        try:
            f.seek(0)
            f.truncate(0)
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        finally:
            unlock_file(f)


def update_json_locked(
    path: str,
    updater: Callable[[Dict[str, Any]], Dict[str, Any]],
    default: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    default = default or {}
    _ensure_parent(path)
    with open(path, "a+", encoding="utf-8") as f:
        lock_file(f, shared=False)
        try:
            f.seek(0)
            raw = f.read()
            if raw.strip():
                try:
                    current = loads_safe(raw, strict_first=True)
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
            unlock_file(f)
