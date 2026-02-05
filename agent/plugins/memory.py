from __future__ import annotations

import os
import time
from typing import Any, Dict, List

from agent.plugins.base import ToolPlugin
from agent.tooling.spec import ToolSpec
from agent.tooling.helpers import MEMORY_FILE


class MemoryTool(ToolPlugin):
    def get_commands(self) -> List[Dict[str, Any]]:
        return [
            {
                "command": "memory",
                "description": "Показать память агента",
                "handler": self._handle_memory_command,
            }
        ]

    def get_spec(self) -> ToolSpec:
        return ToolSpec(
            name="memory",
            description="Long-term memory. Use to save important info (project context, decisions, todos) or read previous notes. Memory persists across sessions.",
            parameters={
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["read", "append", "clear"], "description": "read: get all memory, append: add new entry, clear: reset memory"},
                    "content": {"type": "string", "description": "For append: text to add (will be timestamped automatically)"},
                },
                "required": ["action"],
            },
            parallelizable=False,
        )

    async def execute(self, args: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
        action = args.get("action")
        state_root = ctx.get("state_root") or ctx["cwd"]
        path = os.path.join(state_root, MEMORY_FILE)
        if action == "read":
            if not os.path.exists(path):
                return {"success": True, "output": "(memory is empty)"}
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()
            return {"success": True, "output": content or "(memory is empty)"}
        if action == "append":
            content = args.get("content")
            if not content:
                return {"success": False, "error": "Content required for append"}
            timestamp = time.strftime("%Y-%m-%d %H:%M")
            entry = f"- {timestamp}: {content.strip()}\n"
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "a", encoding="utf-8") as f:
                f.write(entry)
            return {"success": True, "output": "Memory updated"}
        if action == "clear":
            if os.path.exists(path):
                with open(path, "w", encoding="utf-8") as f:
                    f.write("")
            return {"success": True, "output": "Memory cleared"}
        return {"success": False, "error": f"Unknown action: {action}"}

    async def _handle_memory_command(self, update: Any, context: Any, **kwargs: Any) -> None:
        chat_id = update.effective_chat.id if update and update.effective_chat else None
        if not chat_id:
            return
        if not self.config:
            await context.bot.send_message(chat_id=chat_id, text="Память недоступна: нет конфигурации.")
            return
        state_root = os.path.join(self.config.defaults.workdir, "_sandbox")
        path = os.path.join(state_root, MEMORY_FILE)
        if not os.path.exists(path):
            await context.bot.send_message(chat_id=chat_id, text="(память пуста)")
            return
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                content = f.read().strip() or "(память пуста)"
        except Exception:
            await context.bot.send_message(chat_id=chat_id, text="Не удалось прочитать память.")
            return
        await context.bot.send_message(chat_id=chat_id, text=content[:3500])
