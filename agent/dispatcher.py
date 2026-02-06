from __future__ import annotations

from typing import Dict

from config import AppConfig
from .contracts import PlanStep
from .profiles import ExecutorProfile, build_default_profile
from .tooling.registry import ToolRegistry


class Dispatcher:
    def __init__(self, config: AppConfig, tool_registry: ToolRegistry):
        self._config = config
        self._tool_registry = tool_registry
        self._profiles: Dict[str, ExecutorProfile] = {
            "default": build_default_profile(config, tool_registry)
        }

    def get_profile(self, step: PlanStep) -> ExecutorProfile:
        # Пока один профиль. Можно расширить логикой выбора.
        return self._profiles["default"]
