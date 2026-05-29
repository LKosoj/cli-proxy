from __future__ import annotations

import importlib
from typing import Any, Callable, Dict, Optional


class SharedOrchestratorRunner:
    """
    SDK-level shared orchestrator runtime wrapper.

    Modes should depend on this abstraction instead of importing
    agent-domain orchestrator directly.
    """

    def __init__(
        self,
        config: Any,
        *,
        max_clarifications: int = 3,
        continue_without_clarifications: bool = False,
        final_rework_enabled: bool = False,
        final_rework_passes: int = 0,
        template_provider: Optional[Callable[[Any], Dict[str, Any]]] = None,
        backend_path: Optional[str] = None,
    ) -> None:
        if not backend_path:
            raise RuntimeError("Orchestration backend path is required")
        module_name, _, symbol = str(backend_path or "").partition(":")
        if not module_name or not symbol:
            raise RuntimeError("Invalid orchestration backend path")
        module = importlib.import_module(module_name)
        backend_cls = getattr(module, symbol, None)
        if backend_cls is None:
            raise RuntimeError(f"Orchestration backend not found: {backend_path}")
        self._runner = backend_cls(
            config,
            max_clarifications=max_clarifications,
            continue_without_clarifications=continue_without_clarifications,
            final_rework_enabled=final_rework_enabled,
            final_rework_passes=final_rework_passes,
            template_provider=template_provider,
        )

    @property
    def runner(self) -> Any:
        return self._runner

    def set_config(self, config: Any) -> None:
        self._runner._config = config
        self._runner._executor._config = config
        self._runner._dispatcher._config = config

    async def run(self, session: Any, prompt: str, bot_app: Any, context: Any, dest: Dict[str, Any]) -> str:
        return await self._runner.run(session, str(prompt), bot_app, context, dict(dest or {}))

    def clear_session_cache(self, session_id: str) -> None:
        self._runner.clear_session_cache(str(session_id))

    def resolve_question(self, question_id: str, answer: str) -> bool:
        return bool(self._runner.resolve_question(str(question_id), str(answer)))

    def record_message(self, chat_id: int, message_id: int) -> None:
        self._runner.record_message(str(chat_id), int(message_id))

    def get_plugin_ui(self, profile: Any) -> Dict[str, Any]:
        return dict(self._runner.get_plugin_ui(profile) or {})
