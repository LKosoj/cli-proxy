import asyncio
import copy

import pytest
from aiohttp import web

from app.services.config_apply_policy import classify_config_path
from app.services.config_service import ConfigService, FileConfigProvider
from bot import BotApp
from config import (
    AppConfig,
    DefaultsConfig,
    MCPConfig,
    MiniAppConfig,
    TelegramConfig,
    ToolConfig,
    WebhooksConfig,
    save_config,
)
from miniapp.routes import MiniAppRoutes
from miniapp.services.config_service import (
    SECRET_UNCHANGED_SENTINEL,
    app_config_to_dict,
    config_schema,
    config_view_with_revision,
    draft_diff,
    editable_config_fields,
    restore_redacted_secret_values,
    save_draft,
    validate_draft,
)


def _build_config(tmp_path, *, token: str = "t") -> AppConfig:
    return AppConfig(
        telegram=TelegramConfig(token=token, whitelist_chat_ids=[1], admlist_chat_ids=[1]),
        tools={"dummy": ToolConfig(name="dummy", mode="headless", cmd=["bash", "-lc", "cat"])},
        defaults=DefaultsConfig(
            workdir=str(tmp_path),
            state_path=str(tmp_path / "state.json"),
            toolhelp_path=str(tmp_path / "toolhelp.json"),
            log_path=str(tmp_path / "bot.log"),
        ),
        mcp=MCPConfig(enabled=False),
        mcp_clients=[],
        presets=[],
        path=str(tmp_path / "config.yaml"),
        miniapp=MiniAppConfig(enabled=True, base_path="/cli-proxy"),
    )


def _expected_flags(path: str) -> tuple[bool, bool]:
    policy = classify_config_path(path)
    return policy.apply_mode == "restart_required", policy.apply_mode == "hot_reload"


def _expected_secret(path: str) -> bool:
    return bool(classify_config_path(path).secret)


def _assert_schema_field_matches_policy(schema: dict, path: str) -> None:
    section_name, field_name = path.split(".", 1)
    field = schema["sections"][section_name]["fields"][field_name]
    restart_required, reloadable = _expected_flags(path)
    assert field["restart_required"] is restart_required
    assert field["reloadable"] is reloadable
    assert field["secret"] is _expected_secret(path)


def _assert_diff_field_matches_policy(diff: dict, path: str) -> None:
    fields = {item["field"]: item for item in diff["changed"]}
    restart_required, reloadable = _expected_flags(path)
    assert fields[path]["restart_required"] is restart_required
    assert fields[path]["reloadable"] is reloadable
    assert fields[path]["secret"] is _expected_secret(path)
    if restart_required:
        assert path in diff["restart_required"]
        assert path not in diff["reloadable"]
    elif reloadable:
        assert path not in diff["restart_required"]
        assert path in diff["reloadable"]
        assert path not in diff["not_applied"]
    else:
        assert path not in diff["restart_required"]
        assert path not in diff["reloadable"]
        assert path in diff["not_applied"]
    if _expected_secret(path):
        assert path in diff["secret_changed"]
    else:
        assert path not in diff["secret_changed"]


def miniapp_config_leaves(value, prefix: str = ""):
    if isinstance(value, dict):
        for key in sorted(value):
            child = f"{prefix}.{key}" if prefix else str(key)
            yield from miniapp_config_leaves(value[key], child)
        return
    yield prefix, value


SECRET_SCHEMA_PATHS = (
    "telegram.token",
    "defaults.openai_api_key",
    "defaults.github_token",
    "defaults.gemini_oauth_client_secret",
    "webhooks.secret_token",
    "mcp.token",
)

SECRET_REDACTION_PATHS = (
    "defaults.gemini_oauth_client_secret",
    "defaults.github_token",
    "defaults.jina_api_key",
    "defaults.openai_api_key",
    "defaults.tavily_api_key",
    "defaults.zai_api_key",
    "mcp.token",
    "telegram.token",
    "webhooks.secret_token",
)


