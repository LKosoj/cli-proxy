import asyncio
from datetime import timedelta
import logging

import pytest
from telegram.error import RetryAfter

from app.services.assistant_preview_service import AssistantPreviewRateLimiter


@pytest.mark.asyncio
async def test_preview_rate_limit_is_shared_by_chat_and_independent_between_chats() -> None:
    limiter = AssistantPreviewRateLimiter(
        text_edit_interval_sec=0.05,
        timer_edit_interval_sec=0.1,
    )
    calls: list[tuple[int, str, float]] = []
    completed = {name: asyncio.Event() for name in ("first", "same_chat", "other_chat")}
    loop = asyncio.get_running_loop()

    def update(chat_id: int, name: str):
        async def _apply() -> None:
            calls.append((chat_id, name, loop.time()))
            completed[name].set()

        return _apply

    await limiter.submit(
        chat_id=1,
        owner="session-a",
        timer_only=False,
        apply_update=update(1, "first"),
    )
    await asyncio.wait_for(completed["first"].wait(), timeout=1.0)

    await limiter.submit(
        chat_id=1,
        owner="session-b",
        timer_only=False,
        apply_update=update(1, "same_chat"),
    )
    await limiter.submit(
        chat_id=2,
        owner="session-c",
        timer_only=False,
        apply_update=update(2, "other_chat"),
    )

    await asyncio.wait_for(completed["other_chat"].wait(), timeout=1.0)
    assert completed["same_chat"].is_set() is False
    await asyncio.wait_for(completed["same_chat"].wait(), timeout=1.0)

    assert [name for _chat_id, name, _at in calls] == ["first", "other_chat", "same_chat"]
    assert calls[2][2] - calls[0][2] >= 0.045


@pytest.mark.asyncio
async def test_preview_rate_limiter_coalesces_to_latest_update() -> None:
    limiter = AssistantPreviewRateLimiter(
        text_edit_interval_sec=0.05,
        timer_edit_interval_sec=0.1,
    )
    calls: list[str] = []
    completed = {name: asyncio.Event() for name in ("base", "latest")}

    def update(name: str):
        async def _apply() -> None:
            calls.append(name)
            if name in completed:
                completed[name].set()

        return _apply

    await limiter.submit(
        chat_id=1,
        owner="session-a",
        timer_only=False,
        apply_update=update("base"),
    )
    await asyncio.wait_for(completed["base"].wait(), timeout=1.0)

    for value in ("old", "middle", "latest"):
        await limiter.submit(
            chat_id=1,
            owner="session-a",
            timer_only=False,
            apply_update=update(value),
        )

    await asyncio.wait_for(completed["latest"].wait(), timeout=1.0)
    assert calls == ["base", "latest"]


@pytest.mark.asyncio
async def test_preview_retry_after_blocks_requests_and_recovers_with_latest(caplog, monkeypatch) -> None:
    monkeypatch.setenv("PTB_TIMEDELTA", "1")
    limiter = AssistantPreviewRateLimiter(
        text_edit_interval_sec=0.01,
        timer_edit_interval_sec=0.02,
    )
    attempts: list[tuple[str, float]] = []
    first_attempt = asyncio.Event()
    recovered = asyncio.Event()
    loop = asyncio.get_running_loop()

    def update(value: str):
        async def _apply() -> None:
            attempts.append((value, loop.time()))
            if len(attempts) == 1:
                first_attempt.set()
                raise RetryAfter(timedelta(seconds=0.08))
            recovered.set()

        return _apply

    caplog.set_level(logging.INFO, logger="app.services.assistant_preview_service")
    await limiter.submit(
        chat_id=1,
        owner="session-a",
        timer_only=False,
        apply_update=update("initial"),
    )
    await asyncio.wait_for(first_attempt.wait(), timeout=1.0)
    await asyncio.sleep(0)

    await limiter.submit(
        chat_id=1,
        owner="session-a",
        timer_only=False,
        apply_update=update("latest"),
    )
    await asyncio.sleep(0.03)
    assert [value for value, _at in attempts] == ["initial"]

    await asyncio.wait_for(recovered.wait(), timeout=1.0)
    assert [value for value, _at in attempts] == ["initial", "latest"]
    assert attempts[1][1] - attempts[0][1] >= 0.07
    assert "assistant preview cooldown started chat_id=1" in caplog.text
    assert "assistant preview cooldown finished chat_id=1" in caplog.text
    assert "suppressed_updates=1" in caplog.text


@pytest.mark.asyncio
async def test_timer_only_preview_update_uses_longer_interval() -> None:
    limiter = AssistantPreviewRateLimiter(
        text_edit_interval_sec=0.01,
        timer_edit_interval_sec=0.08,
    )
    calls: list[tuple[str, float]] = []
    completed = {name: asyncio.Event() for name in ("text", "timer")}
    loop = asyncio.get_running_loop()

    def update(name: str):
        async def _apply() -> None:
            calls.append((name, loop.time()))
            completed[name].set()

        return _apply

    await limiter.submit(
        chat_id=1,
        owner="session-a",
        timer_only=False,
        apply_update=update("text"),
    )
    await asyncio.wait_for(completed["text"].wait(), timeout=1.0)
    await limiter.submit(
        chat_id=1,
        owner="session-a",
        timer_only=True,
        apply_update=update("timer"),
    )

    await asyncio.sleep(0.03)
    assert completed["timer"].is_set() is False
    await asyncio.wait_for(completed["timer"].wait(), timeout=1.0)
    assert calls[1][1] - calls[0][1] >= 0.07
