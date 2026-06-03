from __future__ import annotations

import asyncio
from pathlib import Path

import yaml

from app.config_runtime.models import DefaultsConfigModel, LintEvolutionConfigModel
from app.services.config_apply_policy import classify_config_path
from app.services.config_service import ConfigService, FileConfigProvider
from config import load_config


def _payload(tmp_path) -> dict:
    return {
        "telegram": {
            "token": "token",
            "whitelist_chat_ids": [1],
            "user_languages": {111111111: "en"},
        },
        "tools": {
            "codex": {
                "mode": "headless",
                "cmd": ["codex"],
                "interactive_cmd": ["codex"],
            }
        },
        "defaults": {
            "workdir": str(tmp_path),
            "default_language": "en",
            "clarification_keywords_by_lang": {
                "ru": ["уточни", "уточните"],
                "en": ["clarify", "unclear"],
            },
            "cli_json_stream_archive_enabled": True,
            "assistant_preview_enabled": True,
            "pending_input_confirmation_enabled": False,
            "memory_events_enabled": False,
            "memory_native_cli_hooks_enabled": False,
            "memory_outcomes_enabled": False,
            "memory_dreaming_enabled": False,
            "memory_events_retention_days": 30,
            "memory_events_max_payload_chars": 6000,
            "memory_events_redaction_enabled": True,
            "memory_dreaming_batch_size": 20,
            "run_artifacts_enabled": True,
            "run_artifacts_retention_days": 45,
            "run_doctor_enabled": True,
            "run_boundary_validation_enabled": True,
            "run_metrics_enabled": True,
            "skill_discovery_mode": "suggest",
            "skill_install_policy": "allowlisted_auto",
            "skill_registry_paths": [".cli-proxy/skills", ".cli-proxy/project-skills"],
            "skill_allowlisted_sources": [
                "local:global-registry",
                "ref:owner-repo-skill",
            ],
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
                "headers": {"Authorization": "Bearer token"},
            },
        ],
    }


def _write_config(tmp_path, payload: dict):
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(payload, sort_keys=False, allow_unicode=False), encoding="utf-8")
    return path


def test_serialize_config_roundtrip_preserves_canonical_mcp_clients(tmp_path) -> None:
    path = _write_config(tmp_path, _payload(tmp_path))
    service = ConfigService(FileConfigProvider(str(path)))

    first_cfg = load_config(str(path))
    first_serialized = asyncio.run(service.serialize_config(first_cfg))
    first_payload = yaml.safe_load(first_serialized)

    assert list(first_payload.keys()) == [
        "telegram",
        "tools",
        "defaults",
        "mcp",
        "miniapp",
        "thread_mode",
        "webhooks",
        "scheduler",
        "security",
        "lint_evolution",
        "mcp_clients",
        "presets",
    ]
    assert [entry["name"] for entry in first_payload["mcp_clients"]] == ["filesystem", "context7"]
    assert first_payload["mcp_clients"][1]["headers"] == {"Authorization": "Bearer token"}
    assert first_payload["defaults"]["cli_json_stream_archive_enabled"] is True
    assert first_payload["defaults"]["assistant_preview_enabled"] is True
    assert first_payload["defaults"]["pending_input_confirmation_enabled"] is False
    assert first_payload["defaults"]["memory_events_enabled"] is False
    assert first_payload["defaults"]["memory_native_cli_hooks_enabled"] is False
    assert first_payload["defaults"]["memory_outcomes_enabled"] is False
    assert first_payload["defaults"]["memory_dreaming_enabled"] is False
    assert first_payload["defaults"]["memory_events_retention_days"] == 30
    assert first_payload["defaults"]["memory_events_max_payload_chars"] == 6000
    assert first_payload["defaults"]["memory_events_redaction_enabled"] is True
    assert first_payload["defaults"]["memory_dreaming_batch_size"] == 20
    assert first_payload["defaults"]["run_artifacts_retention_days"] == 45
    assert first_payload["defaults"]["toolhelp_path"] == "toolhelp.json"
    assert first_payload["defaults"]["skill_install_policy"] == "allowlisted_auto"
    assert first_payload["defaults"]["skill_allowlisted_sources"] == [
        "local:global-registry",
        "ref:owner-repo-skill",
    ]
    # YAML may emit the int key as int or str depending on dumper; normalize and
    # assert the full mapping deterministically.
    ul = {int(k): v for k, v in first_payload["telegram"]["user_languages"].items()}
    assert ul == {111111111: "en"}
    assert first_payload["defaults"]["default_language"] == "en"
    by_lang = first_payload["defaults"].get("clarification_keywords_by_lang", {})
    assert by_lang.get("en") == ["clarify", "unclear"]

    path.write_text(first_serialized, encoding="utf-8")

    second_cfg = load_config(str(path))
    second_serialized = asyncio.run(service.serialize_config(second_cfg))

    assert second_serialized == first_serialized


