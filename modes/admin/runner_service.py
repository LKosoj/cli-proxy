from __future__ import annotations

import asyncio
import copy
import inspect
import json
import logging
import os
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, Mapping, Optional

from .analyzer import AdminAnalyzer
from .executor import (
    AdminExecutionResult,
    AdminExecutor,
    LocalCommandSpec,
    LocalSubprocessTransport,
    SSHCommandSpec,
    SSHSubprocessTransport,
)
from .monitor import AdminMonitor, AdminMonitorSnapshot
from .notifier import AdminNotificationResult, AdminNotifier
from .snapshot_store import AdminSnapshotStore
from .transports import LocalTransportError, SSHTransportError

_LOG = logging.getLogger(__name__)
_SECONDARY_CLI_ACTION_ID = "secondary_cli_step"


class AdminModeRunnerServiceError(RuntimeError):
    """Raised when admin runner request is invalid."""


@dataclass(frozen=True)
class AdminMonitorAnalyzerStepResult:
    snapshot: AdminMonitorSnapshot
    decision: Dict[str, Any]


@dataclass(frozen=True)
class AdminExecutorNotifierStepResult:
    execution_result: AdminExecutionResult
    action_notification: Optional[AdminNotificationResult]
    incident_notification: Optional[AdminNotificationResult]
    target_transport: str = ""
    native_transport_execution: bool = False
    destructive_execution: bool = False
    check_only: bool = False
    dry_run: bool = False
    action_id: str = ""
    server_id: str = ""


@dataclass(frozen=True)
class AdminPipelineStepResult:
    monitor_analyzer: AdminMonitorAnalyzerStepResult
    executor_notifier: AdminExecutorNotifierStepResult


LlmOutputProvider = Callable[[AdminMonitorSnapshot, Mapping[str, Any]], Awaitable[str] | str]
CliOutputProvider = Callable[[AdminMonitorSnapshot, Mapping[str, Any]], Awaitable[str] | str]
DecisionCallback = Callable[[AdminMonitorAnalyzerStepResult], Awaitable[None] | None]
AskUserCallback = Callable[[str, list[str]], Awaitable[str] | str]


