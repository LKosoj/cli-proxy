from __future__ import annotations

import json
import logging
import os
import re
import shlex
from dataclasses import dataclass
from typing import Any, Dict, Iterable, Mapping, Optional

import yaml

from modes.sdk.runtime.json_normalizer import loads_safe, parse_normalize_validate
from .schemas import AdminAnalyzerDecisionSchema

_LOG = logging.getLogger(__name__)
_SAFE_ANALYZER_ACTIONS = {"notify_admin", "ask_user", "no_action", "clear_logs", "clear_tmp", "clear_cache"}

_DEFAULT_LLM_FALLBACK_SYSTEM_PROMPT = (
    "You are Admin Analyzer fallback. Analyze infrastructure snapshot and return only JSON."
)
_DEFAULT_LLM_JSON_CONTRACT_PROMPT = (
    "Return strictly one JSON object with fields: diagnosis, confidence, action, reason, urgency, secondary_cli_command. "
    "Optional fields: server_id, incident_type, risk_level, evidence, suggested_runbook_ids. "
    "Allowed confidence values: high, medium, low. "
    "Action must be either a safe fallback action (`notify_admin` or `ask_user`) "
    "or a remediation action id explicitly present in the session-scoped admin config. "
    "Allowed urgency values: critical, warning, info. "
    "secondary_cli_command is optional and, when present, must be a shell command string for a follow-up diagnostic step. "
    "If confidence is low because the snapshot is insufficient for a safe "
    "service-specific diagnosis, keep action=notify_admin and populate "
    "secondary_cli_command. "
    "If local facts are still insufficient and the issue needs external context, "
    "the secondary CLI step may use the internet only when policy allows it. "
    "If the internet is unavailable, do not invent a fix: prefer notify_admin or ask_user and explain why. "
    "Do not add extra keys or markdown."
)


@dataclass(frozen=True)
class AdminAnalyzerDecision:
    diagnosis: str
    confidence: str
    action: str
    reason: str
    urgency: str
    secondary_cli_command: Optional[str] = None
    server_id: Optional[str] = None
    incident_type: Optional[str] = None
    risk_level: Optional[str] = None
    evidence: Optional[Iterable[Mapping[str, Any]]] = None
    suggested_runbook_ids: Optional[Iterable[str]] = None

    def as_dict(self) -> Dict[str, Any]:
        payload = {
            "diagnosis": str(self.diagnosis or ""),
            "confidence": str(self.confidence or ""),
            "action": str(self.action or ""),
            "reason": str(self.reason or ""),
            "urgency": str(self.urgency or ""),
        }
        secondary_cli_command = str(self.secondary_cli_command or "").strip()
        if secondary_cli_command:
            payload["secondary_cli_command"] = secondary_cli_command
        server_id = str(self.server_id or "").strip()
        if server_id:
            payload["server_id"] = server_id
        incident_type = str(self.incident_type or "").strip()
        if incident_type:
            payload["incident_type"] = incident_type
        risk_level = str(self.risk_level or "").strip().lower()
        if risk_level:
            payload["risk_level"] = risk_level
        evidence = []
        for item in list(self.evidence or []):
            if isinstance(item, Mapping):
                evidence.append({str(k): v for k, v in item.items()})
            else:
                evidence.append({"value": str(item)})
        if evidence:
            payload["evidence"] = evidence
        suggested_runbook_ids = [
            str(item or "").strip()
            for item in list(self.suggested_runbook_ids or [])
            if str(item or "").strip()
        ]
        if suggested_runbook_ids:
            payload["suggested_runbook_ids"] = suggested_runbook_ids
        return payload