def test_config_service_diff_against_disk_is_stable_for_canonical_mcp_clients(tmp_path) -> None:
    path = _write_config(tmp_path, _payload(tmp_path))
    service = ConfigService(FileConfigProvider(str(path)))
    cfg = asyncio.run(service.load())

    diff = asyncio.run(service.diff_against_disk(cfg))

    assert diff == ""


def test_save_atomic_no_false_diff_for_unchanged_config(tmp_path) -> None:
    path = _write_config(tmp_path, _payload(tmp_path))
    original_text = path.read_text(encoding="utf-8")
    service = ConfigService(FileConfigProvider(str(path)))
    cfg = asyncio.run(service.load())

    result = asyncio.run(service.save_atomic(cfg))

    assert result.changed is False
    assert result.diff == ""
    assert result.backup_path is None
    assert path.read_text(encoding="utf-8") == original_text


def test_serialize_config_preserves_unicode_literals(tmp_path) -> None:
    payload = _payload(tmp_path)
    payload["presets"] = [
        {
            "name": "Русский пресет",
            "prompt": "Сделай краткую сводку",
        }
    ]
    path = _write_config(tmp_path, payload)
    service = ConfigService(FileConfigProvider(str(path)))

    cfg = load_config(str(path))
    serialized = asyncio.run(service.serialize_config(cfg))

    assert "Русский пресет" in serialized
    assert "Сделай краткую сводку" in serialized


def test_user_languages_int_key_round_trip(tmp_path) -> None:
    """user_languages: int keys in Python → str keys in YAML → back to int on load."""
    payload = _payload(tmp_path)
    path = _write_config(tmp_path, payload)

    # yaml.safe_dump may store int keys as ints directly; the round-trip is via ConfigService
    service = ConfigService(FileConfigProvider(str(path)))
    cfg = asyncio.run(service.load())

    # After load, keys must be int
    assert 111111111 in cfg.telegram.user_languages
    assert cfg.telegram.user_languages[111111111] == "en"

    # After serialize + reload, still int keys
    serialized = asyncio.run(service.serialize_config(cfg))
    path.write_text(serialized, encoding="utf-8")
    cfg2 = load_config(str(path))
    assert cfg2.telegram.user_languages[111111111] == "en"


def test_config_files_match_runtime_policy_contract() -> None:
    root = Path(__file__).resolve().parents[1]
    example = yaml.safe_load((root / "config_example.yaml").read_text(encoding="utf-8"))

    assert "toolhelp_path" in DefaultsConfigModel.model_fields
    assert example["defaults"]["toolhelp_path"] == "toolhelp.json"

    assert "lint_evolution" in example
    assert set(example["lint_evolution"]) == set(LintEvolutionConfigModel.model_fields)
    for key in sorted(example["lint_evolution"]):
        policy = classify_config_path(f"lint_evolution.{key}")
        assert policy.apply_mode == "hot_reload"
        assert policy.surface == "runtime"
        assert policy.secret is False

    assert classify_config_path("telegram.user_languages").apply_mode == "hot_reload"
    assert classify_config_path("defaults.default_language").apply_mode == "hot_reload"
    assert "default_language" in DefaultsConfigModel.model_fields
    assert example["defaults"]["default_language"] == "ru"

    assert "clarification_keywords_by_lang" in DefaultsConfigModel.model_fields
    assert "clarification_keywords_by_lang" in example["defaults"]

    local_config_path = root / "config.yaml"
    if local_config_path.exists():
        config = yaml.safe_load(local_config_path.read_text(encoding="utf-8"))
        assert config["defaults"]["toolhelp_path"] == "toolhelp.json"
        assert "lint_evolution" in config
        assert set(config["lint_evolution"]) == set(LintEvolutionConfigModel.model_fields)
