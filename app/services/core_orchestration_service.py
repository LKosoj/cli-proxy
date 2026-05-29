"""CoreOrchestrationService — thin execution wrapper around mode.run_pipeline().

Emits trace events (run_started / run_finished / run_failed) and delegates
handoff to AdvancedOrchestratorService.  Does NOT own session lifecycle
(lock, busy, persistence, queue drain, send_output).
"""

from __future__ import annotations

import logging
from typing import Any

from app.services.advanced_orchestrator_service import AdvancedOrchestratorService
from app.services.runtime_progress_service import emit_runtime_progress
from app.services.trace_contract import build_trace_event

_LOG = logging.getLogger(__name__)


class CoreOrchestrationService:

    def __init__(self, advanced_orchestrator: AdvancedOrchestratorService) -> None:
        self._advanced_orchestrator = advanced_orchestrator

    async def execute_mode_run(
        self,
        *,
        session: Any,
        mode: Any,
        mode_id: str,
        user_text: str,
        bot_app: Any,
        context: Any,
        dest: dict,
    ) -> str:
        session_id = str(getattr(session, "id", "") or "")
        trace_started = build_trace_event(
            "run_started",
            mode_id=mode_id,
            session_id=session_id,
            status="running",
        )
        _LOG.info("trace: %s", trace_started)
        emit_runtime_progress(session, trace_started)
        try:
            output = await mode.run_pipeline(
                session=session,
                user_text=user_text,
                bot_app=bot_app,
                context=context,
                dest=dest,
            )
            trace_finished = build_trace_event(
                "run_finished",
                mode_id=mode_id,
                session_id=session_id,
                status="ok",
            )
            _LOG.info("trace: %s", trace_finished)
            emit_runtime_progress(session, trace_finished)
            return str(output or "")
        except Exception as exc:
            trace_failed = build_trace_event(
                "run_failed",
                mode_id=mode_id,
                session_id=session_id,
                status="error",
                error=str(exc)[:500],
            )
            _LOG.info("trace: %s", trace_failed)
            emit_runtime_progress(session, trace_failed)
            raise

    def build_handoff_input(self, *, session: Any, original_user_text: str) -> str:
        return self._advanced_orchestrator.build_handoff_input(
            session=session,
            original_user_text=original_user_text,
        )
