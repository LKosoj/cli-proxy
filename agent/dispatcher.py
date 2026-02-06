from __future__ import annotations

import logging
from typing import Dict

from config import AppConfig
from .contracts import PlanStep
from .profiles import ExecutorProfile, build_default_profile
from .tooling.registry import ToolRegistry

_log = logging.getLogger(__name__)


class Dispatcher:
    def __init__(self, config: AppConfig, tool_registry: ToolRegistry):
        self._config = config
        self._tool_registry = tool_registry
        self._profiles: Dict[str, ExecutorProfile] = {
            "default": build_default_profile(config, tool_registry)
        }
        _log.info("dispatcher initialized, profiles: %s", list(self._profiles.keys()))

    def get_profile(self, step: PlanStep) -> ExecutorProfile:
        # Пока один профиль. Можно расширить логикой выбора.
        profile = self._profiles["default"]
        _log.info("dispatcher: step=%s type=%s -> profile=%s", step.id, step.step_type, profile.name)
        return profile