class AdminAnalyzer:
    def __init__(self, *, prompts_path: Optional[str] = None) -> None:
        self._prompts_path = str(
            prompts_path or os.path.join(os.path.dirname(__file__), "prompts.yaml")
        )
        self._prompts: Optional[Dict[str, str]] = None
        self._last_llm_prompt: str = ""

    @property
    def last_llm_prompt(self) -> str:
        return str(self._last_llm_prompt or "")

    def analyze(
        self,
        *,
        snapshot: Any,
        llm_output: str,
        cli_output: str = "",
        admin_config: Optional[Mapping[str, Any]] = None,
    ) -> Dict[str, Any]:
        has_llm_output = bool(str(llm_output or "").strip())
        rule_decision = self._apply_rule_engine(
            snapshot=snapshot,
            admin_config=admin_config,
            allow_healthy_no_action=not has_llm_output,
            allow_unhealthy_fallback=not has_llm_output,
        )
        if rule_decision is not None:
            primary = rule_decision
        else:
            self._last_llm_prompt = self.build_llm_fallback_prompt(snapshot=snapshot, admin_config=admin_config)
            primary = self.analyze_llm_output(llm_output, admin_config=admin_config)
            if self._is_parse_fallback_decision(primary):
                fallback_rule = self._apply_rule_engine(
                    snapshot=snapshot,
                    admin_config=admin_config,
                    allow_healthy_no_action=False,
                    allow_unhealthy_fallback=True,
                )
                if fallback_rule is not None:
                    primary = fallback_rule
        primary = (
            self._apply_rule_engine(
                snapshot=snapshot,
                primary_decision=primary,
                admin_config=admin_config,
                allow_unhealthy_fallback=False,
            )
            or dict(primary or {})
        )
        return self._apply_cli_post_analysis(
            primary_decision=primary,
            raw_cli_output=cli_output,
            admin_config=admin_config,
        )

    def analyze_llm_output(
        self,
        raw_output: str,
        *,
        admin_config: Optional[Mapping[str, Any]] = None,
    ) -> Dict[str, Any]:
        try:
            payload = parse_normalize_validate(
                str(raw_output or ""),
                AdminAnalyzerDecisionSchema,
                strict_json_document=True,
            )
            return self._normalize_decision(payload, admin_config=admin_config)
        except Exception:
            _LOG.exception("admin analyzer parse/normalize failed")
            return self._fallback_decision()

    def build_llm_fallback_prompt(
        self,
        *,
        snapshot: Any,
        admin_config: Optional[Mapping[str, Any]] = None,
    ) -> str:
        prompts = self._load_prompts()
        system_prompt = str(
            prompts.get("llm_fallback_system") or _DEFAULT_LLM_FALLBACK_SYSTEM_PROMPT
        ).strip()
        contract_prompt = str(
            prompts.get("llm_json_contract") or _DEFAULT_LLM_JSON_CONTRACT_PROMPT
        ).strip()
        snapshot_prefix = str(prompts.get("llm_snapshot_prefix") or "Snapshot JSON:").strip()
        available_action_ids = self._discover_candidate_action_ids(admin_config)
        if available_action_ids:
            contract_prompt = (
                f"{contract_prompt}\n\n"
                "Available remediation action ids from this session config: "
                f"{', '.join(available_action_ids)}."
            )
        environment_services = sorted(self._resolve_environment_services(admin_config).keys())
        if environment_services:
            contract_prompt = (
                f"{contract_prompt}\n"
                "Detected environment services for this session: "
                f"{', '.join(environment_services)}."
            )
        policy = self._resolve_analyzer_policy(admin_config)
        if policy:
            contract_prompt = (
                f"{contract_prompt}\n"
                "Analyzer secondary CLI policy: "
                f"{json.dumps(self._to_json_safe(policy), ensure_ascii=False, sort_keys=True)}"
            )
        snapshot_json = self._serialize_snapshot(snapshot)
        return f"{system_prompt}\n\n{contract_prompt}\n\n{snapshot_prefix}\n{snapshot_json}".strip()

    def _apply_cli_post_analysis(
        self,
        *,
        primary_decision: Mapping[str, Any],
        raw_cli_output: str,
        admin_config: Optional[Mapping[str, Any]] = None,
    ) -> Dict[str, Any]:
        raw = str(raw_cli_output or "").strip()
        if not raw:
            return dict(primary_decision or {})
        cli_decision = self._try_parse_cli_decision(raw_output=raw, admin_config=admin_config)
        primary_confidence = str(primary_decision.get("confidence") or "").strip().lower()
        primary_action = str(primary_decision.get("action") or "").strip().lower()
        if primary_confidence != "low" and primary_action != "notify_admin":
            return dict(primary_decision or {})
        if cli_decision is None:
            feedback_decision = self._try_finalize_from_cli_feedback(
                primary_decision=primary_decision,
                raw_output=raw,
            )
            if feedback_decision is None:
                return dict(primary_decision or {})
            return feedback_decision

        merged = dict(cli_decision)
        cli_reason = str(merged.get("reason") or "").strip()
        merged["reason"] = f"cli_post_analysis:{cli_reason}" if cli_reason else "cli_post_analysis"
        return merged

    def _try_parse_cli_decision(
        self,
        *,
        raw_output: str,
        admin_config: Optional[Mapping[str, Any]] = None,
    ) -> Optional[Dict[str, Any]]:
        try:
            envelope = loads_safe(str(raw_output or ""), strict_first=True)
        except Exception:
            envelope = None
        if isinstance(envelope, Mapping) and isinstance(envelope.get("secondary_cli_feedback"), Mapping):
            return None
        try:
            payload = parse_normalize_validate(
                str(raw_output or ""),
                AdminAnalyzerDecisionSchema,
                strict_json_document=True,
            )
            return self._normalize_decision(payload, admin_config=admin_config)
        except Exception:
            _LOG.exception("admin analyzer cli post-analysis parse failed")
            return None

    def _try_finalize_from_cli_feedback(
        self,
        *,
        primary_decision: Mapping[str, Any],
        raw_output: str,
    ) -> Optional[Dict[str, Any]]:
        try:
            payload = loads_safe(str(raw_output or ""), strict_first=True)
        except Exception:
            return None
        if not isinstance(payload, Mapping):
            return None
        feedback = payload.get("secondary_cli_feedback")
        if not isinstance(feedback, Mapping):
            return None

        timed_out = bool(feedback.get("timed_out", False))
        stdout = str(feedback.get("stdout") or "").strip()
        stderr = str(feedback.get("stderr") or "").strip()
        try:
            returncode = int(feedback.get("returncode", -1))
        except Exception:
            returncode = -1

        reason = "cli_post_analysis:diagnostic_output_captured"
        confidence = "medium"
        if timed_out:
            reason = "cli_post_analysis:diagnostic_step_timed_out"
            confidence = "low"
        elif returncode != 0:
            reason = "cli_post_analysis:diagnostic_step_failed"
            confidence = "low"
        elif not stdout and stderr:
            confidence = "low"

        return AdminAnalyzerDecision(
            diagnosis=str(primary_decision.get("diagnosis") or "secondary_cli_feedback").strip(),
            confidence=confidence,
            action="notify_admin",
            reason=reason,
            urgency=str(primary_decision.get("urgency") or "warning").strip().lower() or "warning",
            server_id=str(primary_decision.get("server_id") or "").strip() or None,
            incident_type=str(primary_decision.get("incident_type") or "").strip() or None,
            risk_level=str(primary_decision.get("risk_level") or "").strip() or None,
            evidence=primary_decision.get("evidence") if isinstance(primary_decision.get("evidence"), list) else None,
            suggested_runbook_ids=(
                primary_decision.get("suggested_runbook_ids")
                if isinstance(primary_decision.get("suggested_runbook_ids"), list)
                else None
            ),
        ).as_dict()

    def _load_prompts(self) -> Dict[str, str]:
        if self._prompts is not None:
            return self._prompts
        loaded_prompts: Dict[str, str] = {}
        try:
            with open(self._prompts_path, "r", encoding="utf-8") as fh:
                raw = yaml.safe_load(fh) or {}
            prompts = raw.get("prompts") if isinstance(raw, Mapping) else {}
            if isinstance(prompts, Mapping):
                loaded_prompts = {str(k): str(v) for k, v in prompts.items()}
        except Exception:
            _LOG.exception("admin analyzer prompts read failed: %s", self._prompts_path)
        self._prompts = loaded_prompts
        return loaded_prompts

    def _serialize_snapshot(self, snapshot: Any) -> str:
        servers = [self._to_json_safe(dict(entry)) for entry in self._iter_server_entries(snapshot)]
        payload = {"servers": servers}
        try:
            return json.dumps(payload, ensure_ascii=False, sort_keys=True)
        except Exception:
            _LOG.exception("admin analyzer snapshot serialization failed")
            return '{"servers":[]}'

    def _normalize_decision(
        self,
        payload: Dict[str, Any],
        *,
        admin_config: Optional[Mapping[str, Any]] = None,
    ) -> Dict[str, Any]:
        decision = AdminAnalyzerDecision(
            diagnosis=str(payload.get("diagnosis") or "").strip(),
            confidence=str(payload.get("confidence") or "").strip().lower(),
            action=str(payload.get("action") or "").strip().lower(),
            reason=str(payload.get("reason") or "").strip(),
            urgency=str(payload.get("urgency") or "").strip().lower(),
            secondary_cli_command=str(payload.get("secondary_cli_command") or "").strip() or None,
            server_id=str(payload.get("server_id") or "").strip() or None,
            incident_type=str(payload.get("incident_type") or "").strip() or None,
            risk_level=str(payload.get("risk_level") or "").strip().lower() or None,
            evidence=payload.get("evidence") if isinstance(payload.get("evidence"), list) else None,
            suggested_runbook_ids=(
                payload.get("suggested_runbook_ids")
                if isinstance(payload.get("suggested_runbook_ids"), list)
                else None
            ),
        )
        if not self._is_action_allowed(decision.action, admin_config=admin_config):
            return self._fallback_decision()
        return decision.as_dict()

    @staticmethod
    def _fallback_decision() -> Dict[str, Any]:
        return AdminAnalyzerDecision(
            diagnosis="unable_to_parse_llm_response",
            confidence="low",
            action="notify_admin",
            reason="invalid_json_or_schema",
            urgency="warning",
        ).as_dict()

    @staticmethod
    def _is_parse_fallback_decision(decision: Mapping[str, Any]) -> bool:
        return (
            str(decision.get("diagnosis") or "").strip() == "unable_to_parse_llm_response"
            and str(decision.get("reason") or "").strip() == "invalid_json_or_schema"
        )

    def _apply_rule_engine(
        self,
        *,
        snapshot: Any,
        primary_decision: Optional[Mapping[str, Any]] = None,
        admin_config: Optional[Mapping[str, Any]] = None,
        allow_healthy_no_action: bool = True,
        allow_unhealthy_fallback: bool = True,
    ) -> Optional[Dict[str, Any]]:
        entries = list(self._iter_server_entries(snapshot))
        if primary_decision is not None:
            return self._maybe_initiate_secondary_cli_step(
                snapshot=snapshot,
                primary_decision=primary_decision,
                admin_config=admin_config,
            )
        if not entries:
            return None

        if allow_unhealthy_fallback:
            configured_violation = self._first_configured_incident_rule_violation(
                entries,
                admin_config=admin_config,
            )
            if configured_violation is not None:
                return configured_violation

            monitor_failure = self._first_monitor_failure(entries)
            if monitor_failure:
                return AdminAnalyzerDecision(
                    diagnosis="monitor_check_failed",
                    confidence="medium",
                    action="notify_admin",
                    reason=f"rule_engine:monitor_check_failed:{monitor_failure}",
                    urgency="warning",
                    incident_type="monitor.check_failed",
                    risk_level="low",
                    evidence=self._evidence(ref=monitor_failure),
                    suggested_runbook_ids=self._suggested_runbook_ids(ref=monitor_failure),
                ).as_dict()

            process_down = self._first_process_down(entries)
            if process_down:
                return AdminAnalyzerDecision(
                    diagnosis="process_down",
                    confidence="high",
                    action="notify_admin",
                    reason=f"rule_engine:process_down:{process_down}",
                    urgency="warning",
                    incident_type="availability.process_down",
                    risk_level="medium",
                    evidence=self._evidence(ref=process_down),
                    suggested_runbook_ids=self._suggested_runbook_ids(ref=process_down),
                ).as_dict()

            unhealthy_state = self._first_unhealthy_state_metric(entries)
            if unhealthy_state:
                return AdminAnalyzerDecision(
                    diagnosis="service_unhealthy",
                    confidence="high",
                    action="notify_admin",
                    reason=f"rule_engine:service_unhealthy:{unhealthy_state}",
                    urgency="warning",
                    incident_type="availability.service_unhealthy",
                    risk_level="medium",
                    evidence=self._evidence(ref=unhealthy_state),
                    suggested_runbook_ids=self._suggested_runbook_ids(ref=unhealthy_state),
                ).as_dict()

        disk_high = any(self._entry_disk_high(entry) for entry in entries)
        if disk_high:
            return AdminAnalyzerDecision(
                diagnosis="disk_high",
                confidence="medium",
                action="clear_logs",
                reason="rule_engine:disk_usage_threshold_exceeded",
                urgency="warning",
                incident_type="saturation.disk_high",
                risk_level="medium",
            ).as_dict()

        ssl_expiring_critical = any(self._entry_ssl_expiring(entry, critical=True) for entry in entries)
        if ssl_expiring_critical:
            return AdminAnalyzerDecision(
                diagnosis="ssl_expiring_critical",
                confidence="high",
                action="notify_admin",
                reason="rule_engine:ssl_expiring_critical",
                urgency="critical",
                incident_type="certificate.expiring_critical",
                risk_level="high",
            ).as_dict()

        ssl_expiring_warning = any(self._entry_ssl_expiring(entry, critical=False) for entry in entries)
        if ssl_expiring_warning:
            return AdminAnalyzerDecision(
                diagnosis="ssl_expiring_warning",
                confidence="medium",
                action="notify_admin",
                reason="rule_engine:ssl_expiring_warning",
                urgency="warning",
                incident_type="certificate.expiring_warning",
                risk_level="medium",
            ).as_dict()

        cpu_high = any(self._entry_cpu_high(entry) for entry in entries)
        if cpu_high:
            return AdminAnalyzerDecision(
                diagnosis="cpu_high_without_root_cause",
                confidence="low",
                action="notify_admin",
                reason="rule_engine:cpu_high_requires_manual_diagnostics",
                urgency="warning",
                incident_type="saturation.cpu_high",
                risk_level="medium",
            ).as_dict()
        if allow_healthy_no_action and all(self._entry_healthy(entry) for entry in entries):
            return AdminAnalyzerDecision(
                diagnosis="healthy",
                confidence="high",
                action="no_action",
                reason="rule_engine:all_monitored_checks_ok",
                urgency="info",
            ).as_dict()
        return None

    def _maybe_initiate_secondary_cli_step(
        self,
        *,
        snapshot: Any,
        primary_decision: Mapping[str, Any],
        admin_config: Optional[Mapping[str, Any]] = None,
    ) -> Dict[str, Any]:
        decision = dict(primary_decision or {})
        if not decision:
            return {}
        if str(decision.get("secondary_cli_command") or "").strip():
            return decision
        if not self._should_request_secondary_cli_step(decision, admin_config=admin_config):
            return decision

        secondary_cli_command = self._build_secondary_cli_command(
            snapshot=snapshot,
            primary_decision=decision,
            admin_config=admin_config,
        )
        if not secondary_cli_command:
            return decision
        decision["secondary_cli_command"] = secondary_cli_command
        return decision

    def _should_request_secondary_cli_step(
        self,
        primary_decision: Mapping[str, Any],
        *,
        admin_config: Optional[Mapping[str, Any]] = None,
    ) -> bool:
        policy = self._resolve_analyzer_policy(admin_config)
        if not bool(policy.get("allow_secondary_cli", True)):
            return False
        confidence = str(primary_decision.get("confidence") or "").strip().lower()
        action = str(primary_decision.get("action") or "").strip().lower()
        reason = str(primary_decision.get("reason") or "").strip().lower()
        risky_action = action not in _SAFE_ANALYZER_ACTIONS and bool(action)
        if confidence == "low" and bool(
            policy.get("require_secondary_confirmation_on_low_confidence", True)
        ):
            return True
        if risky_action and bool(policy.get("require_secondary_confirmation_on_risky_action", True)):
            return True
        if bool(primary_decision.get("signal_conflict")) and bool(
            policy.get("require_secondary_confirmation_on_signal_conflict", True)
        ):
            return True
        if bool(primary_decision.get("policy_conflict")) and bool(
            policy.get("require_secondary_confirmation_on_policy_conflict", True)
        ):
            return True
        if risky_action and bool(policy.get("require_secondary_confirmation_before_remediation", True)):
            return True
        return any(
            marker in reason
            for marker in ("insufficient_data", "need_more_data", "secondary_check", "missing_context")
        )

    def _build_secondary_cli_command(
        self,
        *,
        snapshot: Any,
        primary_decision: Optional[Mapping[str, Any]] = None,
        admin_config: Optional[Mapping[str, Any]] = None,
    ) -> str:
        runbook_command = self._build_runbook_secondary_cli_command(
            primary_decision=primary_decision,
            admin_config=admin_config,
        )
        if runbook_command:
            return runbook_command
        entries = list(self._iter_server_entries(snapshot))
        services = self._resolve_environment_services(admin_config)
        if any(self._entry_has_http_502(entry) for entry in entries):
            if "nginx" in services:
                return (
                    "systemctl status nginx --no-pager || "
                    "journalctl -u nginx -n 100 --no-pager || "
                    "tail -n 100 /var/log/nginx/error.log"
                )
            if "apache" in services:
                return (
                    "systemctl status apache2 --no-pager || "
                    "journalctl -u apache2 -n 100 --no-pager || "
                    "tail -n 100 /var/log/apache2/error.log"
                )
            return (
                "systemctl --failed --no-pager && "
                "journalctl -p err -n 100 --no-pager"
            )
        if any(self._entry_cpu_high(entry) for entry in entries):
            return (
                "top -b -n 1 | head -n 30 && "
                "systemctl --failed --no-pager && "
                "journalctl -p err -n 100 --no-pager"
            )
        database_service = self._first_matching_service(services, ("postgresql", "mysql", "mariadb", "redis"))
        if database_service:
            return (
                f"systemctl status {database_service} --no-pager || "
                f"journalctl -u {database_service} -n 100 --no-pager"
            )
        return "systemctl --failed --no-pager || journalctl -p err -n 100 --no-pager"

    @classmethod
    def _build_runbook_secondary_cli_command(
        cls,
        *,
        primary_decision: Optional[Mapping[str, Any]],
        admin_config: Optional[Mapping[str, Any]],
    ) -> str:
        if not isinstance(primary_decision, Mapping) or not isinstance(admin_config, Mapping):
            return ""
        suggested = primary_decision.get("suggested_runbook_ids")
        if not isinstance(suggested, list):
            return ""
        admin_cfg = admin_config.get("admin", {})
        if not isinstance(admin_cfg, Mapping):
            return ""
        templates_cfg = cls._resolve_runbook_templates(admin_cfg)
        actions_cfg = admin_cfg.get("actions", {})
        if not isinstance(actions_cfg, Mapping):
            return ""
        for runbook_id in suggested:
            template = templates_cfg.get(str(runbook_id or "").strip())
            if not isinstance(template, Mapping):
                continue
            steps = template.get("steps")
            if not isinstance(steps, list):
                continue
            for step in steps:
                if not isinstance(step, Mapping):
                    continue
                if str(step.get("name") or "").strip().lower() != "inspect":
                    continue
                action_id = str(step.get("action_id") or "").strip()
                target = str(step.get("target") or "").strip().lower()
                if not action_id or target not in {"local", "ssh"}:
                    continue
                target_actions = actions_cfg.get(target, {})
                action_payload = target_actions.get(action_id) if isinstance(target_actions, Mapping) else None
                command = cls._command_from_action_payload(action_payload)
                if command:
                    return command
        return ""

    @staticmethod
    def _resolve_runbook_templates(admin_cfg: Mapping[str, Any]) -> Dict[str, Any]:
        runbooks_cfg = admin_cfg.get("runbooks", {})
        templates = runbooks_cfg.get("templates", {}) if isinstance(runbooks_cfg, Mapping) else {}
        if isinstance(templates, Mapping) and templates:
            return dict(templates)
        generated = admin_cfg.get("generated", {})
        generated_runbooks = generated.get("runbooks", {}) if isinstance(generated, Mapping) else {}
        generated_templates = (
            generated_runbooks.get("templates", {})
            if isinstance(generated_runbooks, Mapping)
            else {}
        )
        return dict(generated_templates) if isinstance(generated_templates, Mapping) else {}

    @staticmethod
    def _command_from_action_payload(action_payload: Any) -> str:
        if not isinstance(action_payload, Mapping):
            return ""
        argv = action_payload.get("argv")
        if not isinstance(argv, (list, tuple)) or not argv:
            return ""
        clean_argv = [str(item) for item in argv if str(item).strip()]
        if len(clean_argv) >= 3 and clean_argv[0] == "bash" and clean_argv[1] == "-lc":
            return str(clean_argv[2] or "").strip()
        return shlex.join(clean_argv)

    @staticmethod
    def _resolve_analyzer_policy(admin_config: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
        admin_cfg = admin_config.get("admin", {}) if isinstance(admin_config, Mapping) else {}
        policies_cfg = admin_cfg.get("policies", {}) if isinstance(admin_cfg, Mapping) else {}
        analyzer_cfg = policies_cfg.get("analyzer", {}) if isinstance(policies_cfg, Mapping) else {}
        return dict(analyzer_cfg) if isinstance(analyzer_cfg, Mapping) else {}

    @staticmethod
    def _resolve_environment_services(admin_config: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
        admin_cfg = admin_config.get("admin", {}) if isinstance(admin_config, Mapping) else {}
        environment_cfg = admin_cfg.get("environment", {}) if isinstance(admin_cfg, Mapping) else {}
        services_cfg = environment_cfg.get("services", {}) if isinstance(environment_cfg, Mapping) else {}
        return dict(services_cfg) if isinstance(services_cfg, Mapping) else {}

    @staticmethod
    def _first_matching_service(services: Mapping[str, Any], candidates: tuple[str, ...]) -> str:
        for candidate in candidates:
            token = str(candidate or "").strip()
            if token and token in services:
                return token
        return ""

    @staticmethod
    def _discover_candidate_action_ids(admin_config: Optional[Mapping[str, Any]]) -> list[str]:
        action_ids: list[str] = []
        if not isinstance(admin_config, Mapping):
            return action_ids
        admin_cfg = admin_config.get("admin", {})
        if not isinstance(admin_cfg, Mapping):
            return action_ids
        actions_cfg = admin_cfg.get("actions", {})
        if isinstance(actions_cfg, Mapping):
            remediation_cfg = actions_cfg.get("remediation", {})
            if isinstance(remediation_cfg, Mapping):
                for action_id in remediation_cfg.keys():
                    token = str(action_id or "").strip().lower()
                    if token and token not in action_ids:
                        action_ids.append(token)
            for target in ("local", "ssh"):
                target_cfg = actions_cfg.get(target, {})
                if not isinstance(target_cfg, Mapping):
                    continue
                for action_id in target_cfg.keys():
                    token = str(action_id or "").strip().lower()
                    if not token or token.startswith("probe_"):
                        continue
                    if token not in action_ids:
                        action_ids.append(token)
        return action_ids

    def _is_action_allowed(
        self,
        action_id: str,
        *,
        admin_config: Optional[Mapping[str, Any]] = None,
    ) -> bool:
        token = str(action_id or "").strip().lower()
        if not token:
            return False
        allowed = set(_SAFE_ANALYZER_ACTIONS)
        allowed.update(self._discover_candidate_action_ids(admin_config))
        return token in allowed

    def _iter_server_entries(self, snapshot: Any) -> Iterable[Mapping[str, Any]]:
        raw_servers = []
        if isinstance(snapshot, Mapping):
            raw_servers = snapshot.get("servers", []) or []
        elif hasattr(snapshot, "servers"):
            raw_servers = getattr(snapshot, "servers", []) or []

        if not isinstance(raw_servers, (list, tuple)):
            return []

        normalized: list[Mapping[str, Any]] = []
        for server in raw_servers:
            if isinstance(server, Mapping):
                normalized.append(server)
                continue
            metrics = getattr(server, "metrics", {})
            normalized.append(
                {
                    "server_id": getattr(server, "server_id", ""),
                    "target": getattr(server, "target", ""),
                    "action_id": getattr(server, "action_id", ""),
                    "metrics": metrics if isinstance(metrics, Mapping) else {},
                    "error": getattr(server, "error", None),
                    "returncode": getattr(server, "returncode", None),
                    "timed_out": getattr(server, "timed_out", None),
                }
            )
        return normalized

    @staticmethod
    def _entry_has_http_502(entry: Mapping[str, Any]) -> bool:
        metrics = entry.get("metrics", {})
        if isinstance(metrics, Mapping):
            for key in ("http_status", "status_code", "upstream_status", "nginx_status"):
                value = metrics.get(key)
                if value is None:
                    continue
                if str(value).strip() == "502":
                    return True
                if "502" in str(value):
                    return True
        return "502" in str(entry.get("error") or "")

    @staticmethod
    def _entry_disk_high(entry: Mapping[str, Any]) -> bool:
        metrics = entry.get("metrics", {})
        if not isinstance(metrics, Mapping):
            return False
        for key in ("disk_usage", "disk_usage_pct", "disk_percent", "disk_used_percent", "disk_root_pct"):
            value = metrics.get(key)
            try:
                numeric = float(value)
            except Exception:
                continue
            if numeric > 1.0:
                numeric /= 100.0
            if numeric >= 0.9:
                return True
        level = str(metrics.get("disk_level") or "").strip().lower()
        return level in {"high", "critical"}

    @staticmethod
    def _entry_cpu_high(entry: Mapping[str, Any]) -> bool:
        metrics = entry.get("metrics", {})
        if not isinstance(metrics, Mapping):
            return False
        for key in ("cpu_usage", "cpu_usage_pct", "cpu_percent", "load_avg_1m"):
            value = metrics.get(key)
            try:
                numeric = float(value)
            except Exception:
                continue
            if key != "load_avg_1m" and numeric > 1.0:
                numeric /= 100.0
            if key == "load_avg_1m":
                if numeric >= 4.0:
                    return True
            elif numeric >= 0.9:
                return True
        return str(metrics.get("cpu_level") or "").strip().lower() in {"high", "critical"}

    @classmethod
    def _entry_healthy(cls, entry: Mapping[str, Any]) -> bool:
        if str(entry.get("error") or "").strip():
            return False
        if bool(entry.get("timed_out", False)):
            return False
        returncode = entry.get("returncode")
        if returncode not in (None, ""):
            try:
                if int(returncode) != 0:
                    return False
            except Exception:
                return False
        if "ok" in entry and not bool(entry.get("ok")):
            return False

        metrics = entry.get("metrics", {})
        if not isinstance(metrics, Mapping):
            return "ok" in entry
        if not metrics and "ok" not in entry:
            return False
        return not cls._entry_has_unhealthy_metric(metrics)

    @classmethod
    def _entry_has_unhealthy_metric(cls, metrics: Mapping[str, Any]) -> bool:
        bad_tokens = {"down", "failed", "exited", "missing", "unhealthy", "inactive", "error", "critical"}
        good_state_tokens = cls._good_state_tokens()
        for raw_key, value in metrics.items():
            key = str(raw_key or "").strip().lower()
            if isinstance(value, bool):
                if value is False and any(
                    marker in key
                    for marker in ("alive", "available", "healthy", "ok", "ready", "running", "state", "status", "up")
                ):
                    return True
                continue
            token = str(value or "").strip().lower()
            if token in bad_tokens:
                return True
            if any(marker in key for marker in ("state", "status", "health")) and token:
                try:
                    numeric = int(float(token))
                except Exception:
                    if token not in good_state_tokens:
                        return True
                else:
                    if numeric >= 400:
                        return True
            if key in {"http_status", "status_code", "upstream_status", "nginx_status"}:
                try:
                    if int(float(token)) >= 400:
                        return True
                except Exception:
                    if "5" in token:
                        return True
        return False

    @classmethod
    def _first_configured_incident_rule_violation(
        cls,
        entries: Iterable[Mapping[str, Any]],
        *,
        admin_config: Optional[Mapping[str, Any]],
    ) -> Optional[Dict[str, Any]]:
        rules = cls._configured_incident_rules(admin_config)
        if not rules:
            return None
        for entry in entries:
            metrics = entry.get("metrics", {})
            if not isinstance(metrics, Mapping):
                continue
            for rule in rules:
                if not isinstance(rule, Mapping):
                    continue
                thresholds = rule.get("thresholds")
                if not isinstance(thresholds, list):
                    thresholds = cls._legacy_rule_thresholds(rule)
                for threshold in thresholds:
                    if not isinstance(threshold, Mapping):
                        continue
                    matched = cls._match_threshold(metrics, threshold)
                    if matched is None:
                        continue
                    metric_key, metric_value = matched
                    incident_type = str(
                        threshold.get("incident_type")
                        or rule.get("incident_type")
                        or "generated.threshold_exceeded"
                    ).strip()
                    ref = cls._entry_ref(entry, suffix=f"{metric_key}={metric_value}")
                    runbook_id = str(rule.get("runbook_id") or "").strip()
                    return AdminAnalyzerDecision(
                        diagnosis=incident_type,
                        confidence=str(threshold.get("confidence") or "low").strip().lower() or "low",
                        action=str(rule.get("fallback_action") or "notify_admin").strip().lower(),
                        reason=f"rule_engine:generated_threshold:{ref}",
                        urgency=str(threshold.get("urgency") or "warning").strip().lower() or "warning",
                        incident_type=incident_type,
                        risk_level=str(threshold.get("risk_level") or "medium").strip().lower(),
                        evidence=cls._evidence(ref=ref),
                        suggested_runbook_ids=[runbook_id] if runbook_id else cls._suggested_runbook_ids(ref=ref),
                    ).as_dict()
        return None

    @staticmethod
    def _configured_incident_rules(admin_config: Optional[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
        admin_cfg = admin_config.get("admin", {}) if isinstance(admin_config, Mapping) else {}
        if not isinstance(admin_cfg, Mapping):
            return []
        rules: list[Mapping[str, Any]] = []
        for section_name in ("incidents",):
            section = admin_cfg.get(section_name, {})
            raw_rules = section.get("rules", {}) if isinstance(section, Mapping) else {}
            if isinstance(raw_rules, Mapping):
                rules.extend(dict(item) for item in raw_rules.values() if isinstance(item, Mapping))
            elif isinstance(raw_rules, list):
                rules.extend(dict(item) for item in raw_rules if isinstance(item, Mapping))
        generated = admin_cfg.get("generated", {})
        generated_incidents = generated.get("incidents", {}) if isinstance(generated, Mapping) else {}
        generated_rules = generated_incidents.get("rules", {}) if isinstance(generated_incidents, Mapping) else {}
        if isinstance(generated_rules, Mapping):
            rules.extend(dict(item) for item in generated_rules.values() if isinstance(item, Mapping))
        elif isinstance(generated_rules, list):
            rules.extend(dict(item) for item in generated_rules if isinstance(item, Mapping))
        return rules

    @staticmethod
    def _legacy_rule_thresholds(rule: Mapping[str, Any]) -> list[Dict[str, Any]]:
        metric = str(rule.get("trigger_metric") or "").strip()
        if not metric:
            return []
        return [
            {
                "metric": metric,
                "op": "in",
                "value": [rule.get("on_value")],
                "incident_type": rule.get("incident_type") or "generated.threshold_exceeded",
            }
        ]

    @classmethod
    def _match_threshold(
        cls,
        metrics: Mapping[str, Any],
        threshold: Mapping[str, Any],
    ) -> Optional[tuple[str, Any]]:
        metric = str(threshold.get("metric") or "").strip()
        metric_suffix = str(threshold.get("metric_suffix") or "").strip()
        candidates = []
        if metric and metric in metrics:
            candidates.append((metric, metrics.get(metric)))
        elif metric_suffix:
            candidates.extend(
                (str(key), value)
                for key, value in metrics.items()
                if str(key).endswith(metric_suffix)
            )
        for key, value in candidates:
            if cls._threshold_matches(value, threshold):
                return key, value
        return None

    @classmethod
    def _threshold_matches(cls, value: Any, threshold: Mapping[str, Any]) -> bool:
        op = str(threshold.get("op") or "eq").strip().lower()
        expected = threshold.get("value")
        if op in {"gte", "gt", "lte", "lt"}:
            actual_number = cls._metric_number(value)
            expected_number = cls._metric_number(expected)
            if actual_number is None or expected_number is None:
                return False
            if op == "gte":
                return actual_number >= expected_number
            if op == "gt":
                return actual_number > expected_number
            if op == "lte":
                return actual_number <= expected_number
            return actual_number < expected_number
        actual = str(value).strip().lower()
        if op == "in":
            expected_values = expected if isinstance(expected, (list, tuple, set)) else [expected]
            return actual in {str(item).strip().lower() for item in expected_values}
        if op == "not_in":
            expected_values = expected if isinstance(expected, (list, tuple, set)) else [expected]
            return actual not in {str(item).strip().lower() for item in expected_values}
        return actual == str(expected).strip().lower()

    @classmethod
    def _first_monitor_failure(cls, entries: Iterable[Mapping[str, Any]]) -> str:
        for entry in entries:
            if bool(entry.get("timed_out", False)):
                return cls._entry_ref(entry, suffix="timeout")
            returncode = entry.get("returncode")
            if returncode not in (None, ""):
                try:
                    if int(returncode) != 0:
                        return cls._entry_ref(entry, suffix=f"returncode={returncode}")
                except Exception:
                    return cls._entry_ref(entry, suffix=f"returncode={returncode}")
            error = str(entry.get("error") or "").strip()
            if error:
                return cls._entry_ref(entry, suffix=error)
            if "ok" in entry and not bool(entry.get("ok")):
                return cls._entry_ref(entry, suffix="ok=false")
        return ""

    @classmethod
    def _first_process_down(cls, entries: Iterable[Mapping[str, Any]]) -> str:
        for entry in entries:
            metrics = entry.get("metrics", {})
            if not isinstance(metrics, Mapping):
                continue
            for raw_key, value in metrics.items():
                key = str(raw_key or "").strip().lower()
                token = str(value or "").strip().lower()
                if key.startswith("process_") and key.endswith("_state") and token in cls._bad_state_tokens():
                    return cls._entry_ref(entry, suffix=f"{key}={token or 'empty'}")
                if key.startswith("process_") and key.endswith("_count"):
                    try:
                        count = int(float(token or 0))
                    except Exception:
                        continue
                    if count <= 0:
                        return cls._entry_ref(entry, suffix=f"{key}=0")
        return ""

    @classmethod
    def _first_unhealthy_state_metric(cls, entries: Iterable[Mapping[str, Any]]) -> str:
        skip_keys = {"http_status", "status_code", "upstream_status", "nginx_status"}
        for entry in entries:
            metrics = entry.get("metrics", {})
            if not isinstance(metrics, Mapping):
                continue
            for raw_key, value in metrics.items():
                key = str(raw_key or "").strip().lower()
                if key in skip_keys:
                    continue
                if not any(marker in key for marker in ("state", "status", "health", "result")):
                    continue
                if cls._metric_value_unhealthy(raw_key, value):
                    token = str(value or "").strip().lower()
                    return cls._entry_ref(entry, suffix=f"{key}={token or 'empty'}")
        return ""

    @classmethod
    def _metric_value_unhealthy(cls, raw_key: Any, value: Any) -> bool:
        key = str(raw_key or "").strip().lower()
        if key in {"http_status", "status_code", "upstream_status", "nginx_status"}:
            return False
        if isinstance(value, bool):
            return value is False
        token = str(value or "").strip().lower()
        if not token:
            return False
        if token in cls._bad_state_tokens():
            return True
        if any(marker in key for marker in ("state", "status", "health", "result")):
            try:
                numeric = int(float(token))
            except Exception:
                return token not in cls._good_state_tokens()
            return numeric >= 400
        return False

    @staticmethod
    def _metric_number(value: Any) -> float | None:
        try:
            return float(value)
        except Exception:
            return None

    @staticmethod
    def _bad_state_tokens() -> set[str]:
        return {"down", "failed", "exited", "missing", "unhealthy", "inactive", "error", "critical"}

    @staticmethod
    def _good_state_tokens() -> set[str]:
        return {"active", "ok", "healthy", "running", "up", "unknown", "none", "success", "0", "false"}

    @staticmethod
    def _entry_ref(entry: Mapping[str, Any], *, suffix: str) -> str:
        ref = str(entry.get("server_id") or entry.get("action_id") or "server").strip() or "server"
        clean_suffix = str(suffix or "").strip()
        return f"{ref}:{clean_suffix}" if clean_suffix else ref

    @staticmethod
    def _evidence(*, ref: str) -> list[Dict[str, Any]]:
        return [{"source": "rule_engine", "ref": str(ref or "").strip()}]

    @staticmethod
    def _suggested_runbook_ids(*, ref: str) -> list[str]:
        server_ref = str(ref or "").split(":", 1)
        if len(server_ref) < 2:
            return []
        if server_ref[0] != "scan":
            return []
        service_part = str(ref or "").split(":", 3)
        if len(service_part) < 3:
            return []
        service_id = f"{service_part[1]}:{service_part[2]}"
        fragment = re.sub(r"[^A-Za-z0-9]+", "_", service_id).strip("_").lower()
        if not fragment:
            return []
        return [f"inspect_{fragment[:96]}"]

    @staticmethod
    def _entry_ssl_expiring(entry: Mapping[str, Any], *, critical: bool) -> bool:
        metrics = entry.get("metrics", {})
        if not isinstance(metrics, Mapping):
            return False
        raw_days = metrics.get("ssl_days_left")
        try:
            days_left = float(raw_days)
        except Exception:
            days_left = -1.0
        if days_left >= 0:
            if critical and days_left <= 3:
                return True
            if not critical and 3 < days_left <= 14:
                return True
        status = str(metrics.get("ssl_expiry_state") or "").strip().lower()
        if critical:
            return status == "critical"
        return status == "warning"

    @staticmethod
    def _to_json_safe(value: Any) -> Any:
        if value is None or isinstance(value, (str, int, float, bool)):
            return value
        if isinstance(value, Mapping):
            return {str(k): AdminAnalyzer._to_json_safe(v) for k, v in value.items()}
        if isinstance(value, (list, tuple, set)):
            return [AdminAnalyzer._to_json_safe(v) for v in value]
        return str(value)


__all__ = [
    "AdminAnalyzer",
    "AdminAnalyzerDecision",
    "AdminAnalyzerDecisionSchema",
]
