from __future__ import annotations

import json
from typing import Any, Dict, List

from modes.sdk.runtime import openai_client
from modes.sdk.runtime.json_normalizer import loads_safe
from agent.plugins.base import ToolPlugin
from modes.sdk.runtime.tooling.spec import ToolSpec


def _normalize_list(values: Any) -> List[str]:
    if not isinstance(values, list):
        return []
    out: List[str] = []
    for item in values:
        s = str(item or "").strip()
        if s:
            out.append(s)
    return out[:20]


class IntentPluginTool(ToolPlugin):
    def get_spec(self) -> ToolSpec:
        return ToolSpec(
            name="intent_plugin",
            description=(
                "Analyze user intent and return a structured, actionable interpretation "
                "for deterministic workflows."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "user_text": {"type": "string", "description": "Original user request"},
                    "previous_goal": {"type": "string", "description": "Previous goal (optional)"},
                    "previous_actions": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Previous approved actions (optional)",
                    },
                },
                "required": ["user_text"],
            },
            parallelizable=False,
        )

    async def execute(self, args: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
        user_text = str(args.get("user_text") or "").strip()
        if not user_text:
            return {"success": False, "error": "user_text required"}

        config = getattr(self, "config", None)
        defaults = getattr(config, "defaults", None) if config is not None else None
        if not defaults or not getattr(defaults, "openai_api_key", None) or not getattr(defaults, "openai_model", None):
            return {"success": False, "error": "intent_plugin requires configured OpenAI credentials"}

        previous_goal = str(args.get("previous_goal") or "").strip()
        previous_actions = [str(x).strip() for x in (args.get("previous_actions") or []) if str(x).strip()]
        system = (
            "Ты анализатор намерений для workflow вебмастера. "
            "Верни только JSON-объект без markdown. "
            "Схема JSON: "
            "{"
            "\"task_kind_hint\":\"new_task|continue_task\","
            "\"goal\":\"...\","
            "\"actions\":[\"...\"],"
            "\"constraints\":[\"...\"],"
            "\"acceptance_criteria\":[\"...\"],"
            "\"ambiguities\":[\"...\"],"
            "\"assumptions\":[\"...\"]"
            "}."
        )
        user = json.dumps(
            {
                "user_text": user_text,
                "previous_goal": previous_goal,
                "previous_actions": previous_actions,
                "instructions": [
                    "Сделай actions атомарными и проверяемыми.",
                    "Не добавляй лишние поля.",
                    "Если не хватает данных, заполни ambiguities.",
                ],
            },
            ensure_ascii=False,
        )
        out = await openai_client.chat_completion(
            config,
            system,
            user,
            response_format={"type": "json_object"},
        )
        parsed = loads_safe(out, strict_first=False)
        if not isinstance(parsed, dict):
            return {"success": False, "error": "intent_plugin LLM response is not a JSON object"}
        result = {
            "task_kind_hint": str(parsed.get("task_kind_hint") or "continue_task"),
            "goal": str(parsed.get("goal") or user_text).strip(),
            "actions": _normalize_list(parsed.get("actions")),
            "constraints": _normalize_list(parsed.get("constraints")),
            "acceptance_criteria": _normalize_list(parsed.get("acceptance_criteria")),
            "ambiguities": _normalize_list(parsed.get("ambiguities")),
            "assumptions": _normalize_list(parsed.get("assumptions")),
            "previous_goal": previous_goal,
            "previous_actions": previous_actions,
        }
        if not result["actions"]:
            return {"success": False, "error": "intent_plugin LLM response missing actions[]"}
        return {"success": True, "output": json.dumps(result, ensure_ascii=False)}
