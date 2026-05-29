from app.services.config_apply_policy import ConfigApplyPolicy, classify_config_path


def test_classifies_telegram_token_as_restart_required_secret() -> None:
    policy = classify_config_path("telegram.token")

    assert isinstance(policy, ConfigApplyPolicy)
    assert policy.path_pattern == "telegram.token"
    assert policy.apply_mode == "restart_required"
    assert policy.surface == "runtime"
    assert policy.secret is True


def test_classifies_tool_prompt_regex_as_hot_reload() -> None:
    policy = classify_config_path("tools.codex.prompt_regex")

    assert policy.path_pattern == "tools.*"
    assert policy.apply_mode == "hot_reload"
    assert policy.surface == "runtime"
    assert policy.secret is False


def test_classifies_scheduler_as_restart_required() -> None:
    policy = classify_config_path("scheduler.enabled")

    assert policy.path_pattern == "scheduler.*"
    assert policy.apply_mode == "restart_required"
    assert policy.surface == "runtime"
    assert policy.secret is False


def test_classifies_existing_runtime_reloadable_fields() -> None:
    assert classify_config_path("telegram.whitelist_chat_ids").apply_mode == "hot_reload"
    assert classify_config_path("miniapp.public_url").apply_mode == "hot_reload"
    assert classify_config_path("mcp.token").apply_mode == "hot_reload"
    assert classify_config_path("webhooks.secret_token").apply_mode == "hot_reload"
    assert classify_config_path("mcp.token").secret is True
    assert classify_config_path("webhooks.secret_token").secret is True


def test_classifies_hot_reload_prefix_groups() -> None:
    assert classify_config_path("presets.0.prompt").apply_mode == "hot_reload"
    assert classify_config_path("security.rate_limits.enabled").apply_mode == "hot_reload"
    assert classify_config_path("lint_evolution.enabled").apply_mode == "hot_reload"


def test_classifies_restart_required_prefix_groups() -> None:
    assert classify_config_path("thread_mode.mode").apply_mode == "restart_required"
    assert classify_config_path("mcp_clients.0.enabled").apply_mode == "restart_required"
    assert classify_config_path("mcp_clients.demo.enabled").apply_mode == "restart_required"
    assert classify_config_path("webhooks.enabled").apply_mode == "restart_required"
    assert classify_config_path("miniapp.bind_port").apply_mode == "restart_required"
    assert classify_config_path("mcp.enabled").apply_mode == "restart_required"


def test_classifies_default_secrets_as_reloadable_secret_fields() -> None:
    for path in ("defaults.openai_api_key", "defaults.github_token"):
        policy = classify_config_path(path)

        assert policy.path_pattern == path
        assert policy.apply_mode == "hot_reload"
        assert policy.secret is True


def test_classifies_gemini_oauth_secret_as_restart_required_secret() -> None:
    policy = classify_config_path("defaults.gemini_oauth_client_secret")

    assert policy.path_pattern == "defaults.gemini_oauth_client_secret"
    assert policy.apply_mode == "restart_required"
    assert policy.secret is True


def test_classifies_unknown_and_blank_paths_as_not_supported() -> None:
    assert classify_config_path("").apply_mode == "not_supported"
    assert classify_config_path("unknown.section").apply_mode == "not_supported"


def test_normalizes_extra_dots_and_whitespace() -> None:
    policy = classify_config_path("  tools . codex . prompt_regex  ")

    assert policy.path_pattern == "tools.*"
    assert policy.apply_mode == "hot_reload"
