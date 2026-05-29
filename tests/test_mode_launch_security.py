import asyncio
import types

from app.events.bus import SystemEventBus
from app.security import EventBusAuditService, SecurityFacade
from modes.registry import ModeRegistry
from modes.sdk import BaseMode, ModeCallbackRouterService, ModeRegistryService, ToolResult


class _ProbeMode(BaseMode):
    def __init__(self, mode_id: str, calls: list[dict]) -> None:
        super().__init__()
        self.mode_id = mode_id
        self._calls = calls

    async def handle_input(self, message, ctx):
        return ToolResult.ok()

    async def handle_callback(self, callback, ctx):
        self._calls.append(
            {
                "mode_id": self.mode_id,
                "action": str(callback.action or ""),
                "chat_id": int(callback.chat_id),
                "session_id": str(getattr(ctx.get("session"), "id", "") or ""),
            }
        )
        return ToolResult.ok(f"{self.mode_id}:{callback.action}")


def _build_launch_runtime(
    *,
    allowed_modes: set[str],
    session_id: str = "sess-1",
    preconfigure_security: bool = True,
    include_is_user: bool = True,
):
    events: list[tuple[str, dict]] = []
    sent: list[tuple[int, str]] = []
    calls: list[dict] = []
    bus = SystemEventBus()

    async def _capture(event: str, payload: dict) -> None:
        events.append((event, dict(payload)))

    async def _send_message(_context, *, chat_id: int, text: str, **_kwargs):
        sent.append((int(chat_id), str(text or "")))
        return True

    bus.subscribe(EventBusAuditService.EVENT_NAME, _capture)
    session = types.SimpleNamespace(
        id=session_id,
        active_mode="",
        busy=False,
        queue=[],
        run_lock=asyncio.Lock(),
        is_active_by_tick=(lambda: False),
    )
    registry = ModeRegistry()
    router = ModeCallbackRouterService(
        mode_registry=ModeRegistryService(registry),
        send_message=_send_message,
        get_session=lambda _chat_id: session,
    )
    bot_app = types.SimpleNamespace(
        system_event_bus=bus,
        access_policy_service=types.SimpleNamespace(
            is_mode_allowed_for_chat=(lambda _chat_id, mode_id: str(mode_id or "") in allowed_modes)
        ),
        is_admin=(lambda _chat_id: False),
        is_allowed=(lambda chat_id: int(chat_id) == 100),
    )
    if include_is_user:
        bot_app.is_user = lambda chat_id: int(chat_id) == 100
    if preconfigure_security:
        bot_app.security = SecurityFacade.from_app_config(
            None,
            is_admin_fn=bot_app.is_admin,
            is_user_fn=bot_app.is_user if hasattr(bot_app, "is_user") else (lambda _chat_id: False),
            system_event_bus=bot_app.system_event_bus,
        )
    query = types.SimpleNamespace(
        from_user=types.SimpleNamespace(id=100),
        message=types.SimpleNamespace(chat_id=100, message_id=501),
    )
    return {
        "events": events,
        "sent": sent,
        "calls": calls,
        "registry": registry,
        "router": router,
        "bot_app": bot_app,
        "query": query,
        "session": session,
    }


def test_mode_launch_enable_uses_security_facade_and_emits_allowed_audit() -> None:
    runtime = _build_launch_runtime(allowed_modes={"alpha"})
    runtime["registry"].register(_ProbeMode("alpha", runtime["calls"]))

    handled = asyncio.run(
        runtime["router"].handle_mode_action_callback(
            data="ma:alpha:enable",
            chat_id=100,
            query=runtime["query"],
            context=object(),
            bot_app=runtime["bot_app"],
        )
    )

    assert handled is True
    assert runtime["calls"] == [
        {"mode_id": "alpha", "action": "enable", "chat_id": 100, "session_id": "sess-1"}
    ]
    assert runtime["sent"] == [(100, "alpha:enable")]
    assert len(runtime["events"]) == 1
    event_name, payload = runtime["events"][0]
    assert event_name == EventBusAuditService.EVENT_NAME
    assert payload["category"] == "mode_launch"
    assert payload["action"] == "enable"
    assert payload["status"] == "allowed"
    assert payload["user_id"] == "100"
    assert payload["subject"] == "alpha"
    assert payload["scope"] == "mode.launch.alpha"
    assert payload["reason"] == ""
    assert payload["context"]["chat_id"] == 100
    assert payload["context"]["mode_id"] == "alpha"
    assert payload["context"]["session_id"] == "sess-1"
    assert payload["context"]["callback_data"] == "ma:alpha:enable"
    assert payload["details"]["allowed"] is True


