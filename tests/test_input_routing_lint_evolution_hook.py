from __future__ import annotations

import asyncio
import types

from modes.registry import ModeRegistry
from modes.sdk import BaseMode, ModeInputRoutingService, ModeRegistryService, ToolResult


def _build_router(hook):
    class EchoMode(BaseMode):
        mode_id = "echo"

        async def handle_input(self, _message, _ctx):
            return ToolResult.ok("OK")

        async def handle_callback(self, _callback, _ctx):
            return ToolResult.ok()

    registry = ModeRegistry()
    registry.register(EchoMode())
    router = ModeInputRoutingService(
        mode_registry=ModeRegistryService(registry),
        lint_evolution_hook=hook,
    )

    async def _send_output(*_args, **_kwargs):
        return None

    async def _send_message(*_args, **_kwargs):
        return None

    router.send_output = _send_output
    router.send_message = _send_message
    return router


def test_hook_invoked_with_session_when_set() -> None:
    captured: list[object] = []

    def hook(session) -> None:
        captured.append(session)

    router = _build_router(hook)
    session = types.SimpleNamespace(id="s1", active_mode="echo", workdir="/tmp")

    async def _cli_fallback(*_a, **_kw):
        raise AssertionError("cli fallback must not be called")

    asyncio.run(
        router.route_mode_or_cli(
            bot_app=types.SimpleNamespace(),
            session=session,
            text="hi",
            chat_id=101,
            context=object(),
            dest={"kind": "telegram", "chat_id": 101},
            user_id=7,
            cli_fallback=_cli_fallback,
        )
    )

    assert captured == [session]


def test_routing_continues_when_hook_raises() -> None:
    def hook(_session) -> None:
        raise RuntimeError("boom")

    router = _build_router(hook)
    session = types.SimpleNamespace(id="s1", active_mode="echo", workdir="/tmp")

    async def _cli_fallback(*_a, **_kw):
        raise AssertionError("cli fallback must not be called")

    # Must not raise — exception from hook must be swallowed.
    asyncio.run(
        router.route_mode_or_cli(
            bot_app=types.SimpleNamespace(),
            session=session,
            text="hi",
            chat_id=101,
            context=object(),
            dest={"kind": "telegram", "chat_id": 101},
            user_id=7,
            cli_fallback=_cli_fallback,
        )
    )


def test_no_hook_means_no_call() -> None:
    router = _build_router(hook=None)
    session = types.SimpleNamespace(id="s1", active_mode="echo", workdir="/tmp")

    async def _cli_fallback(*_a, **_kw):
        raise AssertionError("cli fallback must not be called")

    # Just must not blow up.
    asyncio.run(
        router.route_mode_or_cli(
            bot_app=types.SimpleNamespace(),
            session=session,
            text="hi",
            chat_id=101,
            context=object(),
            dest={"kind": "telegram", "chat_id": 101},
            user_id=7,
            cli_fallback=_cli_fallback,
        )
    )