def test_config_validate_and_diff(tmp_path) -> None:
    cfg = _build_config(tmp_path)
    save_config(cfg)

    view = config_view_with_revision(cfg)
    draft = dict(view["config"])
    draft["telegram"] = dict(draft["telegram"])
    draft["telegram"]["token"] = "new-token"

    ok, errors, _warnings = validate_draft(cfg.path, draft)
    assert ok is True
    assert errors == []

    d = draft_diff(view["config"], draft)
    fields = {item["field"]: item for item in d["changed"]}
    assert "telegram.token" in fields
    assert fields["telegram.token"]["restart_required"] is True


def test_config_schema_and_diff_match_apply_policy_for_required_paths(tmp_path) -> None:
    cfg = _build_config(tmp_path)
    cfg.tools["codex"] = ToolConfig(
        name="codex",
        mode="headless",
        cmd=["codex", "exec", "{prompt}"],
        prompt_regex="old",
    )
    save_config(cfg)

    schema = config_schema()
    for path in ("webhooks.enabled", "scheduler.enabled", "thread_mode.mode"):
        _assert_schema_field_matches_policy(schema, path)

    view = config_view_with_revision(cfg)
    draft = dict(view["config"])
    draft["webhooks"] = dict(draft["webhooks"])
    draft["webhooks"]["enabled"] = not bool(draft["webhooks"].get("enabled"))
    draft["scheduler"] = dict(draft["scheduler"])
    draft["scheduler"]["enabled"] = not bool(draft["scheduler"].get("enabled"))
    draft["thread_mode"] = dict(draft["thread_mode"])
    draft["thread_mode"]["mode"] = "group"
    draft["thread_mode"]["topics_chat_id"] = 123
    draft["tools"] = dict(draft["tools"])
    draft["tools"]["codex"] = dict(draft["tools"]["codex"])
    draft["tools"]["codex"]["prompt_regex"] = "new"

    ok, errors, _warnings = validate_draft(cfg.path, draft)
    assert ok is True
    assert errors == []

    diff = draft_diff(view["config"], draft)
    for path in (
        "webhooks.enabled",
        "scheduler.enabled",
        "thread_mode.mode",
        "tools.codex.prompt_regex",
    ):
        _assert_diff_field_matches_policy(diff, path)


@pytest.mark.parametrize("path", SECRET_REDACTION_PATHS)
def test_config_schema_secret_metadata_matches_apply_policy_for_required_paths(path: str) -> None:
    schema = config_schema()
    section_name, field_name = path.split(".", 1)
    field = schema["sections"][section_name]["fields"][field_name]

    _assert_schema_field_matches_policy(schema, path)
    assert field["secret"] is True


def test_miniapp_secret_diff_metadata_matches_config_save_result(tmp_path) -> None:
    cfg = _build_config(tmp_path)
    cfg.defaults.openai_api_key = "old-openai"
    cfg.defaults.github_token = "old-github"
    cfg.defaults.zai_api_key = "old-zai"
    cfg.defaults.tavily_api_key = "old-tavily"
    cfg.defaults.jina_api_key = "old-jina"
    cfg.defaults.gemini_oauth_client_secret = "old-gemini"
    cfg.defaults.openai_model = "old-model"
    cfg.mcp = MCPConfig(enabled=True, token="old-mcp")
    cfg.webhooks = WebhooksConfig(enabled=True, secret_token="old-webhook")
    save_config(cfg)

    view = config_view_with_revision(cfg)
    draft = copy.deepcopy(view["config"])
    draft["telegram"]["token"] = "new-token"
    draft["defaults"]["openai_api_key"] = "new-openai"
    draft["defaults"]["github_token"] = "new-github"
    draft["defaults"]["zai_api_key"] = "new-zai"
    draft["defaults"]["tavily_api_key"] = "new-tavily"
    draft["defaults"]["jina_api_key"] = "new-jina"
    draft["defaults"]["gemini_oauth_client_secret"] = "new-gemini"
    draft["defaults"]["openai_model"] = "new-model"
    draft["mcp"]["token"] = "new-mcp"
    draft["webhooks"]["secret_token"] = "new-webhook"

    diff = draft_diff(view["config"], draft)
    result = asyncio.run(
        ConfigService(FileConfigProvider(cfg.path)).save_draft_with_revision(
            restore_redacted_secret_values(app_config_to_dict(cfg), draft),
            expected_revision=view["revision"],
        )
    )
    changed = {item["field"]: item for item in diff["changed"]}
    expected_secret_changed = sorted(SECRET_REDACTION_PATHS)

    assert result.ok is True
    for path in SECRET_REDACTION_PATHS:
        assert changed[path]["secret"] is True
        assert changed[path]["old"] == SECRET_UNCHANGED_SENTINEL
        assert changed[path]["new"] == SECRET_UNCHANGED_SENTINEL
    assert changed["defaults.openai_model"]["secret"] is False
    assert diff["secret_changed"] == expected_secret_changed
    assert result.secret_changed == expected_secret_changed
    assert "defaults.openai_model" not in result.secret_changed


