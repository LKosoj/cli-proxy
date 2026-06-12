from __future__ import annotations

import asyncio
import json

import summary as summary_mod

from modes.sdk.runtime import openai_client as openai_client_mod


def test_create_async_openai_client_sets_max_retries(monkeypatch):
    captured: dict[str, object] = {}

    class _FakeClient:
        pass

    def _fake_async_openai(**kwargs):
        captured.update(kwargs)
        return _FakeClient()

    monkeypatch.setattr(openai_client_mod, "AsyncOpenAI", _fake_async_openai)

    client = openai_client_mod.create_async_openai_client(
        api_key="k",
        base_url="https://api.openai.com",
    )

    assert isinstance(client, _FakeClient)
    assert captured["api_key"] == "k"
    assert captured["base_url"] == "https://api.openai.com"
    assert captured["max_retries"] == 4
    assert captured["default_headers"] == {"X-Title": "cli-proxy"}


def test_summary_client_factory_uses_shared_openai_builder(monkeypatch):
    summary_mod._openai_clients.clear()

    captured: dict[str, object] = {}

    class _FakeClient:
        pass

    def _fake_builder(*, api_key, base_url=None, timeout=None):
        captured["api_key"] = api_key
        captured["base_url"] = base_url
        captured["timeout"] = timeout
        return _FakeClient()

    monkeypatch.setattr(summary_mod, "create_async_openai_client", _fake_builder)

    client = summary_mod._get_openai_client("k", "https://api.openai.com")

    assert isinstance(client, _FakeClient)
    assert captured["api_key"] == "k"
    assert captured["base_url"] == "https://api.openai.com"
    assert captured["timeout"] is summary_mod._OPENAI_TIMEOUT


def test_chat_completion_normalizes_json_object_response(monkeypatch):
    async def _run():
        class _FakeCompletions:
            async def create(self, **_kwargs):
                return type(
                    "Resp",
                    (),
                    {
                        "choices": [
                            type(
                                "Choice",
                                (),
                                {
                                    "message": type(
                                        "Msg",
                                        (),
                                        {"content": '```json\n{"kind":"ok"}\n```'},
                                    )()
                                },
                            )()
                        ]
                    },
                )()

        class _FakeClient:
            def __init__(self):
                self.chat = type("Chat", (), {"completions": _FakeCompletions()})()

        monkeypatch.setattr(openai_client_mod, "build_client", lambda _cfg: (_FakeClient(), "gpt-test"))
        out = await openai_client_mod.chat_completion(
            config=object(),
            system="s",
            user="u",
            response_format={"type": "json_object"},
        )
        assert json.loads(out) == {"kind": "ok"}

    asyncio.run(_run())


def test_chat_completion_retries_on_invalid_json_object(monkeypatch):
    async def _run():
        calls = {"n": 0}

        class _FakeCompletions:
            async def create(self, **_kwargs):
                calls["n"] += 1
                return type(
                    "Resp",
                    (),
                    {
                        "choices": [
                            type(
                                "Choice",
                                (),
                                {"message": type("Msg", (), {"content": "[1,2]"})()},
                            )()
                        ]
                    },
                )()

        class _FakeClient:
            def __init__(self):
                self.chat = type("Chat", (), {"completions": _FakeCompletions()})()

        monkeypatch.setattr(openai_client_mod, "build_client", lambda _cfg: (_FakeClient(), "gpt-test"))
        failed = False
        try:
            await openai_client_mod.chat_completion(
                config=object(),
                system="s",
                user="u",
                response_format={"type": "json_object"},
            )
        except ValueError:
            failed = True
        assert failed is True
        assert calls["n"] == openai_client_mod.CHAT_COMPLETION_ATTEMPTS

    asyncio.run(_run())


def test_chat_completion_supports_model_override_max_tokens_and_client_factory(monkeypatch):
    async def _run():
        captured: dict[str, object] = {}

        class _FakeCompletions:
            async def create(self, **kwargs):
                captured.update(kwargs)
                return type(
                    "Resp",
                    (),
                    {
                        "choices": [
                            type(
                                "Choice",
                                (),
                                {"message": type("Msg", (), {"content": '{"ok":true}'})()},
                            )()
                        ]
                    },
                )()

        class _FakeClient:
            def __init__(self):
                self.chat = type("Chat", (), {"completions": _FakeCompletions()})()

        cfg = type(
            "Cfg",
            (),
            {
                "defaults": type(
                    "Defaults",
                    (),
                    {
                        "openai_api_key": "cfg-key",
                        "openai_model": "cfg-small",
                        "openai_base_url": "https://api.openai.com",
                    },
                )()
            },
        )()
        out = await openai_client_mod.chat_completion(
            config=cfg,
            system="sys",
            user="usr",
            response_format={"type": "json_object"},
            model="cfg-big",
            temperature=0.0,
            max_tokens=321,
            client_factory=lambda: _FakeClient(),
        )

        assert json.loads(out) == {"ok": True}
        assert captured["model"] == "cfg-big"
        assert captured["temperature"] == 0.0
        assert captured["max_tokens"] == 321

    asyncio.run(_run())


def test_chat_completion_uses_normalize_error_handler_when_json_is_truncated(monkeypatch):
    async def _run():
        class _FakeCompletions:
            async def create(self, **_kwargs):
                return type(
                    "Resp",
                    (),
                    {
                        "choices": [
                            type(
                                "Choice",
                                (),
                                {
                                    "message": type(
                                        "Msg",
                                        (),
                                        {"content": '{"selected_skill_id":"demo","confidence":96'},
                                    )()
                                },
                            )()
                        ]
                    },
                )()

        class _FakeClient:
            def __init__(self):
                self.chat = type("Chat", (), {"completions": _FakeCompletions()})()

        cfg = type(
            "Cfg",
            (),
            {
                "defaults": type(
                    "Defaults",
                    (),
                    {
                        "openai_api_key": "cfg-key",
                        "openai_model": "cfg-small",
                        "openai_base_url": "https://api.openai.com",
                    },
                )()
            },
        )()

        def _recover(content: str, exc: Exception) -> str | None:
            assert isinstance(exc, json.JSONDecodeError)
            assert "selected_skill_id" in content
            return '{"selected_skill_id":"demo","confidence":96}'

        out = await openai_client_mod.chat_completion(
            config=cfg,
            system="sys",
            user="usr",
            response_format={"type": "json_object"},
            client_factory=lambda: _FakeClient(),
            normalize_error_handler=_recover,
        )

        assert json.loads(out) == {"selected_skill_id": "demo", "confidence": 96}

    asyncio.run(_run())
