import logging
from pathlib import Path

import pytest
import yaml

from app.config_runtime.loader import ENV_OVERRIDE_PREFIX, ValidatedSettings, load_validated_settings


def _base_payload(tmp_path) -> dict:
    return {
        "telegram": {
            "token": "yaml-token",
            "whitelist_chat_ids": [1],
        },
        "tools": {
            "codex": {
                "mode": "headless",
                "cmd": ["codex"],
            }
        },
        "defaults": {
            "workdir": str(tmp_path),
        },
    }


def _write_config(tmp_path, payload: dict):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(payload, sort_keys=False, allow_unicode=False), encoding="utf-8")
    return config_path


def test_loader_reads_dotenv_and_env_overrides(tmp_path, monkeypatch) -> None:
    """
    Priority order (lowest to highest):
    1. config.yaml
    2. legacy env aliases from .env/process env
    3. CLI_PROXY__* overrides from .env/process env (highest)

    Within env overlays, process env wins over .env for the same variable name.
    Legacy aliases (OPENAI_API_KEY, etc.) also override YAML values.
    """
    config_path = _write_config(tmp_path, _base_payload(tmp_path))
    (tmp_path / ".env").write_text(
        "\n".join(
            [
                "OPENAI_API_KEY=dotenv-openai",
                f"{ENV_OVERRIDE_PREFIX}TELEGRAM__TOKEN=dotenv-token",
                f"{ENV_OVERRIDE_PREFIX}MCP__PORT=9876",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    # Process env overrides .env for CLI_PROXY__ variables.
    monkeypatch.setenv(f"{ENV_OVERRIDE_PREFIX}TELEGRAM__TOKEN", "process-token")
    # Legacy aliases are env-based overrides too; process env wins for the same alias name.
    monkeypatch.setenv("OPENAI_API_KEY", "process-openai")

    settings = load_validated_settings(str(config_path))

    assert isinstance(settings, ValidatedSettings)
    # CLI_PROXY__TELEGRAM__TOKEN from process env overrides config.yaml
    assert settings.telegram.token == "process-token"
    # Legacy alias OPENAI_API_KEY from process env overrides YAML/defaults.
    assert settings.defaults.openai_api_key == "process-openai"
    # MCP_PORT from .env (not overridden by process env or config)
    assert settings.mcp.port == 9876


def test_loader_legacy_env_aliases_override_non_null_yaml_values(tmp_path, monkeypatch) -> None:
    payload = _base_payload(tmp_path)
    payload["defaults"]["openai_api_key"] = "yaml-openai"
    config_path = _write_config(tmp_path, payload)
    (tmp_path / ".env").write_text("OPENAI_API_KEY=dotenv-openai\n", encoding="utf-8")

    settings_from_dotenv = load_validated_settings(str(config_path))
    assert settings_from_dotenv.defaults.openai_api_key == "dotenv-openai"

    monkeypatch.setenv("OPENAI_API_KEY", "process-openai")
    settings_from_process = load_validated_settings(str(config_path))
    assert settings_from_process.defaults.openai_api_key == "process-openai"


def test_loader_accepts_case_insensitive_env_override_prefix(tmp_path, monkeypatch) -> None:
    config_path = _write_config(tmp_path, _base_payload(tmp_path))
    monkeypatch.setenv("cli_proxy__telegram__token", "lowercase-prefix-token")

    settings = load_validated_settings(str(config_path))

    assert settings.telegram.token == "lowercase-prefix-token"


def test_loader_preserves_tool_env_override_key_case(tmp_path, monkeypatch) -> None:
    config_path = _write_config(tmp_path, _base_payload(tmp_path))
    monkeypatch.setenv(f"{ENV_OVERRIDE_PREFIX}TOOLS__CODEX__ENV__OPENAI_API_KEY", "process-openai")

    settings = load_validated_settings(str(config_path))

    assert settings.tools["codex"].env == {"OPENAI_API_KEY": "process-openai"}


def test_loader_does_not_treat_tool_named_env_as_env_mapping(tmp_path, monkeypatch) -> None:
    payload = _base_payload(tmp_path)
    payload["tools"]["env"] = {
        "mode": "headless",
        "cmd": ["env"],
    }
    config_path = _write_config(tmp_path, payload)
    monkeypatch.setenv(f"{ENV_OVERRIDE_PREFIX}TOOLS__ENV__CMD", '["printenv"]')

    settings = load_validated_settings(str(config_path))

    assert settings.tools["env"].cmd == ["printenv"]


def test_loader_fail_fast_logs_aggregated_validation_errors(tmp_path, caplog) -> None:
    payload = _base_payload(tmp_path)
    payload["telegram"]["token"] = ""
    payload["thread_mode"] = {
        "mode": "group",
        "topics_chat_id": None,
    }
    config_path = _write_config(tmp_path, payload)

    caplog.set_level(logging.ERROR)

    with pytest.raises(SystemExit) as exc_info:
        load_validated_settings(str(config_path))

    assert exc_info.value.code == 1
    assert f"validated config load failed path={config_path}" in caplog.text
    assert "telegram.token" in caplog.text
    assert "thread_mode" in caplog.text
    assert "topics_chat_id is required when mode='group'" in caplog.text


def test_loader_uses_current_env_on_each_call(tmp_path, monkeypatch) -> None:
    config_path = _write_config(tmp_path, _base_payload(tmp_path))
    env_name = f"{ENV_OVERRIDE_PREFIX}TELEGRAM__TOKEN"

    monkeypatch.setenv(env_name, "first-run-token")
    first_settings = load_validated_settings(str(config_path))

    monkeypatch.setenv(env_name, "second-run-token")
    second_settings = load_validated_settings(str(config_path))

    assert first_settings.telegram.token == "first-run-token"
    assert second_settings.telegram.token == "second-run-token"


def test_loader_enables_thread_mode_by_default_when_section_is_omitted(tmp_path) -> None:
    config_path = _write_config(tmp_path, _base_payload(tmp_path))

    settings = load_validated_settings(str(config_path))

    assert settings.thread_mode.enabled is True
    assert settings.thread_mode.mode == "private"
    assert settings.thread_mode.topics_chat_id is None


def test_loader_enables_webhooks_by_default_when_section_is_omitted(tmp_path) -> None:
    config_path = _write_config(tmp_path, _base_payload(tmp_path))

    settings = load_validated_settings(str(config_path))

    assert settings.webhooks.enabled is True
    assert settings.webhooks.path == "/webhooks/telegram"
    assert settings.webhooks.request_timeout_sec == 30.0


def test_loader_accepts_miniapp_bind_settings(tmp_path) -> None:
    payload = _base_payload(tmp_path)
    payload["miniapp"] = {
        "enabled": True,
        "bind_host": "0.0.0.0",
        "bind_port": 8099,
    }
    config_path = _write_config(tmp_path, payload)

    settings = load_validated_settings(str(config_path))

    assert settings.miniapp.enabled is True
    assert settings.miniapp.bind_host == "0.0.0.0"
    assert settings.miniapp.bind_port == 8099


def test_loader_parses_repository_example_file() -> None:
    settings = load_validated_settings("config_example.yaml")

    assert isinstance(settings, ValidatedSettings)
    assert settings.thread_mode.enabled is True
    assert settings.thread_mode.mode == "group"
    assert settings.thread_mode.topics_chat_id == -1001234567890
    assert settings.webhooks.enabled is True
    assert settings.webhooks.public_base_url == "https://bot.example.com"
    assert settings.scheduler.enabled is True
    assert settings.scheduler.timezone == "Europe/Moscow"


def test_loader_parses_repository_local_config_file() -> None:
    settings = load_validated_settings("config.yaml")

    assert isinstance(settings, ValidatedSettings)
    assert len(settings.mcp_clients) >= 1


def test_loader_rejects_legacy_defaults_flags(tmp_path, caplog) -> None:
    payload = _base_payload(tmp_path)
    payload["defaults"]["schema_normalizer_v2_enabled"] = True
    config_path = _write_config(tmp_path, payload)

    caplog.set_level(logging.ERROR)

    with pytest.raises(SystemExit) as exc_info:
        load_validated_settings(str(config_path))

    assert exc_info.value.code == 1
    assert "defaults.schema_normalizer_v2_enabled" in caplog.text
    assert "Extra inputs are not permitted" in caplog.text


def test_loader_rejects_legacy_tool_name_field(tmp_path, caplog) -> None:
    payload = _base_payload(tmp_path)
    payload["tools"]["codex"]["name"] = "codex"
    config_path = _write_config(tmp_path, payload)

    caplog.set_level(logging.ERROR)

    with pytest.raises(SystemExit) as exc_info:
        load_validated_settings(str(config_path))

    assert exc_info.value.code == 1
    assert "tools.codex.name" in caplog.text
    assert "Extra inputs are not permitted" in caplog.text


def _assert_section_fields_have_comments(text: str, section_name: str, expected_fields: list[str]) -> None:
    lines = text.splitlines()
    start_index = next(i for i, line in enumerate(lines) if line.startswith(f"{section_name}:"))

    section_lines: list[str] = []
    for line in lines[start_index + 1:]:
        if line and not line.startswith(" ") and not line.startswith("#"):
            break
        section_lines.append(line)

    def _previous_nonempty(idx: int) -> str:
        for prev_idx in range(idx - 1, -1, -1):
            candidate = section_lines[prev_idx].strip()
            if candidate:
                return candidate
        return ""

    seen: set[str] = set()
    for idx, line in enumerate(section_lines):
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or ":" not in stripped:
            continue
        key = stripped.split(":", 1)[0].strip()
        if key not in expected_fields:
            continue
        seen.add(key)
        after_colon = line.split(":", 1)[1]
        has_inline_comment = "#" in after_colon
        has_previous_comment = _previous_nonempty(idx).startswith("#")
        assert has_inline_comment or has_previous_comment, f"{section_name}.{key} is missing a comment"

    assert seen == set(expected_fields), f"{section_name} does not contain expected fields"


def test_repository_configs_document_thread_mode_webhooks_scheduler_runbook() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    text = (repo_root / "config_example.yaml").read_text(encoding="utf-8")
    assert "topics_chat_id is required when mode='group'" in text
    assert "BotFather -> /mybots -> Bot Settings -> Group Topics" in text
    assert "notification_target.telegram_session_uid" in text

    _assert_section_fields_have_comments(
        text,
        "thread_mode",
        ["enabled", "mode", "topics_chat_id", "topic_title_prefix", "inactivity_ttl_sec"],
    )
    _assert_section_fields_have_comments(
        text,
        "webhooks",
        [
            "enabled",
            "path",
            "public_base_url",
            "secret_token",
            "request_timeout_sec",
            "max_payload_bytes",
        ],
    )
    _assert_section_fields_have_comments(
        text,
        "scheduler",
        [
            "enabled",
            "timezone",
            "tick_interval_sec",
            "max_concurrent_jobs",
            "job_timeout_sec",
            "misfire_grace_sec",
        ],
    )


def test_config_example_documents_run_operations_and_skill_policy_impact() -> None:
    text = Path("config_example.yaml").read_text(encoding="utf-8")

    expected_fragments = [
        "doctor/recover/resume",
        "METRICS.json",
        "off=не выбирать skills; suggest=подмешивать только уже доступные; auto=искать и ставить allowlisted skills локально в проект",
        (
            "manual=только предложить skill; "
            "admin_approve=создать pending approval "
            "в .cli-proxy/skills/.skill_install_approval_ledger.json "
            "и ждать explicit approve/reject от администратора; "
            "allowlisted_auto=автоставить allowlisted skill "
            "в project-local registry"
        ),
        "project-local registry для discover/install/promote flows",
        "без allowlist auto-install не произойдёт",
    ]
    for fragment in expected_fragments:
        assert fragment in text


def test_bot_main_stops_before_polling_when_typed_loader_fails(monkeypatch) -> None:
    import bot

    calls: list[str] = []

    def _fail_fast(_path: str):
        raise SystemExit(1)

    def _unexpected_load_config(_path: str):
        calls.append("load_config")
        raise AssertionError("load_config should not run after typed config fail-fast")

    def _unexpected_build_app(_config):
        calls.append("build_app")
        raise AssertionError("build_app should not run after typed config fail-fast")

    monkeypatch.setattr(bot, "load_validated_settings", _fail_fast)
    monkeypatch.setattr(bot, "load_config", _unexpected_load_config)
    monkeypatch.setattr(bot, "build_app", _unexpected_build_app)
    monkeypatch.setattr(bot, "load_dotenv_near", lambda *args, **kwargs: {})

    with pytest.raises(SystemExit) as exc_info:
        bot.main()

    assert exc_info.value.code == 1
    assert calls == []
