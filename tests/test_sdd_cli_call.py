from __future__ import annotations

import asyncio
import json
import types

import pytest

from agent.cli_routing import RoutedCallError
from modes.sdd.mode import SddMode
from modes.sdd.schemas import PLAN_OUTPUT_SCHEMA

VALID = json.dumps({"architecture": "a", "stack": ["s"], "constraints": ["c"], "risks": ["r"]})


def _bot_app():
    return types.SimpleNamespace(
        config=types.SimpleNamespace(defaults=types.SimpleNamespace(manager_decompose_timeout_sec=30))
    )


def _run(mode):
    return asyncio.run(
        mode._cli_call(
            types.SimpleNamespace(workdir="/tmp"),
            _bot_app(),
            work_type="planning",
            system="sys",
            user="usr",
            schema=PLAN_OUTPUT_SCHEMA,
        )
    )


def _patch(monkeypatch, mode, *, routed, calls):
    async def fake_chat(bot_app, system, user, *, response_format=None):
        calls["chat"] += 1
        return calls["fallback_out"]

    monkeypatch.setattr("modes.sdd.mode.run_prompt_routed_meta", routed)
    monkeypatch.setattr(mode, "_chat_completion", fake_chat)


def test_cli_success_no_fallback(monkeypatch):
    mode = SddMode()
    calls = {"chat": 0, "fallback_out": VALID}

    async def routed(*a, **k):
        return ("claude", VALID)

    _patch(monkeypatch, mode, routed=routed, calls=calls)
    result = _run(mode)
    assert result["architecture"] == "a"
    assert calls["chat"] == 0  # happy path must not touch the fallback


def test_routed_error_triggers_fallback(monkeypatch):
    mode = SddMode()
    calls = {"chat": 0, "fallback_out": VALID}

    async def routed(*a, **k):
        raise RoutedCallError(work_type="planning", tried=[])

    _patch(monkeypatch, mode, routed=routed, calls=calls)
    assert _run(mode)["stack"] == ["s"]
    assert calls["chat"] == 1


def test_cli_invalid_json_triggers_fallback(monkeypatch):
    mode = SddMode()
    calls = {"chat": 0, "fallback_out": VALID}

    async def routed(*a, **k):
        return ("claude", "not json at all")

    _patch(monkeypatch, mode, routed=routed, calls=calls)
    assert _run(mode)["risks"] == ["r"]
    assert calls["chat"] == 1


def test_cli_schema_mismatch_triggers_fallback(monkeypatch):
    mode = SddMode()
    calls = {"chat": 0, "fallback_out": VALID}

    async def routed(*a, **k):
        return ("claude", json.dumps({"wrong_key": 1}))

    _patch(monkeypatch, mode, routed=routed, calls=calls)
    assert _run(mode)["constraints"] == ["c"]
    assert calls["chat"] == 1


def test_fallback_also_invalid_propagates(monkeypatch):
    mode = SddMode()
    calls = {"chat": 0, "fallback_out": "still not json"}

    async def routed(*a, **k):
        raise RoutedCallError(work_type="planning", tried=[])

    _patch(monkeypatch, mode, routed=routed, calls=calls)
    with pytest.raises(Exception):
        _run(mode)  # a second validation failure must NOT be masked
