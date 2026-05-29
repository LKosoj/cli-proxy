from __future__ import annotations

from typing import Any, Dict

from agent.plugins.base import ToolPlugin
from modes.admin.memory import ServerMemory, ServerMemoryError
from modes.admin.plugin_tools import AdminToolError, resolve_workdir
from modes.admin.snapshot_store import safe_server_id
from modes.sdk.runtime.tooling.spec import ToolSpec


class AdminRememberNoteTool(ToolPlugin):
    plugin_id = "admin_remember_note"

    def get_spec(self) -> ToolSpec:
        return ToolSpec(
            name="admin_remember_note",
            description=(
                "Append a free-form note into the server's memory journal. "
                "Use for observations that don't fit a single key/value fact "
                "(e.g. 'nginx reload via systemctl works, service reload gets permission denied'). "
                "Notes are periodically compacted; keep each note focused and short."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "server_id": {"type": "string", "description": "Target server ID."},
                    "text": {"type": "string", "description": "Note text."},
                    "tags": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional tags (e.g. ['nginx', 'reload']).",
                    },
                },
                "required": ["server_id", "text"],
            },
            risk_level="low",
            parallelizable=True,
            category="admin",
            tags=["admin", "memory", "note"],
        )

    async def execute(self, args: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
        try:
            workdir = resolve_workdir(ctx)
        except AdminToolError as exc:
            return {"success": False, "error": str(exc)}

        server_raw = args.get("server_id")
        text = str(args.get("text") or "").strip()
        if not server_raw or not text:
            return {"success": False, "error": "server_id and text are required"}

        tags = args.get("tags") or []
        if not isinstance(tags, list):
            return {"success": False, "error": "tags must be a list of strings"}

        try:
            sid = safe_server_id(server_raw)
            memory = ServerMemory(workdir, sid)
            entry = memory.append_note(text, source="agent", tags=[str(t) for t in tags])
        except (ServerMemoryError, AdminToolError) as exc:
            return {"success": False, "error": str(exc)}

        return {
            "success": True,
            "server_id": sid,
            "ts": entry.ts,
            "output": f"note appended to {sid}/memory/notes.md",
        }
