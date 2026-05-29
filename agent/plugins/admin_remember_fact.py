from __future__ import annotations

from typing import Any, Dict

from agent.plugins.base import ToolPlugin
from modes.admin.memory import ServerMemory, ServerMemoryError
from modes.admin.plugin_tools import AdminToolError, resolve_workdir
from modes.admin.snapshot_store import safe_server_id
from modes.sdk.runtime.tooling.spec import ToolSpec


class AdminRememberFactTool(ToolPlugin):
    plugin_id = "admin_remember_fact"

    def get_spec(self) -> ToolSpec:
        return ToolSpec(
            name="admin_remember_fact",
            description=(
                "Store a structured fact about a server (key/value). "
                "Facts are persistent across sessions and included in dossiers. "
                "Use for stable attributes: service_manager, pkg_manager, python_version, "
                "reload method, known quirks that should not be re-discovered."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "server_id": {"type": "string", "description": "Target server ID."},
                    "key": {"type": "string", "description": "Fact key (e.g. 'service_manager')."},
                    "value": {
                        "description": "Fact value (any JSON-serializable type).",
                    },
                },
                "required": ["server_id", "key", "value"],
            },
            risk_level="low",
            parallelizable=True,
            category="admin",
            tags=["admin", "memory", "fact"],
        )

    async def execute(self, args: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
        try:
            workdir = resolve_workdir(ctx)
        except AdminToolError as exc:
            return {"success": False, "error": str(exc)}

        server_raw = args.get("server_id")
        key = str(args.get("key") or "").strip()
        value = args.get("value")
        if not server_raw or not key:
            return {"success": False, "error": "server_id and key are required"}

        try:
            sid = safe_server_id(server_raw)
            memory = ServerMemory(workdir, sid)
            result = memory.update_fact(key, value, by="agent_plugin")
        except (ServerMemoryError, AdminToolError) as exc:
            return {"success": False, "error": str(exc)}

        return {
            "success": True,
            "server_id": sid,
            "key": result["key"],
            "prev": result["prev"],
            "value": result["value"],
            "output": f"fact {sid}/{result['key']} updated",
        }
