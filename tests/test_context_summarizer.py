import asyncio

from modes.sdk.runtime import context_summarizer as cs


def test_summarize_working_context_returns_original_when_under_limit(monkeypatch):
    async def _run():
        monkeypatch.setattr(
            cs,
            "count_messages_tokens",
            lambda messages, _model: sum(len(str(item.get("content") or "")) for item in messages),
        )

        working = [
            {"role": "assistant", "content": "short"},
            {"role": "tool", "content": "ok"},
        ]
        base = [{"role": "system", "content": "sys"}, {"role": "user", "content": "user"}]
        result, summarized = await cs.summarize_working_context(
            working,
            base_messages=base,
            config=object(),
            max_tokens=10_000,
            threshold=0.8,
        )
        assert summarized is False
        assert result == working

    asyncio.run(_run())


def test_summarize_working_context_replaces_old_head_and_preserves_recent_tail(monkeypatch):
    async def _run():
        monkeypatch.setattr(
            cs,
            "count_messages_tokens",
            lambda messages, _model: sum(len(str(item.get("content") or "")) for item in messages),
        )

        async def _fake_summarize_text_chunks(chunks, _config):
            assert chunks
            return "summary of earlier work"

        monkeypatch.setattr(cs, "_summarize_text_chunks", _fake_summarize_text_chunks)

        working = []
        for idx in range(14):
            working.append({"role": "assistant", "content": f"assistant message {idx} " + ("x" * 120)})
        base = [{"role": "system", "content": "sys"}, {"role": "user", "content": "user"}]

        result, summarized = await cs.summarize_working_context(
            working,
            base_messages=base,
            config=object(),
            max_tokens=200,
            threshold=0.5,
        )

        assert summarized is True
        assert len(result) == 1 + cs.PRESERVE_LAST_N_WORKING_MESSAGES
        assert result[0]["role"] == "assistant"
        assert "[Суммаризация рабочего контекста]" in result[0]["content"]
        assert "summary of earlier work" in result[0]["content"]
        assert result[1:] == working[-cs.PRESERVE_LAST_N_WORKING_MESSAGES:]

    asyncio.run(_run())


def test_summarize_context_preserves_recent_user_tail(monkeypatch):
    async def _run():
        monkeypatch.setattr(
            cs,
            "count_messages_tokens",
            lambda messages, _model: sum(len(str(item.get("content") or "")) for item in messages),
        )

        async def _fake_summarize_text_chunks(chunks, _config):
            assert chunks
            return "older conversation summary"

        monkeypatch.setattr(cs, "_summarize_text_chunks", _fake_summarize_text_chunks)

        messages = [{"role": "system", "content": "sys"}]
        for idx in range(8):
            messages.append({"role": "user", "content": f"user {idx} " + ("u" * 80)})
            messages.append({"role": "assistant", "content": f"assistant {idx} " + ("a" * 80)})

        result, summarized = await cs.summarize_context(
            messages,
            config=object(),
            max_tokens=300,
            threshold=0.5,
        )

        assert summarized is True
        assert result[0]["role"] == "system"
        assert result[1]["role"] == "assistant"
        assert "[Контекст суммаризирован]" in result[1]["content"]
        preserved_tail = result[2:]
        user_tail = [msg for msg in preserved_tail if msg.get("role") == "user"]
        assert len(user_tail) == cs.PRESERVE_LAST_N_EXCHANGES
        assert user_tail[0]["content"].startswith("user 4")
        assert user_tail[-1]["content"].startswith("user 7")

    asyncio.run(_run())
