from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, Optional

from agent.manager import ManagerOrchestrator
from modes.sdk.context import ModeRuntimeContext


SendMessageFn = Callable[..., Awaitable[Any]]
SendOutputFn = Callable[..., Awaitable[Any]]
IsAdminFn = Callable[[Any], bool]


def _require_callable(value: Any, name: str) -> Any:
    if not callable(value):
        raise RuntimeError(f"ManagerRuntimeAdapter {name} is not configured")
    return value


def _normalize_is_admin(value: Any) -> IsAdminFn:
    checker = _require_callable(value, "is_admin")

    def _is_admin(chat_id: Any) -> bool:
        return bool(checker(chat_id))

    return _is_admin


@dataclass(frozen=True)
class ManagerRuntimeAdapter:
    """Narrow runtime surface used by ManagerOrchestrator."""

    config: Any
    send_message: SendMessageFn
    send_output: SendOutputFn
    is_admin: IsAdminFn

    def __post_init__(self) -> None:
        _require_callable(self.send_message, "send_message")
        _require_callable(self.send_output, "send_output")
        object.__setattr__(self, "is_admin", _normalize_is_admin(self.is_admin))

    @classmethod
    def from_bot_app(cls, bot_app: Any, *, config: Any = None) -> "ManagerRuntimeAdapter":
        return cls(
            config=config if config is not None else getattr(bot_app, "config", None),
            send_message=_require_callable(getattr(bot_app, "_send_message", None), "send_message"),
            send_output=_require_callable(getattr(bot_app, "send_output", None), "send_output"),
            is_admin=_require_callable(getattr(bot_app, "is_admin", None), "is_admin"),
        )

    @classmethod
    def from_runtime_context(
        cls,
        runtime_context: ModeRuntimeContext,
        *,
        send_output: Optional[SendOutputFn] = None,
        is_admin: Optional[IsAdminFn] = None,
    ) -> "ManagerRuntimeAdapter":
        messaging = runtime_context.messaging
        return cls(
            config=runtime_context.config,
            send_message=_require_callable(getattr(messaging, "send_message", None), "send_message"),
            send_output=_require_callable(send_output, "send_output"),
            is_admin=_require_callable(is_admin, "is_admin"),
        )

    async def _send_message(self, context: Any, **kwargs: Any) -> Any:
        return await self.send_message(context, **kwargs)


class ManagerModeRunnerService:
    """Manager mode-owned orchestration runtime."""

    capabilities = frozenset({"run_manager", "manager_control"})

    def __init__(self, config: Any) -> None:
        self._orchestrator = ManagerOrchestrator(config)

    def set_config(self, config: Any) -> None:
        self._orchestrator._config = config

    async def run(self, session: Any, prompt: str, bot_app: Any, context: Any, dest: Dict[str, Any]) -> str:
        adapter = ManagerRuntimeAdapter.from_bot_app(bot_app, config=self._orchestrator._config)
        return await self._orchestrator.run(session, str(prompt), adapter, context, dict(dest or {}))

    async def run_with_runtime_context(
        self,
        runtime_context: ModeRuntimeContext,
        prompt: str,
        *,
        send_output: SendOutputFn,
        is_admin: IsAdminFn,
    ) -> str:
        adapter = ManagerRuntimeAdapter.from_runtime_context(
            runtime_context,
            send_output=send_output,
            is_admin=is_admin,
        )
        return await self._orchestrator.run(
            runtime_context.session,
            str(prompt),
            adapter,
            runtime_context.context,
            dict(runtime_context.dest or {}),
        )

    def pause(self, session: Any) -> None:
        self._orchestrator.pause(session)

    def reset(self, session: Any) -> None:
        self._orchestrator.reset(session)

    def supports_capability(self, capability: str) -> bool:
        return str(capability or "").strip() in self.capabilities
