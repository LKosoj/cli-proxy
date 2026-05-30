"""Tests for M9 fixes #1 (mode_callbacks policy None-guard),
#4 (CHARS_PER_TOKEN_FALLBACK = 3.5), and #5 (memory_retrieval warning log).
"""
from __future__ import annotations

import logging

# ── Fix #4: CHARS_PER_TOKEN_FALLBACK ──────────────────────────────────────────


def test_chars_per_token_fallback_is_3_5() -> None:
    from modes.sdk.runtime.token_counter import CHARS_PER_TOKEN_FALLBACK
    assert CHARS_PER_TOKEN_FALLBACK == 3.5


def test_count_tokens_fallback_uses_3_5(monkeypatch) -> None:
    """When tiktoken is unavailable the fallback must use 3.5 chars/token."""
    import modes.sdk.runtime.token_counter as tc_mod

    # Force fallback by patching the encoder cache to return None for our model.
    monkeypatch.setitem(tc_mod._encoder_cache, "dummy-model", None)

    text = "a" * 35
    tokens = tc_mod.count_tokens(text, model="dummy-model")
    # 35 chars / 3.5 = 10 tokens
    assert tokens == 10


def test_count_tokens_fallback_fewer_tokens_than_2_5(monkeypatch) -> None:
    """3.5 chars/token should produce fewer tokens than 2.5 chars/token would."""
    import modes.sdk.runtime.token_counter as tc_mod

    monkeypatch.setitem(tc_mod._encoder_cache, "dummy-model", None)

    text = "x" * 100
    tokens = tc_mod.count_tokens(text, model="dummy-model")
    # With 3.5: 100/3.5 ≈ 28 tokens; with 2.5: 100/2.5 = 40 tokens
    assert tokens < 40, "3.5 char/token fallback should yield fewer tokens than 2.5"
    assert tokens == int(100 / 3.5)


# ── Fix #5: memory_retrieval warning log ──────────────────────────────────────

def test_memory_retrieval_logs_warning_for_empty_prepared_query(tmp_path, caplog) -> None:
    """retrieve_relevant_context must log a warning when the query yields no FTS terms."""
    import json as _json

    from modes.sdk.runtime.memory_retrieval import retrieve_relevant_context

    cwd = str(tmp_path)
    # Write a minimal SESSION.json so _sync_index doesn't fail.
    (tmp_path / "SESSION.json").write_text(
        _json.dumps({"orchestrator_by_task": {}}), encoding="utf-8"
    )

    # A query consisting only of stop-words / punctuation that _prepare_query strips
    # will produce an empty prepared string.  Use a query that's all digits and
    # special chars which the word-tokeniser ignores.
    query = "!!! ??? ---"

    with caplog.at_level(logging.WARNING, logger="modes.sdk.runtime.memory_retrieval"):
        result = retrieve_relevant_context(cwd, query, limit=5)

    assert result == []
    warning_messages = [r.message for r in caplog.records if r.levelno == logging.WARNING]
    assert any("FTS terms" in m or "produced no" in m for m in warning_messages), (
        f"Expected warning about empty FTS terms, got: {warning_messages}"
    )
