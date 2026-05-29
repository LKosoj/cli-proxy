from __future__ import annotations

from .allowlist import is_action_allowlisted, is_valid_action_id
from .mode import AdminMode

PLUGIN = AdminMode

__all__ = ["AdminMode", "PLUGIN", "is_action_allowlisted", "is_valid_action_id"]
