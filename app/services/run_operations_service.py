from __future__ import annotations

import copy
import logging
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, Literal, Optional, Sequence

from modes.sdk.json_store import read_json_locked_if_exists

from app.services.run_artifact_store import RunArtifactHandle, RunArtifactStore
from app.services.run_doctor_service import RunDoctorReport, RunDoctorService
from app.services.run_observability_service import RunObservabilityService


logger = logging.getLogger(__name__)
RunOperationName = Literal["doctor", "recover", "resume", "apply_recommendation"]
RunOperationStatus = Literal["ok", "blocked", "not_found", "disabled", "error"]
WRITE_RUN_OPERATIONS = frozenset({"recover", "resume", "apply_recommendation"})
_RESUME_ALLOWED_ACTIONS = frozenset({"no_action", "resume_same_phase"})
_APPLY_RECOMMENDATION_ALLOWED_ACTIONS = frozenset({"rerun_same_operation", "run_validate", "run_repair"})
_RECOVER_EXECUTION_ACTIONS = frozenset({"rollback_to_checkpoint", "restart_from_phase", "replay_finalize"})
_RECOVER_ALLOWED_ACTIONS = frozenset({"mark_failed"}) | _RECOVER_EXECUTION_ACTIONS

RecommendedActionExecutor = Callable[..., Awaitable[Dict[str, Any]]]


def _clean_text(value: Any, *, max_len: int = 256) -> str:
    text = str(value or "").replace("\r", " ").replace("\n", " ").strip()
    if len(text) <= max_len:
        return text
    return text[: max_len - 3] + "..."


def _resolve_active_mode(session: Any) -> str:
    modes = getattr(session, "modes", None)
    scoped = str(getattr(modes, "active_mode", "") or "").strip()
    if scoped:
        return scoped
    return str(getattr(session, "active_mode", "") or "").strip()


def _session_log_id(session: Any) -> str:
    scope = getattr(session, "conversation_scope", None)
    return _clean_text(
        getattr(scope, "session_uid", None)
        or getattr(session, "id", None)
        or getattr(session, "chat_id", None),
        max_len=128,
    )


def _blocked_signals(session: Any, *, operation: str = "") -> tuple[str, ...]:
    signals: list[str] = []
    if bool(getattr(session, "busy", False)):
        signals.append("busy")
    run_lock = getattr(session, "run_lock", None)
    if run_lock is not None and hasattr(run_lock, "locked"):
        try:
            if bool(run_lock.locked()):
                signals.append("run_lock")
        except Exception:
            logger.warning(
                "legacy fallback used: run operation busy guard failed signal=run_lock operation=%s session_id=%s",
                _clean_text(operation, max_len=64),
                _session_log_id(session),
                exc_info=True,
            )
    tick_fn = getattr(session, "is_active_by_tick", None)
    if callable(tick_fn):
        try:
            if bool(tick_fn()):
                signals.append("tick")
        except Exception:
            logger.warning(
                "legacy fallback used: run operation busy guard failed signal=tick operation=%s session_id=%s",
                _clean_text(operation, max_len=64),
                _session_log_id(session),
                exc_info=True,
            )
    return tuple(signals)


def blocked_run_operation_signals(session: Any, operation: str) -> tuple[str, ...]:
    if str(operation or "").strip().lower() not in WRITE_RUN_OPERATIONS:
        return ()
    return _blocked_signals(session, operation=operation)


def blocked_run_operation_message(blocked_by: Sequence[str]) -> str:
    signals = [str(item or "").strip() for item in blocked_by if str(item or "").strip()]
    return "Сессия занята. Операция недоступна, пока не освободятся сигналы: " + ", ".join(signals) + "."


