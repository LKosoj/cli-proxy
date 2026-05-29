from __future__ import annotations

import time
from typing import Any, Dict, List

from agent.plugins.base import ToolPlugin
from modes.sdk.runtime.tooling.spec import ToolSpec
from agent.tooling import helpers


CLOSED_STATUSES = {"completed", "cancelled"}


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
        session_key = self._scope_key(ctx)
        tasks = self.services.setdefault("task_store", {}).setdefault(session_key, [])
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
                    item = {"id": t["id"], "content": t["content"], "status": t.get("status", "pending"),
                            "created_at": int(time.time() * 1000)}
                    tasks.append(item)
            return self._success(action, tasks, changed=True)
        if action == "update":
            items = args.get("tasks") or []
            if not items:
                return {"success": False, "error": "No tasks provided"}
            changed = False
            for t in items:
                existing = next((x for x in tasks if x["id"] == t.get("id")), None)
                if existing:
                    if t.get("content"):
                        changed = changed or existing.get("content") != t["content"]
                        existing["content"] = t["content"]
                    if t.get("status"):
                        changed = changed or existing.get("status") != t["status"]
                        existing["status"] = t["status"]
            return self._success(action, tasks, changed=changed)
        if action == "list":
            return self._success(action, tasks, changed=False)
        if action == "clear":
            active = [t for t in tasks if t.get("status") not in ("completed", "cancelled")]
            changed = len(active) != len(tasks)
            self.services["task_store"][session_key] = active
            output = f"Cleared completed tasks. {len(active)} remaining."
            return self._success(action, active, changed=changed, output=output)
        return {"success": False, "error": f"Unknown action: {action}"}

    @staticmethod
    def _scope_key(ctx: Dict[str, Any]) -> str:
        explicit = str(ctx.get("manage_tasks_scope_key") or "").strip()
        if explicit:
            return explicit
        session_key = str(ctx.get("session_scoped_key") or ctx.get("session_id") or "default").strip() or "default"
        run_id = str(ctx.get("run_id") or "").strip()
        if run_id:
            return f"{session_key}:run:{run_id}"
        return session_key

    @staticmethod
    def _snapshot(tasks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        return [
            {
                "id": str(task.get("id") or ""),
                "content": str(task.get("content") or ""),
                "status": str(task.get("status") or "pending"),
                "created_at": task.get("created_at"),
            }
            for task in tasks
        ]

    def _success(
        self,
        action: str,
        tasks: List[Dict[str, Any]],
        *,
        changed: bool,
        output: str | None = None,
    ) -> Dict[str, Any]:
        snapshot = self._snapshot(tasks)
        total = len(snapshot)
        closed = sum(1 for task in snapshot if task.get("status") in CLOSED_STATUSES)
        return {
            "success": True,
            "output": output if output is not None else helpers._format_tasks(tasks),
            "manage_tasks": {
                "action": str(action or ""),
                "changed": bool(changed),
                "tasks": snapshot,
                "progress": {
                    "total": total,
                    "closed": closed,
                    "open": total - closed,
                },
            },
        }
