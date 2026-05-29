from __future__ import annotations

from typing import Any, Mapping

_TG_CALLBACK_DATA_MAX_BYTES = 64

# Short prefix for mode action callbacks (was "mode_action").
MODE_ACTION_PREFIX = "ma"

# Both prefixes recognised by the router for backward compatibility.
MODE_ACTION_PREFIXES = ("ma:", "mode_action:")


def _session_id(session: Any) -> str:
    """Return the short session id (e.g. 's1'), not the full runtime UID."""
    return str(getattr(session, "id", "") or "").strip()


def parse_compact_callback_payload(raw: str) -> dict[str, str]:
    pairs: dict[str, str] = {}
    token = str(raw or "").strip()
    if not token:
        return pairs
    for chunk in token.replace("&", "|").split("|"):
        part = str(chunk or "").strip()
        if not part or "=" not in part:
            continue
        key, value = part.split("=", 1)
        key = str(key or "").strip()
        if not key:
            continue
        pairs[key] = str(value or "").strip()
    return pairs


def _session_runtime_uid(session: Any) -> str:
    from session import session_runtime_uid

    return session_runtime_uid(session)


def build_session_overview_callback_data(session: Any) -> str:
    session_uid = _session_runtime_uid(session)
    if session_uid:
        return f"sess_active_pick:{session_uid}"
    return "sess_active"


def build_session_mode_pick_callback_data(session: Any, mode_id: str = "") -> str:
    session_uid = _session_runtime_uid(session)
    mode_token = str(mode_id or "").strip()
    if session_uid:
        if mode_token:
            return f"sess_mode_pick:{session_uid}:{mode_token}"
        return f"sess_mode_pick:{session_uid}"
    if mode_token:
        return f"sess_mode:{mode_token}"
    return "sess_active"


def build_mode_action_callback_data(
    mode_id: str,
    action: str,
    *,
    session: Any = None,
    payload: Any = None,
) -> str:
    mode_token = str(mode_id or "").strip()
    action_token = str(action or "").strip() or "action"
    base = f"{MODE_ACTION_PREFIX}:{mode_token}:{action_token}"

    compact_parts: list[str] = []
    sid = _session_id(session) if session is not None else ""
    if sid:
        compact_parts.append(f"s={sid}")

    if isinstance(payload, Mapping):
        for key, value in payload.items():
            key_token = str(key or "").strip()
            value_token = str(value or "").strip()
            if key_token and value_token:
                compact_parts.append(f"{key_token}={value_token}")
    else:
        value_token = str(payload or "").strip()
        if value_token:
            if any(sep in value_token for sep in ("=", "|", "&")):
                compact_parts.append(value_token)
            else:
                compact_parts.append(f"val={value_token}")

    if not compact_parts:
        return base
    return f"{base}:{'|'.join(compact_parts)}"
