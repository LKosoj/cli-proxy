from __future__ import annotations

import summary as summary_mod

from agent import openai_client as openai_client_mod


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
