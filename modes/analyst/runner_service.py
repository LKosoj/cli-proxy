from __future__ import annotations

import logging
from typing import Any, Dict

from app.services.run_artifact_store import RunArtifactHandle, RunArtifactStore
from modes.sdk import SharedOrchestratorRunner
from session import session_scoped_key
from .state_store import AnalystStateStore, build_context_key, resolve_analyst_state_root
from .template_service import get_analyst_templates_cached, get_effective_template, get_template_for_session

_ANALYST_BLOCKING_CLARIFICATION_RUNTIME_ATTR = "analyst_blocking_clarification_runtime"
_ANALYST_RUN_HANDLE_SESSION_ATTR = "analyst_run_artifact_handle"
_ANALYST_RUN_RESUME_GUARD_SESSION_ATTR = "analyst_run_resume_guard"
_ANALYST_SOURCE_USER_TEXT_RUNTIME_ATTR = "analyst_source_user_text_runtime"

logger = logging.getLogger(__name__)


class AnalystModeRunnerService:
    """Analyst mode-owned orchestrator runtime."""

    capabilities = frozenset({"run_analyst", "template_provider", "session_cache"})

    def __init__(self, config: Any) -> None:
        self._config = config
        self._runtime = SharedOrchestratorRunner(
            config,
            max_clarifications=0,
            continue_without_clarifications=True,
            final_rework_enabled=True,
            template_provider=self._get_effective_template_for_session,
            backend_path="modes.sdk.orchestrator_runner:OrchestratorRunner",
        )

    @property
    def runner(self):
        return self._runtime.runner

    def set_config(self, config: Any) -> None:
        self._config = config
        self._runtime.set_config(config)

    async def run(self, session: Any, prompt: str, bot_app: Any, context: Any, dest: Dict[str, Any]) -> str:
        setattr(
            session,
            _ANALYST_BLOCKING_CLARIFICATION_RUNTIME_ATTR,
            self._requires_blocking_clarification(session),
        )
        self._bind_runtime_source_user_text(session)
        self._record_execute_start(session=session, prompt=prompt, dest=dest)
        try:
            output = await self._runtime.run(session, str(prompt), bot_app, context, dict(dest or {}))
        except Exception as exc:
            self._record_execute_failure(session=session, error=exc)
            raise
        finally:
            self._clear_runtime_source_user_text(session)
        self._record_execute_success(session=session, output=output)
        return output

    @staticmethod
    def _requires_blocking_clarification(session: Any) -> bool:
        raw_flags = getattr(session, "analyst_intent_flags", None)
        if not isinstance(raw_flags, dict):
            return False
        return bool(raw_flags.get("clarification_is_blocking"))

    def _get_effective_template_for_session(self, session: Any) -> Dict[str, Any]:
        registry = get_analyst_templates_cached()
        chat_id = getattr(session, "chat_id", None)
        session_id = str(getattr(session, "id", "") or "").strip()
        store = AnalystStateStore(resolve_analyst_state_root(session))
        analyst_ctx = store.load(session_scoped_key(session) or build_context_key(chat_id, session_id))
        template = get_effective_template(
            registry,
            runtime_template_id=str(getattr(analyst_ctx, "runtime_template_id", "") or "").strip(),
            intent_template_id=str(getattr(analyst_ctx, "effective_template_id", "") or "").strip(),
            session=session,
        )
        return dict(template or {})

    def get_template_for_session(self, session: Any) -> Dict[str, Any]:
        return dict(get_template_for_session(session) or {})

    def get_templates_cached(self, path: str | None = None) -> Dict[str, Dict[str, Any]]:
        return dict(get_analyst_templates_cached(path) or {})

    def clear_session_cache(self, session_id: str) -> None:
        self._runtime.clear_session_cache(str(session_id))

    def supports_capability(self, capability: str) -> bool:
        return str(capability or "").strip() in self.capabilities

    def _artifact_store(self) -> RunArtifactStore | None:
        if self._config is None:
            return None
        return RunArtifactStore(self._config)

    @staticmethod
    def _active_run_handle(session: Any) -> RunArtifactHandle | None:
        handle = getattr(session, _ANALYST_RUN_HANDLE_SESSION_ATTR, None)
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
                    "blocking_clarification_runtime": bool(
                        getattr(session, _ANALYST_BLOCKING_CLARIFICATION_RUNTIME_ATTR, False)
                    ),
                    "resume_guard": dict(getattr(session, _ANALYST_RUN_RESUME_GUARD_SESSION_ATTR, {}) or {}),
                    "runner_prompt_preview": str(prompt or "")[:1000],
                    "runner_source_user_text": str(
                        getattr(session, _ANALYST_SOURCE_USER_TEXT_RUNTIME_ATTR, "") or ""
                    ),
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
                    "unit_id": "analyst:execute",
                    "status": "started",
                    "message": "analyst runner started",
                },
            )
        except Exception:
            logger.exception("analyst runner: failed to record execute start session=%s", getattr(session, "id", ""))

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
                    "unit_id": "analyst:execute",
                    "status": "ok",
                    "message": str(output or "")[:500],
                },
            )
        except Exception:
            logger.exception("analyst runner: failed to record execute success session=%s", getattr(session, "id", ""))

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
                    "unit_id": "analyst:execute",
                    "status": "error",
                    "message": str(error or "")[:500],
                },
            )
            # mark_finished is handled by AnalystMode.run_pipeline() which owns the run lifecycle.
        except Exception:
            logger.exception("analyst runner: failed to record execute failure session=%s", getattr(session, "id", ""))

    def _bind_runtime_source_user_text(self, session: Any) -> None:
        source_user_text = ""
        handle = self._active_run_handle(session)
        store = self._artifact_store()
        if handle is not None and store is not None:
            try:
                state = store.load_state(handle)
                mode_context = dict(state.get("mode_context") or {})
                execution_context = dict(mode_context.get("execution_context") or {})
                input_bundle = dict(mode_context.get("input_bundle") or {})
                source_user_text = str(
                    input_bundle.get("original_user_text")
                    or mode_context.get("source_user_text")
                    or execution_context.get("source_user_text")
                    or ""
                ).strip()
            except Exception:
                logger.exception(
                    "analyst runner: failed to bind raw source_user_text for session=%s",
                    getattr(session, "id", ""),
                )
        setattr(session, _ANALYST_SOURCE_USER_TEXT_RUNTIME_ATTR, source_user_text)

    @staticmethod
    def _clear_runtime_source_user_text(session: Any) -> None:
        setattr(session, _ANALYST_SOURCE_USER_TEXT_RUNTIME_ATTR, "")
