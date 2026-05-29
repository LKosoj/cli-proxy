from __future__ import annotations

import re
from typing import Any, Mapping

_EXEC_TARGETS = {"local", "ssh"}
_ACTION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


def is_valid_action_id(action_id: str) -> bool:
    return bool(_ACTION_ID_RE.fullmatch(str(action_id or "").strip()))


def is_action_allowlisted(config_payload: Mapping[str, Any], *, target: str, action_id: str) -> bool:
    target_key = str(target or "").strip().lower()
    if target_key not in _EXEC_TARGETS:
        return False
    action = str(action_id or "").strip()
    if not action:
        return False

    admin_cfg = config_payload.get("admin", {}) if isinstance(config_payload, Mapping) else {}
    if not isinstance(admin_cfg, Mapping):
        return False

    allowlist = admin_cfg.get("allowlist", {})
    if not isinstance(allowlist, Mapping):
        return False

    raw_entries = allowlist.get(target_key, [])
    entries: set[str] = set()

    if isinstance(raw_entries, Mapping):
        entries = {str(key).strip() for key in raw_entries.keys() if str(key).strip()}
    elif isinstance(raw_entries, (list, tuple, set)):
        entries = {str(item).strip() for item in raw_entries if str(item).strip()}

    return action in entries


__all__ = ["is_action_allowlisted", "is_valid_action_id"]