def test_mode_launch_denied_by_policy_uses_security_facade_and_emits_denied_audit() -> None:
    runtime = _build_launch_runtime(allowed_modes=set())
    runtime["registry"].register(_ProbeMode("alpha", runtime["calls"]))

    handled = asyncio.run(
        runtime["router"].handle_mode_action_callback(
            data="ma:alpha:enable",
            chat_id=100,
            query=runtime["query"],
            context=object(),
            bot_app=runtime["bot_app"],
        )
    )

    assert handled is True
    assert runtime["calls"] == []
    assert runtime["sent"] == [(100, "Режим недоступен для вашего пользователя.")]
    assert len(runtime["events"]) == 1
    event_name, payload = runtime["events"][0]
    assert event_name == EventBusAuditService.EVENT_NAME
    assert payload["category"] == "mode_launch"
    assert payload["action"] == "enable"
    assert payload["status"] == "denied"
    assert payload["user_id"] == "100"
    assert payload["subject"] == "alpha"
    assert payload["reason"] == "mode_not_allowed"
    assert payload["context"]["mode_id"] == "alpha"
    assert payload["context"]["session_id"] == "sess-1"
    assert payload["details"]["allowed"] is False


def test_mode_launch_sequential_enables_keep_audit_context_isolated() -> None:
    runtime = _build_launch_runtime(allowed_modes={"alpha", "beta"})
    runtime["registry"].register(_ProbeMode("alpha", runtime["calls"]))
    runtime["registry"].register(_ProbeMode("beta", runtime["calls"]))

    first = asyncio.run(
        runtime["router"].handle_mode_action_callback(
            data="ma:alpha:enable",
            chat_id=100,
            query=runtime["query"],
            context=object(),
            bot_app=runtime["bot_app"],
        )
    )
    second = asyncio.run(
        runtime["router"].handle_mode_action_callback(
            data="ma:beta:enable",
            chat_id=100,
            query=runtime["query"],
            context=object(),
            bot_app=runtime["bot_app"],
        )
    )

    assert first is True
    assert second is True
    assert [call["mode_id"] for call in runtime["calls"]] == ["alpha", "beta"]
    assert runtime["sent"] == [(100, "alpha:enable"), (100, "beta:enable")]
    assert len(runtime["events"]) == 2
    first_payload = runtime["events"][0][1]
    second_payload = runtime["events"][1][1]
    assert first_payload["subject"] == "alpha"
    assert first_payload["context"]["mode_id"] == "alpha"
    assert first_payload["context"]["callback_data"] == "ma:alpha:enable"
    assert second_payload["subject"] == "beta"
    assert second_payload["context"]["mode_id"] == "beta"
    assert second_payload["context"]["callback_data"] == "ma:beta:enable"


def test_mode_launch_bootstraps_security_facade_for_legacy_bot_app_without_security() -> None:
    runtime = _build_launch_runtime(
        allowed_modes={"alpha"},
        preconfigure_security=False,
        include_is_user=False,
    )
    runtime["registry"].register(_ProbeMode("alpha", runtime["calls"]))

    handled = asyncio.run(
        runtime["router"].handle_mode_action_callback(
            data="ma:alpha:enable",
            chat_id=100,
            query=runtime["query"],
            context=object(),
            bot_app=runtime["bot_app"],
        )
    )

    assert handled is True
    assert hasattr(runtime["bot_app"], "security")
    assert runtime["calls"] == [
        {"mode_id": "alpha", "action": "enable", "chat_id": 100, "session_id": "sess-1"}
    ]
    assert runtime["sent"] == [(100, "alpha:enable")]
    assert len(runtime["events"]) == 1
    assert runtime["events"][0][1]["status"] == "allowed"
