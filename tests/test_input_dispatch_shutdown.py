from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from app.services.input_dispatch_service import InputDispatchService


@pytest.mark.asyncio
async def test_handle_cli_input_does_not_schedule_run_prompt_during_shutdown() -> None:
    executed = False

    async def _run_prompt(_session, _text, _dest, _context):
        nonlocal executed
        executed = True
        return None

    bot_app = SimpleNamespace(
        _shutdown_in_progress=True,
        run_prompt=_run_prompt,
        pending={},
        metrics=SimpleNamespace(inc=lambda *_args, **_kwargs: None),
        _send_message=None,
    )
    service = InputDispatchService(bot_app)
    session = SimpleNamespace(
        id="s1",
        busy=False,
        is_active_by_tick=lambda: False,
        run_lock=asyncio.Lock(),
    )

    await service.handle_cli_input(session, "hello", chat_id=1, context=None)
    await asyncio.sleep(0)

    assert executed is False
