from __future__ import annotations

import logging
from typing import Any, Dict

from agent.plugins.base import ToolPlugin
from agent.tooling.spec import ToolSpec


class ManageMessageTool(ToolPlugin):
    def get_spec(self) -> ToolSpec:
        return ToolSpec(
            name="manage_message",
            description="Delete or edit your own recent messages. Use to fix typos, remove spam, or clean up. Can only manage YOUR OWN messages from this conversation.",
            parameters={
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["delete_last", "delete_by_index", "edit_last"], "description": "Action: delete_last (delete your last message), delete_by_index (delete by index, 0=oldest), edit_last (edit your last message)"},
                    "index": {"type": "number", "description": "For delete_by_index: which message to delete (0=oldest recent, -1=newest)"},
                    "new_text": {"type": "string", "description": "For edit_last: the new text for the message"},
                },
                "required": ["action"],
            },
            parallelizable=False,
        )

    async def execute(self, args: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
        chat_id = ctx.get("chat_id")
        bot = ctx.get("bot")
        context = ctx.get("context")
        if not chat_id or not bot or not context:
            return {"success": False, "error": "Manage message not configured"}
        messages = self.services.setdefault("recent_messages", {}).get(chat_id, [])
        if not messages:
            return {"success": False, "error": "No recent messages to manage"}
        action = args.get("action")
        if action == "delete_last":
            msg_id = messages[-1]
            try:
                ok = await bot._delete_message(context, chat_id, msg_id)
                if ok:
                    messages.pop()
                    self.services["recent_messages"][chat_id] = messages
                    return {"success": True, "output": "Deleted last message"}
                return {"success": False, "error": "Failed to delete (maybe already deleted or too old)"}
            except Exception as e:
                logging.exception(f"tool failed {str(e)}")
                return {"success": False, "error": f"Delete failed: {e}"}
        if action == "delete_by_index":
            idx = args.get("index", -1)
            if idx < 0:
                idx = len(messages) + idx
            if idx < 0 or idx >= len(messages):
                return {"success": False, "error": f"Invalid index. Have {len(messages)} messages (0-{len(messages)-1})"}
            msg_id = messages[idx]
            try:
                ok = await bot._delete_message(context, chat_id, msg_id)
                if ok:
                    messages.pop(idx)
                    self.services["recent_messages"][chat_id] = messages
                    return {"success": True, "output": f"Deleted message at index {idx}"}
                return {"success": False, "error": "Failed to delete"}
            except Exception as e:
                logging.exception(f"tool failed {str(e)}")
                return {"success": False, "error": f"Delete failed: {e}"}
        if action == "edit_last":
            new_text = args.get("new_text")
            if not new_text:
                return {"success": False, "error": "new_text required for edit"}
            msg_id = messages[-1]
            try:
                ok = await bot._edit_message(context, chat_id, msg_id, new_text)
                if ok:
                    return {"success": True, "output": "Edited last message"}
                return {"success": False, "error": "Failed to edit (maybe too old or contains media)"}
            except Exception as e:
                logging.exception(f"tool failed {str(e)}")
                return {"success": False, "error": f"Edit failed: {e}"}
        return {"success": False, "error": f"Unknown action: {action}"}
