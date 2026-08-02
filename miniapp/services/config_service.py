import copy
import hashlib
import logging
import os
from typing import Any, Dict, List, Tuple

import yaml
from pydantic import ValidationError

from app.config_runtime.loader import ValidatedSettings
from app.services.config_apply_policy import classify_config_path
from config import AppConfig, app_config_to_dict as serialize_app_config

logger = logging.getLogger("miniapp")
SECRET_UNCHANGED_SENTINEL = "__CLI_PROXY_SECRET_UNCHANGED__"


class ConfigValidationError(Exception):
    """Raised when MiniApp config draft validation fails."""


class RevisionConflictError(Exception):
    """Raised when config file revision changed during save."""


def _file_revision(path: str) -> str:
    try:
        with open(path, "rb") as f:
            data = f.read()
    except FileNotFoundError:
        return "missing"
    return hashlib.sha256(data).hexdigest()


def app_config_to_dict(config: AppConfig) -> Dict[str, Any]:
    raw = serialize_app_config(config)
    return _sanitize_for_json(raw)


def _sanitize_for_json(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _sanitize_for_json(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_sanitize_for_json(v) for v in value]
    return value


def _policy_metadata(path: str) -> tuple[bool, bool, bool]:
    policy = classify_config_path(path)
    return (
        policy.apply_mode == "restart_required",
        policy.apply_mode == "hot_reload",
        bool(policy.secret),
    )


def _apply_mode_flags(path: str) -> tuple[bool, bool]:
    restart_required, reloadable, _secret = _policy_metadata(path)
    return restart_required, reloadable


def _apply_schema_policy_metadata(schema: Dict[str, Any]) -> Dict[str, Any]:
    sections = schema.get("sections", {})
    if not isinstance(sections, dict):
        return schema
    for section_name, section in sections.items():
        if not isinstance(section, dict):
            continue
        fields = section.get("fields", {})
        if not isinstance(fields, dict):
            continue
        for field_name, metadata in fields.items():
            if not isinstance(metadata, dict):
                continue
            path = f"{section_name}.{field_name}"
            restart_required, reloadable, secret = _policy_metadata(path)
            metadata["restart_required"] = restart_required
            metadata["reloadable"] = reloadable
            metadata["secret"] = secret
    return schema


def _iter_leaf_paths(prefix: str, value: Any):
    if isinstance(value, dict):
        for key in sorted(value.keys()):
            child = f"{prefix}.{key}" if prefix else str(key)
            yield from _iter_leaf_paths(child, value[key])
        return
    yield prefix, value


def _get_path_value(data: Dict[str, Any], path: str) -> tuple[bool, Any]:
    current: Any = data
    parts = [part for part in str(path or "").split(".") if part]
    for part in parts:
        if not isinstance(current, dict) or part not in current:
            return False, None
        current = current[part]
    return True, current


def _set_path_value(data: Dict[str, Any], path: str, value: Any) -> None:
    current: Any = data
    parts = [part for part in str(path or "").split(".") if part]
    for part in parts[:-1]:
        if not isinstance(current, dict) or part not in current:
            return
        current = current[part]
    if isinstance(current, dict) and parts:
        current[parts[-1]] = value


def _is_secret_path(path: str) -> bool:
    _restart_required, _reloadable, secret = _policy_metadata(path)
    return secret


def _redact_secret_value(path: str, value: Any) -> Any:
    if not _is_secret_path(path):
        return value
    if value is None:
        return None
    return SECRET_UNCHANGED_SENTINEL


def redacted_config_view(config: Dict[str, Any]) -> tuple[Dict[str, Any], List[str]]:
    redacted = copy.deepcopy(config)
    redacted_paths: List[str] = []
    for path, value in _iter_leaf_paths("", redacted):
        if not _is_secret_path(path):
            continue
        redacted_paths.append(path)
        _set_path_value(redacted, path, _redact_secret_value(path, value))
    return redacted, redacted_paths


def restore_redacted_secret_values(current: Dict[str, Any], draft: Dict[str, Any]) -> Dict[str, Any]:
    restored = copy.deepcopy(draft)
    for path, value in _iter_leaf_paths("", restored):
        if not _is_secret_path(path) or value != SECRET_UNCHANGED_SENTINEL:
            continue
        found, current_value = _get_path_value(current, path)
        if found:
            _set_path_value(restored, path, current_value)
    return restored


def config_schema() -> Dict[str, Any]:
    # Schema is intentionally explicit for the first iteration of MiniApp UI.
    schema = {
        "sections": {
            "telegram": {
                "title": "Telegram",
                "fields": {
                    "token": {
                        "type": "string",
                        "required": True,
                        "description": "Bot token.",
                    },
                    "whitelist_chat_ids": {
                        "type": "array[int]",
                        "required": True,
                        "description": "Allowed non-admin chats.",
                    },
                    "admlist_chat_ids": {
                        "type": "array[int]",
                        "required": True,
                        "description": "MiniApp admins.",
                    },
                    "user_workdirs": {
                        "type": "map[int,array[string]]",
                        "required": False,
                        "description": "Per-user project roots.",
                    },
                    "user_modes": {
                        "type": "map[int,all|array[string]]",
                        "required": False,
                        "description": (
                            "Per-user allowed modes, including registered modes such as "
                            "agent, analyst, manager, sdd, and webmaster; include direct_cli "
                            "for direct CLI access and orchestrator for session orchestrator access."
                        ),
                    },
                    "connection_pool_size": {
                        "type": "int",
                        "required": True,
                        "description": "HTTP connection pool size.",
                    },
                    "connect_timeout_sec": {
                        "type": "float",
                        "required": True,
                        "description": "Telegram connect timeout.",
                    },
                    "read_timeout_sec": {
                        "type": "float",
                        "required": True,
                        "description": "Telegram read timeout.",
                    },
                    "write_timeout_sec": {
                        "type": "float",
                        "required": True,
                        "description": "Telegram write timeout.",
                    },
                    "pool_timeout_sec": {
                        "type": "float",
                        "required": True,
                        "description": "Telegram pool timeout.",
                    },
                    "polling_timeout_sec": {
                        "type": "int",
                        "required": True,
                        "description": "Polling timeout.",
                    },
                    "poll_interval_sec": {
                        "type": "float",
                        "required": True,
                        "description": "Polling interval.",
                    },
                },
            },
            "defaults": {
                "title": "Defaults",
                "type": "object",
                "description": "Runtime defaults.",
                "fields": {
                    "workdir": {
                        "type": "string",
                        "required": True,
                        "description": "Default workdir for new sessions.",
                    },
                    "idle_timeout_sec": {
                        "type": "int",
                        "required": True,
                        "description": "Idle timeout before session cleanup.",
                    },
                    "summary_max_chars": {
                        "type": "int",
                        "required": True,
                        "description": "Max summary size in characters.",
                    },
                    "html_filename_prefix": {
                        "type": "string",
                        "required": True,
                        "description": "Prefix for generated HTML artifacts.",
                    },
                    "state_path": {
                        "type": "string",
                        "required": True,
                        "description": "Runtime state file path.",
                    },
                    "desktop_state_path": {
                        "type": "string",
                        "required": True,
                        "description": "Desktop app state file path.",
                    },
                    "toolhelp_path": {
                        "type": "string",
                        "required": True,
                        "description": "Cached tool help file path.",
                    },
                    "openai_api_key": {
                        "type": "string",
                        "required": False,
                        "description": "OpenAI API key override.",
                    },
                    "openai_model": {
                        "type": "string",
                        "required": False,
                        "description": "Default OpenAI model.",
                    },
                    "openai_big_model": {
                        "type": "string",
                        "required": False,
                        "description": "Large-model override for summaries.",
                    },
                    "openai_base_url": {
                        "type": "string",
                        "required": False,
                        "description": "Custom OpenAI-compatible base URL.",
                    },
                    "zai_api_key": {
                        "type": "string",
                        "required": False,
                        "description": "Z.ai API key override.",
                    },
                    "tavily_api_key": {
                        "type": "string",
                        "required": False,
                        "description": "Tavily API key override.",
                    },
                    "jina_api_key": {
                        "type": "string",
                        "required": False,
                        "description": "Jina API key override.",
                    },
                    "github_token": {
                        "type": "string",
                        "required": False,
                        "description": "GitHub token override.",
                    },
                    "gemini_oauth_client_secret": {
                        "type": "string",
                        "required": False,
                        "description": "Gemini OAuth client secret for quota refresh.",
                    },
                    "log_path": {
                        "type": "string",
                        "required": True,
                        "description": "Main log file path.",
                    },
                    "image_temp_dir": {
                        "type": "string",
                        "required": True,
                        "description": "Temp directory for image uploads.",
                    },
                    "image_max_mb": {
                        "type": "int",
                        "required": True,
                        "description": "Max uploaded image size in MiB.",
                    },
                    "memory_max_kb": {
                        "type": "int",
                        "required": True,
                        "description": "Max compacted memory size in KiB.",
                    },
                    "memory_compact_target_kb": {
                        "type": "int",
                        "required": True,
                        "description": "Target compacted memory size in KiB.",
                    },
                    "memory_events_enabled": {
                        "type": "bool",
                        "required": True,
                        "description": "Enable shadow append-only memory event capture.",
                    },
                    "memory_native_cli_hooks_enabled": {
                        "type": "bool",
                        "required": True,
                        "description": "Enable opt-in native CLI hook memory adapters.",
                    },
                    "memory_outcomes_enabled": {
                        "type": "bool",
                        "required": True,
                        "description": "Enable shadow outcome records for memory learning.",
                    },
                    "memory_dreaming_enabled": {
                        "type": "bool",
                        "required": True,
                        "description": "Enable async dreaming/consolidation pipeline.",
                    },
                    "memory_events_retention_days": {
                        "type": "int",
                        "required": True,
                        "description": "Retention window for memory events.",
                    },
                    "memory_events_max_payload_chars": {
                        "type": "int",
                        "required": True,
                        "description": "Max redacted payload size per memory event.",
                    },
                    "memory_events_redaction_enabled": {
                        "type": "bool",
                        "required": True,
                        "description": "Redact obvious secrets before storing memory events.",
                    },
                    "memory_dreaming_batch_size": {
                        "type": "int",
                        "required": True,
                        "description": "Max batch size for one dreaming pass.",
                    },
                    "clarification_enabled": {
                        "type": "bool",
                        "required": True,
                        "description": "Enable clarification prompts.",
                    },
                    "pending_input_confirmation_enabled": {
                        "type": "bool",
                        "required": True,
                        "description": (
                            "Require explicit confirmation for every new incoming "
                            "message before further processing. Busy-session queue "
                            "choice is always confirmed separately."
                        ),
                    },
                    "default_cli": {
                        "type": "string",
                        "required": False,
                        "description": "Default CLI for new sessions.",
                    },
                    "default_execution_backend": {
                        "type": "enum[headless,tmux]",
                        "required": True,
                        "description": "Default CLI execution backend for new sessions.",
                    },
                    "clarification_keywords": {
                        "type": "array[string]",
                        "required": True,
                        "description": "Keywords that trigger clarification handling.",
                    },
                    "manager_max_tasks": {
                        "type": "int",
                        "required": True,
                        "description": "Max tasks for manager mode decomposition.",
                    },
                    "manager_max_attempts": {
                        "type": "int",
                        "required": True,
                        "description": "Max manager attempts per task.",
                    },
                    "manager_decompose_timeout_sec": {
                        "type": "int",
                        "required": True,
                        "description": "Manager decompose timeout in seconds.",
                    },
                    "manager_dev_timeout_sec": {
                        "type": "int",
                        "required": True,
                        "description": "Manager dev timeout in seconds.",
                    },
                    "manager_review_timeout_sec": {
                        "type": "int",
                        "required": True,
                        "description": "Manager review timeout in seconds.",
                    },
                    "analyst_use_cli_timeout_sec": {
                        "type": "int",
                        "required": True,
                        "description": "Analyst CLI timeout in seconds.",
                    },
                    "webmaster_use_cli_timeout_sec": {
                        "type": "int",
                        "required": True,
                        "description": "Webmaster CLI timeout in seconds.",
                    },
                    "webmaster_validation_max_fix_iterations": {
                        "type": "int",
                        "required": True,
                        "description": "Max webmaster validation/fix iterations.",
                    },
                    "manager_dev_report_max_chars": {
                        "type": "int",
                        "required": True,
                        "description": "Max manager dev report size in characters.",
                    },
                    "manager_auto_resume": {
                        "type": "bool",
                        "required": True,
                        "description": "Enable manager auto-resume.",
                    },
                    "manager_auto_commit": {
                        "type": "bool",
                        "required": True,
                        "description": "Enable manager auto-commit.",
                    },
                    "manager_response_archive": {
                        "type": "bool",
                        "required": True,
                        "description": "Archive raw manager responses.",
                    },
                    "cli_json_stream_archive_enabled": {
                        "type": "bool",
                        "required": True,
                        "description": "Archive raw and normalized CLI JSON stream events.",
                    },
                    "assistant_preview_enabled": {
                        "type": "bool",
                        "required": True,
                        "description": "Show in-progress assistant preview in Telegram and Desktop sessions.",
                    },
                    "codebase_mapper_usage": {
                        "type": "enum[auto,enabled,disabled]",
                        "required": True,
                        "description": "Codebase mapper usage mode.",
                    },
                    "run_artifacts_enabled": {
                        "type": "bool",
                        "required": True,
                        "description": "Enable persistent per-run artifacts.",
                    },
                    "run_artifacts_retention_days": {
                        "type": "int",
                        "required": True,
                        "description": "Retention window for stored run artifacts.",
                    },
                    "run_doctor_enabled": {
                        "type": "bool",
                        "required": True,
                        "description": "Enable doctor/recover readiness checks.",
                    },
                    "run_boundary_validation_enabled": {
                        "type": "bool",
                        "required": True,
                        "description": "Enable phase boundary validation.",
                    },
                    "run_metrics_enabled": {
                        "type": "bool",
                        "required": True,
                        "description": "Enable per-run metrics collection.",
                    },
                    "skill_discovery_mode": {
                        "type": "enum[off,suggest,auto]",
                        "required": True,
                        "description": "Skill selection/discovery mode for task-bearing CLI calls.",
                    },
                    "skill_install_policy": {
                        "type": "enum[manual,admin_approve,allowlisted_auto]",
                        "required": True,
                        "description": "Install policy for externally discovered skills.",
                    },
                    "skill_registry_paths": {
                        "type": "array[string]",
                        "required": True,
                        "description": "Local skill registry paths.",
                    },
                    "skill_allowlisted_sources": {
                        "type": "array[string]",
                        "required": True,
                        "description": "Allowed external skill source types.",
                    },
                    "cli_routing": {
                        "type": "map[string,array[string]]",
                        "required": False,
                        "description": "Per-work-type CLI routing priorities.",
                    },
                    "tool_disclosure": {
                        "type": "enum[full,progressive]",
                        "required": True,
                        "description": "Tool disclosure mode: full sends all schemas, progressive sends summaries.",
                    },
                    "context_window_tokens": {
                        "type": "int",
                        "required": True,
                        "description": "Context window size in tokens.",
                    },
                    "context_reserve_tokens": {
                        "type": "int",
                        "required": True,
                        "description": "Reserved tokens for LLM response generation.",
                    },
                    "summarization_threshold": {
                        "type": "float",
                        "required": True,
                        "description": "Context fill ratio that triggers summarization (0.0–1.0).",
                    },
                    "llm_trace_enabled": {
                        "type": "bool",
                        "required": True,
                        "description": "Enable LLM trace logging to EVENTS.jsonl.",
                    },
                },
            },
            "tools": {"title": "Tools", "type": "map[string,object]", "description": "CLI tool definitions."},
            "mcp": {
                "title": "MCP",
                "type": "object",
                "description": "Built-in MCP bridge settings.",
                "fields": {
                    "enabled": {
                        "type": "bool",
                        "required": True,
                        "description": "Enable built-in MCP bridge.",
                    },
                    "host": {
                        "type": "string",
                        "required": True,
                        "description": "MCP bridge bind host.",
                    },
                    "port": {
                        "type": "int",
                        "required": True,
                        "description": "MCP bridge bind port.",
                    },
                    "token": {
                        "type": "string",
                        "required": False,
                        "description": "MCP bridge bearer token.",
                    },
                },
            },
            "mcp_clients": {"title": "MCP Clients", "type": "array[object]", "description": "External MCP servers."},
            "presets": {"title": "Presets", "type": "array[object]", "description": "Prompt presets."},
            "miniapp": {
                "title": "MiniApp",
                "fields": {
                    "enabled": {
                        "type": "bool",
                        "required": True,
                        "description": "Enable MiniApp server.",
                    },
                    "bind_host": {
                        "type": "string",
                        "required": True,
                        "description": "Shared ingress bind host for MiniApp and webhooks.",
                    },
                    "bind_port": {
                        "type": "int",
                        "required": True,
                        "description": "Shared ingress bind port for MiniApp and webhooks.",
                    },
                    "base_path": {
                        "type": "string",
                        "required": True,
                        "description": "MiniApp base path, fixed /cli-proxy.",
                    },
                    "public_url": {
                        "type": "string",
                        "required": False,
                        "description": "Public absolute URL for Telegram WebApp button.",
                    },
                    "max_edit_file_size_kb": {
                        "type": "int",
                        "required": True,
                        "description": "Max editable text file size in KiB.",
                    },
                    "enable_delete": {
                        "type": "bool",
                        "required": True,
                        "description": "Allow delete file/dir operations.",
                    },
                },
            },
            "thread_mode": {
                "title": "Thread Mode",
                "type": "object",
                "description": "Telegram thread mode settings.",
                "fields": {
                    "enabled": {
                        "type": "bool",
                        "required": True,
                        "description": "Enable thread/topic mode.",
                    },
                    "mode": {
                        "type": "enum[private,group]",
                        "required": True,
                        "description": "Thread mode strategy.",
                    },
                    "topics_chat_id": {
                        "type": "int",
                        "required": False,
                        "description": "Target forum chat id for group mode.",
                    },
                    "topic_title_prefix": {
                        "type": "string",
                        "required": False,
                        "description": "Prefix for auto-created topic titles.",
                    },
                    "inactivity_ttl_sec": {
                        "type": "int",
                        "required": True,
                        "description": "Thread inactivity TTL in seconds.",
                    },
                },
            },
            "webhooks": {
                "title": "Webhooks",
                "type": "object",
                "description": "Telegram webhook server settings.",
                "fields": {
                    "enabled": {
                        "type": "bool",
                        "required": True,
                        "description": "Enable webhook ingress.",
                    },
                    "path": {
                        "type": "string",
                        "required": True,
                        "description": "Webhook HTTP path.",
                    },
                    "public_base_url": {
                        "type": "string",
                        "required": False,
                        "description": "External base URL for webhook runbooks.",
                    },
                    "secret_token": {
                        "type": "string",
                        "required": False,
                        "description": "Expected webhook secret token.",
                    },
                    "request_timeout_sec": {
                        "type": "float",
                        "required": True,
                        "description": "Webhook request timeout in seconds.",
                    },
                    "max_payload_bytes": {
                        "type": "int",
                        "required": True,
                        "description": "Max accepted webhook payload size in bytes.",
                    },
                },
            },
            "scheduler": {
                "title": "Scheduler",
                "type": "object",
                "description": "Background scheduler settings.",
                "fields": {
                    "enabled": {
                        "type": "bool",
                        "required": True,
                        "description": "Enable scheduler service.",
                    },
                    "timezone": {
                        "type": "string",
                        "required": True,
                        "description": "Scheduler timezone.",
                    },
                    "tick_interval_sec": {
                        "type": "int",
                        "required": True,
                        "description": "Scheduler poll interval in seconds.",
                    },
                    "max_concurrent_jobs": {
                        "type": "int",
                        "required": True,
                        "description": "Max concurrent scheduler jobs.",
                    },
                    "job_timeout_sec": {
                        "type": "int",
                        "required": True,
                        "description": "Max scheduler job runtime in seconds.",
                    },
                    "misfire_grace_sec": {
                        "type": "int",
                        "required": True,
                        "description": "Allowed lateness window for missed fires.",
                    },
                },
            },
            "security": {
                "title": "Security",
                "type": "object",
                "description": "Security settings including rate limits.",
                "fields": {
                    "rate_limits.enabled": {
                        "type": "bool",
                        "required": True,
                        "description": "Enable runtime rate limiting.",
                    },
                    "rate_limits.backend": {
                        "type": "enum[sqlite]",
                        "required": True,
                        "description": "Rate limit backend.",
                    },
                    "rate_limits.sqlite_path": {
                        "type": "string",
                        "required": False,
                        "description": "Custom sqlite path for rate limit storage.",
                    },
                    "rate_limits.default": {
                        "type": "object",
                        "required": False,
                        "description": "Default rate limit policy as JSON object.",
                    },
                    "rate_limits.policies": {
                        "type": "map[string,object]",
                        "required": False,
                        "description": "Named rate limit policies as JSON object map.",
                    },
                    "content_screening.enabled": {
                        "type": "bool",
                        "required": True,
                        "description": "Screen external tool output (fetch_page, search_web, ...) for prompt injection.",
                    },
                    "content_screening.mode": {
                        "type": "enum[warn,block]",
                        "required": True,
                        "description": "warn -> prefix warning + original text; block -> original text withheld.",
                    },
                    "content_screening.max_chars": {
                        "type": "int",
                        "required": True,
                        "description": "Truncation limit (head+tail) for the text sent to the classifier.",
                    },
                    "content_screening.timeout_ms": {
                        "type": "int",
                        "required": True,
                        "description": "Content screening classifier call timeout in milliseconds.",
                    },
                },
            },
            "lint_evolution": {
                "title": "Lint Evolution",
                "type": "object",
                "description": "Background lint rule evolution settings.",
                "fields": {
                    "enabled": {
                        "type": "bool",
                        "required": True,
                        "description": "Enable lint evolution on mode session activity.",
                    },
                    "level1_cooldown_hours": {
                        "type": "float",
                        "required": True,
                        "description": "Level 1 cooldown in hours.",
                    },
                    "level2_cooldown_hours": {
                        "type": "float",
                        "required": True,
                        "description": "Level 2 cooldown in hours.",
                    },
                    "level3_cooldown_hours": {
                        "type": "float",
                        "required": True,
                        "description": "Level 3 cooldown in hours.",
                    },
                    "lock_ttl_minutes": {
                        "type": "float",
                        "required": True,
                        "description": "Evolution run lock TTL in minutes.",
                    },
                    "error_retry_hours": {
                        "type": "float",
                        "required": True,
                        "description": "Retry cooldown after errors in hours.",
                    },
                    "fp_growth_threshold_pct": {
                        "type": "float",
                        "required": True,
                        "description": "False-positive growth threshold for canary checks.",
                    },
                    "canary_rolling_days": {
                        "type": "float",
                        "required": True,
                        "description": "Rolling canary window in days.",
                    },
                    "canary_baseline_days": {
                        "type": "float",
                        "required": True,
                        "description": "Baseline canary window in days.",
                    },
                    "canary_max_schema_fields_per_180d": {
                        "type": "int",
                        "required": True,
                        "description": "Max schema fields to add over 180 days.",
                    },
                },
            },
        }
    }
    return _apply_schema_policy_metadata(schema)


def editable_config_fields() -> set[str]:
    schema = config_schema()
    sections = schema.get("sections", {})
    fields: set[str] = set()
    for section_name, section in sections.items():
        section_fields = section.get("fields", {}) if isinstance(section, dict) else {}
        if not isinstance(section_fields, dict):
            continue
        for field_name in section_fields:
            fields.add(f"{section_name}.{field_name}")
    return fields


def validate_draft(app_config_path: str, draft: Dict[str, Any]) -> Tuple[bool, List[str], List[str]]:
    errors: List[str] = []
    warnings: List[str] = []
    if not isinstance(draft, dict):
        return False, ["draft must be an object"], warnings

    if "telegram" not in draft:
        errors.append("telegram section is required")
    if "defaults" not in draft:
        errors.append("defaults section is required")
    if "tools" not in draft:
        errors.append("tools section is required")

    # Validate against the typed config model so MiniApp rejects the same invalid payloads as startup/reload.
    try:
        ValidatedSettings.model_validate(copy.deepcopy(draft))
    except ValidationError as exc:
        for error in exc.errors():
            location = ".".join(str(part) for part in error.get("loc", ())) or "<root>"
            errors.append(f"{location}: {error.get('msg', 'validation error')}")
    except Exception as exc:
        errors.append(str(exc))

    return len(errors) == 0, errors, warnings


def _flatten(prefix: str, value: Any, out: Dict[str, Any]) -> None:
    if isinstance(value, dict):
        for key in sorted(value.keys()):
            child = f"{prefix}.{key}" if prefix else str(key)
            _flatten(child, value[key], out)
        return
    out[prefix] = value


def draft_diff(current: Dict[str, Any], draft: Dict[str, Any]) -> Dict[str, Any]:
    flat_current: Dict[str, Any] = {}
    flat_draft: Dict[str, Any] = {}
    _flatten("", current, flat_current)
    _flatten("", draft, flat_draft)

    changed: List[Dict[str, Any]] = []
    restart_required: List[str] = []
    applied_now: List[str] = []
    not_applied: List[str] = []
    secret_changed: List[str] = []

    all_keys = sorted(set(flat_current.keys()) | set(flat_draft.keys()))
    for key in all_keys:
        before = flat_current.get(key)
        after = flat_draft.get(key)
        if before == after:
            continue
        needs_restart, reloadable, secret = _policy_metadata(key)
        changed.append(
            {
                "field": key,
                "old": _redact_secret_value(key, before),
                "new": _redact_secret_value(key, after),
                "restart_required": needs_restart,
                "reloadable": reloadable,
                "secret": secret,
            }
        )
        if secret:
            secret_changed.append(key)
        if needs_restart:
            restart_required.append(key)
        elif reloadable:
            applied_now.append(key)
        else:
            not_applied.append(key)

    return {
        "changed": changed,
        "restart_required": restart_required,
        "reloadable": applied_now,
        "not_applied": not_applied,
        "secret_changed": secret_changed,
    }


def save_draft(
    app_config_path: str,
    current_revision: str,
    expected_revision: str | None,
    draft: Dict[str, Any],
) -> str:
    logger.warning(
        "legacy bridge used: miniapp save_draft compatibility wrapper",
        extra={
            "action": "config_save",
            "path": app_config_path,
            "reason": "use app.services.config_service.ConfigService.save_draft_with_revision",
        },
    )
    if expected_revision and expected_revision != current_revision:
        raise RevisionConflictError("revision mismatch")

    encoded = yaml.safe_dump(copy.deepcopy(draft), sort_keys=False, allow_unicode=True)
    tmp_path = f"{app_config_path}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        f.write(encoded)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_path, app_config_path)
    logger.info("miniapp config saved", extra={"action": "config_save", "status": "ok", "path": app_config_path})
    return _file_revision(app_config_path)


def config_view_with_revision(config: AppConfig) -> Dict[str, Any]:
    redacted_config, redacted_paths = redacted_config_view(app_config_to_dict(config))
    return {
        "revision": _file_revision(config.path),
        "config": redacted_config,
        "redaction": {
            "sentinel": SECRET_UNCHANGED_SENTINEL,
            "fields": redacted_paths,
        },
    }
