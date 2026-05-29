from __future__ import annotations

from typing import Any, Dict, List, Optional

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError as JsonSchemaValidationError
from pydantic import BaseModel, ConfigDict, Field, ValidationError as PydanticValidationError


def _schema_error_context(contract: str) -> str:
    return f"admin.{contract}"


class AdminPinnedCliConfigSchema(BaseModel):
    model_config = ConfigDict(extra="allow")
    name: Optional[str] = None
    transport: Optional[str] = None
    target: Optional[str] = None
    host: Optional[str] = None
    user: Optional[str] = None
    port: Optional[int] = None
    key_path: Optional[str] = None
    options: List[str] = Field(default_factory=list)


class AdminRuntimeConfigSchema(BaseModel):
    model_config = ConfigDict(extra="allow")
    pinned_cli: AdminPinnedCliConfigSchema = Field(default_factory=AdminPinnedCliConfigSchema)
    pinned_executor_profile: Optional[str] = None
    initialized_at: Optional[float] = None
    last_scan_at: Optional[float] = None
    scan_status: str = "not_started"
    scan_error: Optional[str] = None


class AdminEnvironmentConfigSchema(BaseModel):
    model_config = ConfigDict(extra="allow")
    services: Dict[str, Any] = Field(default_factory=dict)
    server_roles: List[str] = Field(default_factory=list)
    stack_facts: Dict[str, Any] = Field(default_factory=dict)


class AdminDiagnosticsConfigSchema(BaseModel):
    model_config = ConfigDict(extra="allow")
    checks: List[Any] = Field(default_factory=list)


class AdminIncidentsConfigSchema(BaseModel):
    model_config = ConfigDict(extra="allow")
    rules: List[Any] = Field(default_factory=list)


class AdminActionsConfigSchema(BaseModel):
    model_config = ConfigDict(extra="allow")
    local: Dict[str, Any] = Field(default_factory=dict)
    ssh: Dict[str, Any] = Field(default_factory=dict)
    remediation: Dict[str, Any] = Field(default_factory=dict)


class AdminAnalyzerPolicySchema(BaseModel):
    model_config = ConfigDict(extra="allow")
    allow_secondary_cli: bool = True
    allow_internet_secondary_cli: bool = False
    require_secondary_confirmation_on_low_confidence: bool = True
    require_secondary_confirmation_on_risky_action: bool = True
    require_secondary_confirmation_on_signal_conflict: bool = True
    require_secondary_confirmation_on_policy_conflict: bool = True
    require_secondary_confirmation_before_remediation: bool = True


class AdminExecutorPolicySchema(BaseModel):
    model_config = ConfigDict(extra="allow")
    mandatory_notify_actions: List[str] = Field(default_factory=list)
    auto_actions_per_hour: int = 0
    cooldown_sec: float = 0.0
    maintenance_window: Dict[str, Any] = Field(default_factory=dict)


class AdminPoliciesConfigSchema(BaseModel):
    model_config = ConfigDict(extra="allow")
    version: str = "v1"
    analyzer: AdminAnalyzerPolicySchema = Field(default_factory=AdminAnalyzerPolicySchema)
    executor: AdminExecutorPolicySchema = Field(default_factory=AdminExecutorPolicySchema)
    per_action: Dict[str, Any] = Field(default_factory=dict)


class AdminGeneratedConfigSchema(BaseModel):
    model_config = ConfigDict(extra="allow")
    environment: Dict[str, Any] = Field(default_factory=dict)
    diagnostics: Dict[str, Any] = Field(default_factory=dict)
    incidents: Dict[str, Any] = Field(default_factory=dict)
    actions: Dict[str, Any] = Field(default_factory=dict)
    policies: Dict[str, Any] = Field(default_factory=dict)
    runbooks: Dict[str, Any] = Field(default_factory=dict)
    runtime_overlay: Dict[str, Any] = Field(default_factory=dict)
    manual: Dict[str, Any] = Field(default_factory=dict)
    scan_meta: Dict[str, Any] = Field(default_factory=dict)
    scan_summary: Dict[str, Any] = Field(default_factory=dict)


class AdminConfigSchema(BaseModel):
    model_config = ConfigDict(extra="allow")
    runtime: AdminRuntimeConfigSchema = Field(default_factory=AdminRuntimeConfigSchema)
    environment: AdminEnvironmentConfigSchema = Field(default_factory=AdminEnvironmentConfigSchema)
    diagnostics: AdminDiagnosticsConfigSchema = Field(default_factory=AdminDiagnosticsConfigSchema)
    incidents: AdminIncidentsConfigSchema = Field(default_factory=AdminIncidentsConfigSchema)
    actions: AdminActionsConfigSchema = Field(default_factory=AdminActionsConfigSchema)
    policies: AdminPoliciesConfigSchema = Field(default_factory=AdminPoliciesConfigSchema)
    runbooks: Dict[str, Any] = Field(default_factory=dict)
    generated: AdminGeneratedConfigSchema = Field(default_factory=AdminGeneratedConfigSchema)


class AdminConfigRootSchema(BaseModel):
    model_config = ConfigDict(extra="allow")
    admin: AdminConfigSchema


AdminAnalyzerAllowedActions = (
    "notify_admin",
)


AdminPinnedCliConfigSchema.model_rebuild()
AdminRuntimeConfigSchema.model_rebuild()
AdminEnvironmentConfigSchema.model_rebuild()
AdminDiagnosticsConfigSchema.model_rebuild()
AdminIncidentsConfigSchema.model_rebuild()
AdminActionsConfigSchema.model_rebuild()
AdminAnalyzerPolicySchema.model_rebuild()
AdminExecutorPolicySchema.model_rebuild()
AdminPoliciesConfigSchema.model_rebuild()
AdminGeneratedConfigSchema.model_rebuild()
AdminConfigSchema.model_rebuild()
AdminConfigRootSchema.model_rebuild()


