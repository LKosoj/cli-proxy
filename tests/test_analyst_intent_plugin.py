import json
import types

import pytest

from agent.plugins.analyst_intent_plugin import AnalystIntentPluginTool


def _build_plugin() -> AnalystIntentPluginTool:
    plugin = AnalystIntentPluginTool()
    plugin.initialize(
        config=types.SimpleNamespace(
            defaults=types.SimpleNamespace(openai_api_key="k", openai_model="m")
        ),
        services={},
    )
    return plugin


@pytest.mark.asyncio
async def test_analyst_intent_plugin_normalizes_new_schema_response(monkeypatch):
    plugin = _build_plugin()
    captured = {"system": "", "user": ""}

    async def _fake_chat_completion(_cfg, _system, _user, response_format=None, **_kwargs):
        assert response_format == {"type": "json_object"}
        captured["system"] = _system
        captured["user"] = _user
        return json.dumps(
            {
                "analysis_profile": "codebase",
                "document_kind": "analysis",
                "detail_level": "standard",
                "summary": "Нужен анализ существующего проекта.",
                "template_hint": "project_analysis",
            },
            ensure_ascii=False,
        )

    monkeypatch.setattr("agent.plugins.analyst_intent_plugin.openai_client.chat_completion", _fake_chat_completion)

    res = await plugin.execute({"user_text": "Проанализируй текущий проект"}, {})
    assert res["success"] is True
    out = json.loads(res["output"])
    assert out == {
        "analysis_profile": "codebase",
        "document_kind": "analysis",
        "detail_level": "standard",
        "summary": "Нужен анализ существующего проекта.",
        "template_hint": "project_analysis",
    }
    assert "analysis_profile" in captured["system"]
    assert "greenfield|codebase|codebase_plus_research|research_only|audit" in captured["system"]
    assert "template_hint=change_spec" in captured["system"]
    assert "requires_codebase_grounding" not in out
    assert "task_type" not in out
    assert json.loads(captured["user"])["user_text"] == "Проанализируй текущий проект"


@pytest.mark.asyncio
async def test_analyst_intent_plugin_routes_change_spec_with_new_contract(monkeypatch):
    plugin = _build_plugin()

    async def _fake_chat_completion(_cfg, _system, _user, response_format=None, **_kwargs):
        assert response_format == {"type": "json_object"}
        return json.dumps(
            {
                "analysis_profile": "codebase",
                "document_kind": "spec",
                "detail_level": "full",
                "summary": (
                    "Нужно ТЗ на доработку существующего функционала переноса "
                    "сессий с поддержкой codex CLI."
                ),
                "template_hint": "change_spec",
            },
            ensure_ascii=False,
        )

    monkeypatch.setattr("agent.plugins.analyst_intent_plugin.openai_client.chat_completion", _fake_chat_completion)

    res = await plugin.execute(
        {
            "user_text": "Добавить поддержку codex CLI в перенос сессий",
            "selected_template": "default",
            "project_root": "/repo",
            "workdir": "/repo",
        },
        {},
    )
    assert res["success"] is True
    out = json.loads(res["output"])
    assert out["analysis_profile"] == "codebase"
    assert out["document_kind"] == "spec"
    assert out["detail_level"] == "full"
    assert out["template_hint"] == "change_spec"
    assert "template_id" not in out


@pytest.mark.asyncio
async def test_analyst_intent_plugin_keeps_clarification_questions(monkeypatch):
    plugin = _build_plugin()

    async def _fake_chat_completion(_cfg, _system, _user, response_format=None, **_kwargs):
        return json.dumps(
            {
                "analysis_profile": "greenfield",
                "document_kind": "analysis",
                "detail_level": "standard",
                "summary": "Запрос недостаточно конкретен.",
                "clarification_questions": [
                    "Нужен анализ, ТЗ или аудит?",
                    "Есть ли существующий репозиторий?",
                    "Какой результат нужен на выходе?",
                    "Лишний вопрос будет отброшен.",
                ],
            },
            ensure_ascii=False,
        )

    monkeypatch.setattr("agent.plugins.analyst_intent_plugin.openai_client.chat_completion", _fake_chat_completion)

    res = await plugin.execute(
        {"user_text": "Сделай что-нибудь", "selected_template": "default"},
        {},
    )
    assert res["success"] is True
    out = json.loads(res["output"])
    assert out["clarification_questions"] == [
        "Нужен анализ, ТЗ или аудит?",
        "Есть ли существующий репозиторий?",
        "Какой результат нужен на выходе?",
    ]


@pytest.mark.asyncio
async def test_analyst_intent_plugin_rejects_removed_expanded_contract(monkeypatch):
    plugin = _build_plugin()

    async def _fake_chat_completion(_cfg, _system, _user, response_format=None, **_kwargs):
        return json.dumps(
            {
                "task_type": "change_spec",
                "template_id": "change_spec",
                "confidence": 0.85,
                "reason": "Старый расширенный contract",
                "detail_level": "full",
                "document_kind": "spec",
                "needs_cli": True,
                "needs_clarification": False,
                "requires_codebase_grounding": True,
                "requires_repo_audit": False,
                "requires_final_repo_review": False,
                "clarification_is_blocking": False,
            },
            ensure_ascii=False,
        )

    monkeypatch.setattr("agent.plugins.analyst_intent_plugin.openai_client.chat_completion", _fake_chat_completion)

    res = await plugin.execute(
        {"user_text": "Добавить поддержку codex CLI в перенос сессий"},
        {},
    )
    assert res["success"] is False
    assert "removed fields" in res["error"]
    assert "task_type" in res["error"]
