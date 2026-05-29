import asyncio
import json
import types
from unittest.mock import AsyncMock, patch

from agent.plugins.intent_plugin import IntentPluginTool


def test_intent_plugin_parses_numbered_actions() -> None:
    async def _run() -> None:
        tool = IntentPluginTool()
        tool.initialize(
            config=types.SimpleNamespace(
                defaults=types.SimpleNamespace(openai_api_key="k", openai_model="m", openai_base_url=None)
            ),
            services={},
        )
        text = "1. Увеличить стрелки\n2. Выравнять слайды\n3. Проверить мобилку"
        llm_json = {
            "task_kind_hint": "continue_task",
            "goal": "Правки слайдера",
            "actions": ["Увеличить стрелки", "Выравнять слайды", "Проверить мобилку"],
            "constraints": [],
            "acceptance_criteria": ["Все пункты выполнены"],
            "ambiguities": [],
            "assumptions": [],
        }
        mocked_completion = AsyncMock(return_value=json.dumps(llm_json, ensure_ascii=False))
        with patch("modes.sdk.runtime.openai_client.chat_completion", new=mocked_completion):
            resp = await tool.execute({"user_text": text}, {})
        assert resp["success"] is True
        data = json.loads(resp["output"])
        assert data["actions"][:3] == [
            "Увеличить стрелки",
            "Выравнять слайды",
            "Проверить мобилку",
        ]
        assert data["task_kind_hint"] == "continue_task"

    asyncio.run(_run())


def test_intent_plugin_uses_json_response_format() -> None:
    async def _run() -> None:
        tool = IntentPluginTool()
        tool.initialize(
            config=types.SimpleNamespace(
                defaults=types.SimpleNamespace(openai_api_key="k", openai_model="m", openai_base_url=None)
            ),
            services={},
        )
        llm_json = {
            "task_kind_hint": "continue_task",
            "goal": "Правки слайдера",
            "actions": ["Увеличить стрелки"],
            "constraints": [],
            "acceptance_criteria": [],
            "ambiguities": [],
            "assumptions": [],
        }
        mocked_completion = AsyncMock(return_value=json.dumps(llm_json, ensure_ascii=False))
        with patch("modes.sdk.runtime.openai_client.chat_completion", new=mocked_completion):
            resp = await tool.execute({"user_text": "сделай правки"}, {})
        assert resp["success"] is True
        assert mocked_completion.await_count == 1
        assert mocked_completion.await_args.kwargs.get("response_format") == {"type": "json_object"}

    asyncio.run(_run())


def test_intent_plugin_requires_user_text() -> None:
    async def _run() -> None:
        tool = IntentPluginTool()
        resp = await tool.execute({}, {})
        assert resp["success"] is False
        assert "user_text required" in resp["error"]

    asyncio.run(_run())


def test_intent_plugin_requires_openai_config() -> None:
    async def _run() -> None:
        tool = IntentPluginTool()
        resp = await tool.execute({"user_text": "x"}, {})
        assert resp["success"] is False
        assert "requires configured OpenAI credentials" in resp["error"]

    asyncio.run(_run())
