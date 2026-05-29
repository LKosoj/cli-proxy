from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.services.input_dispatch_service import InputDispatchService


@pytest.mark.asyncio
async def test_handle_user_input_propagates_mode_errors_without_cli_fallback() -> None:
    cli_calls = {"n": 0}

    async def _handle_cli_input(*_args, **_kwargs):
        cli_calls["n"] += 1

    class _FailingRouter:
        def __init__(self) -> None:
            self.send_message = None
            self.dialogs = None
            self.mode_registry = None

        async def route_mode_or_cli(self, **_kwargs):
            raise RuntimeError("mode route failed")

    bot_app = SimpleNamespace(
        mode_input_router=_FailingRouter(),
        mode_dialogs=None,
        mode_registry_service=SimpleNamespace(),
        _send_message=None,
        _handle_cli_input=_handle_cli_input,
    )
    service = InputDispatchService(bot_app)

    session = SimpleNamespace(
        id="s1",
        active_mode="analyst",
        advanced_orchestrator_enabled=False,
    )

    with pytest.raises(RuntimeError, match="mode route failed"):
        await service.handle_user_input(session, "hello", chat_id=1, context=object())

    assert cli_calls["n"] == 0
