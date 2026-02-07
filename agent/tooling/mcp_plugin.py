from __future__ import annotations

import json
import logging
from typing import Any, Dict

from agent.mcp.manager import MCPManager
from agent.mcp.stdio_client import MCPToolInfo
from agent.plugins.base import ToolPlugin
from agent.tooling.spec import ToolSpec


def _normalize_schema(schema: Dict[str, Any]) -> Dict[str, Any]:
    """
    MCP tool schemas are JSON Schema-ish, but can be missing 'type' or use non-object roots.
    We force an object root to keep our validator strict and predictable.
    """
    if not isinstance(schema, dict):
        return {"type": "object", "properties": {}}
    out = dict(schema)
    if out.get("type") != "object":
        out["type"] = "object"
    out.setdefault("properties", {})
    out.setdefault("required", [])
    if not isinstance(out.get("properties"), dict):
        out["properties"] = {}
    if not isinstance(out.get("required"), list):
        out["required"] = []
    return out


def _render_mcp_result(result: Dict[str, Any]) -> str:
    # MCP tools/call result typically has "content": [{type:"text", text:"..."}]
    content = result.get("content")
    if isinstance(content, list):
        parts = []
        for item in content:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "text" and isinstance(item.get("text"), str):
                parts.append(item["text"])
            else:
                parts.append(json.dumps(item, ensure_ascii=False))
        if parts:
            return "\n".join(parts).strip()
    # Fallback: dump whole result.
    return json.dumps(result, ensure_ascii=False)


class MCPRemoteToolPlugin(ToolPlugin):
    def __init__(self, *, registry_name: str, server_name: str, tool: MCPToolInfo, manager: MCPManager) -> None:
        super().__init__()
        self._registry_name = registry_name
        self._server_name = server_name
        self._tool = tool
        self._manager = manager

        # Make it easier to identify in menus/logs. Tool name must stay strict, so we disable prefixing.
        self.plugin_id = f"MCP[{server_name}]"
        self.function_prefix = None

    def get_function_prefix(self) -> str:
        # Disable ToolRegistry prefixing; MCP tools already have unique names.
        return ""

    def get_source_name(self) -> str:
        return f"MCP:{self._server_name}"

    def get_spec(self) -> ToolSpec:
        return ToolSpec(
            name=self._registry_name,
            description=(self._tool.description or "").strip()
            or f"MCP tool '{self._tool.name}' from server '{self._server_name}'",
            parameters=_normalize_schema(self._tool.input_schema),
            parallelizable=False,
            timeout_ms=30_000,
        )

    async def execute(self, args: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
        try:
            result = await self._manager.call(self._server_name, self._tool.name, args or {})
            return {"success": True, "output": _render_mcp_result(result), "error": None}
        except Exception as e:
            logging.exception(f"tool failed {str(e)}")
            return {"success": False, "output": "", "error": str(e)}
