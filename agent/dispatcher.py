from __future__ import annotations

from typing import Dict

from config import AppConfig
from .contracts import PlanStep
from .profiles import ExecutorProfile, build_default_profile


class Dispatcher:
    def __init__(self, config: AppConfig):
        self._config = config
        self._profiles: Dict[str, ExecutorProfile] = {
            "default": build_default_profile(config)
        }

    def get_profile(self, step: PlanStep) -> ExecutorProfile:
        # Пока один профиль. Можно расширить логикой выбора.
        return self._profiles["default"]