def test_draft_diff_returns_not_applied_fields() -> None:
    diff = draft_diff(
        {"operator_file": {"local_only": "old"}},
        {"operator_file": {"local_only": "new"}},
    )

    assert diff["not_applied"] == ["operator_file.local_only"]
    assert diff["restart_required"] == []
    assert diff["reloadable"] == []
    assert diff["secret_changed"] == []


def test_config_view_redacts_policy_secret_values(tmp_path) -> None:
    cfg = _build_config(tmp_path, token="telegram-real-secret")
    cfg.defaults.openai_api_key = "openai-real-secret"
    cfg.defaults.zai_api_key = "zai-real-secret"
    cfg.defaults.github_token = "github-real-secret"
    cfg.defaults.tavily_api_key = "tavily-real-secret"
    cfg.defaults.jina_api_key = "jina-real-secret"
    cfg.defaults.gemini_oauth_client_secret = "gemini-real-secret"
    cfg.mcp = MCPConfig(enabled=True, token="mcp-real-secret")
    cfg.webhooks = WebhooksConfig(enabled=True, secret_token="webhook-real-secret")
    save_config(cfg)

    view = config_view_with_revision(cfg)
    encoded = repr(view)
    redacted_fields = set(view["redaction"]["fields"])
    policy_secret_fields = {
        path
        for path, _value in miniapp_config_leaves(app_config_to_dict(cfg))
        if classify_config_path(path).secret
    }

    assert view["redaction"]["sentinel"] == SECRET_UNCHANGED_SENTINEL
    assert set(SECRET_REDACTION_PATHS) <= redacted_fields
    assert policy_secret_fields <= redacted_fields
    for value in (
        "telegram-real-secret",
        "openai-real-secret",
        "zai-real-secret",
        "github-real-secret",
        "tavily-real-secret",
        "jina-real-secret",
        "gemini-real-secret",
        "mcp-real-secret",
        "webhook-real-secret",
    ):
        assert value not in encoded
    assert view["config"]["telegram"]["token"] == SECRET_UNCHANGED_SENTINEL
    assert view["config"]["defaults"]["openai_api_key"] == SECRET_UNCHANGED_SENTINEL
    assert view["config"]["defaults"]["zai_api_key"] == SECRET_UNCHANGED_SENTINEL
    assert view["config"]["defaults"]["github_token"] == SECRET_UNCHANGED_SENTINEL
    assert view["config"]["defaults"]["tavily_api_key"] == SECRET_UNCHANGED_SENTINEL
    assert view["config"]["defaults"]["jina_api_key"] == SECRET_UNCHANGED_SENTINEL
    assert view["config"]["defaults"]["gemini_oauth_client_secret"] == SECRET_UNCHANGED_SENTINEL
    assert view["config"]["mcp"]["token"] == SECRET_UNCHANGED_SENTINEL
    assert view["config"]["webhooks"]["secret_token"] == SECRET_UNCHANGED_SENTINEL


