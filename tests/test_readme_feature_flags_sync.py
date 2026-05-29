import subprocess
import sys
from pathlib import Path

from app.services.config_apply_policy import classify_config_path


def _assert_readme_config_policy_matches_backend(text: str) -> None:
    hot_reload_paths = [
        "defaults.cli_json_stream_archive_enabled",
        "defaults.assistant_preview_enabled",
        "defaults.pending_input_confirmation_enabled",
        "telegram.whitelist_chat_ids",
        "telegram.admlist_chat_ids",
        "telegram.user_workdirs",
        "telegram.user_modes",
        "miniapp.public_url",
        "miniapp.enable_delete",
        "mcp.token",
        "webhooks.secret_token",
        "defaults.openai_api_key",
        "defaults.zai_api_key",
        "defaults.github_token",
        "defaults.tavily_api_key",
        "defaults.jina_api_key",
    ]
    restart_required_paths = [
        ("telegram.token", "`telegram.token`"),
        ("telegram.connection_pool_size", "connection_pool_size"),
        ("miniapp.enabled", "`miniapp.enabled`"),
        ("miniapp.bind_host", "`miniapp.bind_host`"),
        ("miniapp.bind_port", "`miniapp.bind_port`"),
        ("miniapp.base_path", "`miniapp.base_path`"),
        ("miniapp.max_edit_file_size_kb", "`miniapp.max_edit_file_size_kb`"),
        ("defaults.memory_events_enabled", "`defaults.memory_events_enabled`"),
        ("defaults.memory_native_cli_hooks_enabled", "`defaults.memory_native_cli_hooks_enabled`"),
        ("defaults.memory_outcomes_enabled", "`defaults.memory_outcomes_enabled`"),
        ("defaults.memory_dreaming_enabled", "`defaults.memory_dreaming_enabled`"),
        ("defaults.memory_events_retention_days", "`defaults.memory_events_retention_days`"),
        ("defaults.memory_events_max_payload_chars", "`defaults.memory_events_max_payload_chars`"),
        ("defaults.memory_events_redaction_enabled", "`defaults.memory_events_redaction_enabled`"),
        ("defaults.memory_dreaming_batch_size", "`defaults.memory_dreaming_batch_size`"),
        ("defaults.run_artifacts_enabled", "`defaults.run_artifacts_enabled`"),
        ("defaults.run_artifacts_retention_days", "`defaults.run_artifacts_retention_days`"),
        ("defaults.run_doctor_enabled", "`defaults.run_doctor_enabled`"),
        ("defaults.run_boundary_validation_enabled", "`defaults.run_boundary_validation_enabled`"),
        ("defaults.run_metrics_enabled", "`defaults.run_metrics_enabled`"),
        ("defaults.skill_discovery_mode", "`defaults.skill_discovery_mode`"),
        ("defaults.skill_install_policy", "`defaults.skill_install_policy`"),
        ("defaults.skill_registry_paths", "`defaults.skill_registry_paths`"),
        ("defaults.skill_allowlisted_sources", "`defaults.skill_allowlisted_sources`"),
        ("defaults.gemini_oauth_client_secret", "`defaults.gemini_oauth_client_secret`"),
        ("mcp.enabled", "`mcp.enabled`"),
        ("mcp.host", "`mcp.host`"),
        ("mcp.port", "`mcp.port`"),
        ("mcp_clients", "`mcp_clients`"),
        ("thread_mode.mode", "`thread_mode.*`"),
        ("scheduler.enabled", "`scheduler.*`"),
        ("webhooks.enabled", "`webhooks.enabled`"),
        ("webhooks.path", "`webhooks.path`"),
        ("webhooks.public_base_url", "`webhooks.public_base_url`"),
        ("webhooks.request_timeout_sec", "`webhooks.request_timeout_sec`"),
        ("webhooks.max_payload_bytes", "`webhooks.max_payload_bytes`"),
    ]

    assert "ConfigApplyPolicy" in text
    assert "`hot_reload`" in text
    assert "`restart_required`" in text
    for path in hot_reload_paths:
        assert classify_config_path(path).apply_mode == "hot_reload"
        assert f"`{path}`" in text
    for path, readme_token in restart_required_paths:
        assert classify_config_path(path).apply_mode == "restart_required"
        assert readme_token in text

    assert "  - `defaults.*`;" not in text
    assert "  - `mcp`, `mcp_clients`;" not in text
    assert "  - `thread_mode`, `webhooks`, `scheduler`;" not in text


