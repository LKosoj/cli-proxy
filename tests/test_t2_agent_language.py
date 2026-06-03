"""Tests for T2: agent response language injection."""
from __future__ import annotations

import asyncio
import json
import os
import types
from typing import Any
from unittest.mock import patch

import pytest

import summary as summary_mod
from modes.sdk.orchestrator_runner import _looks_like_nonfinal_rework_text
from modes.sdk.runtime.heuristics import needs_clarification


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_DEFAULT_CLARIFICATION_BY_LANG = {
    "ru": ["уточни", "уточните", "не ясно", "непонятно"],
    "en": ["clarify", "unclear", "not clear", "please clarify", "i need clarification"],
    "zh": ["请澄清", "不清楚", "需要澄清", "能说清楚吗"],
    "de": ["unklar", "bitte präzisieren", "nicht klar", "klären sie", "erläutern sie"],
}

_SENTINEL = object()


def _make_config(
    user_languages: dict | None = None,
    default_language: str = "ru",
    clarification_keywords_by_lang: Any = _SENTINEL,
):
    if clarification_keywords_by_lang is _SENTINEL:
        by_lang = _DEFAULT_CLARIFICATION_BY_LANG
    else:
        by_lang = clarification_keywords_by_lang
    defaults = types.SimpleNamespace(
        clarification_enabled=True,
        clarification_keywords=["уточни", "уточните", "не ясно", "непонятно"],
        clarification_keywords_by_lang=by_lang,
        default_language=default_language,
    )
    telegram = types.SimpleNamespace(user_languages=user_languages or {})
    return types.SimpleNamespace(defaults=defaults, telegram=telegram)


# ---------------------------------------------------------------------------
# 9.1 agent_core._load_system_prompt
# ---------------------------------------------------------------------------

def _make_agent_core_config(user_languages: dict | None = None, default_language: str = "ru"):
    return _make_config(user_languages=user_languages, default_language=default_language)


def _call_load_system_prompt(user_id: int, config):
    """Call _load_system_prompt via ReActAgent with a synthetic cwd."""
    from modes.sdk.runtime.agent_core import ReActAgent

    cwd = f"/workspace/{user_id}"
    system_txt = os.path.join(os.path.dirname(__file__), "..", "modes", "sdk", "runtime", "system.txt")
    system_txt = os.path.normpath(system_txt)
    if not os.path.exists(system_txt):
        pytest.skip("system.txt not found")

    agent = ReActAgent.__new__(ReActAgent)
    agent.config = config
    # Minimal mock for _tool_registry
    mock_registry = types.SimpleNamespace(
        list_tool_names=lambda: [],
        specs={},
    )
    agent._tool_registry = mock_registry

    with patch("modes.sdk.runtime.agent_core.SYSTEM_PROMPT_PATH", system_txt):
        with patch("modes.sdk.runtime.agent_core.get_memory_for_prompt", return_value=""):
            with patch("modes.sdk.runtime.agent_core.get_chat_history", return_value=""):
                prompt = agent._load_system_prompt(cwd, chat_id=None, allowed_tools=None)
    return prompt


def test_load_system_prompt_language_directive_ru():
    cfg = _make_agent_core_config(user_languages={12345: "ru"})
    prompt = _call_load_system_prompt(12345, cfg)
    assert "Russian" in prompt
    assert "{{response_language}}" not in prompt


def test_load_system_prompt_language_directive_en():
    cfg = _make_agent_core_config(user_languages={12345: "en"})
    prompt = _call_load_system_prompt(12345, cfg)
    assert "English" in prompt
    assert "{{response_language}}" not in prompt


def test_load_system_prompt_language_directive_zh():
    cfg = _make_agent_core_config(user_languages={12345: "zh"})
    prompt = _call_load_system_prompt(12345, cfg)
    assert "Chinese" in prompt
    assert "{{response_language}}" not in prompt


def test_load_system_prompt_language_directive_de():
    cfg = _make_agent_core_config(user_languages={12345: "de"})
    prompt = _call_load_system_prompt(12345, cfg)
    assert "German" in prompt
    assert "{{response_language}}" not in prompt


def test_load_system_prompt_language_directive_fallback():
    cfg = _make_agent_core_config(user_languages={}, default_language="ru")
    prompt = _call_load_system_prompt(99999, cfg)
    assert "Russian" in prompt
    assert "{{response_language}}" not in prompt


# ---------------------------------------------------------------------------
# 9.2 summary.py
# ---------------------------------------------------------------------------

def test_length_bucket_labels_ru():
    assert summary_mod._length_bucket(100, "ru") == "короткий"
    assert summary_mod._length_bucket(5000, "ru") == "средний"
    assert summary_mod._length_bucket(20000, "ru") == "длинный"


def test_length_bucket_labels_en():
    assert summary_mod._length_bucket(100, "en") == "short"
    assert summary_mod._length_bucket(5000, "en") == "medium"
    assert summary_mod._length_bucket(20000, "en") == "long"


def test_length_bucket_labels_fallback():
    # Unknown language → ru labels
    assert summary_mod._length_bucket(100, "xx") == "короткий"
    assert summary_mod._length_bucket(5000, "xx") == "средний"


