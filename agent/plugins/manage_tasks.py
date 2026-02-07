from __future__ import annotations

import time
from typing import Any, Dict

from agent.plugins.base import ToolPlugin
from agent.tooling.spec import ToolSpec
from agent.tooling import helpers


class ManageTasksTool(ToolPlugin):
    def get_spec(self) -> ToolSpec:
        return ToolSpec(
            name="manage_tasks",
            description="Manage task list: create, update status, or list all tasks. Use for planning complex multi-step work.",
            parameters={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["add", "update", "list", "clear"],
                        "description": "Action: add new task, update status, list all, clear completed",
                    },
                    "tasks": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "id": {"type": "string"},
                                "content": {"type": "string"},
                                "status": {"type": "string", "enum": ["pending", "in_progress", "completed", "cancelled"]},
                            },
                        },
                    },
                },
                "required": ["action"],
            },
            parallelizable=False,
        )

    async def execute(self, args: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
        session_id = ctx.get("session_id") or "default"
        tasks = self.services.setdefault("task_store", {}).setdefault(session_id, [])
        action = args.get("action")
        if action == "add":
            items = args.get("tasks") or []
            if not items:
                return {"success": False, "error": "No tasks provided"}
            for t in items:
                if not t.get("id") or not t.get("content"):
                    return {"success": False, "error": "Task requires id and content"}
                existing = next((x for x in tasks if x["id"] == t["id"]), None)
                if existing:
                    if t.get("content"):
                        existing["content"] = t["content"]
                    if t.get("status"):
                        existing["status"] = t["status"]
                else:
                    tasks.append({"id": t["id"], "content": t["content"], "status": t.get("status", "pending"), "created_at": int(time.time() * 1000)})
            return {"success": True, "output": helpers._format_tasks(tasks)}
        if action == "update":
            items = args.get("tasks") or []
            if not items:
                return {"success": False, "error": "No tasks provided"}
            for t in items:
                existing = next((x for x in tasks if x["id"] == t.get("id")), None)
                if existing:
                    if t.get("content"):
                        existing["content"] = t["content"]
                    if t.get("status"):
                        existing["status"] = t["status"]
            return {"success": True, "output": helpers._format_tasks(tasks)}
        if action == "list":
            return {"success": True, "output": helpers._format_tasks(tasks)}
        if action == "clear":
            active = [t for t in tasks if t.get("status") not in ("completed", "cancelled")]
            self.services["task_store"][session_id] = active
            return {"success": True, "output": f"Cleared completed tasks. {len(active)} remaining."}
        return {"success": False, "error": f"Unknown action: {action}"}