def test_redacted_secret_restore_preserves_updates_and_clears(tmp_path) -> None:
    cfg = _build_config(tmp_path, token="old-telegram")
    cfg.defaults.openai_api_key = "old-openai"
    cfg.defaults.github_token = "old-github"
    save_config(cfg)

    view = config_view_with_revision(cfg)
    draft = copy.deepcopy(view["config"])
    draft["defaults"]["openai_api_key"] = "new-openai"
    draft["defaults"]["github_token"] = None
    draft["defaults"]["openai_model"] = "model-after-redaction"
    restored = restore_redacted_secret_values(app_config_to_dict(cfg), draft)

    assert restored["telegram"]["token"] == "old-telegram"
    assert restored["defaults"]["openai_api_key"] == "new-openai"
    assert restored["defaults"]["github_token"] is None
    assert restored["defaults"]["zai_api_key"] is None

    result = asyncio.run(
        ConfigService(FileConfigProvider(cfg.path)).save_draft_with_revision(
            restored,
            expected_revision=view["revision"],
        )
    )

    assert result.ok is True
    assert result.secret_changed == ["defaults.github_token", "defaults.openai_api_key"]
    saved = app_config_to_dict(asyncio.run(ConfigService(FileConfigProvider(cfg.path)).load()))
    assert saved["telegram"]["token"] == "old-telegram"
    assert saved["defaults"]["openai_api_key"] == "new-openai"
    assert saved["defaults"]["github_token"] is None
    assert saved["defaults"]["openai_model"] == "model-after-redaction"


def test_config_schema_and_diff_expose_restart_required_defaults_fields(tmp_path) -> None:
    cfg = _build_config(tmp_path)
    save_config(cfg)

    schema = config_schema()
    defaults_fields = schema["sections"]["defaults"]["fields"]
    miniapp_fields = schema["sections"]["miniapp"]["fields"]
    webhook_fields = schema["sections"]["webhooks"]["fields"]
    lint_fields = schema["sections"]["lint_evolution"]["fields"]
    assert defaults_fields["cli_json_stream_archive_enabled"]["restart_required"] is False
    assert defaults_fields["assistant_preview_enabled"]["restart_required"] is False
    assert defaults_fields["memory_events_enabled"]["restart_required"] is True
    assert defaults_fields["memory_native_cli_hooks_enabled"]["restart_required"] is True
    assert defaults_fields["memory_outcomes_enabled"]["restart_required"] is True
    assert defaults_fields["memory_dreaming_enabled"]["restart_required"] is True
    assert defaults_fields["memory_events_retention_days"]["restart_required"] is True
    assert defaults_fields["memory_events_max_payload_chars"]["restart_required"] is True
    assert defaults_fields["memory_events_redaction_enabled"]["restart_required"] is True
    assert defaults_fields["memory_dreaming_batch_size"]["restart_required"] is True
    assert defaults_fields["run_artifacts_enabled"]["restart_required"] is True
    assert defaults_fields["skill_discovery_mode"]["type"] == "enum[off,suggest,auto]"
    assert defaults_fields["skill_install_policy"]["type"] == "enum[manual,admin_approve,allowlisted_auto]"
    assert miniapp_fields["bind_host"]["restart_required"] is True
    assert miniapp_fields["bind_port"]["type"] == "int"
    assert miniapp_fields["max_edit_file_size_kb"]["restart_required"] is True
    assert webhook_fields["enabled"]["restart_required"] is True
    assert lint_fields["enabled"]["restart_required"] is False
    assert lint_fields["lock_ttl_minutes"]["type"] == "float"

    view = config_view_with_revision(cfg)
    draft = dict(view["config"])
    draft["defaults"] = dict(draft["defaults"])
    draft["defaults"]["cli_json_stream_archive_enabled"] = True
    draft["defaults"]["assistant_preview_enabled"] = True
    draft["defaults"]["run_metrics_enabled"] = False
    draft["defaults"]["skill_discovery_mode"] = "auto"
    draft["defaults"]["skill_registry_paths"] = [".cli-proxy/skills", ".cli-proxy/project-skills"]
    draft["miniapp"] = dict(draft["miniapp"])
    draft["miniapp"]["max_edit_file_size_kb"] = 2048
    draft["webhooks"] = dict(draft["webhooks"])
    draft["webhooks"]["enabled"] = False
    draft["lint_evolution"] = dict(draft["lint_evolution"])
    draft["lint_evolution"]["enabled"] = True

    ok, errors, _warnings = validate_draft(cfg.path, draft)
    assert ok is True
    assert errors == []

    d = draft_diff(view["config"], draft)
    fields = {item["field"]: item for item in d["changed"]}
    assert fields["defaults.cli_json_stream_archive_enabled"]["restart_required"] is False
    assert fields["defaults.cli_json_stream_archive_enabled"]["reloadable"] is True
    assert fields["defaults.assistant_preview_enabled"]["restart_required"] is False
    assert fields["defaults.assistant_preview_enabled"]["reloadable"] is True
    assert fields["defaults.run_metrics_enabled"]["restart_required"] is True
    assert fields["defaults.run_metrics_enabled"]["reloadable"] is False
    assert fields["defaults.skill_discovery_mode"]["restart_required"] is True
    assert fields["defaults.skill_registry_paths"]["reloadable"] is False
    assert fields["miniapp.max_edit_file_size_kb"]["restart_required"] is True
    assert fields["miniapp.max_edit_file_size_kb"]["reloadable"] is False
    assert fields["webhooks.enabled"]["restart_required"] is True
    assert fields["webhooks.enabled"]["reloadable"] is False
    assert fields["lint_evolution.enabled"]["restart_required"] is False
    assert fields["lint_evolution.enabled"]["reloadable"] is True