class AdminModeRunnerService:
    """Admin mode-owned monitor/analyzer runtime."""

    capabilities = frozenset({"run_admin_monitor_analyzer", "run_admin_executor_notifier", "run_admin_pipeline"})

    def __init__(
        self,
        config: Any,
        *,
        monitor: Optional[AdminMonitor] = None,
        analyzer: Optional[AdminAnalyzer] = None,
        executor: Optional[AdminExecutor] = None,
        notifier: Optional[AdminNotifier] = None,
        llm_output_provider: Optional[LlmOutputProvider] = None,
        cli_output_provider: Optional[CliOutputProvider] = None,
        cli_step_local_transport: Optional[LocalSubprocessTransport] = None,
        cli_step_ssh_transport: Optional[SSHSubprocessTransport] = None,
        cli_step_timeout_sec: float = 30.0,
    ) -> None:
        self._config = config
        self._monitor = monitor or AdminMonitor()
        self._analyzer = analyzer or AdminAnalyzer()
        self._executor = executor or AdminExecutor()
        self._notifier = notifier
        self._llm_output_provider = llm_output_provider
        self._cli_output_provider = cli_output_provider
        self._cli_step_local_transport = cli_step_local_transport or LocalSubprocessTransport()
        self._cli_step_ssh_transport = cli_step_ssh_transport or SSHSubprocessTransport()
        self._cli_step_timeout_sec = float(cli_step_timeout_sec or 30.0)

    def set_config(self, config: Any) -> None:
        self._config = config

    def ensure_notifier(self, *, state_store: Any) -> AdminNotifier:
        notifier = self._notifier
        if notifier is None:
            notifier = AdminNotifier(state_store=state_store)
            self._notifier = notifier
        return notifier

    def is_pipeline_ready(self) -> bool:
        return (
            self._monitor is not None
            and self._analyzer is not None
            and self._executor is not None
            and self._notifier is not None
        )

    async def run_monitor_analyzer_once(
        self,
        *,
        config_payload: Mapping[str, Any],
        session_workdir: str = "",
        llm_output: Optional[str] = None,
        cli_output: Optional[str] = None,
    ) -> AdminMonitorAnalyzerStepResult:
        monitor_config_payload = self._build_monitor_config_for_pinned_cli(
            config_payload=config_payload,
        )
        snapshot = await self._monitor.collect_snapshot(
            config_payload=monitor_config_payload,
            session_workdir=str(session_workdir or ""),
        )
        self._persist_monitor_snapshot_history(
            snapshot=snapshot,
            session_workdir=str(session_workdir or ""),
        )
        llm_raw_output = await self._resolve_llm_output(
            snapshot=snapshot,
            config_payload=config_payload,
            llm_output=llm_output,
        )
        cli_raw_output = await self._resolve_cli_output(
            snapshot=snapshot,
            config_payload=config_payload,
            cli_output=cli_output,
        )
        decision = await self._analyze_snapshot(
            snapshot=snapshot,
            llm_output=llm_raw_output,
            cli_output=cli_raw_output,
            admin_config=config_payload,
        )
        decision = await self._maybe_run_secondary_cli_feedback_loop(
            snapshot=snapshot,
            config_payload=config_payload,
            session_workdir=session_workdir,
            llm_raw_output=llm_raw_output,
            initial_cli_output=cli_raw_output,
            initial_decision=decision,
        )
        return AdminMonitorAnalyzerStepResult(snapshot=snapshot, decision=dict(decision or {}))

    def _persist_monitor_snapshot_history(
        self,
        *,
        snapshot: AdminMonitorSnapshot,
        session_workdir: str,
    ) -> None:
        workdir = str(session_workdir or "").strip()
        if not workdir:
            return
        for server in tuple(getattr(snapshot, "servers", ()) or ()):
            server_id = str(getattr(server, "server_id", "") or getattr(server, "action_id", "") or "").strip()
            if not server_id:
                continue
            check_id = str(getattr(server, "action_id", "") or server_id).strip()
            value = {
                "ok": bool(getattr(server, "ok", False)),
                "target": str(getattr(server, "target", "") or ""),
                "returncode": int(getattr(server, "returncode", 0) or 0),
                "timed_out": bool(getattr(server, "timed_out", False)),
                "duration_ms": int(getattr(server, "duration_ms", 0) or 0),
                "metrics": dict(getattr(server, "metrics", {}) or {}),
                "error": getattr(server, "error", None),
            }
            try:
                store = AdminSnapshotStore.for_server(workdir, server_id)
                store.insert_snapshot(
                    check_id=check_id,
                    value=value,
                    ts=int(float(getattr(server, "collected_at_ts", 0) or 0) or 0),
                )
            except Exception:
                _LOG.exception("admin runner snapshot history persist failed server_id=%s", server_id)

    async def run_monitor_analyzer_loop(
        self,
        *,
        config_payload: Mapping[str, Any],
        session_workdir: str = "",
        interval_sec: float = 30.0,
        max_iterations: Optional[int] = None,
        stop_event: Optional[asyncio.Event] = None,
        on_decision: Optional[DecisionCallback] = None,
        llm_output: Optional[str] = None,
        cli_output: Optional[str] = None,
    ) -> list[AdminMonitorAnalyzerStepResult]:
        interval = float(interval_sec)
        if interval <= 0:
            raise AdminModeRunnerServiceError("interval_sec must be > 0")
        if max_iterations is not None and int(max_iterations) <= 0:
            raise AdminModeRunnerServiceError("max_iterations must be > 0 when provided")

        results: list[AdminMonitorAnalyzerStepResult] = []
        iteration = 0
        while True:
            if stop_event is not None and stop_event.is_set():
                break

            step_result = await self.run_monitor_analyzer_once(
                config_payload=config_payload,
                session_workdir=session_workdir,
                llm_output=llm_output,
                cli_output=cli_output,
            )
            results.append(step_result)
            if on_decision is not None:
                await self._maybe_await(on_decision(step_result))

            iteration += 1
            if max_iterations is not None and iteration >= int(max_iterations):
                break

            if stop_event is None:
                await asyncio.sleep(interval)
                continue

            try:
                await asyncio.wait_for(stop_event.wait(), timeout=interval)
            except asyncio.TimeoutError:
                continue
            break
        return results

    async def run_executor_notifier_once(
        self,
        *,
        decision: Mapping[str, Any],
        session_id: str,
        chat_id: int,
        state_store: Any,
        messaging: Any,
        ask_user: Optional[AskUserCallback] = None,
        local_transport: Optional[LocalSubprocessTransport] = None,
        ssh_transport: Optional[SSHSubprocessTransport] = None,
        local_spec: Optional[LocalCommandSpec] = None,
        ssh_spec: Optional[SSHCommandSpec] = None,
        now_ts: Optional[float] = None,
    ) -> AdminExecutorNotifierStepResult:
        execution_result = await self._executor.apply_analyzer_decision(
            decision=dict(decision or {}),
            session_id=str(session_id or ""),
            chat_id=int(chat_id),
            state_store=state_store,
            local_transport=local_transport,
            ssh_transport=ssh_transport,
            local_spec=local_spec,
            ssh_spec=ssh_spec,
            ask_user=ask_user,
            now_ts=now_ts,
        )

        notifier = self.ensure_notifier(state_store=state_store)

        action_notification: Optional[AdminNotificationResult] = None
        incident_notification: Optional[AdminNotificationResult] = None

        action_row = None
        if execution_result.logged_action_id:
            action_row = state_store.get_action(str(execution_result.logged_action_id), chat_id=int(chat_id))
        if isinstance(action_row, Mapping):
            action_notification = await notifier.notify_action(
                session_id=str(session_id or ""),
                chat_id=int(chat_id),
                action_row=action_row,
                messaging=messaging,
                now_ts=now_ts,
            )

            incident_row = self._resolve_incident_row_from_action(
                state_store=state_store,
                action_row=action_row,
                chat_id=int(chat_id),
            )
            if isinstance(incident_row, Mapping):
                incident_notification = await notifier.notify_incident(
                    session_id=str(session_id or ""),
                    chat_id=int(chat_id),
                    incident_row=incident_row,
                    messaging=messaging,
                    now_ts=now_ts,
                )

        decision_payload = dict(decision or {})
        target_transport = ""
        if local_spec is not None:
            target_transport = "local"
        elif ssh_spec is not None:
            target_transport = "ssh"
        action_name = str(
            decision_payload.get("action")
            or (getattr(local_spec, "action_id", "") if local_spec is not None else "")
            or (getattr(ssh_spec, "action_id", "") if ssh_spec is not None else "")
            or ""
        ).strip().lower()
        server_id = str(
            decision_payload.get("server_id")
            or decision_payload.get("target_server_id")
            or ""
        ).strip()
        native_transport_execution = bool(local_spec is not None or ssh_spec is not None)
        return AdminExecutorNotifierStepResult(
            execution_result=execution_result,
            action_notification=action_notification,
            incident_notification=incident_notification,
            target_transport=target_transport,
            native_transport_execution=native_transport_execution,
            destructive_execution=bool(
                native_transport_execution and action_name not in {"", "notify_admin", "ask_user"}
            ),
            check_only=False,
            dry_run=False,
            action_id=str(
                (getattr(local_spec, "action_id", "") if local_spec is not None else "")
                or (getattr(ssh_spec, "action_id", "") if ssh_spec is not None else "")
                or action_name
                or ""
            ).strip(),
            server_id=server_id,
        )

    async def run_pipeline_once(
        self,
        *,
        config_payload: Mapping[str, Any],
        session_workdir: str,
        session_id: str,
        chat_id: int,
        state_store: Any,
        messaging: Any,
        ask_user: Optional[AskUserCallback] = None,
        local_transport: Optional[LocalSubprocessTransport] = None,
        ssh_transport: Optional[SSHSubprocessTransport] = None,
        local_spec: Optional[LocalCommandSpec] = None,
        ssh_spec: Optional[SSHCommandSpec] = None,
        llm_output: Optional[str] = None,
        cli_output: Optional[str] = None,
        now_ts: Optional[float] = None,
    ) -> AdminPipelineStepResult:
        monitor_analyzer = await self.run_monitor_analyzer_once(
            config_payload=config_payload,
            session_workdir=session_workdir,
            llm_output=llm_output,
            cli_output=cli_output,
        )
        effective_local_spec, effective_ssh_spec = self._resolve_pipeline_execution_specs(
            decision=monitor_analyzer.decision,
            config_payload=config_payload,
            session_workdir=session_workdir,
            local_spec=local_spec,
            ssh_spec=ssh_spec,
        )
        executor_notifier = await self.run_executor_notifier_once(
            decision=monitor_analyzer.decision,
            session_id=session_id,
            chat_id=chat_id,
            state_store=state_store,
            messaging=messaging,
            ask_user=ask_user,
            local_transport=local_transport,
            ssh_transport=ssh_transport,
            local_spec=effective_local_spec,
            ssh_spec=effective_ssh_spec,
            now_ts=now_ts,
        )
        return AdminPipelineStepResult(
            monitor_analyzer=monitor_analyzer,
            executor_notifier=executor_notifier,
        )

    async def _resolve_llm_output(
        self,
        *,
        snapshot: AdminMonitorSnapshot,
        config_payload: Mapping[str, Any],
        llm_output: Optional[str],
    ) -> str:
        explicit_output = str(llm_output or "")
        if explicit_output:
            return explicit_output
        if self._llm_output_provider is None:
            return ""
        provided = self._llm_output_provider(snapshot, dict(config_payload or {}))
        if inspect.isawaitable(provided):
            provided = await provided
        return str(provided or "")

    async def _analyze_snapshot(
        self,
        *,
        snapshot: AdminMonitorSnapshot,
        llm_output: str,
        cli_output: str,
        admin_config: Mapping[str, Any],
    ) -> Dict[str, Any]:
        analyze = getattr(self._analyzer, "analyze")
        kwargs: Dict[str, Any] = {
            "snapshot": snapshot,
            "llm_output": llm_output,
            "cli_output": cli_output,
        }
        try:
            signature = inspect.signature(analyze)
        except (TypeError, ValueError):
            signature = None
        if signature is not None:
            parameters = signature.parameters.values()
            if "admin_config" in signature.parameters or any(
                parameter.kind == inspect.Parameter.VAR_KEYWORD for parameter in parameters
            ):
                kwargs["admin_config"] = admin_config
        try:
            result = analyze(**kwargs)
        except TypeError:
            if "admin_config" not in kwargs:
                raise
            kwargs.pop("admin_config", None)
            result = analyze(**kwargs)
        if inspect.isawaitable(result):
            result = await result
        return dict(result or {})

    async def _resolve_cli_output(
        self,
        *,
        snapshot: AdminMonitorSnapshot,
        config_payload: Mapping[str, Any],
        cli_output: Optional[str],
    ) -> str:
        explicit_output = str(cli_output or "")
        if explicit_output:
            return explicit_output
        if self._cli_output_provider is None:
            return ""
        provided = self._cli_output_provider(snapshot, dict(config_payload or {}))
        if inspect.isawaitable(provided):
            provided = await provided
        return str(provided or "")

    async def _maybe_run_secondary_cli_feedback_loop(
        self,
        *,
        snapshot: AdminMonitorSnapshot,
        config_payload: Mapping[str, Any],
        session_workdir: str,
        llm_raw_output: str,
        initial_cli_output: str,
        initial_decision: Mapping[str, Any],
    ) -> Dict[str, Any]:
        decision = dict(initial_decision or {})
        if not decision:
            return {}
        if str(initial_cli_output or "").strip():
            return decision

        secondary_cli_command = str(decision.get("secondary_cli_command") or "").strip()
        if not secondary_cli_command:
            return decision

        pinned_cli = self._resolve_pinned_cli(config_payload)
        if not pinned_cli:
            return decision
        if self._secondary_cli_requires_internet(secondary_cli_command) and not self._allow_internet_secondary_cli(config_payload):
            final_cli_output = self._serialize_secondary_cli_feedback(
                {
                    "command": secondary_cli_command,
                    "transport": self._resolve_cli_step_transport_kind(pinned_cli),
                    "stdout": "",
                    "stderr": "internet_secondary_cli_blocked_by_policy",
                    "returncode": -1,
                    "timed_out": False,
                }
            )
            final_decision = await self._analyze_snapshot(
                snapshot=snapshot,
                llm_output=llm_raw_output,
                cli_output=final_cli_output,
                admin_config=config_payload,
            )
            return dict(final_decision or {})
        feedback = await self._execute_secondary_cli_command(
            command=secondary_cli_command,
            pinned_cli=pinned_cli,
            session_workdir=session_workdir,
        )
        final_cli_output = await self._resolve_secondary_cli_feedback_output(
            snapshot=snapshot,
            config_payload=config_payload,
            feedback=feedback,
        )
        final_decision = await self._analyze_snapshot(
            snapshot=snapshot,
            llm_output=llm_raw_output,
            cli_output=final_cli_output,
            admin_config=config_payload,
        )
        return dict(final_decision or {})

    async def _resolve_secondary_cli_feedback_output(
        self,
        *,
        snapshot: AdminMonitorSnapshot,
        config_payload: Mapping[str, Any],
        feedback: Mapping[str, Any],
    ) -> str:
        fallback_output = self._serialize_secondary_cli_feedback(feedback)
        if self._cli_output_provider is None:
            return fallback_output

        provider_payload = self._build_secondary_cli_provider_payload(
            config_payload=config_payload,
            feedback=feedback,
        )
        provided = self._cli_output_provider(snapshot, provider_payload)
        if inspect.isawaitable(provided):
            provided = await provided
        provided_text = str(provided or "").strip()
        if provided_text:
            return provided_text
        return fallback_output

    async def _execute_secondary_cli_command(
        self,
        *,
        command: str,
        pinned_cli: Mapping[str, Any],
        session_workdir: str,
    ) -> Dict[str, Any]:
        transport = self._resolve_cli_step_transport_kind(pinned_cli)
        try:
            if transport == "ssh":
                result = await self._cli_step_ssh_transport.run(
                    self._build_secondary_cli_ssh_spec(
                        command=command,
                        pinned_cli=pinned_cli,
                        session_workdir=session_workdir,
                    )
                )
            else:
                result = await self._cli_step_local_transport.run(
                    self._build_secondary_cli_local_spec(
                        command=command,
                        session_workdir=session_workdir,
                    )
                )
            return {
                "command": str(command or "").strip(),
                "transport": transport,
                "stdout": str(getattr(result, "stdout", "") or ""),
                "stderr": str(getattr(result, "stderr", "") or ""),
                "returncode": int(getattr(result, "returncode", -1)),
                "timed_out": bool(getattr(result, "timed_out", False)),
            }
        except (ValueError, LocalTransportError, SSHTransportError) as exc:
            _LOG.exception("admin secondary cli step failed transport=%s command=%s", transport, command)
            return {
                "command": str(command or "").strip(),
                "transport": transport,
                "stdout": "",
                "stderr": str(exc),
                "returncode": -1,
                "timed_out": False,
            }

    def _build_secondary_cli_local_spec(
        self,
        *,
        command: str,
        session_workdir: str,
    ) -> LocalCommandSpec:
        cwd = str(session_workdir or "").strip()
        return LocalCommandSpec(
            action_id=_SECONDARY_CLI_ACTION_ID,
            argv=("bash", "-lc", str(command or "").strip()),
            cwd=cwd if cwd and os.path.isdir(cwd) else None,
            timeout_sec=self._cli_step_timeout_sec,
        )

    def _build_secondary_cli_ssh_spec(
        self,
        *,
        command: str,
        pinned_cli: Mapping[str, Any],
        session_workdir: str,
    ) -> SSHCommandSpec:
        return self._build_pinned_ssh_spec(
            action_id=_SECONDARY_CLI_ACTION_ID,
            argv=("bash", "-lc", str(command or "").strip()),
            timeout_sec=self._cli_step_timeout_sec,
            pinned_cli=pinned_cli,
            session_workdir=session_workdir,
        )

    @staticmethod
    def _serialize_secondary_cli_feedback(feedback: Mapping[str, Any]) -> str:
        return json.dumps(
            {"secondary_cli_feedback": dict(feedback or {})},
            ensure_ascii=False,
            sort_keys=True,
        )

    @staticmethod
    def _build_secondary_cli_provider_payload(
        *,
        config_payload: Mapping[str, Any],
        feedback: Mapping[str, Any],
    ) -> Dict[str, Any]:
        payload = dict(config_payload or {})
        admin_cfg = payload.get("admin", {})
        admin_copy = dict(admin_cfg) if isinstance(admin_cfg, Mapping) else {}
        runtime_cfg = admin_copy.get("runtime", {})
        runtime_copy = dict(runtime_cfg) if isinstance(runtime_cfg, Mapping) else {}
        runtime_copy["secondary_cli_feedback"] = dict(feedback or {})
        admin_copy["runtime"] = runtime_copy
        payload["admin"] = admin_copy
        return payload

    @staticmethod
    def _allow_internet_secondary_cli(config_payload: Mapping[str, Any]) -> bool:
        admin_cfg = config_payload.get("admin", {}) if isinstance(config_payload, Mapping) else {}
        policies_cfg = admin_cfg.get("policies", {}) if isinstance(admin_cfg, Mapping) else {}
        analyzer_cfg = policies_cfg.get("analyzer", {}) if isinstance(policies_cfg, Mapping) else {}
        return bool(analyzer_cfg.get("allow_internet_secondary_cli", False)) if isinstance(analyzer_cfg, Mapping) else False

    @staticmethod
    def _secondary_cli_requires_internet(command: str) -> bool:
        normalized = str(command or "").strip().lower()
        if not normalized:
            return False
        internet_markers = (
            "http://",
            "https://",
            "curl ",
            "wget ",
            "lynx ",
            "links ",
            "dig ",
            "nslookup ",
            "host ",
        )
        return any(marker in normalized for marker in internet_markers)

    def _build_monitor_config_for_pinned_cli(
        self,
        *,
        config_payload: Mapping[str, Any],
    ) -> Dict[str, Any]:
        payload = copy.deepcopy(dict(config_payload or {}))
        pinned_cli = self._resolve_pinned_cli(payload)
        transport = self._resolve_pipeline_transport_kind(pinned_cli)
        if transport is None:
            return payload
        if transport == "ssh" and not self._has_ssh_pinned_cli_credentials(pinned_cli):
            _LOG.warning("admin runner skipped pinned ssh monitor rewrite because pinned_cli is incomplete")
            return payload

        admin_cfg = payload.get("admin", {})
        if not isinstance(admin_cfg, Mapping):
            return payload
        monitor_cfg = admin_cfg.get("monitor", {})
        if not isinstance(monitor_cfg, Mapping):
            return payload
        raw_servers = monitor_cfg.get("servers", ())
        if not isinstance(raw_servers, (list, tuple)):
            return payload

        rewritten_servers: list[Any] = []
        actions_cfg = admin_cfg.get("actions", {})
        actions_copy = dict(actions_cfg) if isinstance(actions_cfg, Mapping) else {}
        target_actions = actions_copy.get(transport, {})
        target_actions_copy = dict(target_actions) if isinstance(target_actions, Mapping) else {}
        for item in raw_servers:
            if not isinstance(item, Mapping):
                rewritten_servers.append(item)
                continue
            server = dict(item)
            original_target = str(server.get("target") or "").strip().lower()
            action_id = str(server.get("action_id") or "").strip()
            server["target"] = transport
            if action_id:
                action_payload = self._resolve_pipeline_action_payload(
                    config_payload=config_payload,
                    action_id=action_id,
                    preferred_target=transport,
                    original_target=original_target,
                )
                if action_payload is not None:
                    target_actions_copy[action_id] = self._coerce_action_payload_for_target(
                        action_payload=action_payload,
                        target=transport,
                        pinned_cli=pinned_cli,
                    )
            rewritten_servers.append(server)

        admin_copy = dict(admin_cfg)
        monitor_copy = dict(monitor_cfg)
        monitor_copy["servers"] = rewritten_servers
        actions_copy[transport] = target_actions_copy
        admin_copy["monitor"] = monitor_copy
        admin_copy["actions"] = actions_copy
        payload["admin"] = admin_copy
        return payload

    def _resolve_pipeline_execution_specs(
        self,
        *,
        decision: Mapping[str, Any],
        config_payload: Mapping[str, Any],
        session_workdir: str,
        local_spec: Optional[LocalCommandSpec],
        ssh_spec: Optional[SSHCommandSpec],
    ) -> tuple[Optional[LocalCommandSpec], Optional[SSHCommandSpec]]:
        pinned_cli = self._resolve_pinned_cli(config_payload)
        transport = self._resolve_pipeline_transport_kind(pinned_cli)
        if transport is None:
            return local_spec, ssh_spec
        if transport == "ssh" and not self._has_ssh_pinned_cli_credentials(pinned_cli):
            _LOG.warning("admin runner skipped pinned ssh execution rewrite because pinned_cli is incomplete")
            return local_spec, ssh_spec

        if transport == "local":
            if local_spec is not None:
                return local_spec, None
            if ssh_spec is not None:
                return self._build_local_spec_from_ssh_spec(ssh_spec), None
        else:
            if ssh_spec is not None:
                return None, self._build_pinned_ssh_spec(
                    action_id=str(ssh_spec.action_id or ""),
                    argv=tuple(ssh_spec.argv or ()),
                    timeout_sec=float(ssh_spec.timeout_sec),
                    pinned_cli=pinned_cli,
                    session_workdir=session_workdir,
                    default_options=tuple(ssh_spec.options or ()),
                )
            if local_spec is not None:
                return None, self._build_pinned_ssh_spec(
                    action_id=str(local_spec.action_id or ""),
                    argv=tuple(local_spec.argv or ()),
                    timeout_sec=float(local_spec.timeout_sec),
                    pinned_cli=pinned_cli,
                    session_workdir=session_workdir,
                )

        action_id = str(decision.get("action") or "").strip().lower()
        if not action_id:
            return local_spec, ssh_spec
        action_payload = self._resolve_pipeline_action_payload(
            config_payload=config_payload,
            action_id=action_id,
            preferred_target=transport,
        )
        if action_payload is None:
            return local_spec, ssh_spec

        try:
            if transport == "local":
                return (
                    AdminMonitor._build_local_command_spec(
                        action_id=action_id,
                        action_payload=self._coerce_action_payload_for_target(
                            action_payload=action_payload,
                            target=transport,
                            pinned_cli=pinned_cli,
                        ),
                        session_workdir=session_workdir,
                        timeout_override_sec=None,
                    ),
                    None,
                )
            return (
                None,
                AdminMonitor._build_ssh_command_spec(
                    action_id=action_id,
                    action_payload=self._coerce_action_payload_for_target(
                        action_payload=action_payload,
                        target=transport,
                        pinned_cli=pinned_cli,
                    ),
                    session_workdir=session_workdir,
                    timeout_override_sec=None,
                ),
            )
        except Exception:
            _LOG.exception(
                "admin runner failed to derive pinned execution spec action_id=%s transport=%s",
                action_id,
                transport,
            )
            return local_spec, ssh_spec

    @staticmethod
    def _resolve_pipeline_action_payload(
        *,
        config_payload: Mapping[str, Any],
        action_id: str,
        preferred_target: str,
        original_target: str = "",
    ) -> Any:
        candidates: list[str] = []
        for item in (preferred_target, original_target, "local", "ssh"):
            target = str(item or "").strip().lower()
            if target in {"local", "ssh"} and target not in candidates:
                candidates.append(target)
        for target in candidates:
            payload = AdminMonitor._resolve_exec_action_payload(
                config_payload=config_payload,
                target=target,
                action_id=action_id,
            )
            if payload is not None:
                return payload
        return None

    @staticmethod
    def _coerce_action_payload_for_target(
        *,
        action_payload: Any,
        target: str,
        pinned_cli: Mapping[str, Any],
    ) -> Any:
        if target == "local":
            return copy.deepcopy(action_payload)

        if isinstance(action_payload, Mapping):
            payload = copy.deepcopy(dict(action_payload))
        elif isinstance(action_payload, (list, tuple)):
            payload = {"argv": [str(item) for item in action_payload if str(item).strip()]}
        else:
            return action_payload

        payload["host"] = str(pinned_cli.get("host") or "").strip()
        payload["key_path"] = str(pinned_cli.get("key_path") or "").strip()
        password_env = str(pinned_cli.get("password_env") or "").strip()
        if password_env:
            payload["password_env"] = password_env
        if pinned_cli.get("password"):
            payload["password"] = str(pinned_cli.get("password") or "")
        user = str(pinned_cli.get("user") or "").strip()
        if user:
            payload["user"] = user
        elif "user" in payload:
            payload.pop("user", None)
        if pinned_cli.get("port") is not None:
            payload["port"] = pinned_cli.get("port")
        if isinstance(pinned_cli.get("options"), (list, tuple)):
            payload["options"] = [str(item).strip() for item in pinned_cli.get("options", ()) if str(item).strip()]
        return payload

    @staticmethod
    def _build_local_spec_from_ssh_spec(spec: SSHCommandSpec) -> LocalCommandSpec:
        return LocalCommandSpec(
            action_id=str(spec.action_id or "").strip(),
            argv=tuple(spec.argv or ()),
            timeout_sec=float(spec.timeout_sec),
        )

    @staticmethod
    def _has_ssh_pinned_cli_credentials(pinned_cli: Mapping[str, Any]) -> bool:
        return bool(
            str(pinned_cli.get("host") or "").strip()
            and (
                str(pinned_cli.get("key_path") or "").strip()
                or str(pinned_cli.get("password") or "")
                or str(pinned_cli.get("password_env") or "").strip()
            )
        )

    @staticmethod
    def _resolve_pipeline_transport_kind(pinned_cli: Mapping[str, Any]) -> Optional[str]:
        if not pinned_cli:
            return None
        raw_target = str(
            pinned_cli.get("target")
            or pinned_cli.get("transport")
            or ""
        ).strip().lower()
        if raw_target in {"local", "ssh"}:
            return raw_target
        if AdminModeRunnerService._has_ssh_pinned_cli_credentials(pinned_cli):
            return "ssh"
        return None

    @staticmethod
    def _build_pinned_ssh_spec(
        *,
        action_id: str,
        argv: tuple[str, ...],
        timeout_sec: float,
        pinned_cli: Mapping[str, Any],
        session_workdir: str = "",
        default_options: tuple[str, ...] = (),
    ) -> SSHCommandSpec:
        host = str(pinned_cli.get("host") or "").strip()
        key_path = str(pinned_cli.get("key_path") or "").strip()
        workdir = str(session_workdir or "").strip()
        password = str(pinned_cli.get("password") or "")
        password_env = str(pinned_cli.get("password_env") or "").strip()
        if not password and password_env and workdir:
            from app.services.ssh_config_loader import load_ssh_secrets, resolve_ssh_secret

            password = str(resolve_ssh_secret(load_ssh_secrets(workdir), password_env) or "")
        if not host or (not key_path and not password):
            raise ValueError("ssh pinned_cli requires host and key_path/password_env")
        if workdir and key_path and not key_path.startswith("/"):
            key_path = os.path.abspath(os.path.join(workdir, key_path))

        raw_port = pinned_cli.get("port", 22)
        port = int(raw_port or 22)
        options_raw = pinned_cli.get("options", default_options)
        options = ()
        if isinstance(options_raw, (list, tuple)):
            options = tuple(str(item).strip() for item in options_raw if str(item).strip())

        return SSHCommandSpec(
            action_id=str(action_id or "").strip(),
            host=host,
            user=str(pinned_cli.get("user") or "").strip() or None,
            port=port,
            key_path=key_path,
            argv=tuple(str(item) for item in (argv or ()) if str(item).strip()),
            timeout_sec=float(timeout_sec),
            options=options,
            password=password or None,
        )

    @staticmethod
    def _resolve_pinned_cli(config_payload: Mapping[str, Any]) -> Dict[str, Any]:
        admin_cfg = config_payload.get("admin", {}) if isinstance(config_payload, Mapping) else {}
        runtime_cfg = admin_cfg.get("runtime", {}) if isinstance(admin_cfg, Mapping) else {}
        pinned_cli = runtime_cfg.get("pinned_cli", {}) if isinstance(runtime_cfg, Mapping) else {}
        runtime_profile = dict(pinned_cli) if isinstance(pinned_cli, Mapping) else {}
        if AdminModeRunnerService._profile_has_explicit_target(runtime_profile):
            return runtime_profile
        for candidate in AdminModeRunnerService._generated_pinned_cli_candidates(admin_cfg):
            profile = dict(candidate)
            if not profile:
                continue
            if runtime_profile.get("name") and not profile.get("name"):
                profile["name"] = runtime_profile.get("name")
            if AdminModeRunnerService._profile_has_explicit_target(profile):
                return profile
        if isinstance(pinned_cli, Mapping):
            return dict(pinned_cli)
        return {}

    @staticmethod
    def _generated_pinned_cli_candidates(admin_cfg: Any) -> list[Mapping[str, Any]]:
        if not isinstance(admin_cfg, Mapping):
            return []
        candidates: list[Mapping[str, Any]] = []
        environment_cfg = admin_cfg.get("environment", {})
        environment_pinned = environment_cfg.get("pinned_cli") if isinstance(environment_cfg, Mapping) else None
        if isinstance(environment_pinned, Mapping):
            candidates.append(environment_pinned)
        generated_cfg = admin_cfg.get("generated", {})
        generated_environment = generated_cfg.get("environment", {}) if isinstance(generated_cfg, Mapping) else {}
        generated_pinned = (
            generated_environment.get("pinned_cli")
            if isinstance(generated_environment, Mapping)
            else None
        )
        if isinstance(generated_pinned, Mapping):
            candidates.append(generated_pinned)
        return candidates

    @staticmethod
    def _profile_has_explicit_target(profile: Mapping[str, Any]) -> bool:
        target = str(profile.get("target") or profile.get("transport") or "").strip().lower()
        if target == "local":
            return True
        if target == "ssh":
            return AdminModeRunnerService._has_ssh_pinned_cli_credentials(profile)
        return AdminModeRunnerService._has_ssh_pinned_cli_credentials(profile)

    @staticmethod
    def _resolve_cli_step_transport_kind(pinned_cli: Mapping[str, Any]) -> str:
        raw_target = str(
            pinned_cli.get("target")
            or pinned_cli.get("transport")
            or ""
        ).strip().lower()
        if raw_target == "ssh":
            return "ssh"
        if pinned_cli.get("host") and (
            pinned_cli.get("key_path") or pinned_cli.get("password") or pinned_cli.get("password_env")
        ):
            return "ssh"
        return "local"

    @staticmethod
    async def _maybe_await(value: Any) -> None:
        if inspect.isawaitable(value):
            await value

    @staticmethod
    def _resolve_incident_row_from_action(
        *,
        state_store: Any,
        action_row: Mapping[str, Any],
        chat_id: int,
    ) -> Optional[Dict[str, Any]]:
        payload = action_row.get("payload")
        if not isinstance(payload, Mapping):
            return None
        refs = payload.get("incident_refs")
        if not isinstance(refs, Mapping):
            return None
        incident_id = str(refs.get("incident_id") or "").strip()
        if not incident_id:
            return None
        row = state_store.get_incident(incident_id, chat_id=int(chat_id))
        if isinstance(row, Mapping):
            return dict(row)
        return None

    def supports_capability(self, capability: str) -> bool:
        return str(capability or "").strip() in self.capabilities


__all__ = [
    "AdminModeRunnerService",
    "AdminModeRunnerServiceError",
    "AdminMonitorAnalyzerStepResult",
    "AdminExecutorNotifierStepResult",
    "AdminPipelineStepResult",
]
