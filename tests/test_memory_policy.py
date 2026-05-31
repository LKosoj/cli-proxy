import json

import pytest

from config import AppConfig, DefaultsConfig, MCPConfig, TelegramConfig
from modes.sdk.runtime import memory_policy


def _cfg(tmp_path) -> AppConfig:
    return AppConfig(
        telegram=TelegramConfig(token="", whitelist_chat_ids=[]),
        tools={},
        defaults=DefaultsConfig(
            workdir=str(tmp_path),
            state_path=str(tmp_path / "state.json"),
            toolhelp_path=str(tmp_path / "toolhelp.json"),
            log_path=str(tmp_path / "bot.log"),
        ),
        mcp=MCPConfig(enabled=False),
        mcp_clients=[],
        presets=[],
        path=str(tmp_path / "config.yaml"),
    )


@pytest.mark.asyncio
async def test_decide_memory_save_accepts_verified_semantic_memory(tmp_path, monkeypatch):
    async def _fake_chat_completion(*_args, **_kwargs):
        return json.dumps(
            {
                "save": True,
                "category": "config",
                "layer": "semantic",
                "content": "sqlite fts5 включен",
                "source": "agent",
                "confidence": 0.9,
                "verification_status": "verified",
                "evidence_type": "config",
                "evidence_ref": "config.yaml",
            },
            ensure_ascii=False,
        )

    monkeypatch.setattr(memory_policy, "chat_completion", _fake_chat_completion)

    decision = await memory_policy.decide_memory_save(_cfg(tmp_path), "q", "a", "")

    assert decision is not None
    assert decision["tag"] == "CONFIG"
    assert decision["verification_status"] == "verified"
    assert decision["evidence_type"] == "config"
    assert decision["evidence_ref"] == "config.yaml"


@pytest.mark.asyncio
async def test_decide_memory_save_rejects_unverified_semantic_memory(tmp_path, monkeypatch):
    async def _fake_chat_completion(*_args, **_kwargs):
        return json.dumps(
            {
                "save": True,
                "category": "decision",
                "layer": "semantic",
                "content": "использовать sqlite",
                "source": "agent",
                "confidence": 0.99,
                "verification_status": "unverified",
                "evidence_type": "none",
            },
            ensure_ascii=False,
        )

    monkeypatch.setattr(memory_policy, "chat_completion", _fake_chat_completion)

    assert await memory_policy.decide_memory_save(_cfg(tmp_path), "q", "a", "") is None


@pytest.mark.asyncio
async def test_decide_memory_save_rejects_llm_controlled_user_evidence(tmp_path, monkeypatch):
    async def _fake_chat_completion(*_args, **_kwargs):
        return json.dumps(
            {
                "save": True,
                "category": "config",
                "layer": "semantic",
                "content": "sqlite fts5 включен",
                "source": "agent",
                "confidence": 0.9,
                "verification_status": "verified",
                "evidence_type": "user",
            },
            ensure_ascii=False,
        )

    monkeypatch.setattr(memory_policy, "chat_completion", _fake_chat_completion)

    assert await memory_policy.decide_memory_save(_cfg(tmp_path), "q", "a", "") is None


@pytest.mark.asyncio
async def test_decide_memory_save_allows_unverified_task_state_with_ttl(tmp_path, monkeypatch):
    async def _fake_chat_completion(*_args, **_kwargs):
        return json.dumps(
            {
                "save": True,
                "category": "task_state",
                "layer": "task_state",
                "content": "проверить sqlite позже",
                "source": "agent",
                "confidence": 0.5,
                "verification_status": "unverified",
                "evidence_type": "none",
            },
            ensure_ascii=False,
        )

    monkeypatch.setattr(memory_policy, "chat_completion", _fake_chat_completion)

    decision = await memory_policy.decide_memory_save(_cfg(tmp_path), "q", "a", "")

    assert decision is not None
    assert decision["tag"] == "TASK"
    assert decision["layer"] == "task_state"
    assert decision["ttl_days"] == 14
    assert decision["verification_status"] == "unverified"


@pytest.mark.asyncio
async def test_compress_memory_rejects_trust_escalation(tmp_path, monkeypatch):
    original = (
        "- 2026-02-10 12:00: [TASK] [LAYER:task_state] [SRC:agent] [ID:u1] "
        "[VER:unverified] [EVID:none] sqlite hypothesis\n"
    )
    escalated = (
        "- 2026-02-10 12:00: [TASK] [LAYER:task_state] [SRC:agent] [ID:u1] "
        "[VER:verified] [EVID:config] sqlite hypothesis\n"
    )

    async def _fake_chat_completion(*_args, **_kwargs):
        return escalated

    monkeypatch.setattr(memory_policy, "chat_completion", _fake_chat_completion)

    assert await memory_policy.compress_memory(_cfg(tmp_path), original, 100) is None


