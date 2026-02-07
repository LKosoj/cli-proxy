from __future__ import annotations

import asyncio
import time
import uuid
from typing import Any, Dict

from agent.plugins.base import ToolPlugin
from agent.tooling.spec import ToolSpec


class AskUserTool(ToolPlugin):
    def get_spec(self) -> ToolSpec:
        return ToolSpec(
            name="ask_user",
            description=(
                "Ask user a question with button options. Use when you need confirmation or choice from user. "
                "Returns the selected option."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "question": {"type": "string", "description": "The question to ask the user"},
                    "options": {"type": "array", "items": {"type": "string"}, "description": (
                        "Button options for user to choose from (2-4 options)"
                    )},
                },
                "required": ["question", "options"],
            },
            parallelizable=False,
        )

    async def execute(self, args: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
        question = args.get("question")
        options = args.get("options") or []
        if not question or len(options) < 2:
            return {"success": False, "error": "Need at least 2 options"}
        if len(options) > 4:
            options = options[:4]
        bot = ctx.get("bot")
        context = ctx.get("context")
        chat_id = ctx.get("chat_id")
        session_id = ctx.get("session_id") or "default"
        if not bot or not context or not chat_id:
            return {"success": False, "error": "Ask callback not configured"}
        question_id = f"ask_{int(time.time())}_{uuid.uuid4().hex[:6]}"
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        pending = self.services.setdefault("pending_questions", {})
        pending[question_id] = fut
        await bot._send_ask_question(context, chat_id, session_id, question_id, question, options)
        try:
            answer = await asyncio.wait_for(fut, timeout=120)
            return {"success": True, "output": f"User selected: {answer}"}
        except asyncio.TimeoutError:
            pending.pop(question_id, None)
            return {"success": False, "error": "Failed to get user response: timeout"}