def read_json_without_side_effects(path: str, *, default: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    payload = read_json_locked_if_exists(path, default=default or {})
    return payload if isinstance(payload, dict) else dict(default or {})


@dataclass(frozen=True)
class RunOperationResult:
    operation: RunOperationName
    status: RunOperationStatus
    mode_id: str
    phase: str
    message: str
    run_id: Optional[str] = None
    report: Optional[Dict[str, Any]] = None
    recommended_action: Optional[str] = None
    blocked_by: tuple[str, ...] = ()


class RunOperationsService:
    def __init__(
        self,
        *,
        enabled: bool,
        artifact_store: RunArtifactStore,
        doctor_service: RunDoctorService,
        observability_service: Optional[RunObservabilityService] = None,
        recommended_action_executor: Optional[RecommendedActionExecutor] = None,
        now_fn=None,
        logger_: Optional[logging.Logger] = None,
    ) -> None:
        self.enabled = bool(enabled)
        self.artifact_store = artifact_store
        self.doctor_service = doctor_service
        self.observability_service = observability_service
        self.recommended_action_executor = recommended_action_executor
        self._now = now_fn or time.time
        self._log = logger_ or logging.getLogger(__name__)

    def is_enabled(self) -> bool:
        return bool(self.enabled)

    async def doctor_run(
        self,
        *,
        session: Any,
        mode_id: Optional[str] = None,
        run_id: Optional[str] = None,
        context: Any = None,
        dest: Optional[Dict[str, Any]] = None,
    ) -> RunOperationResult:
        return await self._execute("doctor", session=session, mode_id=mode_id, run_id=run_id, context=context, dest=dest)

    async def recover_run(
        self,
        *,
        session: Any,
        mode_id: Optional[str] = None,
        run_id: Optional[str] = None,
        context: Any = None,
        dest: Optional[Dict[str, Any]] = None,
    ) -> RunOperationResult:
        return await self._execute("recover", session=session, mode_id=mode_id, run_id=run_id, context=context, dest=dest)

    async def resume_run(
        self,
        *,
        session: Any,
        mode_id: Optional[str] = None,
        run_id: Optional[str] = None,
        context: Any = None,
        dest: Optional[Dict[str, Any]] = None,
    ) -> RunOperationResult:
        return await self._execute("resume", session=session, mode_id=mode_id, run_id=run_id, context=context, dest=dest)

    async def apply_recommendation_run(
        self,
        *,
        session: Any,
        mode_id: Optional[str] = None,
        run_id: Optional[str] = None,
        context: Any = None,
        dest: Optional[Dict[str, Any]] = None,
    ) -> RunOperationResult:
        return await self._execute(
            "apply_recommendation",
            session=session,
            mode_id=mode_id,
            run_id=run_id,
            context=context,
            dest=dest,
        )

    async def _execute(
        self,
        operation: RunOperationName,
        *,
        session: Any,
        mode_id: Optional[str] = None,
        run_id: Optional[str] = None,
        context: Any = None,
        dest: Optional[Dict[str, Any]] = None,
    ) -> RunOperationResult:
        resolved_mode_id = _clean_text(mode_id or _resolve_active_mode(session), max_len=64)
        resolved_run_id = _clean_text(run_id, max_len=128)
        if not self.is_enabled():
            return RunOperationResult(
                operation=operation,
                status="disabled",
                mode_id=resolved_mode_id,
                phase="",
                message="Run-операции отключены в конфигурации.",
            )
        if not resolved_mode_id:
            return RunOperationResult(
                operation=operation,
                status="not_found",
                mode_id="",
                phase="",
                message="Режим для операции не определён.",
            )
        blocked_by = blocked_run_operation_signals(session, operation)
        if blocked_by:
            return RunOperationResult(
                operation=operation,
                status="blocked",
                mode_id=resolved_mode_id,
                phase="",
                message=blocked_run_operation_message(blocked_by),
                blocked_by=blocked_by,
            )

        run = self._resolve_run(session=session, mode_id=resolved_mode_id, run_id=resolved_run_id)
        if run is None:
            if resolved_run_id:
                message = f"Run `{resolved_run_id}` для режима `{resolved_mode_id}` не найден."
            else:
                message = f"Для режима `{resolved_mode_id}` нет run artifacts."
            return RunOperationResult(
                operation=operation,
                status="not_found",
                mode_id=resolved_mode_id,
                phase="",
                message=message,
            )

        state = self.artifact_store.load_state(run)
        phase = _clean_text(state.get("phase"), max_len=64) or "unknown"
        try:
            report = self.doctor_service.diagnose(run, mode_id=resolved_mode_id, phase=phase)
        except Exception:
            self._log.exception(
                "run operation failed during doctor pass operation=%s mode=%s run_id=%s",
                operation,
                resolved_mode_id,
                run.run_id,
            )
            return RunOperationResult(
                operation=operation,
                status="error",
                mode_id=resolved_mode_id,
                phase=phase,
                run_id=run.run_id,
                message="Не удалось выполнить doctor-pass для операции.",
            )

        if operation == "doctor":
            self._append_operation_event(run, operation=operation, report=report, status="completed")
            return RunOperationResult(
                operation=operation,
                status="ok",
                mode_id=resolved_mode_id,
                phase=report.phase,
                run_id=run.run_id,
                report=report.to_dict(),
                recommended_action=self._public_recommended_action(report),
                message=self._doctor_message(run, report),
            )

        if operation == "apply_recommendation":
            return await self._apply_recommendation_operation(
                session=session,
                run=run,
                state=state,
                report=report,
                mode_id=resolved_mode_id,
                context=context,
                dest=dest,
            )

        blocked_message = self._blocked_recovery_message(
            operation=operation,
            mode_id=resolved_mode_id,
            report=report,
        )
        if blocked_message:
            self._persist_requested_operation(run, operation=operation, report=report, status="blocked")
            self._record_recovery_attempt(run, phase=report.phase, state=state, action=operation, status="blocked")
            self._append_operation_event(run, operation=operation, report=report, status="blocked")
            return RunOperationResult(
                operation=operation,
                status="blocked",
                mode_id=resolved_mode_id,
                phase=report.phase,
                run_id=run.run_id,
                report=report.to_dict(),
                recommended_action=self._public_recommended_action(report),
                message=blocked_message,
            )

        if operation == "recover" and str(report.recommended_action or "").strip() in _RECOVER_EXECUTION_ACTIONS:
            return await self._execute_recover_action(
                session=session,
                run=run,
                state=state,
                report=report,
                mode_id=resolved_mode_id,
                context=context,
                dest=dest,
            )

        if operation == "recover" and report.recommended_action == "mark_failed":
            try:
                self.artifact_store.mark_finished(run, status="failed", phase=report.phase)
            except Exception as exc:
                self._log.exception(
                    "run recover failed to mark run as failed mode=%s run_id=%s",
                    resolved_mode_id,
                    run.run_id,
                )
                extra = {"error": _clean_text(exc, max_len=256)}
                self._persist_requested_operation(
                    run,
                    operation=operation,
                    report=report,
                    status="error",
                    extra=extra,
                )
                self._record_recovery_attempt(
                    run,
                    phase=report.phase,
                    state=state,
                    action=operation,
                    status="error",
                )
                self._append_operation_event(
                    run,
                    operation=operation,
                    report=report,
                    status="error",
                    extra=extra,
                )
                return RunOperationResult(
                    operation=operation,
                    status="error",
                    mode_id=resolved_mode_id,
                    phase=report.phase,
                    run_id=run.run_id,
                    report=report.to_dict(),
                    recommended_action=self._public_recommended_action(report),
                    message=f"Не удалось перевести run {run.run_id} в failed state.",
                )

        self._persist_requested_operation(run, operation=operation, report=report, status="prepared")
        self._record_recovery_attempt(run, phase=report.phase, state=state, action=operation, status="prepared")
        self._append_operation_event(run, operation=operation, report=report, status="prepared")
        return RunOperationResult(
            operation=operation,
            status="ok",
            mode_id=resolved_mode_id,
            phase=report.phase,
            run_id=run.run_id,
            report=report.to_dict(),
            recommended_action=self._public_recommended_action(report),
            message=self._recovery_message(operation=operation, run=run, report=report),
        )

    def _resolve_run(
        self,
        *,
        session: Any,
        mode_id: str,
        run_id: str,
    ) -> Optional[RunArtifactHandle]:
        if run_id:
            return self.artifact_store.get_run(session=session, mode_id=mode_id, run_id=run_id)
        return self.artifact_store.latest_run(session=session, mode_id=mode_id)

    def _record_recovery_attempt(
        self,
        run: RunArtifactHandle,
        *,
        phase: str,
        state: Dict[str, Any],
        action: str,
        status: str,
    ) -> None:
        observability = self.observability_service
        if observability is None or not observability.is_enabled():
            return
        unit_id = _clean_text(state.get("current_unit_id") or state.get("current_step_id"), max_len=128) or None
        try:
            observability.record_recovery_attempt(
                run,
                phase=phase,
                unit_id=unit_id,
                action=action,
                status=status,
                ts=float(self._now()),
            )
        except Exception:
            self._log.exception("run operation failed to record recovery attempt run_id=%s", run.run_id)

    def _append_operation_event(
        self,
        run: RunArtifactHandle,
        *,
        operation: RunOperationName,
        report: RunDoctorReport,
        status: str,
        extra: Optional[Dict[str, Any]] = None,
    ) -> None:
        try:
            payload = {
                "event_type": "run_operation",
                "operation": operation,
                "status": _clean_text(status, max_len=32),
                "phase": report.phase,
                "recommended_action": report.recommended_action,
                "doctor_status": report.status,
                "issue_codes": [item.code for item in report.issues],
            }
            if isinstance(extra, dict):
                payload.update(dict(extra))
            self.artifact_store.append_event(
                run,
                payload,
            )
        except Exception:
            self._log.exception("run operation failed to append event run_id=%s", run.run_id)

    def _persist_requested_operation(
        self,
        run: RunArtifactHandle,
        *,
        operation: RunOperationName,
        report: RunDoctorReport,
        status: str,
        extra: Optional[Dict[str, Any]] = None,
    ) -> None:
        requested_at = float(self._now())
        current = read_json_locked_if_exists(run.recovery_path, default={})
        payload = dict(current if isinstance(current, dict) else {})
        items = list(payload.get("requested_operations") or [])
        item = {
            "operation": operation,
            "status": _clean_text(status, max_len=32),
            "requested_at": requested_at,
            "mode_id": report.mode_id,
            "phase": report.phase,
            "recommended_action": report.recommended_action,
            "issue_codes": [issue.code for issue in report.issues],
        }
        if isinstance(extra, dict):
            item.update(dict(extra))
        localized_recovery_nodes = self._localized_recovery_nodes(run=run, report=report)
        if localized_recovery_nodes and not isinstance(item.get("recovery_nodes"), dict):
            item["recovery_nodes"] = localized_recovery_nodes
        items.append(item)
        payload["last_requested_operation"] = dict(item)
        payload["requested_operations"] = items
        self.artifact_store.save_recovery(run, payload)

    def _localized_recovery_nodes(
        self,
        *,
        run: RunArtifactHandle,
        report: RunDoctorReport,
    ) -> Optional[Dict[str, Any]]:
        if _clean_text(report.mode_id, max_len=64) != "manager":
            return None
        if _clean_text(report.recommended_action, max_len=96) != "replay_finalize":
            return None
        plan_payload = self.artifact_store.load_plan(run)
        recovery_nodes = plan_payload.get("recovery_nodes") if isinstance(plan_payload, dict) else None
        if not isinstance(recovery_nodes, dict):
            return None
        replay_finalize = recovery_nodes.get("replay_finalize")
        if not isinstance(replay_finalize, dict):
            return None
        localized = copy.deepcopy(replay_finalize)
        localized["source_run_id"] = _clean_text(localized.get("source_run_id") or run.run_id, max_len=128) or run.run_id
        return {"replay_finalize": localized}

    @staticmethod
    def _doctor_message(run: RunArtifactHandle, report: RunDoctorReport) -> str:
        issue_codes = ", ".join(issue.code for issue in report.issues) or "нет"
        if report.status == "ok":
            if report.recommended_action == "no_action":
                return (
                    f"Doctor: run {run.run_id} выглядит консистентным. "
                    f"Фаза: {report.phase}. Дополнительное восстановление не требуется."
                )
            return (
                f"Doctor: run {run.run_id} выглядит консистентным. "
                f"Фаза: {report.phase}. Рекомендация: {report.recommended_action}."
            )
        return (
            f"Doctor: run {run.run_id} требует восстановления. "
            f"Фаза: {report.phase}. Рекомендация: {report.recommended_action}. "
            f"Проблемы: {issue_codes}."
        )

    @staticmethod
    def _recovery_message(
        *,
        operation: RunOperationName,
        run: RunArtifactHandle,
        report: RunDoctorReport,
    ) -> str:
        if operation == "resume" and report.recommended_action == "no_action":
            return (
                f"Resume: run {run.run_id} готов к продолжению без дополнительного recovery workflow. "
                f"Фаза: {report.phase}."
            )
        verb = "Recovery" if operation == "recover" else "Resume"
        suffix = (
            " Автоматический replay не выполнялся."
            if report.recommended_action != "mark_failed"
            else " Run переведён в failed state."
        )
        return (
            f"{verb}: для run {run.run_id} подготовлен безопасный workflow. "
            f"Фаза: {report.phase}. Рекомендация: {report.recommended_action}.{suffix}"
        )

    @staticmethod
    def _action_label(action: str) -> str:
        token = str(action or "").strip()
        if token == "rerun_same_operation":
            return "Rerun"
        if token == "run_validate":
            return "Validate"
        if token == "run_repair":
            return "Repair"
        return token or "Action"

    @staticmethod
    def _public_recommended_action(report: RunDoctorReport) -> Optional[str]:
        action = str(report.recommended_action or "").strip()
        if action == "no_action":
            return None
        return action or None

    async def _apply_recommendation_operation(
        self,
        *,
        session: Any,
        run: RunArtifactHandle,
        state: Dict[str, Any],
        report: RunDoctorReport,
        mode_id: str,
        context: Any,
        dest: Optional[Dict[str, Any]],
    ) -> RunOperationResult:
        blocked_message = self._blocked_apply_recommendation_message(mode_id=mode_id, report=report)
        if blocked_message:
            self._persist_requested_operation(
                run,
                operation="apply_recommendation",
                report=report,
                status="blocked",
            )
            self._record_recovery_attempt(
                run,
                phase=report.phase,
                state=state,
                action="apply_recommendation",
                status="blocked",
            )
            self._append_operation_event(
                run,
                operation="apply_recommendation",
                report=report,
                status="blocked",
            )
            return RunOperationResult(
                operation="apply_recommendation",
                status="blocked",
                mode_id=mode_id,
                phase=report.phase,
                run_id=run.run_id,
                report=report.to_dict(),
                recommended_action=self._public_recommended_action(report),
                message=blocked_message,
            )

        recommended_action = str(report.recommended_action or "").strip()
        mapper_operation = self._codebase_mapper_operation_from_report(report=report, state=state)
        executor = self.recommended_action_executor
        if not mapper_operation or executor is None:
            message = "Run-операция для doctor recommendation недоступна."
            self._persist_requested_operation(
                run,
                operation="apply_recommendation",
                report=report,
                status="blocked",
                extra={"executed_operation": mapper_operation},
            )
            self._record_recovery_attempt(
                run,
                phase=report.phase,
                state=state,
                action="apply_recommendation",
                status="blocked",
            )
            self._append_operation_event(
                run,
                operation="apply_recommendation",
                report=report,
                status="blocked",
                extra={"executed_operation": mapper_operation},
            )
            return RunOperationResult(
                operation="apply_recommendation",
                status="blocked",
                mode_id=mode_id,
                phase=report.phase,
                run_id=run.run_id,
                report=report.to_dict(),
                recommended_action=self._public_recommended_action(report),
                message=message,
            )

        try:
            execution_payload = await executor(
                session=session,
                mode_id=mode_id,
                operation=mapper_operation,
                run=run,
                state=state,
                report=report,
                context=context,
                dest=dict(dest or {}),
            )
        except Exception:
            self._log.exception(
                "run operation failed to execute recommended action mode=%s run_id=%s action=%s operation=%s",
                mode_id,
                run.run_id,
                recommended_action,
                mapper_operation,
            )
            self._persist_requested_operation(
                run,
                operation="apply_recommendation",
                report=report,
                status="error",
                extra={"executed_operation": mapper_operation},
            )
            self._record_recovery_attempt(
                run,
                phase=report.phase,
                state=state,
                action="apply_recommendation",
                status="error",
            )
            self._append_operation_event(
                run,
                operation="apply_recommendation",
                report=report,
                status="error",
                extra={"executed_operation": mapper_operation},
            )
            return RunOperationResult(
                operation="apply_recommendation",
                status="error",
                mode_id=mode_id,
                phase=report.phase,
                run_id=run.run_id,
                report=report.to_dict(),
                recommended_action=self._public_recommended_action(report),
                message="Не удалось выполнить doctor recommendation.",
            )

        execution_dict = dict(execution_payload or {})
        execution_status = _clean_text(execution_dict.get("status"), max_len=32) or "ok"
        if execution_status == "ok":
            result_status: RunOperationStatus = "ok"
        elif execution_status == "blocked":
            result_status = "blocked"
        else:
            result_status = "error"
        message = _clean_text(
            execution_dict.get("message")
            or self._recommended_action_message(
                run=run,
                action=recommended_action,
                executed_operation=mapper_operation,
            ),
            max_len=512,
        )
        self._persist_requested_operation(
            run,
            operation="apply_recommendation",
            report=report,
            status="executed" if result_status == "ok" else execution_status,
            extra={"executed_operation": mapper_operation},
        )
        self._record_recovery_attempt(
            run,
            phase=report.phase,
            state=state,
            action="apply_recommendation",
            status="executed" if result_status == "ok" else execution_status,
        )
        self._append_operation_event(
            run,
            operation="apply_recommendation",
            report=report,
            status="executed" if result_status == "ok" else execution_status,
            extra={"executed_operation": mapper_operation},
        )
        return RunOperationResult(
            operation="apply_recommendation",
            status=result_status,
            mode_id=mode_id,
            phase=report.phase,
            run_id=run.run_id,
            report=report.to_dict(),
            recommended_action=self._public_recommended_action(report),
            message=message,
        )

    async def _execute_recover_action(
        self,
        *,
        session: Any,
        run: RunArtifactHandle,
        state: Dict[str, Any],
        report: RunDoctorReport,
        mode_id: str,
        context: Any,
        dest: Optional[Dict[str, Any]],
    ) -> RunOperationResult:
        executor = self.recommended_action_executor
        action = str(report.recommended_action or "").strip()
        if executor is None or not action:
            message = "Recovery executor недоступен для этого run."
            self._persist_requested_operation(
                run,
                operation="recover",
                report=report,
                status="blocked",
                extra={"executed_operation": action},
            )
            self._record_recovery_attempt(
                run,
                phase=report.phase,
                state=state,
                action="recover",
                status="blocked",
            )
            self._append_operation_event(
                run,
                operation="recover",
                report=report,
                status="blocked",
                extra={"executed_operation": action},
            )
            return RunOperationResult(
                operation="recover",
                status="blocked",
                mode_id=mode_id,
                phase=report.phase,
                run_id=run.run_id,
                report=report.to_dict(),
                recommended_action=self._public_recommended_action(report),
                message=message,
            )

        try:
            execution_payload = await executor(
                session=session,
                mode_id=mode_id,
                operation=action,
                run=run,
                state=state,
                report=report,
                context=context,
                dest=dict(dest or {}),
            )
        except Exception:
            self._log.exception(
                "run recover executor failed mode=%s run_id=%s action=%s",
                mode_id,
                run.run_id,
                action,
            )
            execution_payload = {
                "status": "error",
                "message": "Не удалось выполнить recovery action.",
                "executed_operation": action,
            }

        execution_status = _clean_text((execution_payload or {}).get("status"), max_len=32).lower()
        if execution_status not in {"ok", "blocked", "error"}:
            execution_status = "error"
        attempt_status = "executed" if execution_status == "ok" else execution_status
        message = _clean_text((execution_payload or {}).get("message"), max_len=512)
        if not message:
            if execution_status == "ok":
                message = f"Recovery action `{action}` выполнена."
            elif execution_status == "blocked":
                message = f"Recovery action `{action}` заблокирована."
            else:
                message = f"Recovery action `{action}` завершилась с ошибкой."
        extra = {
            "executed_operation": _clean_text((execution_payload or {}).get("executed_operation") or action, max_len=96)
            or action,
            "executed_via": _clean_text((execution_payload or {}).get("executed_via"), max_len=96) or None,
        }
        spawned_run_id = _clean_text((execution_payload or {}).get("spawned_run_id"), max_len=128) or None
        if spawned_run_id:
            extra["spawned_run_id"] = spawned_run_id
            self._mark_run_superseded(
                run,
                phase=report.phase,
                spawned_run_id=spawned_run_id,
                action=action,
            )
        self._persist_requested_operation(
            run,
            operation="recover",
            report=report,
            status=attempt_status,
            extra=extra,
        )
        self._record_recovery_attempt(
            run,
            phase=report.phase,
            state=state,
            action="recover",
            status=attempt_status,
        )
        self._append_operation_event(
            run,
            operation="recover",
            report=report,
            status=attempt_status,
            extra=extra,
        )
        return RunOperationResult(
            operation="recover",
            status="ok" if execution_status == "ok" else execution_status,
            mode_id=mode_id,
            phase=report.phase,
            run_id=run.run_id,
            report=report.to_dict(),
            recommended_action=self._public_recommended_action(report),
            message=message,
        )

    @staticmethod
    def _recommended_action_message(
        *,
        run: RunArtifactHandle,
        action: str,
        executed_operation: str,
    ) -> str:
        label = RunOperationsService._action_label(action)
        return (
            f"{label}: для run {run.run_id} запущена операция `{executed_operation}` "
            "по рекомендации doctor."
        )

    @staticmethod
    def _codebase_mapper_operation_from_report(
        *,
        report: RunDoctorReport,
        state: Dict[str, Any],
    ) -> Optional[str]:
        action = str(report.recommended_action or "").strip()
        if action == "rerun_same_operation":
            current = _clean_text((state.get("mode_context") or {}).get("operation"), max_len=64)
            return current or "run"
        if action == "run_validate":
            return "validate"
        if action == "run_repair":
            return "repair"
        return None

    @staticmethod
    def _blocked_apply_recommendation_message(
        *,
        mode_id: str,
        report: RunDoctorReport,
    ) -> Optional[str]:
        action = str(report.recommended_action or "").strip()
        if not action or action == "no_action":
            return "Doctor не требует дополнительного действия для этого run."
        if mode_id != "codebase_mapper":
            return "Применение doctor recommendation доступно только для Codebase Mapper."
        if action == "manual_review_required":
            return "Doctor требует ручного подтверждения; автоматическое применение рекомендации недоступно."
        if action not in _APPLY_RECOMMENDATION_ALLOWED_ACTIONS:
            return f"Автоматическое применение рекомендации `{action}` не поддерживается."
        return None

    @staticmethod
    def _blocked_recovery_message(
        *,
        operation: RunOperationName,
        mode_id: str,
        report: RunDoctorReport,
    ) -> Optional[str]:
        action = str(report.recommended_action or "").strip()
        if operation == "resume":
            if action == "no_action" and mode_id == "codebase_mapper":
                return (
                    "Resume отклонён: для Codebase Mapper mid-operation resume запрещён. "
                    "Используйте отдельную операцию rerun/validate/repair."
                )
            if action == "mark_failed":
                return "Resume отклонён: doctor требует перевести запуск в failed state."
            if action and action not in _RESUME_ALLOWED_ACTIONS:
                if mode_id == "codebase_mapper":
                    return (
                        "Resume отклонён: для Codebase Mapper mid-operation resume запрещён. "
                        "Используйте отдельную операцию rerun/validate/repair."
                    )
                if action == "manual_review_required":
                    return "Resume отклонён: doctor требует ручного подтверждения перед продолжением."
                return (
                    "Resume отклонён: doctor рекомендует другой recovery workflow "
                    f"(`{action}`), а не resume текущего run."
                )
            if not bool(report.can_resume):
                return "Resume отклонён: doctor не разрешает автоматическое продолжение этого run."
        if operation == "recover":
            if not action or action == "no_action":
                return "Recovery отклонён: doctor не требует восстановления для этого run."
            if action == "resume_same_phase":
                return "Recovery отклонён: doctor рекомендует обычный resume, а не recovery workflow."
            if action == "manual_review_required":
                return "Recovery отклонён: doctor требует ручного подтверждения перед повторным выполнением."
            if action not in _RECOVER_ALLOWED_ACTIONS:
                return f"Recovery отклонён: action `{action}` не поддерживается shared recovery backend."
        return None

    def _mark_run_superseded(
        self,
        run: RunArtifactHandle,
        *,
        phase: str,
        spawned_run_id: str,
        action: str,
    ) -> None:
        try:
            state = self.artifact_store.load_state(run)
            mode_context = dict(state.get("mode_context") or {})
            mode_context["superseded_by_run_id"] = str(spawned_run_id)
            mode_context["recovery_action"] = str(action)
            state["mode_context"] = mode_context
            state["status"] = "superseded"
            state["phase"] = str(phase or state.get("phase") or "unknown")
            self.artifact_store.save_state(run, state)
            recovery = self.artifact_store.load_recovery(run)
            recovery["recommended_action"] = ""
            recovery["can_resume"] = False
            self.artifact_store.save_recovery(run, recovery)
            self.artifact_store.mark_finished(run, status="superseded", phase=state["phase"])
        except Exception:
            self._log.exception(
                "run operation failed to mark run as superseded run_id=%s spawned_run_id=%s",
                run.run_id,
                spawned_run_id,
            )
