from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from app.config_runtime.models import AppConfigModel


def _build_payload() -> dict:
    return {
        "telegram": {
            "token": "telegram-token",
            "whitelist_chat_ids": [123456789],
            "admlist_chat_ids": [123456789],
            "user_workdirs": {"123456789": "/srv/git_projects"},
            "user_modes": {
                "123456789": ["agent", "direct_cli"],
                "987654321": "all",
            },
            "connection_pool_size": 8,
            "connect_timeout_sec": 20,
            "read_timeout_sec": 20,
            "write_timeout_sec": 20,
            "pool_timeout_sec": 10,
            "polling_timeout_sec": 5,
            "poll_interval_sec": 0,
        },
        "tools": {
            "codex": {
                "mode": "headless",
                "cmd": "codex",
                "interactive_cmd": ["codex"],
                "resume_cmd": ["codex", "exec", "resume", "{resume}"],
                "image_cmd": ["--image", "{image}"],
                "help_cmd": "/status",
                "env": {
                    "OPENAI_API_KEY": None,
                },
                "auto_commands": ["/approvals full"],
                "separate_stderr": True,
            }
        },
        "defaults": {
            "workdir": "/srv/git_projects",
            "idle_timeout_sec": 100,
            "summary_max_chars": 4000,
            "html_filename_prefix": "cli-output",
            "state_path": "state.json",
            "desktop_state_path": "desktop_state.json",
            "toolhelp_path": "toolhelp.json",
            "openai_api_key": None,
            "openai_model": None,
            "openai_big_model": None,
            "openai_base_url": "https://api.openai.com",
            "zai_api_key": None,
            "tavily_api_key": None,
            "jina_api_key": None,
            "github_token": None,
            "gemini_oauth_client_secret": None,
            "log_path": "./logs/bot.log",
            "image_temp_dir": ".cli-proxy/.attachments",
            "image_max_mb": 10,
            "memory_max_kb": 32,
            "memory_compact_target_kb": 24,
            "memory_events_enabled": False,
            "memory_native_cli_hooks_enabled": False,
            "memory_outcomes_enabled": False,
            "memory_dreaming_enabled": False,
            "memory_events_retention_days": 30,
            "memory_events_max_payload_chars": 6000,
            "memory_events_redaction_enabled": True,
            "memory_dreaming_batch_size": 20,
            "clarification_enabled": True,
            "pending_input_confirmation_enabled": True,
            "default_cli": "codex",
            "clarification_keywords": ["уточни", "неясно"],
            "manager_max_tasks": 10,
            "manager_max_attempts": 3,
            "manager_decompose_timeout_sec": 1200,
            "manager_dev_timeout_sec": 3600,
            "manager_review_timeout_sec": 1200,
            "analyst_use_cli_timeout_sec": 3600,
            "webmaster_use_cli_timeout_sec": 3600,
            "webmaster_validation_max_fix_iterations": 2,
            "manager_dev_report_max_chars": 20000,
            "manager_auto_resume": True,
            "manager_auto_commit": True,
            "manager_response_archive": True,
            "cli_json_stream_archive_enabled": False,
            "assistant_preview_enabled": True,
            "codebase_mapper_usage": "auto",
            "run_artifacts_enabled": True,
            "run_artifacts_retention_days": 30,
            "run_doctor_enabled": True,
            "run_boundary_validation_enabled": True,
            "run_metrics_enabled": True,
            "skill_discovery_mode": "suggest",
            "skill_install_policy": "manual",
            "skill_registry_paths": [".cli-proxy/skills", ".cli-proxy/custom-skills"],
            "skill_allowlisted_sources": [
                "local:global-registry",
                "local:project-registry",
                "path:absolute",
                "registry:npx-skills",
                "ref:owner-repo-skill",
            ],
            "cli_routing": {
                "default": ["codex", "claude"],
            },
        },
        "mcp": {
            "enabled": False,
            "host": "127.0.0.1",
            "port": 8765,
            "token": None,
        },
        "mcp_clients": [
            {
                "name": "context7",
                "transport": "http",
                "url": "http://127.0.0.1:8888/servers/context7/mcp",
                "timeout_ms": 30000,
            },
            {
                "name": "chrome-devtools",
                "transport": "http",
                "url": "http://127.0.0.1:8888/servers/chrome-devtools/mcp",
                "timeout_ms": 30000,
            },
        ],
        "presets": [
            {
                "name": "tests",
                "prompt": "Запусти тесты и дай краткий отчёт.",
            }
        ],
        "miniapp": {
            "enabled": True,
            "base_path": "/cli-proxy",
            "public_url": "",
            "max_edit_file_size_kb": 5120,
            "enable_delete": True,
        },
        "thread_mode": {
            "enabled": True,
            "mode": "group",
            "topics_chat_id": -1001234567890,
            "topic_title_prefix": "cli",
            "inactivity_ttl_sec": 3600,
        },
        "webhooks": {
            "enabled": False,
            "path": "/webhooks/telegram",
            "public_base_url": None,
            "secret_token": None,
            "request_timeout_sec": 30,
            "max_payload_bytes": 1048576,
        },
        "scheduler": {
            "enabled": True,
            "timezone": "Europe/Moscow",
            "tick_interval_sec": 60,
            "max_concurrent_jobs": 2,
            "job_timeout_sec": 3600,
            "misfire_grace_sec": 30,
        },
        "security": {
            "rate_limits": {
                "enabled": True,
                "backend": "sqlite",
                "sqlite_path": "/tmp/security-rate-limits.sqlite3",
                "default": {
                    "limit": 10,
                    "window_sec": 60,
                    "burst_limit": 3,
                    "burst_window_sec": 5,
                },
                "policies": {
                    "miniapp.auth": {
                        "limit": 5,
                        "window_sec": 60,
                        "burst_limit": 2,
                        "burst_window_sec": 10,
                    }
                },
            }
        },
    }