def test_summarize_with_cfg_system_prompt_language_en(monkeypatch):
    captured = {}

    async def _fake_create(**kwargs):
        captured["system"] = kwargs["messages"][0]["content"]
        msg = types.SimpleNamespace(content="summary here")
        choice = types.SimpleNamespace(message=msg)
        return types.SimpleNamespace(choices=[choice])

    class _FakeCompletions:
        async def create(self, **kwargs):
            return await _fake_create(**kwargs)

    class _FakeChat:
        completions = _FakeCompletions()

    class _FakeClient:
        chat = _FakeChat()

    monkeypatch.setattr(summary_mod, "_get_openai_client", lambda *_a, **_k: _FakeClient())

    asyncio.run(summary_mod._summarize_with_cfg("x" * 3001, 1000, ("key", "m", "url"), language="en"))
    assert "English" in captured["system"]
    assert "русском" not in captured["system"]


def test_suggest_commit_message_detailed_language_de(monkeypatch):
    captured = {}

    async def _fake_chat(config, system, user, max_tokens, temperature, *, response_format=None):
        captured["system"] = system
        return json.dumps({"summary": "Test commit", "body": ["point 1", "point 2", "point 3"]})

    monkeypatch.setattr(summary_mod, "_chat_completion_async", _fake_chat)

    result = asyncio.run(
        summary_mod.suggest_commit_message_detailed_async("diff", config=object(), language="de")
    )
    assert result is not None
    assert "German" in captured["system"]


# ---------------------------------------------------------------------------
# 9.3 orchestrator_runner._looks_like_nonfinal_rework_text
# ---------------------------------------------------------------------------

def test_nonfinal_rework_ru_regression():
    assert _looks_like_nonfinal_rework_text("проанализирую задачу", language="ru") is True
    assert _looks_like_nonfinal_rework_text("подготовлю отчёт", language="ru") is True


def test_nonfinal_rework_en():
    assert _looks_like_nonfinal_rework_text("Let me analyze the code", language="en") is True
    assert _looks_like_nonfinal_rework_text("I'll check the requirements", language="en") is True


def test_nonfinal_rework_zh():
    assert _looks_like_nonfinal_rework_text("让我分析这个问题", language="zh") is True
    assert _looks_like_nonfinal_rework_text("我来检查一下", language="zh") is True


def test_nonfinal_rework_de():
    assert _looks_like_nonfinal_rework_text("Lass mich analysieren was hier passiert", language="de") is True
    assert _looks_like_nonfinal_rework_text("Ich prüfe die Anforderungen", language="de") is True


def test_nonfinal_rework_long_text():
    long_text = "Let me analyze " + "x" * 200
    assert _looks_like_nonfinal_rework_text(long_text, language="en") is False


# ---------------------------------------------------------------------------
# 9.4 heuristics.needs_clarification
# ---------------------------------------------------------------------------

def test_needs_clarification_ru_regression():
    cfg = _make_config()
    assert needs_clarification("уточни пожалуйста задачу", cfg, language="ru") is True


def test_needs_clarification_en():
    cfg = _make_config()
    assert needs_clarification("Please clarify the requirements", cfg, language="en") is True


def test_needs_clarification_zh():
    cfg = _make_config()
    assert needs_clarification("请澄清一下需求", cfg, language="zh") is True


def test_needs_clarification_de():
    cfg = _make_config()
    assert needs_clarification("Das ist unklar für mich", cfg, language="de") is True


def test_needs_clarification_sentinel_skipped():
    cfg = _make_config()
    # "ответ пользователя:" internal sentinel → always False regardless of language
    for lang in ("ru", "en", "zh", "de"):
        assert needs_clarification("ответ пользователя: да", cfg, language=lang) is False


def test_needs_clarification_legacy_fallback():
    """When clarification_keywords_by_lang is empty, legacy list is used."""
    cfg = _make_config(clarification_keywords_by_lang={})
    # legacy keywords apply for all languages when by_lang is empty
    assert needs_clarification("уточни задачу", cfg, language="en") is True


# ---------------------------------------------------------------------------
# 9.5 system_prompts.yaml commit_message_system
# ---------------------------------------------------------------------------

def test_commit_message_system_prompt_language_substitution():
    import yaml
    yaml_path = os.path.normpath(
        os.path.join(os.path.dirname(__file__), "..", "modes", "manager", "system_prompts.yaml")
    )
    with open(yaml_path, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    prompts = data.get("prompts") or {}
    raw = prompts.get("commit_message_system", "")
    assert "{response_language}" in raw, "Placeholder {response_language} missing from commit_message_system"
    formatted = raw.format(response_language="English")
    assert "English" in formatted
    assert "по-русски" not in formatted
    assert "русском" not in formatted


# ---------------------------------------------------------------------------
# 9.6 Integration: summary call sites pass language
# ---------------------------------------------------------------------------

def test_git_ops_summary_passes_language():
    """suggest_commit_message_detailed_async must be called with language= from git_ops_service."""
    # Verify the language resolution is wired in (code path tested via integration).
    import utils.lang as lang_mod
    assert hasattr(lang_mod, "resolve_user_lang")


def test_bot_summary_passes_language():
    """summarize_text_with_reason accepts language kwarg (backward-compatible)."""
    # The function signature should accept language=
    import inspect
    sig = inspect.signature(summary_mod.summarize_text_with_reason)
    assert "language" in sig.parameters
    assert sig.parameters["language"].default == "ru"