def test_readme_feature_flags_are_documented_in_ru_and_en():
    repo_root = Path(__file__).resolve().parents[1]
    ru = (repo_root / "README.md").read_text(encoding="utf-8")
    en = (repo_root / "README_EN.MD").read_text(encoding="utf-8")

    expected_tokens = [
        "defaults.codebase_mapper_usage",
        "defaults.memory_events_enabled",
        "defaults.memory_native_cli_hooks_enabled",
        "defaults.memory_outcomes_enabled",
        "defaults.memory_dreaming_enabled",
        "defaults.cli_json_stream_archive_enabled",
        "defaults.assistant_preview_enabled",
        "manager_auto_commit",
        "webmaster_validation_max_fix_iterations",
    ]
    for token in expected_tokens:
        assert token in ru
        assert token in en


def test_readme_project_prompts_are_documented_in_ru_and_en():
    repo_root = Path(__file__).resolve().parents[1]
    ru = (repo_root / "README.md").read_text(encoding="utf-8")
    en = (repo_root / "README_EN.MD").read_text(encoding="utf-8")

    shared_tokens = [
        ".cli-proxy/.manager/prompt/prompts.yaml",
        ".cli-proxy/.manager/prompt/learning.yaml",
        ".cli-proxy/.webmaster/prompt/prompts.yaml",
        ".cli-proxy/.webmaster/prompt/learning.yaml",
        "SessionManager.create",
        "logger.exception",
    ]
    for token in shared_tokens:
        assert token in ru
        assert token in en

    assert "при создании сессии" in ru
    assert "при включении режима" in ru
    assert "не запускается" in ru

    assert "when a session is created" in en
    assert "when mode is enabled" in en
    assert "mode is not started" in en


def test_readme_manager_dynamic_decomposition_is_documented_in_ru_and_en():
    repo_root = Path(__file__).resolve().parents[1]
    ru = (repo_root / "README.md").read_text(encoding="utf-8")
    en = (repo_root / "README_EN.MD").read_text(encoding="utf-8")

    shared_tokens = [
        "_min_tasks_dynamic(analysis)",
        "MIN_TASKS_FLOOR",
        "MIN_TASKS_PER_REQ",
        "MIN_TASKS_PER_REMAINING",
        "ATOMICITY_MAX_REQS_PER_TASK",
        "TASK_COUNT_BELOW_MIN",
        "TASK_TOO_BROAD_REQ_COVERAGE",
        "config.yaml",
    ]
    for token in shared_tokens:
        assert token in ru
        assert token in en

    assert "не выносятся в `config.yaml`" in ru
    assert "not exposed in `config.yaml`" in en


def test_readme_handoff_and_status_model_are_documented_in_ru_and_en():
    repo_root = Path(__file__).resolve().parents[1]
    ru = (repo_root / "README.md").read_text(encoding="utf-8")
    en = (repo_root / "README_EN.MD").read_text(encoding="utf-8")

    shared_tokens = [
        "session.orchestrator.last_mode_output",
        "session.orchestrator.pending_input",
        "pending_questions",
        "active_plugin_flow",
        "queue_origin",
        "build_common_mode_stage",
    ]
    for token in shared_tokens:
        assert token in ru
        assert token in en

    assert "Режим Вебмастер" in ru
    assert "Webmaster mode" in en


