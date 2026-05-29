from __future__ import annotations

import shutil
from typing import List, Optional

from config import AppConfig, ToolConfig


def tool_exec(tool: ToolConfig) -> Optional[str]:
    for cmd in (tool.cmd, tool.headless_cmd, tool.interactive_cmd):
        if cmd and len(cmd) > 0:
            return str(cmd[0])
    return None


def _is_claude_tool(name: str) -> bool:
    """Проверяет, является ли инструмент claude (требует special check)."""
    return name == "claude"


def _check_claude_via_bot_user(workdir: str) -> bool:
    """
    Проверяет доступность claude через запуск от имени claude-bot.

    Возвращает True, если:
    - Пользователь claude-bot существует
    - claude установлен у пользователя
    - workdir доступен для записи
    """
    try:
        from .claude_env_checker import check_claude_env
        result = check_claude_env(workdir=workdir, username="claude-bot")
        return result.is_claude_available()
    except ImportError:
        # Модуль не найден, пробуем простую проверку
        pass
    except Exception:
        # Любая другая ошибка — считаем, что проверка не пройдена
        pass
    return False


def is_tool_available(config: AppConfig, name: str) -> bool:
    tool = (config.tools or {}).get(name)
    if not tool:
        return False
    if not bool(getattr(tool, "enabled", True)):
        return False

    # Специальная проверка для claude — через пользователя claude-bot
    if _is_claude_tool(name):
        workdir = getattr(config.defaults, "workdir", "/srv/git_projects")
        if _check_claude_via_bot_user(workdir):
            return True
        # Если проверка через claude-bot не пройдена, пробуем стандартную
        # (на случай, если claude установлен глобально)

    exe = tool_exec(tool)
    return bool(exe and shutil.which(exe))


def available_tools(config: AppConfig) -> List[str]:
    return [name for name in (config.tools or {}).keys() if is_tool_available(config, name)]
