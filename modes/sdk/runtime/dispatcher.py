from __future__ import annotations

import logging
from typing import Dict

from config import AppConfig
from sessions.session_state_access import get_active_mode
from .contracts import PlanStep
from .profiles import ExecutorProfile, build_analyst_profile, build_default_profile
from .tooling.registry import ToolRegistry

_log = logging.getLogger(__name__)


class Dispatcher:
    def __init__(self, config: AppConfig, tool_registry: ToolRegistry):
        self._config = config
        self._tool_registry = tool_registry
        self._profiles: Dict[str, ExecutorProfile] = {
            "default": build_default_profile(config, tool_registry),
            "analyst": build_analyst_profile(config, tool_registry),
        }
        _log.info("dispatcher initialized, profiles: %s", list(self._profiles.keys()))

    def get_profile(self, step: PlanStep, session: object | None = None) -> ExecutorProfile:
        requested = str(getattr(session, "executor_profile", "") or "").strip() if session is not None else ""
        if not requested and session is not None:
            active_mode = str(get_active_mode(session, "") or "").strip()
            analyst_flags = getattr(session, "analyst_intent_flags", None)
            if active_mode == "analyst" or isinstance(analyst_flags, dict):
                requested = "analyst"
        profile = self._profiles.get(requested) or self._profiles["default"]
        _log.info("dispatcher: step=%s type=%s -> profile=%s", step.id, step.step_type, profile.name)
        return profile
