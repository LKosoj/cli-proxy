from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Mapping, Optional

from app.services.run_artifact_store import RunArtifactHandle, RunArtifactStore
from app.services.run_boundary_validation_service import (
    RunBoundaryIssue,
    RunBoundaryReport,
    RunBoundaryValidationService,
)
from app.services.run_observability_service import RunObservabilityService

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ModeRunLifecycleStartResult:
    handle: RunArtifactHandle
    state: dict[str, Any]
    boundary_report: RunBoundaryReport | None = None
    phase_event: dict[str, Any] | None = None


@dataclass(frozen=True)
class ModeRunLifecyclePhaseResult:
    handle: RunArtifactHandle
    state: dict[str, Any]
    boundary_report: RunBoundaryReport | None = None


@dataclass(frozen=True)
class ModeRunLifecycleFinishResult:
    handle: RunArtifactHandle
    state: dict[str, Any]
    boundary_report: RunBoundaryReport | None = None
    phase_event: dict[str, Any] | None = None


@dataclass(frozen=True)
class ModeRunLifecycleEventResult:
    handle: RunArtifactHandle
    event: dict[str, Any]


class ModeRunLifecycleService:
    """Facade for mode run artifact lifecycle operations."""

    def __init__(
        self,
        *,
        artifact_store: RunArtifactStore,
        observability: RunObservabilityService | None = None,
        boundary_validator: RunBoundaryValidationService | None = None,
    ) -> None:
        self.artifact_store = artifact_store
        self.observability = observability
        self.boundary_validator = boundary_validator

    def start(
        self,
        *,
        session: Any,
        mode_id: str,
        run_id: str | None = None,
        phase: str | None = None,
        source_prompt_hash: str | None = None,
        mode_context: Mapping[str, Any] | None = None,
        bind_observability: bool = True,
        record_phase_start: bool = True,
        validate_boundary: bool = True,
    ) -> ModeRunLifecycleStartResult:
        handle = self.artifact_store.start_run(
            session=session,
            mode_id=mode_id,
            run_id=run_id,
            phase=phase,
            source_prompt_hash=source_prompt_hash,
            mode_context=dict(mode_context or {}),
        )
        if bind_observability and self.observability is not None:
            self.observability.bind_session(session, handle)

        phase_event = None
        if record_phase_start and phase and self.observability is not None:
            phase_event = self.observability.record_phase_start(handle, phase=phase)

        boundary_report = self._validate(handle, phase=phase) if validate_boundary else None
        return ModeRunLifecycleStartResult(
            handle=handle,
            state=self.artifact_store.load_state(handle),
            boundary_report=boundary_report,
            phase_event=phase_event,
        )

    def save_phase(
        self,
        handle: RunArtifactHandle,
        *,
        phase: str,
        state: Mapping[str, Any] | None = None,
        mode_context: Mapping[str, Any] | None = None,
        validate_boundary: bool = True,
    ) -> ModeRunLifecyclePhaseResult:
        payload = dict(state or {})
        payload["phase"] = str(phase or "")
        if mode_context is not None:
            existing = self.artifact_store.load_state(handle)
            existing_context = existing.get("mode_context")
            merged_context = dict(existing_context if isinstance(existing_context, dict) else {})
            merged_context.update(dict(mode_context or {}))
            payload["mode_context"] = merged_context

        saved_state = self.artifact_store.save_state(handle, payload)
        boundary_report = self._validate(handle, phase=phase) if validate_boundary else None
        return ModeRunLifecyclePhaseResult(
            handle=handle,
            state=saved_state,
            boundary_report=boundary_report,
        )

    def mark_finished(
        self,
        handle: RunArtifactHandle,
        *,
        status: str = "completed",
        phase: str | None = None,
        session: Any | None = None,
        record_phase_end: bool = True,
        validate_boundary: bool = True,
        duration_sec: Any = None,
        tool_calls: Any = None,
        input_tokens: Any = None,
        output_tokens: Any = None,
        cost_usd: Any = None,
        corr_id: Any = None,
        message: Any = None,
    ) -> ModeRunLifecycleFinishResult:
        state = self.artifact_store.mark_finished(handle, status=status, phase=phase)
        resolved_phase = phase or self._state_phase(state)

        phase_event = None
        if record_phase_end and resolved_phase and self.observability is not None:
            phase_event = self.observability.record_phase_end(
                handle,
                phase=resolved_phase,
                status=status,
                duration_sec=duration_sec,
                tool_calls=tool_calls,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cost_usd=cost_usd,
                corr_id=corr_id,
                message=message,
            )
        if session is not None and self.observability is not None:
            self.observability.unbind_session(session, run=handle)

        boundary_report = self._validate(handle, phase=resolved_phase) if validate_boundary else None
        return ModeRunLifecycleFinishResult(
            handle=handle,
            state=state,
            boundary_report=boundary_report,
            phase_event=phase_event,
        )

    def record_event(
        self,
        handle: RunArtifactHandle,
        *,
        event_type: str,
        payload: Mapping[str, Any] | None = None,
        use_observability: bool = True,
    ) -> ModeRunLifecycleEventResult:
        event_payload = dict(payload or {})
        event_payload["event_type"] = str(event_type or event_payload.get("event_type") or "event")
        if (
            use_observability
            and event_payload["event_type"] == "runtime_progress"
            and self.observability is not None
        ):
            event = self.observability.record_runtime_progress(handle, event=event_payload)
        else:
            event = self.artifact_store.append_event(handle, event_payload)
        return ModeRunLifecycleEventResult(handle=handle, event=event)

    def _validate(self, handle: RunArtifactHandle, *, phase: str | None) -> RunBoundaryReport | None:
        if self.boundary_validator is None or not self.boundary_validator.is_enabled():
            return None
        resolved_phase = phase
        try:
            resolved_phase = resolved_phase or self._state_phase(self.artifact_store.load_state(handle))
            return self.boundary_validator.validate(
                handle,
                mode_id=handle.mode_id,
                phase=resolved_phase,
            )
        except Exception as exc:
            logger.exception(
                "mode run boundary validation failed fallback_category=best_effort mode=%s phase=%s run_id=%s",
                handle.mode_id,
                resolved_phase,
                handle.run_id,
            )
            return RunBoundaryReport(
                mode_id=handle.mode_id,
                phase=resolved_phase or "",
                status="error",
                issues=[
                    RunBoundaryIssue(
                        code="boundary_validation_exception",
                        message="Boundary validation failed while validating persisted run state.",
                        details={
                            "category": "best_effort",
                            "error_type": type(exc).__name__,
                        },
                    )
                ],
                next_allowed_phases=[],
                contract=None,
            )

    @staticmethod
    def _state_phase(state: Mapping[str, Any]) -> Optional[str]:
        phase = state.get("phase")
        if phase is None:
            return None
        token = str(phase or "").strip()
        return token or None
