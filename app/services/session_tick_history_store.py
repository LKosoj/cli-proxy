import hashlib
import json
import logging
import os
import re
import tempfile
from typing import Any, Dict, List

from app.services.path_normalization import normalize_optional_state_path

logger = logging.getLogger(__name__)

MAX_TICKS_PER_SESSION = 100


def _safe_name(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value or ""))
    return cleaned.strip("._") or "unknown"


def _base_dir(session: Any) -> str:
    cfg = getattr(session, "config", None)
    defaults = getattr(cfg, "defaults", None)
    try:
        state_path = normalize_optional_state_path(getattr(defaults, "state_path", None))
    except TypeError:
        state_path = None
    if state_path:
        root = os.path.dirname(os.path.abspath(state_path)) or "."
    else:
        root = str(getattr(session, "workdir", "") or ".")
    return os.path.join(root, "session_ticks")


def _history_path(session: Any) -> str:
    from session import session_runtime_uid

    session_uid = session_runtime_uid(session)
    if session_uid:
        return os.path.join(_base_dir(session), f"{_safe_name(session_uid)}.json")

    sid = _safe_name(str(getattr(session, "id", "") or "unknown"))
    chat_id = getattr(session, "chat_id", None)
    workdir = str(getattr(session, "workdir", "") or "")
    workdir_hash = hashlib.sha1(workdir.encode("utf-8", errors="ignore")).hexdigest()[:10] if workdir else "noworkdir"
    if chat_id is not None:
        try:
            fallback_key = f"{int(chat_id)}_{sid}_{workdir_hash}"
        except Exception:
            logger.exception("tick store failed to normalize chat_id; fallback uid will be used")
            fallback_key = ""
    else:
        fallback_key = ""
    if not fallback_key:
        tool_name = str(getattr(getattr(session, "tool", None), "name", "") or "")
        digest = hashlib.sha1(f"{sid}|{workdir}|{tool_name}".encode("utf-8", errors="ignore")).hexdigest()[:16]
        fallback_key = f"unknown_{sid}_{workdir_hash}_{digest}"
    return os.path.join(_base_dir(session), f"{fallback_key}.json")


def _normalize_entry(raw: Any, *, allow_short: bool = False) -> Dict[str, Any] | None:
    kind_value: str | None = None
    if isinstance(raw, dict):
        value = raw.get("value")
        ts_raw = raw.get("ts")
        allow_short = bool(raw.get("allow_short", allow_short))
        raw_kind = str(raw.get("kind") or "").strip().lower()
        kind_value = raw_kind or None
    else:
        value = raw
        ts_raw = None
    text = "" if value is None else str(value)
    ts_value: float | None = None
    if ts_raw is not None:
        try:
            ts_value = float(ts_raw)
        except Exception:
            logger.exception("tick store failed to parse tick ts")
            ts_value = None
    if len(text.strip()) < 6 and not allow_short:
        return None
    entry: Dict[str, Any] = {"ts": ts_value, "value": text}
    if allow_short:
        entry["allow_short"] = True
    if kind_value:
        entry["kind"] = kind_value
    return entry


def _read_list(path: str) -> List[Dict[str, Any]]:
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except PermissionError:
        # Тихо игнорируем ошибки прав доступа (например, при запуске от claude-bot)
        logger.info("tick store permission denied read path=%s", path)
        return []
    except Exception:
        logger.exception("tick store failed to read file path=%s", path)
        return []
    if not isinstance(raw, list):
        return []
    items: List[Dict[str, Any]] = []
    for item in raw:
        normalized = _normalize_entry(item)
        if normalized is not None:
            items.append(normalized)
    return items


def _write_atomic(path: str, items: List[Dict[str, Any]]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        dir=os.path.dirname(path) or ".",
        prefix=".ticks_",
        suffix=".tmp",
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(items, f, ensure_ascii=False, separators=(",", ":"))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except Exception:
        logger.exception("tick store failed to write file path=%s", path)
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


def load_session_ticks(session: Any, *, limit: int = MAX_TICKS_PER_SESSION) -> List[Dict[str, Any]]:
    # Use cache from session object if available to avoid redundant disk I/O
    cache = getattr(session, "_ticks_cache", None)
    if cache is not None:
        max_items = int(limit) if int(limit) > 0 else MAX_TICKS_PER_SESSION
        return cache[-max_items:]

    path = _history_path(session)
    items = _read_list(path)
    # Store in memory cache
    try:
        setattr(session, "_ticks_cache", items)
    except Exception:
        pass

    max_items = int(limit) if int(limit) > 0 else MAX_TICKS_PER_SESSION
    return items[-max_items:]


def append_session_tick(
    session: Any,
    *,
    value: str,
    ts: float,
    limit: int = MAX_TICKS_PER_SESSION,
    allow_short: bool = False,
    kind: str | None = None,
    replace_last: bool = False,
) -> bool:
    path = _history_path(session)
    # Try to use memory cache first if it exists to avoid read_list call
    items = getattr(session, "_ticks_cache", None)
    if items is None:
        items = _read_list(path)

    normalized = _normalize_entry(
        {
            "ts": ts,
            "value": value,
            "allow_short": allow_short,
            "kind": kind,
        }
    )
    if normalized is None:
        return False
    replaced = False
    normalized_kind = str(normalized.get("kind") or "").strip().lower() or None
    if replace_last and items and normalized_kind:
        last_item = items[-1] if isinstance(items[-1], dict) else None
        last_kind = str((last_item or {}).get("kind") or "").strip().lower() or None
        if last_kind == normalized_kind:
            items[-1] = normalized
            replaced = True
    if not replaced:
        items.append(normalized)
    max_items = int(limit) if int(limit) > 0 else MAX_TICKS_PER_SESSION
    items = items[-max_items:]

    # Update memory cache
    try:
        setattr(session, "_ticks_cache", items)
    except Exception:
        pass

    try:
        _write_atomic(path, items)
    except PermissionError:
        # Тихо игнорируем ошибки прав доступа (например, при запуске от claude-bot)
        logger.info("tick store permission denied path=%s", path)
    except Exception:
        logger.exception("tick store failed to append path=%s", path)
    return replaced


def clear_session_ticks(session: Any) -> None:
    # Clear memory cache
    if hasattr(session, "_ticks_cache"):
        try:
            delattr(session, "_ticks_cache")
        except Exception:
            pass

    path = _history_path(session)
    try:
        if os.path.exists(path):
            os.remove(path)
    except Exception:
        logger.exception("tick store failed to clear file path=%s", path)
