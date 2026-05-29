"""App-level services.

Важно: не импортировать подмодули тут напрямую.
`config.py` импортирует `app.services.dotenv_loader`, что приводит к импорту пакета `app.services`
и выполнению этого файла. Жадные импорты сервисов создают циклический импорт
(`config -> app.services -> application_facade -> config_service -> config`).

Экспортируем имена через ленивый `__getattr__` (PEP 562).
"""

from __future__ import annotations

import importlib
from typing import Any

__all__ = [
    "ConfigService",
    "ModeRunLifecycleService",
    "SessionMutationService",
    "SessionService",
    "TaskService",
    "ThemeService",
]

_EXPORTS = {
    "ConfigService": ("app.services.config_service", "ConfigService"),
    "ModeRunLifecycleService": (
        "app.services.mode_run_lifecycle_service",
        "ModeRunLifecycleService",
    ),
    "SessionMutationService": ("app.services.session_mutation_service", "SessionMutationService"),
    "SessionService": ("app.services.session_service", "SessionService"),
    "TaskService": ("app.services.task_service", "TaskService"),
    "ThemeService": ("app.services.theme_service", "ThemeService"),
}


def __getattr__(name: str) -> Any:
    target = _EXPORTS.get(name)
    if not target:
        raise AttributeError(name)
    mod_name, attr = target
    module = importlib.import_module(mod_name)
    return getattr(module, attr)
