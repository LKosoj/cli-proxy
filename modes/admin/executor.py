from __future__ import annotations

import hashlib
import inspect
import json
import logging
import shlex
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, Mapping, Optional, Protocol

from .transports import (
    LocalCommandResult,
    LocalCommandSpec,
    LocalSubprocessTransport,
    SSHCommandResult,
    SSHCommandSpec,
    SSHSubprocessTransport,
)
from .ui import build_admin_approval_prompt

_LOG = logging.getLogger(__name__)
_EXECUTOR_EVENT_SOURCE = "executor"
_EXECUTOR_REAL_EXECUTION_EVENTS = {"executed_success", "executed_failed"}
_APPROVAL_APPROVE_OPTION = "✅ Подтвердить"
_APPROVAL_REJECT_OPTION = "⛔ Отклонить"
_LOW_CONFIDENCE_LABELS = {"low"}
_RISK_ACTION_LEVELS = {"high", "critical"}


class AdminExecutorError(RuntimeError):
    """Raised when admin execution request is invalid."""


@dataclass(frozen=True)
class AdminExecutionContext:
    command: str
    action_id: str
    target: str
    dry_run: bool
    check_only: bool
    session_id: str
    chat_id: int
    user_id: Optional[int] = None
    flags: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AdminExecutionResult:
    success: bool
    text: str
    returncode: Optional[int] = None
    logged_action_id: Optional[str] = None


AskUserFn = Callable[[str, list[str]], Awaitable[str] | str]