def _set_nested(payload: dict, dotted_path: str, value) -> None:
    current = payload
    parts = dotted_path.split(".")
    for part in parts[:-1]:
        current = current[part]
    current[parts[-1]] = value


def test_config_models_parse_valid_payload_with_normalization() -> None:
    payload = _build_payload()

    model = AppConfigModel.model_validate(payload)

    assert model.telegram.user_workdirs[123456789] == ["/srv/git_projects"]
    assert model.telegram.user_modes[123456789] == ["agent", "direct_cli"]
    assert model.telegram.user_modes[987654321] == "all"
    assert model.tools["codex"].cmd == ["codex"]
    assert model.mcp_clients[0].transport == "http"
    assert model.mcp_clients[1].name == "chrome-devtools"
    assert model.thread_mode.topics_chat_id == -1001234567890
    assert model.scheduler.max_concurrent_jobs == 2
    assert model.security.rate_limits.default is not None
    assert model.security.rate_limits.default.burst_limit == 3
    assert model.security.rate_limits.policies["miniapp.auth"].burst_window_sec == 10
    assert model.defaults.run_artifacts_enabled is True
    assert model.defaults.run_artifacts_retention_days == 30
    assert model.defaults.memory_events_enabled is False
    assert model.defaults.memory_native_cli_hooks_enabled is False
    assert model.defaults.memory_outcomes_enabled is False
    assert model.defaults.memory_dreaming_enabled is False
    assert model.defaults.memory_events_retention_days == 30
    assert model.defaults.memory_events_max_payload_chars == 6000
    assert model.defaults.memory_events_redaction_enabled is True
    assert model.defaults.memory_dreaming_batch_size == 20
    assert model.defaults.cli_json_stream_archive_enabled is False
    assert model.defaults.assistant_preview_enabled is True
    assert model.defaults.pending_input_confirmation_enabled is True
    assert model.defaults.run_doctor_enabled is True
    assert model.defaults.run_boundary_validation_enabled is True
    assert model.defaults.run_metrics_enabled is True
    assert model.defaults.skill_discovery_mode == "suggest"
    assert model.defaults.skill_install_policy == "manual"
    assert model.defaults.skill_registry_paths == [".cli-proxy/skills", ".cli-proxy/custom-skills"]
    assert model.defaults.skill_allowlisted_sources == [
        "local:global-registry",
        "local:project-registry",
        "path:absolute",
        "registry:npx-skills",
        "ref:owner-repo-skill",
    ]


def test_config_models_parse_repository_template() -> None:
    payload = yaml.safe_load(Path("config_example.yaml").read_text(encoding="utf-8"))

    model = AppConfigModel.model_validate(payload)

    assert model.thread_mode.enabled is True
    assert model.thread_mode.mode == "group"
    assert model.thread_mode.topics_chat_id == -1001234567890
    assert model.webhooks.enabled is True
    assert model.webhooks.path == "/webhooks/telegram"
    assert model.scheduler.enabled is True
    assert model.scheduler.timezone == "Europe/Moscow"
    assert model.security.rate_limits.backend == "sqlite"
    assert {entry.name for entry in model.mcp_clients} >= {"context7", "chrome-devtools"}
    assert model.defaults.run_artifacts_enabled is True
    assert model.defaults.cli_json_stream_archive_enabled is False
    assert model.defaults.assistant_preview_enabled is False
    assert model.defaults.pending_input_confirmation_enabled is True
    assert model.defaults.toolhelp_path == "toolhelp.json"
    assert model.defaults.skill_discovery_mode == "suggest"
    assert model.lint_evolution.enabled is False
    assert model.lint_evolution.canary_max_schema_fields_per_180d == 3