@pytest.mark.asyncio
async def test_compress_memory_rejects_new_verified_entry(tmp_path, monkeypatch):
    original = (
        "- 2026-02-10 12:00: [TASK] [LAYER:task_state] [SRC:agent] [ID:u1] "
        "[VER:unverified] [EVID:none] sqlite hypothesis\n"
    )
    compressed = original + (
        "- 2026-02-10 12:01: [CONFIG] [LAYER:semantic] [SRC:agent] [ID:v1] "
        "[VER:verified] [EVID:config] sqlite setting\n"
    )

    async def _fake_chat_completion(*_args, **_kwargs):
        return compressed

    monkeypatch.setattr(memory_policy, "chat_completion", _fake_chat_completion)

    assert await memory_policy.compress_memory(_cfg(tmp_path), original, 100) is None


@pytest.mark.asyncio
async def test_compress_memory_rejects_verified_text_change(tmp_path, monkeypatch):
    original = (
        "- 2026-02-10 12:00: [CONFIG] [LAYER:semantic] [SRC:agent] [ID:v1] "
        "[VER:verified] [EVID:config] sqlite disabled\n"
    )
    changed = (
        "- 2026-02-10 12:00: [CONFIG] [LAYER:semantic] [SRC:agent] [ID:v1] "
        "[VER:verified] [EVID:config] postgres enabled\n"
    )

    async def _fake_chat_completion(*_args, **_kwargs):
        return changed

    monkeypatch.setattr(memory_policy, "chat_completion", _fake_chat_completion)

    assert await memory_policy.compress_memory(_cfg(tmp_path), original, 100) is None


@pytest.mark.asyncio
async def test_compress_memory_rejects_verified_without_id(tmp_path, monkeypatch):
    original = (
        "- 2026-02-10 12:00: [CONFIG] [LAYER:semantic] [SRC:agent] [ID:v1] "
        "[VER:verified] [EVID:config] sqlite setting\n"
    )
    changed = (
        "- 2026-02-10 12:00: [CONFIG] [LAYER:semantic] [SRC:agent] "
        "[VER:verified] [EVID:config] postgres enabled\n"
    )

    async def _fake_chat_completion(*_args, **_kwargs):
        return changed

    monkeypatch.setattr(memory_policy, "chat_completion", _fake_chat_completion)

    assert await memory_policy.compress_memory(_cfg(tmp_path), original, 100) is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "original_ref, compressed_ref",
    [
        ("", "[REF:fake] "),
        ("[REF:config.yaml] ", "[REF:fake] "),
        ("[REF:config.yaml] ", ""),
    ],
)
async def test_compress_memory_rejects_verified_evidence_ref_change(
    tmp_path,
    monkeypatch,
    original_ref,
    compressed_ref,
):
    original = (
        "- 2026-02-10 12:00: [CONFIG] [LAYER:semantic] [SRC:agent] [ID:v1] "
        f"[VER:verified] [EVID:config] {original_ref}sqlite setting\n"
    )
    changed = (
        "- 2026-02-10 12:00: [CONFIG] [LAYER:semantic] [SRC:agent] [ID:v1] "
        f"[VER:verified] [EVID:config] {compressed_ref}sqlite setting\n"
    )

    async def _fake_chat_completion(*_args, **_kwargs):
        return changed

    monkeypatch.setattr(memory_policy, "chat_completion", _fake_chat_completion)

    assert await memory_policy.compress_memory(_cfg(tmp_path), original, 100) is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "extra_token",
    [
        "[REF:fake]",
        "[EVID:tool]",
        "[VER:verified]",
    ],
)
async def test_compress_memory_rejects_duplicate_raw_trust_tokens(tmp_path, monkeypatch, extra_token):
    original = (
        "- 2026-02-10 12:00: [CONFIG] [LAYER:semantic] [SRC:agent] [ID:v1] "
        "[VER:verified] [EVID:config] [REF:config.yaml] sqlite setting\n"
    )
    changed = (
        "- 2026-02-10 12:00: [CONFIG] [LAYER:semantic] [SRC:agent] [ID:v1] "
        f"[VER:verified] [EVID:config] [REF:config.yaml] {extra_token} sqlite setting\n"
    )

    async def _fake_chat_completion(*_args, **_kwargs):
        return changed

    monkeypatch.setattr(memory_policy, "chat_completion", _fake_chat_completion)

    assert await memory_policy.compress_memory(_cfg(tmp_path), original, 100) is None


@pytest.mark.asyncio
async def test_compress_memory_preserves_existing_verified_trust(tmp_path, monkeypatch):
    original = (
        "- 2026-02-10 12:00: [CONFIG] [LAYER:semantic] [SRC:agent] [ID:v1] "
        "[VER:verified] [EVID:config] sqlite setting\n"
    )

    async def _fake_chat_completion(*_args, **_kwargs):
        return original

    monkeypatch.setattr(memory_policy, "chat_completion", _fake_chat_completion)

    assert await memory_policy.compress_memory(_cfg(tmp_path), original, 100) == original.strip()