class _AdminActionLogStore(Protocol):
    def create_action(
        self,
        action_id: str,
        *,
        session_id: str,
        chat_id: int = 0,
        payload: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        ...

    def create_incident(
        self,
        incident_id: str,
        *,
        session_id: str,
        chat_id: int = 0,
        payload: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        ...

    def create_alert_state(
        self,
        alert_id: str,
        *,
        session_id: str,
        chat_id: int = 0,
        payload: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        ...

    def list_actions(self, session_id: str, *, chat_id: Optional[int] = None, limit: int = 20) -> list[Dict[str, Any]]:
        ...

    def create_approved_override(
        self,
        override_id: str,
        *,
        session_id: str,
        chat_id: int = 0,
        payload: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        ...

    def get_approved_override(self, override_id: str, *, chat_id: Optional[int] = None) -> Optional[Dict[str, Any]]:
        ...


@dataclass(frozen=True)
class _SecurityPolicyDecision:
    allowed: bool
    code: str = "allowed"
    message: str = ""
    details: Mapping[str, Any] = field(default_factory=dict)


class AdminExecutor:
    async def execute(
        self,
        *,
        context: AdminExecutionContext,
        local_transport: LocalSubprocessTransport,
        ssh_transport: SSHSubprocessTransport,
        local_spec: Optional[LocalCommandSpec] = None,
        ssh_spec: Optional[SSHCommandSpec] = None,
        state_store: Optional[_AdminActionLogStore] = None,
        now_ts: Optional[float] = None,
    ) -> AdminExecutionResult:
        target = str(context.target or "").strip().lower()
        if target not in {"local", "ssh"}:
            raise AdminExecutorError(f"unsupported target: {target}")

        if target == "local":
            if local_spec is None:
                raise AdminExecutorError("missing LocalCommandSpec for local target")
            command_preview = shlex.join(tuple(local_spec.argv))
        else:
            if ssh_spec is None:
                raise AdminExecutorError("missing SSHCommandSpec for ssh target")
            command_preview = shlex.join(tuple(ssh_spec.argv))

        now_value = float(now_ts if now_ts is not None else time.time())
        policy_decision = self._evaluate_security_policies(
            context=context,
            state_store=state_store,
            now_ts=now_value,
        )
        if not policy_decision.allowed:
            blocked = AdminExecutionResult(
                success=False,
                text=(
                    f"SECURITY POLICY BLOCKED: {policy_decision.code}\n"
                    f"{policy_decision.message}"
                ),
                returncode=-1,
            )
            return self._with_execution_log(
                result=blocked,
                context=context,
                state_store=state_store,
                event="policy_blocked",
                command_preview=command_preview,
                policy=dict(policy_decision.details),
                now_ts=now_value,
            )

        if bool(context.check_only):
            check_only = AdminExecutionResult(
                success=True,
                text=(
                    f"CHECK action: {context.action_id}\n"
                    f"Target: {target}\n"
                    f"Command: {command_preview}\n"
                    f"Validation: ok"
                ),
                returncode=0,
            )
            return self._with_execution_log(
                result=check_only,
                context=context,
                state_store=state_store,
                event="check_only",
                command_preview=command_preview,
                now_ts=now_value,
            )

        if bool(context.dry_run):
            dry_run = AdminExecutionResult(
                success=True,
                text=(
                    f"DRY-RUN action: {context.action_id}\n"
                    f"Target: {target}\n"
                    f"Command: {command_preview}\n"
                    "Execution skipped."
                ),
                returncode=0,
            )
            return self._with_execution_log(
                result=dry_run,
                context=context,
                state_store=state_store,
                event="dry_run_intent",
                command_preview=command_preview,
                now_ts=now_value,
            )

        if target == "local":
            local_result = await local_transport.run(local_spec)
            execution = self._from_local_result(local_result)
        else:
            ssh_result = await ssh_transport.run(ssh_spec)
            execution = self._from_ssh_result(ssh_result)

        execution_event = "executed_success" if bool(execution.success) else "executed_failed"
        return self._with_execution_log(
            result=execution,
            context=context,
            state_store=state_store,
            event=execution_event,
            command_preview=command_preview,
            now_ts=now_value,
        )

    def _with_execution_log(
        self,
        *,
        result: AdminExecutionResult,
        context: AdminExecutionContext,
        state_store: Optional[_AdminActionLogStore],
        event: str,
        command_preview: str,
        policy: Optional[Dict[str, Any]] = None,
        now_ts: Optional[float] = None,
    ) -> AdminExecutionResult:
        if state_store is None:
            return result
        try:
            logged_action_id = self._log_executor_action(
                state_store=state_store,
                context=context,
                result=result,
                event=event,
                command_preview=command_preview,
                policy=policy or {},
                now_ts=now_ts,
            )
        except Exception:
            _LOG.exception(
                "admin executor failed to persist action log session_id=%s action_id=%s event=%s",
                str(context.session_id or ""),
                str(context.action_id or ""),
                str(event or ""),
            )
            return result
        return AdminExecutionResult(
            success=bool(result.success),
            text=str(result.text or ""),
            returncode=result.returncode,
            logged_action_id=logged_action_id,
        )

    def _evaluate_security_policies(
        self,
        *,
        context: AdminExecutionContext,
        state_store: Optional[_AdminActionLogStore],
        now_ts: float,
    ) -> _SecurityPolicyDecision:
        flags = dict(context.flags or {})
        action_id = str(context.action_id or "").strip()
        target = str(context.target or "").strip().lower()

        mandatory_notify_actions: set[str] = set()
        for item in list(flags.get("mandatory_notify_actions") or []):
            token = str(item or "").strip()
            if token:
                mandatory_notify_actions.add(token)
        notified_actions = {
            str(item or "").strip()
            for item in list(flags.get("notified_actions") or [])
            if str(item or "").strip()
        }
        notify_sent = bool(flags.get("notify_sent")) or action_id in notified_actions
        if action_id in mandatory_notify_actions and not notify_sent:
            return _SecurityPolicyDecision(
                allowed=False,
                code="mandatory_notify",
                message=(
                    f"Action `{action_id}` требует обязательного уведомления перед выполнением. "
                    "Отправьте уведомление и повторите запуск."
                ),
                details={"action_id": action_id, "notify_sent": bool(notify_sent)},
            )

        maintenance = self._evaluate_maintenance_window(
            flags=flags,
            action_id=action_id,
            target=target,
            now_ts=now_ts,
        )
        if not maintenance.allowed:
            return maintenance

        cooldown_sec = self._as_float(flags.get("cooldown_sec"), default=0.0)
        if cooldown_sec > 0:
            last_ts = self._resolve_last_execution_ts(
                context=context,
                state_store=state_store,
                now_ts=now_ts,
            )
            if last_ts is not None:
                elapsed = max(0.0, now_ts - last_ts)
                if elapsed < cooldown_sec:
                    return _SecurityPolicyDecision(
                        allowed=False,
                        code="cooldown",
                        message=(
                            f"Action `{action_id}` находится в cooldown. "
                            f"Повторите через {cooldown_sec - elapsed:.1f} сек."
                        ),
                        details={
                            "action_id": action_id,
                            "cooldown_sec": cooldown_sec,
                            "elapsed_sec": elapsed,
                        },
                    )

        rate_limit_max = self._as_int(flags.get("rate_limit_max"), default=0)
        rate_limit_window_sec = self._as_float(flags.get("rate_limit_window_sec"), default=0.0)
        if rate_limit_max > 0 and rate_limit_window_sec > 0:
            recent_ts = self._collect_recent_execution_timestamps(
                context=context,
                state_store=state_store,
                now_ts=now_ts,
                window_sec=rate_limit_window_sec,
            )
            recent_count = sum(1 for ts in recent_ts if now_ts - ts <= rate_limit_window_sec)
            if recent_count >= rate_limit_max:
                return _SecurityPolicyDecision(
                    allowed=False,
                    code="rate_limit",
                    message=(
                        f"Action `{action_id}` превысил лимит {rate_limit_max} за {rate_limit_window_sec:.0f} сек."
                    ),
                    details={
                        "action_id": action_id,
                        "rate_limit_max": rate_limit_max,
                        "rate_limit_window_sec": rate_limit_window_sec,
                        "recent_count": recent_count,
                    },
                )

        return _SecurityPolicyDecision(allowed=True)

    def _resolve_last_execution_ts(
        self,
        *,
        context: AdminExecutionContext,
        state_store: Optional[_AdminActionLogStore],
        now_ts: float,
    ) -> Optional[float]:
        from_history = self._collect_recent_execution_timestamps(
            context=context,
            state_store=state_store,
            now_ts=now_ts,
            window_sec=None,
        )
        if from_history:
            return max(from_history)
        flags = dict(context.flags or {})
        fallback = self._as_float(flags.get("last_executed_ts"), default=0.0)
        if fallback > 0:
            return fallback
        return None

    def _collect_recent_execution_timestamps(
        self,
        *,
        context: AdminExecutionContext,
        state_store: Optional[_AdminActionLogStore],
        now_ts: float,
        window_sec: Optional[float],
    ) -> list[float]:
        cutoff = (now_ts - float(window_sec)) if window_sec and window_sec > 0 else None
        timestamps: list[float] = []

        flags = dict(context.flags or {})
        for value in list(flags.get("recent_execution_timestamps") or []):
            ts = self._as_float(value, default=0.0)
            if ts <= 0:
                continue
            if cutoff is not None and ts < cutoff:
                continue
            timestamps.append(ts)

        if state_store is not None:
            try:
                rows = state_store.list_actions(
                    str(context.session_id or ""),
                    chat_id=int(context.chat_id),
                    limit=200,
                )
            except Exception:
                _LOG.exception(
                    "admin executor failed to read action history session_id=%s action_id=%s",
                    str(context.session_id or ""),
                    str(context.action_id or ""),
                )
                rows = []
            for row in rows:
                payload = row.get("payload") if isinstance(row, dict) else None
                if not isinstance(payload, Mapping):
                    continue
                if str(payload.get("source") or "") != _EXECUTOR_EVENT_SOURCE:
                    continue
                request = payload.get("request")
                if not isinstance(request, Mapping):
                    continue
                if str(request.get("action_id") or "") != str(context.action_id or ""):
                    continue
                event = str(payload.get("event") or "")
                if event not in _EXECUTOR_REAL_EXECUTION_EVENTS:
                    continue
                ts = self._as_float(payload.get("logged_at_ts"), default=0.0)
                if ts <= 0:
                    continue
                if cutoff is not None and ts < cutoff:
                    continue
                timestamps.append(ts)

        return timestamps

    def _evaluate_maintenance_window(
        self,
        *,
        flags: Dict[str, Any],
        action_id: str,
        target: str,
        now_ts: float,
    ) -> _SecurityPolicyDecision:
        window = flags.get("maintenance_window")
        if not isinstance(window, Mapping):
            return _SecurityPolicyDecision(allowed=True)
        if bool(window.get("enabled", True)) is False:
            return _SecurityPolicyDecision(allowed=True)

        actions = {
            str(item or "").strip()
            for item in list(window.get("actions") or [])
            if str(item or "").strip()
        }
        targets = {
            str(item or "").strip().lower()
            for item in list(window.get("targets") or [])
            if str(item or "").strip()
        }
        if actions and action_id not in actions:
            return _SecurityPolicyDecision(allowed=True)
        if targets and target not in targets:
            return _SecurityPolicyDecision(allowed=True)

        start_ts = self._as_float(window.get("start_ts"), default=0.0)
        end_ts = self._as_float(window.get("end_ts"), default=0.0)
        if start_ts <= 0 or end_ts <= 0 or end_ts < start_ts:
            return _SecurityPolicyDecision(allowed=True)
        if start_ts <= now_ts <= end_ts:
            return _SecurityPolicyDecision(allowed=True)
        return _SecurityPolicyDecision(
            allowed=False,
            code="maintenance_window",
            message=(
                f"Action `{action_id}` разрешен только в maintenance window "
                f"[{start_ts:.3f}, {end_ts:.3f}]."
            ),
            details={
                "action_id": action_id,
                "target": target,
                "start_ts": start_ts,
                "end_ts": end_ts,
                "now_ts": now_ts,
            },
        )

    @staticmethod
    def _as_float(value: Any, *, default: float) -> float:
        try:
            return float(value)
        except Exception:
            return float(default)

    @staticmethod
    def _as_int(value: Any, *, default: int) -> int:
        try:
            return int(value)
        except Exception:
            return int(default)

    @staticmethod
    def _log_executor_action(
        *,
        state_store: _AdminActionLogStore,
        context: AdminExecutionContext,
        result: AdminExecutionResult,
        event: str,
        command_preview: str,
        policy: Dict[str, Any],
        now_ts: Optional[float],
    ) -> str:
        session_id = str(context.session_id or "").strip()
        action_id = str(context.action_id or "").strip() or "unknown"
        log_action_id = f"executor:{session_id}:{action_id}:{time.time_ns()}"
        payload = {
            "source": _EXECUTOR_EVENT_SOURCE,
            "event": str(event or "").strip(),
            "request": {
                "command": str(context.command or ""),
                "action_id": action_id,
                "target": str(context.target or ""),
                "dry_run": bool(context.dry_run),
                "check_only": bool(context.check_only),
                "flags": dict(context.flags or {}),
                "command_preview": str(command_preview or ""),
            },
            "policy": dict(policy or {}),
            "result": {
                "success": bool(result.success),
                "text": str(result.text or ""),
                "returncode": result.returncode,
            },
            "logged_at_ts": float(now_ts if now_ts is not None else time.time()),
        }
        state_store.create_action(
            log_action_id,
            session_id=session_id,
            chat_id=int(context.chat_id),
            payload=payload,
        )
        return log_action_id

    async def apply_analyzer_decision(
        self,
        *,
        decision: Mapping[str, Any],
        session_id: str,
        chat_id: int = 0,
        state_store: _AdminActionLogStore,
        local_transport: Optional[LocalSubprocessTransport] = None,
        ssh_transport: Optional[SSHSubprocessTransport] = None,
        local_spec: Optional[LocalCommandSpec] = None,
        ssh_spec: Optional[SSHCommandSpec] = None,
        ask_user: Optional[AskUserFn] = None,
        now_ts: Optional[float] = None,
    ) -> AdminExecutionResult:
        sid = str(session_id or "").strip()
        if not sid:
            raise AdminExecutorError("session_id is empty for analyzer decision")
        cid = int(chat_id)
        decision_now_ts = float(now_ts if now_ts is not None else time.time())
        decision_payload = self._normalize_analyzer_decision(decision)
        if str(decision_payload.get("action") or "").strip().lower() == "no_action":
            return AdminExecutionResult(
                success=True,
                text=(
                    "ANALYZER action=no_action\n"
                    f"Diagnosis: {str(decision_payload.get('diagnosis') or '').strip()}\n"
                    f"Reason: {str(decision_payload.get('reason') or '').strip()}\n"
                    f"Urgency: {str(decision_payload.get('urgency') or '').strip()}"
                ),
                returncode=0,
            )
        has_execution_spec = local_spec is not None or ssh_spec is not None
        override_ctx = self._build_override_context(
            decision_payload=decision_payload,
            session_id=sid,
            chat_id=cid,
            local_spec=local_spec,
            ssh_spec=ssh_spec,
        )
        approval = self._resolve_approved_override(
            state_store=state_store,
            session_id=sid,
            chat_id=cid,
            override_ctx=override_ctx,
            now_ts=decision_now_ts,
        )
        if approval is None:
            approval = await self._resolve_manual_approval(
                decision_payload=decision_payload,
                ask_user=ask_user,
                has_execution_spec=has_execution_spec,
            )
            if (
                approval is not None
                and bool(approval.get("approved"))
                and str(approval.get("reason") or "") == "approved_by_user"
            ):
                self._persist_approved_override(
                    state_store=state_store,
                    session_id=sid,
                    chat_id=cid,
                    override_ctx=override_ctx,
                    approved_at_ts=decision_now_ts,
                )
        if approval is not None:
            decision_payload["manual_approval"] = dict(approval)
        detected_at_ts = self._resolve_detected_at_ts(decision=decision, fallback_ts=decision_now_ts)
        sla_target_sec = self._resolve_sla_target_sec(decision=decision)
        notified_at_ts = decision_now_ts
        delivery_latency_sec = max(0.0, notified_at_ts - detected_at_ts)
        notification_metrics = {
            "detected_at_ts": detected_at_ts,
            "notified_at_ts": notified_at_ts,
            "delivery_latency_sec": delivery_latency_sec,
            "sla_target_sec": sla_target_sec,
            "sla_met": bool(delivery_latency_sec <= sla_target_sec),
        }

        if approval is not None and not bool(approval.get("approved")):
            result = AdminExecutionResult(
                success=False,
                text=(
                    f"ANALYZER action={str(decision_payload.get('action') or '').strip()}\n"
                    f"Manual approval required: {str(approval.get('reason') or 'rejected')}"
                ),
                returncode=-1,
            )
        else:
            result = await self._execute_analyzer_action(
                decision_payload=decision_payload,
                session_id=sid,
                local_transport=local_transport,
                ssh_transport=ssh_transport,
                local_spec=local_spec,
                ssh_spec=ssh_spec,
            )
        incident_refs = self._persist_incident_and_alert_state(
            state_store=state_store,
            session_id=sid,
            chat_id=cid,
            decision_payload=decision_payload,
            result=result,
            notification_metrics=notification_metrics,
            logged_at_ts=decision_now_ts,
        )
        logged_action_id = self._log_analyzer_action(
            state_store=state_store,
            session_id=sid,
            chat_id=cid,
            decision_payload=decision_payload,
            result=result,
            incident_refs=incident_refs,
            notification_metrics=notification_metrics,
            logged_at_ts=decision_now_ts,
        )
        return AdminExecutionResult(
            success=bool(result.success),
            text=str(result.text or ""),
            returncode=result.returncode,
            logged_action_id=logged_action_id,
        )

    @staticmethod
    def _build_override_context(
        *,
        decision_payload: Mapping[str, Any],
        session_id: str,
        chat_id: int,
        local_spec: Optional[LocalCommandSpec],
        ssh_spec: Optional[SSHCommandSpec],
    ) -> Dict[str, Any]:
        action = str(decision_payload.get("action") or "").strip().lower()
        server_id = str(decision_payload.get("server_id") or decision_payload.get("target_server_id") or "").strip() or "*"
        risk_level = str(decision_payload.get("risk_level") or "").strip().lower()
        if not risk_level:
            urgency = str(decision_payload.get("urgency") or "").strip().lower()
            risk_level = "critical" if urgency in {"critical", "warning"} else "normal"
        if local_spec is not None:
            action_id = str(local_spec.action_id or action or "unknown").strip() or "unknown"
            normalized_command = shlex.join(tuple(local_spec.argv or ()))
            command_hash = hashlib.sha256(normalized_command.encode("utf-8")).hexdigest()
            params: Dict[str, Any] = {
                "target": "local",
                "argv": list(local_spec.argv or ()),
                "cwd": str(local_spec.cwd or ""),
                "timeout_sec": float(local_spec.timeout_sec),
                "env": dict(local_spec.env or {}),
            }
        elif ssh_spec is not None:
            action_id = str(ssh_spec.action_id or action or "unknown").strip() or "unknown"
            normalized_command = shlex.join(tuple(ssh_spec.argv or ()))
            command_hash = hashlib.sha256(normalized_command.encode("utf-8")).hexdigest()
            params = {
                "target": "ssh",
                "host": str(ssh_spec.host or ""),
                "user": str(ssh_spec.user or ""),
                "port": int(ssh_spec.port),
                "argv": list(ssh_spec.argv or ()),
                "timeout_sec": float(ssh_spec.timeout_sec),
                "key_path": str(ssh_spec.key_path or ""),
                "options": list(ssh_spec.options or ()),
            }
        else:
            action_id = str(action or "unknown").strip() or "unknown"
            normalized_command = str(decision_payload.get("normalized_command") or str(action or "")).strip()
            command_hash = str(decision_payload.get("command_hash") or "").strip()
            if not command_hash and normalized_command:
                command_hash = hashlib.sha256(normalized_command.encode("utf-8")).hexdigest()
            params = {"target": "logical", "action": str(action or ""), "command": normalized_command}

        hash_payload = {
            "chat_id": int(chat_id),
            "session_id": str(session_id or ""),
            "server_id": server_id,
            "action_id": action_id,
            "normalized_command": normalized_command,
            "command_hash": command_hash,
            "risk_level": risk_level,
            "params": params,
        }
        encoded = json.dumps(hash_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
        return {
            "override_id": f"override:{chat_id}:{session_id}:{digest}",
            "hash": digest,
            "chat_id": int(chat_id),
            "session_id": str(session_id or ""),
            "server_id": server_id,
            "action_id": action_id,
            "normalized_command": normalized_command,
            "command_hash": command_hash,
            "risk_level": risk_level,
            "params": params,
        }

    @staticmethod
    def _resolve_approved_override(
        *,
        state_store: _AdminActionLogStore,
        session_id: str,
        chat_id: int,
        override_ctx: Mapping[str, Any],
        now_ts: float,
    ) -> Optional[Dict[str, Any]]:
        override_id = str(override_ctx.get("override_id") or "").strip()
        if not override_id:
            return None
        try:
            row = state_store.get_approved_override(override_id, chat_id=int(chat_id))
        except Exception:
            _LOG.exception(
                "admin executor failed to read approved override session_id=%s override_id=%s",
                str(session_id or ""),
                override_id,
            )
            return None
        if not isinstance(row, Mapping):
            return None
        if str(row.get("session_id") or "") != str(session_id or ""):
            return None
        if int(row.get("chat_id") or 0) != int(chat_id):
            return None
        payload = row.get("payload")
        if not isinstance(payload, Mapping):
            return None
        if str(payload.get("hash") or "") != str(override_ctx.get("hash") or ""):
            return None
        if not bool(payload.get("approved", False)):
            return None
        expires_at_ts = AdminExecutor._as_float(payload.get("expires_at_ts"), default=0.0)
        if expires_at_ts > 0 and float(now_ts) > expires_at_ts:
            return None
        return {
            "required": False,
            "approved": True,
            "selection": "",
            "reason": "approved_override_used",
            "triggers": ["approved_override_used"],
            "override_id": override_id,
            "hash": str(override_ctx.get("hash") or ""),
        }

    @staticmethod
    def _persist_approved_override(
        *,
        state_store: _AdminActionLogStore,
        session_id: str,
        chat_id: int,
        override_ctx: Mapping[str, Any],
        approved_at_ts: float,
    ) -> None:
        override_id = str(override_ctx.get("override_id") or "").strip()
        if not override_id:
            return
        payload = {
            "hash": str(override_ctx.get("hash") or ""),
            "chat_id": int(chat_id),
            "session_id": str(session_id or ""),
            "server_id": str(override_ctx.get("server_id") or ""),
            "action_id": str(override_ctx.get("action_id") or ""),
            "normalized_command": str(override_ctx.get("normalized_command") or ""),
            "command_hash": str(override_ctx.get("command_hash") or ""),
            "risk_level": str(override_ctx.get("risk_level") or ""),
            "params": dict(override_ctx.get("params") or {}),
            "approved": True,
            "approved_at_ts": float(approved_at_ts),
        }
        try:
            state_store.create_approved_override(
                override_id,
                session_id=str(session_id or ""),
                chat_id=int(chat_id),
                payload=payload,
            )
        except Exception:
            _LOG.exception(
                "admin executor failed to persist approved override session_id=%s override_id=%s",
                str(session_id or ""),
                override_id,
            )

    @staticmethod
    def _resolve_detected_at_ts(
        *,
        decision: Mapping[str, Any],
        fallback_ts: float,
    ) -> float:
        for key in ("detected_at_ts", "detected_ts", "event_ts"):
            value = decision.get(key)
            try:
                detected_at_ts = float(value)
            except Exception:
                continue
            if detected_at_ts > 0:
                return detected_at_ts
        meta = decision.get("meta")
        if isinstance(meta, Mapping):
            value = meta.get("detected_at_ts")
            try:
                detected_at_ts = float(value)
            except Exception:
                detected_at_ts = 0.0
            if detected_at_ts > 0:
                return detected_at_ts
        return float(fallback_ts)

    @staticmethod
    def _resolve_sla_target_sec(*, decision: Mapping[str, Any]) -> float:
        for key in ("sla_target_sec", "notify_sla_sec"):
            value = decision.get(key)
            try:
                sla_target_sec = float(value)
            except Exception:
                continue
            if sla_target_sec > 0:
                return sla_target_sec
        return 15.0

    async def _resolve_manual_approval(
        self,
        *,
        decision_payload: Dict[str, Any],
        ask_user: Optional[AskUserFn],
        has_execution_spec: bool,
    ) -> Optional[Dict[str, Any]]:
        triggers = self._collect_manual_approval_triggers(
            decision_payload=decision_payload,
            has_execution_spec=has_execution_spec,
        )
        if not triggers:
            return None

        action = str(decision_payload.get("action") or "").strip().lower()
        confidence = str(decision_payload.get("confidence") or "").strip().lower()
        diagnosis = str(decision_payload.get("diagnosis") or "").strip()
        reason = str(decision_payload.get("reason") or "").strip()

        if ask_user is None:
            return {
                "required": True,
                "approved": False,
                "selection": "",
                "reason": "ask_user_unavailable",
                "triggers": list(triggers),
            }

        question = build_admin_approval_prompt(
            action=action,
            confidence=confidence,
            diagnosis=diagnosis,
            reason=reason,
            triggers=list(triggers),
        )
        options = [_APPROVAL_APPROVE_OPTION, _APPROVAL_REJECT_OPTION]
        try:
            selected = await self._call_ask_user(
                ask_user=ask_user,
                question=question,
                options=options,
            )
        except Exception:
            _LOG.exception(
                "admin executor ask_user failed for manual approval action=%s confidence=%s",
                action,
                confidence,
            )
            return {
                "required": True,
                "approved": False,
                "selection": "",
                "reason": "ask_user_failed",
                "triggers": list(triggers),
            }

        normalized = str(selected or "").strip()
        if normalized.casefold() == _APPROVAL_APPROVE_OPTION.casefold():
            return {
                "required": True,
                "approved": True,
                "selection": normalized,
                "reason": "approved_by_user",
                "triggers": list(triggers),
            }
        if normalized.casefold() == _APPROVAL_REJECT_OPTION.casefold():
            return {
                "required": True,
                "approved": False,
                "selection": normalized,
                "reason": "rejected_by_user",
                "triggers": list(triggers),
            }
        return {
            "required": True,
            "approved": False,
            "selection": normalized,
            "reason": "invalid_selection",
            "triggers": list(triggers),
        }

    @staticmethod
    def _collect_manual_approval_triggers(
        *,
        decision_payload: Mapping[str, Any],
        has_execution_spec: bool,
    ) -> list[str]:
        triggers: list[str] = []
        confidence = str(decision_payload.get("confidence") or "").strip().lower()
        if confidence in _LOW_CONFIDENCE_LABELS:
            triggers.append("low_confidence")
        action = str(decision_payload.get("action") or "").strip().lower()
        risk_level = str(decision_payload.get("risk_level") or "").strip().lower()
        risky_action = risk_level in _RISK_ACTION_LEVELS or action.startswith("restart_")
        if risky_action:
            triggers.append("risky_action")
        if not has_execution_spec and risky_action:
            triggers.append("missing_execution_spec")
        if bool(decision_payload.get("signal_conflict")):
            triggers.append("signal_conflict")
        if bool(decision_payload.get("policy_conflict")):
            triggers.append("policy_conflict")
        return triggers

    @staticmethod
    async def _call_ask_user(
        *,
        ask_user: AskUserFn,
        question: str,
        options: list[str],
    ) -> str:
        selected = ask_user(str(question or ""), list(options or []))
        if inspect.isawaitable(selected):
            selected = await selected
        return str(selected or "").strip()

    @staticmethod
    def _persist_incident_and_alert_state(
        *,
        state_store: _AdminActionLogStore,
        session_id: str,
        chat_id: int,
        decision_payload: Dict[str, Any],
        result: AdminExecutionResult,
        notification_metrics: Dict[str, Any],
        logged_at_ts: float,
    ) -> Dict[str, str]:
        action_name = str(decision_payload.get("action") or "unknown").strip() or "unknown"
        suffix = f"{session_id}:{action_name}:{time.time_ns()}"
        incident_id = f"incident:{suffix}"
        alert_id = f"alert:{suffix}"
        incident_payload = {
            "source": "analyzer",
            "decision": dict(decision_payload),
            "result": {
                "success": bool(result.success),
                "returncode": result.returncode,
            },
            "alert_id": alert_id,
            "notification_metrics": dict(notification_metrics),
            "logged_at_ts": float(logged_at_ts),
        }
        alert_payload = {
            "source": "analyzer",
            "incident_id": incident_id,
            "action": str(decision_payload.get("action") or ""),
            "severity": str(decision_payload.get("urgency") or ""),
            "status": "active",
            "notification_metrics": dict(notification_metrics),
            "logged_at_ts": float(logged_at_ts),
        }
        state_store.create_incident(
            incident_id,
            session_id=session_id,
            chat_id=int(chat_id),
            payload=incident_payload,
        )
        state_store.create_alert_state(
            alert_id,
            session_id=session_id,
            chat_id=int(chat_id),
            payload=alert_payload,
        )
        return {"incident_id": incident_id, "alert_id": alert_id}

    async def _execute_analyzer_action(
        self,
        *,
        decision_payload: Dict[str, Any],
        session_id: str,
        local_transport: Optional[LocalSubprocessTransport],
        ssh_transport: Optional[SSHSubprocessTransport],
        local_spec: Optional[LocalCommandSpec],
        ssh_spec: Optional[SSHCommandSpec],
    ) -> AdminExecutionResult:
        action = str(decision_payload.get("action") or "").strip().lower()
        diagnosis = str(decision_payload.get("diagnosis") or "").strip()
        reason = str(decision_payload.get("reason") or "").strip()
        urgency = str(decision_payload.get("urgency") or "").strip()
        executor_flags = {"source": "analyzer"}
        if bool(decision_payload.get("notify_sent", False)):
            executor_flags["notify_sent"] = True
        if isinstance(decision_payload.get("notified_actions"), list):
            executor_flags["notified_actions"] = list(decision_payload.get("notified_actions") or [])
        if action not in {"notify_admin", "ask_user"}:
            if local_spec is not None:
                exec_context = AdminExecutionContext(
                    command=action,
                    action_id=str(local_spec.action_id or action),
                    target="local",
                    dry_run=False,
                    check_only=False,
                    session_id=session_id,
                    chat_id=0,
                    flags=executor_flags,
                )
                return await self.execute(
                    context=exec_context,
                    local_transport=local_transport or LocalSubprocessTransport(),
                    ssh_transport=ssh_transport or SSHSubprocessTransport(),
                    local_spec=local_spec,
                    ssh_spec=None,
                )
            if ssh_spec is not None:
                exec_context = AdminExecutionContext(
                    command=action,
                    action_id=str(ssh_spec.action_id or action),
                    target="ssh",
                    dry_run=False,
                    check_only=False,
                    session_id=session_id,
                    chat_id=0,
                    flags=executor_flags,
                )
                return await self.execute(
                    context=exec_context,
                    local_transport=local_transport or LocalSubprocessTransport(),
                    ssh_transport=ssh_transport or SSHSubprocessTransport(),
                    local_spec=None,
                    ssh_spec=ssh_spec,
                )
            return AdminExecutionResult(
                success=False,
                text=(
                    f"ANALYZER action={action}\n"
                    f"Diagnosis: {diagnosis}\n"
                    f"Urgency: {urgency}\n"
                    "Execution spec is missing for requested action."
                ),
                returncode=-1,
            )

        if action == "notify_admin":
            return AdminExecutionResult(
                success=True,
                text=(
                    f"ANALYZER action={action}\n"
                    f"Diagnosis: {diagnosis}\n"
                    f"Reason: {reason}\n"
                    f"Urgency: {urgency}"
                ),
                returncode=0,
            )
        if action == "ask_user":
            return AdminExecutionResult(
                success=True,
                text=(
                    f"ANALYZER action={action}\n"
                    f"Diagnosis: {diagnosis}\n"
                    f"Reason: {reason}\n"
                    f"Urgency: {urgency}"
                ),
                returncode=0,
            )

        return AdminExecutionResult(
            success=False,
            text=(
                f"ANALYZER action={action or '<empty>'}\n"
                "Unsupported analyzer action."
            ),
            returncode=-1,
        )

    @staticmethod
    def _normalize_analyzer_decision(decision: Mapping[str, Any]) -> Dict[str, Any]:
        payload = dict(decision or {})
        return {
            "diagnosis": str(payload.get("diagnosis") or "").strip(),
            "confidence": str(payload.get("confidence") or "").strip().lower(),
            "action": str(payload.get("action") or "").strip().lower(),
            "reason": str(payload.get("reason") or "").strip(),
            "urgency": str(payload.get("urgency") or "").strip().lower(),
            "server_id": str(payload.get("server_id") or payload.get("target_server_id") or "").strip(),
            "incident_type": str(payload.get("incident_type") or "").strip(),
            "risk_level": str(payload.get("risk_level") or "").strip().lower(),
            "evidence": list(payload.get("evidence") or [])
            if isinstance(payload.get("evidence"), list)
            else [],
            "suggested_runbook_ids": list(payload.get("suggested_runbook_ids") or [])
            if isinstance(payload.get("suggested_runbook_ids"), list)
            else [],
            "normalized_command": str(payload.get("normalized_command") or "").strip(),
            "command_hash": str(payload.get("command_hash") or "").strip(),
            "signal_conflict": bool(payload.get("signal_conflict", False)),
            "policy_conflict": bool(payload.get("policy_conflict", False)),
            "notify_sent": bool(payload.get("notify_sent", False)),
            "notified_actions": list(payload.get("notified_actions") or [])
            if isinstance(payload.get("notified_actions"), list)
            else [],
        }

    @staticmethod
    def _log_analyzer_action(
        *,
        state_store: _AdminActionLogStore,
        session_id: str,
        chat_id: int,
        decision_payload: Dict[str, Any],
        result: AdminExecutionResult,
        incident_refs: Dict[str, str],
        notification_metrics: Dict[str, Any],
        logged_at_ts: float,
    ) -> str:
        action_name = str(decision_payload.get("action") or "unknown").strip() or "unknown"
        action_id = f"analyzer:{session_id}:{action_name}:{time.time_ns()}"
        payload = {
            "source": "analyzer",
            "decision": dict(decision_payload),
            "incident_refs": dict(incident_refs),
            "notification_metrics": dict(notification_metrics),
            "result": {
                "success": bool(result.success),
                "text": str(result.text or ""),
                "returncode": result.returncode,
            },
            "logged_at_ts": float(logged_at_ts),
        }
        state_store.create_action(
            action_id,
            session_id=session_id,
            chat_id=int(chat_id),
            payload=payload,
        )
        return action_id

    @staticmethod
    def _from_local_result(result: LocalCommandResult) -> AdminExecutionResult:
        chunks = [
            f"RUN action: {result.action_id}",
            "Target: local",
            f"Exit code: {result.returncode}",
            f"Duration: {result.duration_ms}ms",
        ]
        if result.timed_out:
            chunks.append("Timed out: yes")
        if result.stdout.strip():
            chunks.append(f"STDOUT:\n{result.stdout.strip()}")
        if result.stderr.strip():
            chunks.append(f"STDERR:\n{result.stderr.strip()}")
        success = bool(result.returncode == 0 and not result.timed_out)
        return AdminExecutionResult(
            success=success,
            text="\n\n".join(chunks),
            returncode=int(result.returncode),
        )

    @staticmethod
    def _from_ssh_result(result: SSHCommandResult) -> AdminExecutionResult:
        target = f"{result.user + '@' if result.user else ''}{result.host}:{result.port}"
        chunks = [
            f"RUN action: {result.action_id}",
            f"Target: {target}",
            f"Exit code: {result.returncode}",
            f"Duration: {result.duration_ms}ms",
        ]
        if result.timed_out:
            chunks.append("Timed out: yes")
        if result.stdout.strip():
            chunks.append(f"STDOUT:\n{result.stdout.strip()}")
        if result.stderr.strip():
            chunks.append(f"STDERR:\n{result.stderr.strip()}")
        success = bool(result.returncode == 0 and not result.timed_out)
        return AdminExecutionResult(
            success=success,
            text="\n\n".join(chunks),
            returncode=int(result.returncode),
        )


__all__ = [
    "AdminExecutionContext",
    "AdminExecutionResult",
    "AdminExecutor",
    "AdminExecutorError",
]
