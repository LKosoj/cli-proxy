from __future__ import annotations

import asyncio
import json
import types

import summary as summary_mod


def test_detailed_commit_prompt_requires_russian_language_and_json_output(monkeypatch):
    async def _run() -> None:
        captured = {"system": "", "user": "", "response_format": None, "max_tokens": None}

        async def _fake_chat_completion(
            config,
            system: str,
            user: str,
            max_tokens: int,
            temperature: float,
            *,
            response_format=None,
        ) -> str:
            captured["system"] = system
            captured["user"] = user
            captured["response_format"] = response_format
            captured["max_tokens"] = max_tokens
            return json.dumps(
                {
                    "summary": "Исправлен парсинг",
                    "body": ["Обновлен модуль summary.py", "Тесты: не запускались"],
                },
                ensure_ascii=False,
            )

        monkeypatch.setattr(summary_mod, "_chat_completion_async", _fake_chat_completion)

        out = await summary_mod.suggest_commit_message_detailed_async("diff context", config=object())
        assert out is not None
        assert "Russian" in captured["system"]
        assert "JSON" in captured["system"]
        assert captured["response_format"] == {"type": "json_object"}
        assert captured["max_tokens"] == summary_mod._COMMIT_MESSAGE_MAX_TOKENS

    asyncio.run(_run())


def test_detailed_commit_message_accepts_json_object(monkeypatch):
    async def _run() -> None:
        async def _fake_chat_completion(
            config,
            system: str,
            user: str,
            max_tokens: int,
            temperature: float,
            *,
            response_format=None,
        ) -> str:
            _ = (config, system, user, max_tokens, temperature, response_format)
            return json.dumps(
                {
                    "summary": "Обновлена генерация commit message",
                    "body": [
                        "summary.py переведен на JSON-only ответ модели",
                        "Добавлен response_format json_object",
                        "Тесты: не запускались",
                    ],
                },
                ensure_ascii=False,
            )

        monkeypatch.setattr(summary_mod, "_chat_completion_async", _fake_chat_completion)

        out = await summary_mod.suggest_commit_message_detailed_async("diff context", config=object())
        assert out == (
            "Обновлена генерация commit message",
            "summary.py переведен на JSON-only ответ модели\n"
            "Добавлен response_format json_object\n"
            "Тесты: не запускались",
        )

    asyncio.run(_run())


def test_detailed_commit_message_rejects_legacy_summary_body_format(monkeypatch):
    async def _run() -> None:
        async def _fake_chat_completion(
            config,
            system: str,
            user: str,
            max_tokens: int,
            temperature: float,
            *,
            response_format=None,
        ) -> str:
            _ = (config, system, user, max_tokens, temperature, response_format)
            return "SUMMARY: Исправлен парсинг\nBODY:\n- Обновлен модуль summary.py"

        monkeypatch.setattr(summary_mod, "_chat_completion_async", _fake_chat_completion)

        out = await summary_mod.suggest_commit_message_detailed_async("diff context", config=object())
        assert out is None

    asyncio.run(_run())


def test_chat_completion_passes_json_response_format_to_client(monkeypatch):
    async def _run() -> None:
        seen = {"response_format": None}

        class _FakeCompletions:
            async def create(self, *, model, messages, max_tokens=None, temperature=None, response_format=None):
                _ = (model, messages, max_tokens, temperature)
                seen["response_format"] = response_format
                msg = types.SimpleNamespace(content='{"summary":"ok","body":["Тесты: не запускались"]}')
                choice = types.SimpleNamespace(message=msg)
                return types.SimpleNamespace(choices=[choice])

        class _FakeChat:
            def __init__(self):
                self.completions = _FakeCompletions()

        class _FakeClient:
            def __init__(self):
                self.chat = _FakeChat()

        cfg = types.SimpleNamespace(
            defaults=types.SimpleNamespace(
                openai_api_key="k",
                openai_big_model="m-big",
                openai_base_url="https://api.openai.com",
            )
        )
        monkeypatch.setattr(summary_mod, "_get_openai_client", lambda *_args, **_kwargs: _FakeClient())

        out = await summary_mod._chat_completion_async(
            cfg,
            "system",
            "user",
            120,
            0.2,
            response_format={"type": "json_object"},
        )
        assert out
        assert seen["response_format"] == {"type": "json_object"}

    asyncio.run(_run())


def test_chat_completion_logs_when_finish_reason_is_length(monkeypatch, caplog):
    async def _run() -> None:
        class _FakeCompletions:
            async def create(self, *, model, messages, max_tokens=None, temperature=None, response_format=None):
                _ = (model, messages, max_tokens, temperature, response_format)
                msg = types.SimpleNamespace(content='{"summary":"ok"')
                choice = types.SimpleNamespace(message=msg, finish_reason="length")
                return types.SimpleNamespace(choices=[choice])

        class _FakeChat:
            def __init__(self):
                self.completions = _FakeCompletions()

        class _FakeClient:
            def __init__(self):
                self.chat = _FakeChat()

        cfg = types.SimpleNamespace(
            defaults=types.SimpleNamespace(
                openai_api_key="k",
                openai_big_model="m-big",
                openai_base_url="https://api.openai.com",
            )
        )
        monkeypatch.setattr(summary_mod, "_get_openai_client", lambda *_args, **_kwargs: _FakeClient())

        with caplog.at_level("WARNING"):
            out = await summary_mod._chat_completion_async(
                cfg,
                "system",
                "user",
                321,
                0.2,
                response_format={"type": "json_object"},
            )
        assert out == '{"summary":"ok"'
        assert "chat_completion truncated response" in caplog.text
        assert "max_tokens=321" in caplog.text

    asyncio.run(_run())


def test_detailed_commit_message_retries_up_to_five_times(monkeypatch):
    async def _run() -> None:
        calls = {"n": 0}

        async def _fake_chat_completion(
            config,
            system: str,
            user: str,
            max_tokens: int,
            temperature: float,
            *,
            response_format=None,
        ) -> str:
            _ = (config, system, user, max_tokens, temperature, response_format)
            calls["n"] += 1
            if calls["n"] < 5:
                return '{"summary":"Исправлен commit flow","body":["Строка с literal "old" ломает json"]}'
            return json.dumps(
                {
                    "summary": "Исправлен commit flow",
                    "body": [
                        "Добавлен повторный запрос к модели при невалидном JSON",
                        "Количество попыток увеличено до пяти",
                        "Тесты: не запускались",
                    ],
                },
                ensure_ascii=False,
            )

        monkeypatch.setattr(summary_mod, "_chat_completion_async", _fake_chat_completion)

        out = await summary_mod.suggest_commit_message_detailed_async("diff context", config=object())
        assert out == (
            "Исправлен commit flow",
            "Добавлен повторный запрос к модели при невалидном JSON\n"
            "Количество попыток увеличено до пяти\n"
            "Тесты: не запускались",
        )
        assert calls["n"] == 5

    asyncio.run(_run())
