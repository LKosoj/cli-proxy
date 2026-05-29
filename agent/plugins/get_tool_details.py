from __future__ import annotations

import json
from typing import Any, Dict, Optional

from agent.plugins.base import ToolPlugin
from modes.sdk.runtime.tooling.spec import ToolSpec


class GetToolDetailsTool(ToolPlugin):
    """Meta-tool: returns full JSON schema for requested tools (progressive disclosure)."""

    plugin_id = "get_tool_details"

    def get_spec(self) -> ToolSpec:
        return ToolSpec(
            name="get_tool_details",
            description=(
                "Возвращает полную JSON-схему (параметры, описание) для указанных инструментов. "
                "Вызови перед первым использованием инструмента, чтобы узнать его параметры."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "tool_names": {
                        "type": "string",
                        "description": "Имена инструментов через запятую (например: 'run_command,read_file')",
                    },
                },
                "required": ["tool_names"],
            },
            timeout_ms=5_000,
            category="system",
            one_liner="Получить полную схему инструмента по имени",
        )

    async def execute(self, args: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
        raw = str(args.get("tool_names") or "").strip()
        if not raw:
            return {"success": False, "error": "tool_names is required"}

        names = [n.strip() for n in raw.split(",") if n.strip()]
        if not names:
            return {"success": False, "error": "No tool names provided"}

        if len(names) > 10:
            names = names[:10]

        registry = self._services.get("_tool_registry") if self._services else None
        results: Dict[str, Any] = {}

        for name in names:
            if registry is not None:
                detail = registry.get_tool_detail(name)
                if detail:
                    results[name] = detail["function"]
                else:
                    suggestions = registry.get_missing_suggestions(name)
                    results[name] = {
                        "error": f"Tool '{name}' not found",
                        "suggestions": suggestions,
                    }
            else:
                results[name] = {"error": "Tool registry unavailable"}

        return {
            "success": True,
            "output": json.dumps(results, ensure_ascii=False, indent=2),
        }

    def initialize(self, config: Any = None, services: Optional[Dict[str, Any]] = None) -> None:
        self._config = config
        self._services = services or {}