def test_readme_thread_mode_webhooks_and_scheduler_are_documented_in_ru_and_en():
    repo_root = Path(__file__).resolve().parents[1]
    ru = (repo_root / "README.md").read_text(encoding="utf-8")
    en = (repo_root / "README_EN.MD").read_text(encoding="utf-8")

    shared_tokens = [
        "thread_mode.mode=group",
        "thread_mode.topics_chat_id",
        "bot.get_me().has_topics_enabled",
        "BotFather",
        "Group Topics",
        "webhooks.path: /webhooks/telegram",
        "WebhookReceivedEvent",
        "ScheduledJobEvent",
        "notification_target.telegram_session_uid",
        "NotificationQueueService",
    ]
    for token in shared_tokens:
        assert token in ru
        assert token in en

    assert "shared HTTP ingress" in en
    assert "shared HTTP ingress" in ru
    assert "базовым рабочим режимом" in ru
    assert "base operating mode" in en


def test_readme_config_reload_policy_matches_backend_policy_ru():
    repo_root = Path(__file__).resolve().parents[1]
    ru = (repo_root / "README.md").read_text(encoding="utf-8")

    _assert_readme_config_policy_matches_backend(ru)
    assert "операторское действие" in ru
    assert "не обещают hot-apply" in ru


def test_readme_config_reload_policy_matches_backend_policy_en():
    repo_root = Path(__file__).resolve().parents[1]
    en = (repo_root / "README_EN.MD").read_text(encoding="utf-8")

    _assert_readme_config_policy_matches_backend(en)
    assert "operator action" in en
    assert "do not promise hot-apply" in en


def test_readme_run_operations_and_skill_runtime_are_documented_in_ru_and_en():
    repo_root = Path(__file__).resolve().parents[1]
    ru = (repo_root / "README.md").read_text(encoding="utf-8")
    en = (repo_root / "README_EN.MD").read_text(encoding="utf-8")

    shared_tokens = [
        "Run operations: `doctor` / `recover` / `resume`",
        "RECOVERY.json",
        "Desktop",
        "MiniApp",
        "defaults.run_doctor_enabled",
        "defaults.run_boundary_validation_enabled",
        "defaults.run_metrics_enabled",
        "defaults.skill_discovery_mode",
        "defaults.skill_install_policy",
        "defaults.skill_registry_paths",
        "defaults.skill_allowlisted_sources",
        "project-local registry",
        ".skill_install_approval_ledger.json",
        "/admin skills list|approve <approval_id>|reject <approval_id>",
        "manual_review_required",
    ]
    for token in shared_tokens:
        assert token in ru
        assert token in en

    assert "slash-команда управляет CLI resume-токеном" in ru
    assert "slash command manages the CLI resume token" in en


def test_readme_docs_do_not_use_ordered_marker_with_closing_parenthesis_or_em_dash():
    repo_root = Path(__file__).resolve().parents[1]
    for name in ("README.md", "README_EN.MD"):
        text = (repo_root / name).read_text(encoding="utf-8")
        lines = text.splitlines()
        assert "—" not in text
        assert not any(line.lstrip().startswith(tuple(f"{i})" for i in range(10))) for line in lines)


def test_readme_docs_do_not_contain_broken_prompt_apostrophe_forms():
    repo_root = Path(__file__).resolve().parents[1]
    ru = (repo_root / "README.md").read_text(encoding="utf-8")
    en = (repo_root / "README_EN.MD").read_text(encoding="utf-8")

    assert "prompt's" not in ru
    assert "prompt'ов" not in ru
    assert "Prompt'ы" not in ru
    assert "prompt's" not in en


def test_flake8_ignores_readme_markdown_when_paths_are_passed_explicitly():
    repo_root = Path(__file__).resolve().parents[1]
    flake8_bin = repo_root / ".venv" / "bin" / "flake8"
    if not flake8_bin.exists():
        flake8_bin = Path(sys.executable).resolve().parent / "flake8"

    result = subprocess.run(
        [str(flake8_bin), "README.md", "README_EN.MD"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