def test_editable_config_fields_returns_schema_dot_notation() -> None:
    schema = config_schema()
    expected = {
        f"{section_name}.{field_name}"
        for section_name, section in schema["sections"].items()
        for field_name in section.get("fields", {})
    }

    fields = editable_config_fields()

    assert isinstance(fields, set)
    assert fields == expected
    assert {"telegram.token", "defaults.workdir", "miniapp.bind_port"} <= fields
    assert "tools" not in fields
    assert "mcp_clients" not in fields


def test_webhooks_enabled_restart_required_parity(tmp_path) -> None:
    cfg = _build_config(tmp_path)
    save_config(cfg)

    schema = config_schema()
    _assert_schema_field_matches_policy(schema, "webhooks.enabled")

    view = config_view_with_revision(cfg)
    draft = dict(view["config"])
    draft["webhooks"] = dict(draft["webhooks"])
    draft["webhooks"]["enabled"] = False

    ok, errors, _warnings = validate_draft(cfg.path, draft)
    assert ok is True
    assert errors == []

    diff = draft_diff(view["config"], draft)
    _assert_diff_field_matches_policy(diff, "webhooks.enabled")


def test_miniapp_max_edit_file_size_restart_required_parity(tmp_path) -> None:
    cfg = _build_config(tmp_path)
    save_config(cfg)

    schema = config_schema()
    _assert_schema_field_matches_policy(schema, "miniapp.max_edit_file_size_kb")

    view = config_view_with_revision(cfg)
    draft = dict(view["config"])
    draft["miniapp"] = dict(draft["miniapp"])
    draft["miniapp"]["max_edit_file_size_kb"] = 1024

    ok, errors, _warnings = validate_draft(cfg.path, draft)
    assert ok is True
    assert errors == []

    diff = draft_diff(view["config"], draft)
    _assert_diff_field_matches_policy(diff, "miniapp.max_edit_file_size_kb")


def test_config_validate_rejects_typed_invalid_thread_mode_draft(tmp_path) -> None:
    cfg = _build_config(tmp_path)
    save_config(cfg)

    view = config_view_with_revision(cfg)
    draft = dict(view["config"])
    draft["thread_mode"] = {
        "enabled": True,
        "mode": "group",
        "topics_chat_id": None,
    }

    ok, errors, _warnings = validate_draft(cfg.path, draft)

    assert ok is False
    assert any("thread_mode" in item for item in errors)
    assert any("topics_chat_id" in item for item in errors)


