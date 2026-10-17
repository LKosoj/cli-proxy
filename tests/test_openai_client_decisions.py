from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import pytest

from modes.sdk.runtime import openai_client
from modes.sdk.runtime.openai_client import decision_noul, decisions, get_decision_model


def _config(**overrides) -> SimpleNamespace:
    defaults = {
        "openai_api_key": "gw-key",
        "openai_base_url": "http://gateway/v1/",
        "openai_decision_model": None,
    }
    defaults.update(overrides)
    return SimpleNamespace(defaults=SimpleNamespace(**defaults))


def test_get_decision_model_returns_none_when_not_configured() -> None:
    assert get_decision_model(_config()) is None
    assert get_decision_model(_config(openai_decision_model="  ")) is None
    assert get_decision_model(SimpleNamespace()) is None
    assert get_decision_model(_config(openai_decision_model=" llmgateway/decision ")) == "llmgateway/decision"


@pytest.mark.asyncio
async def test_decisions_posts_contract_and_returns_answers(monkeypatch) -> None:
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["auth"] = request.headers.get("authorization")
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"answers": {"q": {"type": "noul", "noul": 0.8}}, "usage": {}})

    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        openai_client.httpx,
        "AsyncClient",
        lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw),
    )

    answers = await decisions(
        _config(),
        model="llmgateway/decision",
        state="text",
        questions={"q": {"type": "noul", "instructions": "?"}},
    )

    assert captured["url"] == "http://gateway/v1/decisions"
    assert captured["auth"] == "Bearer gw-key"
    assert captured["body"] == {
        "model": "llmgateway/decision",
        "state": "text",
        "questions": {"q": {"type": "noul", "instructions": "?"}},
    }
    assert decision_noul(answers, "q") == 0.8


@pytest.mark.asyncio
async def test_decisions_raises_on_http_error(monkeypatch) -> None:
    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        openai_client.httpx,
        "AsyncClient",
        lambda **kw: real_client(transport=httpx.MockTransport(lambda _r: httpx.Response(502)), **kw),
    )

    with pytest.raises(httpx.HTTPStatusError):
        await decisions(_config(), model="m", state="s", questions={"q": {"type": "noul", "instructions": "?"}})


def test_decision_noul_handles_missing_and_invalid_values() -> None:
    assert decision_noul({}, "q") is None
    assert decision_noul({"q": {"noul": "bad"}}, "q") is None
    assert decision_noul({"q": {"noul": float("nan")}}, "q") is None
    assert decision_noul({"q": {"noul": 1.7}}, "q") == 1.0
