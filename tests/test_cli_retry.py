import asyncio

from modes.sdk.runtime.cli_retry import (
    classify_retryable_cli_exception,
    classify_retryable_cli_text,
    run_cli_with_retry,
)


def test_classify_retryable_cli_text_detects_quota_error() -> None:
    reason = classify_retryable_cli_text("[API Error: Qwen API quota exceeded: exhausted]")
    assert "quota exceeded" in reason.lower()


def test_classify_retryable_cli_exception_detects_timeout() -> None:
    reason = classify_retryable_cli_exception(RuntimeError("request timed out"))
    assert "timed out" in reason.lower()


def test_run_cli_with_retry_retries_once_on_retryable_output() -> None:
    async def _run():
        calls = {"n": 0}

        async def _invoke() -> str:
            calls["n"] += 1
            if calls["n"] == 1:
                return "[API Error: Qwen API quota exceeded]"
            return "ok"

        result = await run_cli_with_retry(_invoke, max_attempts=2)
        assert result["output"] == "ok"
        assert result["retried"] is True
        assert result["attempts"] == 2

    asyncio.run(_run())