def test_thread_mode_is_enabled_by_default_when_section_is_omitted() -> None:
    payload = _build_payload()
    payload.pop("thread_mode")

    model = AppConfigModel.model_validate(payload)

    assert model.thread_mode.enabled is True
    assert model.thread_mode.mode == "private"
    assert model.thread_mode.topics_chat_id is None


def test_defaults_reject_invalid_skill_discovery_mode() -> None:
    payload = _build_payload()
    payload["defaults"]["skill_discovery_mode"] = "always"

    with pytest.raises(ValidationError) as exc_info:
        AppConfigModel.model_validate(payload)

    assert "defaults.skill_discovery_mode" in str(exc_info.value)


def test_webhooks_are_enabled_by_default_when_section_is_omitted() -> None:
    payload = _build_payload()
    payload.pop("webhooks")

    model = AppConfigModel.model_validate(payload)

    assert model.webhooks.enabled is True
    assert model.webhooks.path == "/webhooks/telegram"
    assert model.webhooks.request_timeout_sec == 30


def test_thread_mode_chat_alias_is_rejected() -> None:
    payload = _build_payload()
    payload["thread_mode"]["mode"] = "chat"
    payload["thread_mode"]["topics_chat_id"] = None

    with pytest.raises(ValidationError) as exc_info:
        AppConfigModel.model_validate(payload)

    assert "thread_mode.mode" in str(exc_info.value)
    assert "private" in str(exc_info.value)
    assert "group" in str(exc_info.value)


@pytest.mark.parametrize(
    ("field_path", "value", "location_fragment"),
    [
        ("defaults.schema_normalizer_v2_enabled", True, "schema_normalizer_v2_enabled"),
        ("tools.codex.name", "codex", "tools.codex.name"),
    ],
)
def test_config_models_reject_legacy_loader_normalized_fields(
    field_path: str,
    value,
    location_fragment: str,
) -> None:
    payload = _build_payload()
    _set_nested(payload, field_path, value)

    with pytest.raises(ValidationError) as exc_info:
        AppConfigModel.model_validate(payload)

    assert "Extra inputs are not permitted" in str(exc_info.value)
    assert location_fragment in str(exc_info.value)


@pytest.mark.parametrize(
    ("field_path", "value", "error_fragment"),
    [
        ("telegram.token", "", "String should have at least 1 character"),
        ("telegram.connection_pool_size", 0, "greater than or equal to 1"),
        ("tools.codex.cmd", [], "at least 1 item"),
        ("mcp.port", 0, "greater than or equal to 1"),
        ("webhooks.request_timeout_sec", 0, "greater than 0"),
        ("scheduler.max_concurrent_jobs", 0, "greater than or equal to 1"),
        ("security.rate_limits.default.limit", 0, "greater than or equal to 1"),
    ],
)
def test_config_models_reject_field_constraints(field_path: str, value, error_fragment: str) -> None:
    payload = _build_payload()
    _set_nested(payload, field_path, value)

    with pytest.raises(ValidationError) as exc_info:
        AppConfigModel.model_validate(payload)

    assert error_fragment in str(exc_info.value)


def test_thread_mode_group_requires_topics_chat_id() -> None:
    payload = _build_payload()
    payload["thread_mode"]["topics_chat_id"] = None

    with pytest.raises(ValidationError) as exc_info:
        AppConfigModel.model_validate(payload)

    assert "topics_chat_id is required when mode='group'" in str(exc_info.value)


def test_rate_limits_require_default_or_policies_when_enabled() -> None:
    payload = _build_payload()
    payload["security"]["rate_limits"]["default"] = None
    payload["security"]["rate_limits"]["policies"] = {}

    with pytest.raises(ValidationError) as exc_info:
        AppConfigModel.model_validate(payload)

    assert "default or policies is required when rate_limits.enabled=true" in str(exc_info.value)


def test_rate_limits_burst_requires_complete_valid_pair() -> None:
    payload = _build_payload()
    payload["security"]["rate_limits"]["default"]["burst_window_sec"] = None

    with pytest.raises(ValidationError) as exc_info:
        AppConfigModel.model_validate(payload)

    assert "burst_limit and burst_window_sec must be set together" in str(exc_info.value)


def test_mcp_client_stdio_requires_cmd() -> None:
    payload = _build_payload()
    payload["mcp_clients"] = [
        {
            "name": "filesystem",
            "transport": "stdio",
            "cmd": [],
            "timeout_ms": 30000,
        }
    ]

    with pytest.raises(ValidationError) as exc_info:
        AppConfigModel.model_validate(payload)

    assert "cmd is required when transport='stdio'" in str(exc_info.value)


def test_mcp_client_http_requires_url() -> None:
    payload = _build_payload()
    payload["mcp_clients"] = [
        {
            "name": "context7",
            "transport": "http",
            "timeout_ms": 30000,
        }
    ]

    with pytest.raises(ValidationError) as exc_info:
        AppConfigModel.model_validate(payload)

    assert "url is required when transport='http'" in str(exc_info.value)
