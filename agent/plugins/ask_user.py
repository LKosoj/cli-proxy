from __future__ import annotations

import asyncio
import time
import uuid
from typing import Any, Dict

from agent.plugins.base import ToolPlugin
from modes.sdk.runtime.ask_user_schema import apply_ask_schema, validate_ask_payload
from modes.sdk.runtime.tooling.spec import ToolSpec


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
                    "allow_custom": {"type": "boolean", "description": "Allow user to provide a custom text answer", "default": True},
                    "system_options": {"type": "boolean", "description": "Add system options like 'Stop and clarify'", "default": True},
                },
                "required": ["question", "options"],
            },
            timeout_ms=86_400_000,
            parallelizable=False,
        )

    async def execute(self, args: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
        question = str(args.get("question") or "").strip()
        options = list(args.get("options") or [])
        allow_custom = bool(args.get("allow_custom", True))
        system_options = bool(args.get("system_options", True))
        question, options, issues = apply_ask_schema(question, options)
        post_issues = validate_ask_payload(question, options)
        fatal = {"empty_question", "too_few_options", "placeholder_options"}
        if post_issues and fatal & set(post_issues):
            return {"success": False, "error": f"Invalid ask_user payload: {', '.join(post_issues)}"}
        bot = ctx.get("bot")
        context = ctx.get("context")
        chat_id = ctx.get("chat_id")
        session_id = ctx.get("session_id") or "default"
        if not bot or not context or not chat_id:
            return {"success": False, "error": "Ask callback not configured"}
        # Keep option normalization aligned with transport-side rendering.
        normalized_options = options
        if hasattr(bot, "_normalize_ask_options"):
            normalized_options = list(bot._normalize_ask_options(normalized_options, allow_custom=allow_custom))
        if hasattr(bot, "_ensure_min_ask_options"):
            normalized_options = list(bot._ensure_min_ask_options(normalized_options, system_options=system_options))

        if len(normalized_options) < 1:
            return {"success": False, "error": "Need at least 1 option after normalization"}
        question_id = f"ask_{int(time.time())}_{uuid.uuid4().hex[:6]}"
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        pending = self.services.setdefault("pending_questions", {})
        pending[question_id] = fut
        try:
            await bot._send_ask_question(
                context, chat_id, session_id, question_id, question, normalized_options,
                allow_custom=allow_custom,
                system_options=system_options,
            )
            answer = await fut
            return {"success": True, "output": f"User selected: {answer}"}
        except asyncio.CancelledError:
            raise
        finally:
            pending.pop(question_id, None)
            if hasattr(bot, "_clear_pending_question"):
                bot._clear_pending_question(question_id)