def test_config_validate_rejects_legacy_defaults_flags_instead_of_normalizing(tmp_path) -> None:
    cfg = _build_config(tmp_path)
    save_config(cfg)

    view = config_view_with_revision(cfg)
    draft = dict(view["config"])
    draft["defaults"] = dict(draft["defaults"])
    draft["defaults"]["schema_normalizer_v2_enabled"] = True

    ok, errors, _warnings = validate_draft(cfg.path, draft)

    assert ok is False
    assert any("defaults.schema_normalizer_v2_enabled" in item for item in errors)
    assert any("Extra inputs are not permitted" in item for item in errors)


def test_config_validate_rejects_legacy_chat_session_token_in_miniapp_draft(tmp_path) -> None:
    cfg = _build_config(tmp_path)
    save_config(cfg)

    view = config_view_with_revision(cfg)
    draft = dict(view["config"])
    draft["miniapp"] = dict(draft["miniapp"])
    draft["miniapp"]["selected_session_uid"] = "1:s1"

    ok, errors, _warnings = validate_draft(cfg.path, draft)

    assert ok is False
    assert any("miniapp.selected_session_uid" in item for item in errors)
    assert any("Extra inputs are not permitted" in item for item in errors)


def test_config_validate_sequential_runs_with_different_intents_do_not_leak_state(tmp_path) -> None:
    cfg = _build_config(tmp_path, token="intent-a-token")
    save_config(cfg)

    view = config_view_with_revision(cfg)

    draft_a = dict(view["config"])
    draft_a["miniapp"] = dict(draft_a["miniapp"])
    draft_a["miniapp"]["selected_session_uid"] = "1:intent-a"

    ok_a, errors_a, _warnings_a = validate_draft(cfg.path, draft_a)

    draft_b = dict(view["config"])
    draft_b["telegram"] = dict(draft_b["telegram"])
    draft_b["telegram"]["token"] = "intent-b-token"

    ok_b, errors_b, _warnings_b = validate_draft(cfg.path, draft_b)

    assert ok_a is False
    assert any("miniapp.selected_session_uid" in item for item in errors_a)
    assert ok_b is True
    assert errors_b == []


def test_miniapp_save_draft_preserves_unicode_literals(tmp_path) -> None:
    cfg = _build_config(tmp_path)
    save_config(cfg)

    view = config_view_with_revision(cfg)
    draft = dict(view["config"])
    draft["presets"] = [
        {
            "name": "Русский пресет",
            "prompt": "Сделай краткую сводку по-русски",
        }
    ]

    save_draft(
        cfg.path,
        current_revision=view["revision"],
        expected_revision=view["revision"],
        draft=draft,
    )

    saved = tmp_path.joinpath("config.yaml").read_text(encoding="utf-8")
    assert "Русский пресет" in saved
    assert "Сделай краткую сводку по-русски" in saved


@pytest.mark.parametrize("legacy_session_uid", ["1:s1", "-100777:s1"])
def test_miniapp_routes_reject_legacy_chat_session_uid_entrypoint_input(tmp_path, legacy_session_uid: str) -> None:
    cfg = _build_config(tmp_path)
    save_config(cfg)
    routes = MiniAppRoutes(BotApp(cfg))

    with pytest.raises(web.HTTPBadRequest) as exc:
        routes._resolve_visible_session(
            user_id=1,
            is_admin=True,
            session_uid=legacy_session_uid,
        )

    assert str(exc.value.reason or "") == (
        "session_uid chat_id:session_id format is not supported; use canonical session_uid"
    )


def test_config_schema_content_screening_fields_are_restart_required() -> None:
    schema = config_schema()
    for path in (
        "security.content_screening.enabled",
        "security.content_screening.mode",
        "security.content_screening.max_chars",
        "security.content_screening.timeout_ms",
    ):
        _assert_schema_field_matches_policy(schema, path)
        restart_required, reloadable = _expected_flags(path)
        assert restart_required is True
        assert reloadable is False

    security_fields = schema["sections"]["security"]["fields"]
    assert "content_screening.model" not in security_fields
