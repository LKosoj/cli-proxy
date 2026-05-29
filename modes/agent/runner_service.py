from __future__ import annotations

import logging
from typing import Any, Dict

from app.services.run_artifact_store import RunArtifactHandle, RunArtifactStore
from modes.sdk import SharedOrchestratorRunner

_AGENT_RUN_HANDLE_SESSION_ATTR = "agent_run_artifact_handle"
_AGENT_RUN_RESUME_GUARD_SESSION_ATTR = "agent_run_resume_guard"

logger = logging.getLogger(__name__)


class AgentModeRunnerService:
    """Agent mode-owned orchestrator runtime."""

    capabilities = frozenset({"run_agent", "resolve_question", "plugin_ui", "message_tracking", "session_cache"})

    def __init__(self, config: Any) -> None:
        self._config = config
        self._runtime = SharedOrchestratorRunner(
            config,
            max_clarifications=3,
            continue_without_clarifications=False,
            backend_path="modes.sdk.orchestrator_runner:OrchestratorRunner",
        )

    @property
    def runner(self):
        return self._runtime.runner

    def set_config(self, config: Any) -> None:
        self._config = config
        self._runtime.set_config(config)

    async def run(self, session: Any, prompt: str, bot_app: Any, context: Any, dest: Dict[str, Any]) -> str:
        self._record_execute_start(session=session, prompt=prompt, dest=dest)
        try:
            output = await self._runtime.run(session, str(prompt), bot_app, context, dict(dest or {}))
        except Exception as exc:
            self._record_execute_failure(session=session, error=exc)
            raise
        self._record_execute_success(session=session, output=output)
        return output

    def record_message(self, chat_id: int, message_id: int) -> None:
        self._runtime.record_message(int(chat_id), int(message_id))

    def resolve_question(self, question_id: str, answer: str) -> bool:
        return bool(self._runtime.resolve_question(str(question_id), str(answer)))

    def clear_session_cache(self, session_id: str) -> None:
        self._runtime.clear_session_cache(str(session_id))

    def get_plugin_ui(self, profile: Any) -> Dict[str, Any]:
        return dict(self._runtime.get_plugin_ui(profile) or {})

    def supports_capability(self, capability: str) -> bool:
        return str(capability or "").strip() in self.capabilities

    def _artifact_store(self) -> RunArtifactStore | None:
        if self._config is None:
            return None
        cached = getattr(self, "_cached_artifact_store", None)
        if cached is not None and getattr(cached, "config", None) is self._config:
            return cached
        store = RunArtifactStore(self._config)
        self._cached_artifact_store = store
        return store

    @staticmethod
    def _active_run_handle(session: Any) -> RunArtifactHandle | None:
        handle = getattr(session, _AGENT_RUN_HANDLE_SESSION_ATTR, None)
        return handle if isinstance(handle, RunArtifactHandle) else None

    def _record_execute_start(self, *, session: Any, prompt: str, dest: Dict[str, Any]) -> None:
        handle = self._active_run_handle(session)
        store = self._artifact_store()
        if handle is None or store is None:
            return
        try:
            state = store.load_state(handle)
            mode_context = dict(state.get("mode_context") or {})
            execution_context = dict(mode_context.get("execution_context") or {})
            execution_context.update(
                {
                    "runner_dest_kind": str((dest or {}).get("kind") or "telegram"),
                    "runner_chat_id": (dest or {}).get("chat_id"),
                    "runner_prompt_preview": str(prompt or "")[:1000],
                    "resume_guard": dict(getattr(session, _AGENT_RUN_RESUME_GUARD_SESSION_ATTR, {}) or {}),
                }
            )
            mode_context["execution_context"] = execution_context
            state["phase"] = "execute"
            state["status"] = "running"
            state["mode_context"] = mode_context
            store.save_state(handle, state)
            store.append_checkpoint(
                handle,
                {
                    "phase": "execute",
                    "unit_id": "agent:orchestrator",
                    "status": "started",
                    "message": "agent orchestrator runner started",
                },
            )
        except Exception:
            logger.exception("agent runner: failed to record execute start session=%s", getattr(session, "id", ""))

    def _record_execute_success(self, *, session: Any, output: Any) -> None:
        handle = self._active_run_handle(session)
        store = self._artifact_store()
        if handle is None or store is None:
            return
        try:
            store.append_checkpoint(
                handle,
                {
                    "phase": "execute",
                    "unit_id": "agent:orchestrator",
                    "status": "ok",
                    "message": str(output or "")[:500],
                },
            )
        except Exception:
            logger.exception("agent runner: failed to record execute success session=%s", getattr(session, "id", ""))

    def _record_execute_failure(self, *, session: Any, error: Exception) -> None:
        handle = self._active_run_handle(session)
        store = self._artifact_store()
        if handle is None or store is None:
            return
        try:
            state = store.load_state(handle)
            mode_context = dict(state.get("mode_context") or {})
            mode_context["runtime_error"] = str(error or "")
            state["phase"] = "execute"
            state["status"] = "failed"
            state["mode_context"] = mode_context
            store.save_state(handle, state)
            store.append_checkpoint(
                handle,
                {
                    "phase": "execute",
                    "unit_id": "agent:orchestrator",
                    "status": "error",
                    "message": str(error or "")[:500],
                },
            )
            # mark_finished is handled by AgentMode.run_pipeline() which owns the run lifecycle.
        except Exception:
            logger.exception("agent runner: failed to record execute failure session=%s", getattr(session, "id", ""))
