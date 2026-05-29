from __future__ import annotations

import yaml

from app.config_runtime.loader import ENV_OVERRIDE_PREFIX
from app.services.config_service import ConfigService, FileConfigProvider
from config import AppConfig, load_config
from miniapp.services.config_service import app_config_to_dict


def _base_payload(tmp_path) -> dict:
    return {
        "telegram": {
            "token": "yaml-token",
            "whitelist_chat_ids": [1],
            "admlist_chat_ids": [2],
            "user_workdirs": {"11": str(tmp_path / "project")},
            "user_modes": {"11": "all", "22": ["agent", "manager", "direct_cli"]},
        },
        "tools": {
            "codex": {
                "mode": "headless",
                "cmd": "codex",
                "interactive_cmd": ["codex"],
                "env": {"OPENAI_API_KEY": None},
            }
        },
        "defaults": {
            "workdir": str(tmp_path),
            "cli_json_stream_archive_enabled": True,
            "assistant_preview_enabled": True,
            "pending_input_confirmation_enabled": False,
            "memory_events_enabled": True,
            "memory_native_cli_hooks_enabled": True,
            "memory_outcomes_enabled": True,
            "memory_dreaming_enabled": True,
            "memory_events_retention_days": 21,
            "memory_events_max_payload_chars": 4096,
            "memory_events_redaction_enabled": True,
            "memory_dreaming_batch_size": 7,
            "run_artifacts_enabled": False,
            "run_artifacts_retention_days": 14,
            "run_doctor_enabled": False,
            "run_boundary_validation_enabled": False,
            "run_metrics_enabled": False,
            "skill_discovery_mode": "auto",
            "skill_install_policy": "admin_approve",
            "skill_registry_paths": [".cli-proxy/skills", ".cli-proxy/project-skills"],
            "gemini_oauth_client_secret": "gemini-config-secret",
            "skill_allowlisted_sources": [
                "local:global-registry",
                "registry:npx-skills",
            ],
            "cli_routing": {"default": ["codex", "claude"]},
        },
        "mcp_clients": [
            {
                "name": "filesystem",
                "transport": "stdio",
                "cmd": ["node", "filesystem.js"],
            },
            {
                "name": "context7",
                "transport": "http",
                "url": "http://127.0.0.1:8888/servers/context7/mcp",
            },
        ],
        "miniapp": {
            "enabled": True,
            "public_url": "https://example.com/cli-proxy",
        },
        "thread_mode": {
            "enabled": True,
            "mode": "group",
            "topics_chat_id": -1001234567890,
            "topic_title_prefix": "team",
            "inactivity_ttl_sec": 7200,
        },
        "webhooks": {
            "enabled": True,
            "path": "/telegram/webhook",
            "public_base_url": "https://bot.example.com",
            "secret_token": "secret",
            "request_timeout_sec": 15.5,
            "max_payload_bytes": 4096,
        },
        "scheduler": {
            "enabled": True,
            "timezone": "Europe/Moscow",
            "tick_interval_sec": 30,
            "max_concurrent_jobs": 3,
            "job_timeout_sec": 600,
            "misfire_grace_sec": 10,
        },
        "security": {
            "rate_limits": {
                "enabled": True,
                "backend": "sqlite",
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


def _write_config(tmp_path, payload: dict):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(payload, sort_keys=False, allow_unicode=False), encoding="utf-8")
    return config_path


def test_load_config_adapts_validated_settings_to_app_config(tmp_path) -> None:
    config_path = _write_config(tmp_path, _base_payload(tmp_path))

    cfg = load_config(str(config_path))

    assert isinstance(cfg, AppConfig)
    assert cfg.path == str(config_path)
    assert cfg.telegram.user_workdirs == {11: [str(tmp_path / "project")]}
    assert cfg.telegram.user_modes == {11: "all", 22: ["agent", "manager", "direct_cli"]}
    assert cfg.tools["codex"].cmd == ["codex"]
    assert cfg.defaults.cli_routing == {"default": ["codex", "claude"]}
    assert cfg.defaults.cli_json_stream_archive_enabled is True
    assert cfg.defaults.assistant_preview_enabled is True
    assert cfg.defaults.pending_input_confirmation_enabled is False
    assert cfg.defaults.memory_events_enabled is True
    assert cfg.defaults.memory_native_cli_hooks_enabled is True
    assert cfg.defaults.memory_outcomes_enabled is True
    assert cfg.defaults.memory_dreaming_enabled is True
    assert cfg.defaults.memory_events_retention_days == 21
    assert cfg.defaults.memory_events_max_payload_chars == 4096
    assert cfg.defaults.memory_events_redaction_enabled is True
    assert cfg.defaults.memory_dreaming_batch_size == 7
    assert cfg.defaults.run_artifacts_enabled is False
    assert cfg.defaults.run_artifacts_retention_days == 14
    assert cfg.defaults.run_doctor_enabled is False
    assert cfg.defaults.run_boundary_validation_enabled is False
    assert cfg.defaults.run_metrics_enabled is False
    assert cfg.defaults.skill_discovery_mode == "auto"
    assert cfg.defaults.skill_install_policy == "admin_approve"
    assert cfg.defaults.skill_registry_paths == [".cli-proxy/skills", ".cli-proxy/project-skills"]
    assert cfg.defaults.gemini_oauth_client_secret == "gemini-config-secret"
    assert cfg.defaults.skill_allowlisted_sources == [
        "local:global-registry",
        "registry:npx-skills",
    ]
    assert [client.name for client in cfg.mcp_clients] == ["filesystem", "context7"]
    assert cfg.miniapp.public_url == "https://example.com/cli-proxy"
    assert cfg.thread_mode.mode == "group"
    assert cfg.thread_mode.topics_chat_id == -1001234567890
    assert cfg.webhooks.path == "/telegram/webhook"
    assert cfg.webhooks.request_timeout_sec == 15.5
    assert cfg.scheduler.timezone == "Europe/Moscow"
    assert cfg.scheduler.max_concurrent_jobs == 3
    assert cfg.security.rate_limits.default is not None
    assert cfg.security.rate_limits.default.burst_limit == 3
    assert cfg.security.rate_limits.policies["miniapp.auth"].limit == 5


def test_load_config_maps_miniapp_bind_settings_to_app_config(tmp_path) -> None:
    payload = _base_payload(tmp_path)
    payload["miniapp"]["bind_host"] = "0.0.0.0"
    payload["miniapp"]["bind_port"] = 8099
    config_path = _write_config(tmp_path, payload)

    cfg = load_config(str(config_path))
    serialized = app_config_to_dict(cfg)

    assert cfg.miniapp.bind_host == "0.0.0.0"
    assert cfg.miniapp.bind_port == 8099
    assert serialized["miniapp"]["bind_host"] == "0.0.0.0"
    assert serialized["miniapp"]["bind_port"] == 8099


def test_load_config_uses_fresh_env_overrides_on_each_call(tmp_path, monkeypatch) -> None:
    config_path = _write_config(tmp_path, _base_payload(tmp_path))
    env_name = f"{ENV_OVERRIDE_PREFIX}WEBHOOKS__REQUEST_TIMEOUT_SEC"

    monkeypatch.setenv(env_name, "8.5")
    first_cfg = load_config(str(config_path))

    monkeypatch.setenv(env_name, "9.5")
    second_cfg = load_config(str(config_path))

    assert first_cfg.webhooks.request_timeout_sec == 8.5
    assert second_cfg.webhooks.request_timeout_sec == 9.5


def test_config_serialization_preserves_new_sections(tmp_path) -> None:
    config_path = _write_config(tmp_path, _base_payload(tmp_path))
    cfg = load_config(str(config_path))

    serialized = app_config_to_dict(cfg)

    assert serialized["thread_mode"]["topic_title_prefix"] == "team"
    assert serialized["webhooks"]["request_timeout_sec"] == 15.5
    assert serialized["scheduler"]["job_timeout_sec"] == 600
    assert serialized["security"]["rate_limits"]["default"]["burst_window_sec"] == 5
    assert serialized["defaults"]["cli_json_stream_archive_enabled"] is True
    assert serialized["defaults"]["assistant_preview_enabled"] is True
    assert serialized["defaults"]["pending_input_confirmation_enabled"] is False
    assert serialized["defaults"]["run_artifacts_enabled"] is False
    assert serialized["defaults"]["skill_discovery_mode"] == "auto"
    assert serialized["defaults"]["skill_registry_paths"] == [".cli-proxy/skills", ".cli-proxy/project-skills"]


def test_config_service_serialize_config_includes_new_sections(tmp_path) -> None:
    config_path = _write_config(tmp_path, _base_payload(tmp_path))
    provider = FileConfigProvider(str(config_path))
    service = ConfigService(provider)
    cfg = load_config(str(config_path))

    rendered = __import__("asyncio").run(service.serialize_config(cfg))
    loaded = yaml.safe_load(rendered)

    assert loaded["thread_mode"]["topics_chat_id"] == -1001234567890
    assert loaded["webhooks"]["public_base_url"] == "https://bot.example.com"
    assert loaded["scheduler"]["misfire_grace_sec"] == 10
    assert loaded["defaults"]["pending_input_confirmation_enabled"] is False
    assert loaded["security"]["rate_limits"]["policies"]["miniapp.auth"]["burst_limit"] == 2