AdminStatusPayloadSchema: Dict[str, Any] = {
    "type": "object",
    "required": [
        "mode",
        "session_id",
        "active",
        "busy",
        "run_lock_locked",
        "tick_active",
        "mode_tasks_running",
        "pipeline_status",
        "monitor_status",
        "analyzer_status",
        "executor_status",
        "notifier_status",
        "scan_status",
    ],
    "properties": {
        "mode": {"type": "string", "enum": ["admin"]},
        "session_id": {"type": "string"},
        "active": {"type": "boolean"},
        "busy": {"type": "boolean"},
        "run_lock_locked": {"type": "boolean"},
        "tick_active": {"type": "boolean"},
        "mode_tasks_running": {"type": "boolean"},
        "pipeline_status": {
            "type": "string",
            "enum": ["disabled", "initializing", "idle", "running", "completed", "failed"],
        },
        "monitor_status": {
            "type": "string",
            "enum": ["disabled", "idle", "ready", "running", "completed", "failed", "skipped"],
        },
        "analyzer_status": {
            "type": "string",
            "enum": ["idle", "running", "completed", "failed", "skipped"],
        },
        "analyzer_message": {"type": "string"},
        "executor_status": {
            "type": "string",
            "enum": ["idle", "running", "completed", "failed", "skipped"],
        },
        "executor_message": {"type": "string"},
        "notifier_status": {
            "type": "string",
            "enum": ["disabled", "idle", "ready", "running", "completed", "failed", "skipped"],
        },
        "notifier_message": {"type": "string"},
        "scan_status": {
            "type": "string",
            "enum": ["not_started", "initializing", "running", "ready", "failed"],
        },
        "scan_error": {"type": ["string", "null"]},
        "initialized_at": {"type": ["number", "null"]},
        "last_scan_at": {"type": ["number", "null"]},
        "pinned_cli": {"type": "object"},
        "pinned_executor_profile": {"type": ["string", "null"]},
        "component_readiness": {
            "type": "object",
            "required": ["monitor", "analyzer", "executor", "notifier"],
            "properties": {
                "monitor": {"type": "boolean"},
                "analyzer": {"type": "boolean"},
                "executor": {"type": "boolean"},
                "notifier": {"type": "boolean"},
            },
            "additionalProperties": True,
        },
        "last_monitor_snapshot": {"type": "object"},
        "last_analyzer_decision": {"type": "object"},
        "last_action": {"type": "object"},
        "pending_ask_user": {"type": "object"},
        "pending_approvals": {"type": "object"},
        "pending_skill_installs": {"type": "object"},
        "mute_state": {"type": "object"},
        "recent_incidents": {"type": "array"},
        "recent_admin_actions": {"type": "array"},
        "approved_overrides": {"type": "array"},
        "status_updated_at": {"type": "number"},
    },
    "additionalProperties": True,
}


AdminAnalyzerDecisionSchema: Dict[str, Any] = {
    "type": "object",
    "required": ["diagnosis", "confidence", "action", "reason", "urgency"],
    "properties": {
        "diagnosis": {"type": "string", "minLength": 1, "maxLength": 512},
        "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
        "action": {
            "type": "string",
            "pattern": "^[a-z][a-z0-9._:-]{0,127}$",
        },
        "reason": {"type": "string", "minLength": 1, "maxLength": 2048},
        "urgency": {"type": "string", "enum": ["critical", "warning", "info"]},
        "secondary_cli_command": {"type": "string", "minLength": 1, "maxLength": 2048},
        "server_id": {"type": "string", "maxLength": 256},
        "incident_type": {
            "type": "string",
            "pattern": "^[a-z][a-z0-9_.:-]{0,127}$",
        },
        "risk_level": {"type": "string", "enum": ["low", "medium", "high", "critical"]},
        "evidence": {
            "type": "array",
            "items": {"type": "object", "additionalProperties": True},
            "maxItems": 20,
        },
        "suggested_runbook_ids": {
            "type": "array",
            "items": {"type": "string", "minLength": 1, "maxLength": 128},
            "maxItems": 20,
        },
    },
    "additionalProperties": False,
}


def validate_admin_config_payload(payload: Dict[str, Any]) -> None:
    try:
        AdminConfigRootSchema.model_validate(payload)
    except PydanticValidationError as exc:
        first_error = exc.errors()[0] if exc.errors() else {}
        loc = first_error.get("loc") or ()
        path = ".".join(str(part) for part in loc)
        where = f" at {path}" if path else ""
        message = str(first_error.get("msg") or str(exc))
        raise ValueError(f"{_schema_error_context('config')} schema validation failed{where}: {message}") from exc


def validate_admin_payload(payload: Dict[str, Any], schema: Dict[str, Any], *, contract: str) -> None:
    try:
        Draft202012Validator(schema).validate(payload)
    except JsonSchemaValidationError as exc:
        path = "/".join(str(x) for x in exc.path)
        where = f" at {path}" if path else ""
        raise ValueError(f"{_schema_error_context(contract)} schema validation failed{where}: {exc.message}") from exc


__all__ = [
    "AdminAnalyzerAllowedActions",
    "AdminAnalyzerDecisionSchema",
    "AdminConfigRootSchema",
    "AdminConfigSchema",
    "AdminPinnedCliConfigSchema",
    "AdminRuntimeConfigSchema",
    "AdminStatusPayloadSchema",
    "validate_admin_config_payload",
    "validate_admin_payload",
]
