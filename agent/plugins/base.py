from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional

from agent.tooling.spec import ToolSpec


class ToolPlugin(ABC):
    plugin_id: Optional[str] = None
    function_prefix: Optional[str] = None

    def get_plugin_id(self) -> str:
        return self.plugin_id or self.__class__.__name__

    def get_function_prefix(self) -> str:
        return self.function_prefix or self.get_plugin_id()

    def initialize(self, config: Any = None, services: Optional[Dict[str, Any]] = None) -> None:
        self.config = config
        self.services = services or {}

    def close(self) -> None:
        return None

    def get_source_name(self) -> str:
        return self.get_plugin_id()

    def get_commands(self) -> List[Dict[str, Any]]:
        return []

    # Telegram UI integration (optional).
    #
    # These are intentionally lightweight and return either:
    # - ready-to-register telegram.ext handler objects (ConversationHandler, InlineQueryHandler, etc), or
    # - dict configs that bot.py can adapt into handlers (to avoid tight coupling in the agent layer).
    def get_message_handlers(self) -> List[Dict[str, Any]]:
        return []

    def get_inline_handlers(self) -> List[Dict[str, Any]]:
        return []

    @abstractmethod
    def get_spec(self) -> ToolSpec:
        raise NotImplementedError

    @abstractmethod
    async def execute(self, args: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
        raise NotImplementedError
