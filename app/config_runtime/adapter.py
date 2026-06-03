from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .loader import ValidatedSettings

if TYPE_CHECKING:
    from config import AppConfig


def _copy_mapping(value: dict[Any, Any] | None) -> dict[Any, Any] | None:
    if value is None:
        return None
    return dict(value)


def adapt_validated_settings(settings: ValidatedSettings, *, path: str) -> "AppConfig":
    from config import (
        AppConfig,
        DefaultsConfig,
        LintEvolutionConfig,
        MCPClientServerConfig,
        MCPConfig,
        MiniAppConfig,
        PresetConfig,
        SchedulerConfig,
        SecurityConfig,
        SecurityRateLimitPolicyConfig,
        SecurityRateLimitsConfig,
        TelegramConfig,
        ThreadModeConfig,
        ToolConfig,
        WebhooksConfig,
    )

    telegram = TelegramConfig(
        token=settings.telegram.token,
        whitelist_chat_ids=list(settings.telegram.whitelist_chat_ids),
        admlist_chat_ids=list(settings.telegram.admlist_chat_ids),
        user_workdirs={int(chat_id): list(paths) for chat_id, paths in settings.telegram.user_workdirs.items()},
        user_modes={
            int(chat_id): ("all" if value == "all" else list(value))
            for chat_id, value in settings.telegram.user_modes.items()
        },
        user_languages={int(uid): lang for uid, lang in settings.telegram.user_languages.items()},
        connection_pool_size=settings.telegram.connection_pool_size,
        connect_timeout_sec=settings.telegram.connect_timeout_sec,
        read_timeout_sec=settings.telegram.read_timeout_sec,
        write_timeout_sec=settings.telegram.write_timeout_sec,
        pool_timeout_sec=settings.telegram.pool_timeout_sec,
        polling_timeout_sec=settings.telegram.polling_timeout_sec,
        poll_interval_sec=settings.telegram.poll_interval_sec,
    )

    tools = {
        name: ToolConfig(
            name=name,
            mode=tool.mode,
            cmd=list(tool.cmd),
            enabled=tool.enabled,
            headless_cmd=list(tool.headless_cmd) if tool.headless_cmd else None,
            resume_cmd=list(tool.resume_cmd) if tool.resume_cmd else None,
            image_cmd=list(tool.image_cmd) if tool.image_cmd else None,
            interactive_cmd=list(tool.interactive_cmd) if tool.interactive_cmd else None,
            prompt_regex=tool.prompt_regex,
            resume_regex=tool.resume_regex,
            help_cmd=tool.help_cmd,
            env=_copy_mapping(tool.env),
            auto_commands=list(tool.auto_commands) if tool.auto_commands else None,
            separate_stderr=tool.separate_stderr,
            no_session_persistence_on_fresh=tool.no_session_persistence_on_fresh,
        )
        for name, tool in settings.tools.items()
    }

    defaults = DefaultsConfig(
        workdir=settings.defaults.workdir,
        idle_timeout_sec=settings.defaults.idle_timeout_sec,
        summary_max_chars=settings.defaults.summary_max_chars,
        html_filename_prefix=settings.defaults.html_filename_prefix,
        state_path=settings.defaults.state_path,
        desktop_state_path=settings.defaults.desktop_state_path,
        toolhelp_path=settings.defaults.toolhelp_path,
        openai_api_key=settings.defaults.openai_api_key,
        openai_model=settings.defaults.openai_model,
        openai_big_model=settings.defaults.openai_big_model,
        openai_base_url=settings.defaults.openai_base_url,
        zai_api_key=settings.defaults.zai_api_key,
        tavily_api_key=settings.defaults.tavily_api_key,
        jina_api_key=settings.defaults.jina_api_key,
        github_token=settings.defaults.github_token,
        gemini_oauth_client_secret=settings.defaults.gemini_oauth_client_secret,
        log_path=settings.defaults.log_path,
        image_temp_dir=settings.defaults.image_temp_dir,
        image_max_mb=settings.defaults.image_max_mb,
        memory_max_kb=settings.defaults.memory_max_kb,
        memory_compact_target_kb=settings.defaults.memory_compact_target_kb,
        memory_events_enabled=settings.defaults.memory_events_enabled,
        memory_native_cli_hooks_enabled=settings.defaults.memory_native_cli_hooks_enabled,
        memory_outcomes_enabled=settings.defaults.memory_outcomes_enabled,
        memory_dreaming_enabled=settings.defaults.memory_dreaming_enabled,
        memory_events_retention_days=settings.defaults.memory_events_retention_days,
        memory_events_max_payload_chars=settings.defaults.memory_events_max_payload_chars,
        memory_events_redaction_enabled=settings.defaults.memory_events_redaction_enabled,
        memory_dreaming_batch_size=settings.defaults.memory_dreaming_batch_size,
        clarification_enabled=settings.defaults.clarification_enabled,
        pending_input_confirmation_enabled=settings.defaults.pending_input_confirmation_enabled,
        default_cli=settings.defaults.default_cli,
        default_language=settings.defaults.default_language,
        clarification_keywords=list(settings.defaults.clarification_keywords),
        manager_max_tasks=settings.defaults.manager_max_tasks,
        manager_max_attempts=settings.defaults.manager_max_attempts,
        manager_decompose_timeout_sec=settings.defaults.manager_decompose_timeout_sec,
        manager_dev_timeout_sec=settings.defaults.manager_dev_timeout_sec,
        manager_review_timeout_sec=settings.defaults.manager_review_timeout_sec,
        analyst_use_cli_timeout_sec=settings.defaults.analyst_use_cli_timeout_sec,
        webmaster_use_cli_timeout_sec=settings.defaults.webmaster_use_cli_timeout_sec,
        webmaster_validation_max_fix_iterations=settings.defaults.webmaster_validation_max_fix_iterations,
        manager_dev_report_max_chars=settings.defaults.manager_dev_report_max_chars,
        manager_auto_resume=settings.defaults.manager_auto_resume,
        manager_auto_commit=settings.defaults.manager_auto_commit,
        manager_response_archive=settings.defaults.manager_response_archive,
        cli_json_stream_archive_enabled=settings.defaults.cli_json_stream_archive_enabled,
        assistant_preview_enabled=settings.defaults.assistant_preview_enabled,
        codebase_mapper_usage=settings.defaults.codebase_mapper_usage,
        run_artifacts_enabled=settings.defaults.run_artifacts_enabled,
        run_artifacts_retention_days=settings.defaults.run_artifacts_retention_days,
        run_doctor_enabled=settings.defaults.run_doctor_enabled,
        run_boundary_validation_enabled=settings.defaults.run_boundary_validation_enabled,
        run_metrics_enabled=settings.defaults.run_metrics_enabled,
        tool_disclosure=settings.defaults.tool_disclosure,
        context_window_tokens=settings.defaults.context_window_tokens,
        context_reserve_tokens=settings.defaults.context_reserve_tokens,
        summarization_threshold=settings.defaults.summarization_threshold,
        llm_trace_enabled=settings.defaults.llm_trace_enabled,
        skill_discovery_mode=settings.defaults.skill_discovery_mode,
        skill_install_policy=settings.defaults.skill_install_policy,
        skill_registry_paths=list(settings.defaults.skill_registry_paths),
        skill_allowlisted_sources=list(settings.defaults.skill_allowlisted_sources),
        cli_routing={name: list(priority) for name, priority in settings.defaults.cli_routing.items()}
        if settings.defaults.cli_routing
        else None,
    )

    mcp = MCPConfig(
        enabled=settings.mcp.enabled,
        host=settings.mcp.host,
        port=settings.mcp.port,
        token=settings.mcp.token,
    )

    mcp_clients = [
        MCPClientServerConfig(
            name=entry.name,
            enabled=entry.enabled,
            transport=entry.transport,
            cmd=list(entry.cmd),
            url=entry.url,
            cwd=entry.cwd,
            env=_copy_mapping(entry.env),
            headers=_copy_mapping(entry.headers),
            timeout_ms=entry.timeout_ms,
        )
        for entry in settings.mcp_clients
    ]

    presets = [PresetConfig(name=entry.name, prompt=entry.prompt) for entry in settings.presets]

    miniapp = MiniAppConfig(
        enabled=settings.miniapp.enabled,
        bind_host=settings.miniapp.bind_host,
        bind_port=settings.miniapp.bind_port,
        base_path=settings.miniapp.base_path,
        public_url=settings.miniapp.public_url,
        max_edit_file_size_kb=settings.miniapp.max_edit_file_size_kb,
        enable_delete=settings.miniapp.enable_delete,
    )

    thread_mode = ThreadModeConfig(
        enabled=settings.thread_mode.enabled,
        mode=settings.thread_mode.mode,
        topics_chat_id=settings.thread_mode.topics_chat_id,
        topic_title_prefix=settings.thread_mode.topic_title_prefix,
        inactivity_ttl_sec=settings.thread_mode.inactivity_ttl_sec,
    )

    webhooks = WebhooksConfig(
        enabled=settings.webhooks.enabled,
        path=settings.webhooks.path,
        public_base_url=settings.webhooks.public_base_url,
        secret_token=settings.webhooks.secret_token,
        request_timeout_sec=settings.webhooks.request_timeout_sec,
        max_payload_bytes=settings.webhooks.max_payload_bytes,
    )

    scheduler = SchedulerConfig(
        enabled=settings.scheduler.enabled,
        timezone=settings.scheduler.timezone,
        tick_interval_sec=settings.scheduler.tick_interval_sec,
        max_concurrent_jobs=settings.scheduler.max_concurrent_jobs,
        job_timeout_sec=settings.scheduler.job_timeout_sec,
        misfire_grace_sec=settings.scheduler.misfire_grace_sec,
    )

    security = SecurityConfig(
        rate_limits=SecurityRateLimitsConfig(
            enabled=settings.security.rate_limits.enabled,
            backend=settings.security.rate_limits.backend,
            sqlite_path=settings.security.rate_limits.sqlite_path,
            default=(
                SecurityRateLimitPolicyConfig(
                    limit=settings.security.rate_limits.default.limit,
                    window_sec=settings.security.rate_limits.default.window_sec,
                    burst_limit=settings.security.rate_limits.default.burst_limit,
                    burst_window_sec=settings.security.rate_limits.default.burst_window_sec,
                )
                if settings.security.rate_limits.default is not None
                else None
            ),
            policies={
                scope: SecurityRateLimitPolicyConfig(
                    limit=policy.limit,
                    window_sec=policy.window_sec,
                    burst_limit=policy.burst_limit,
                    burst_window_sec=policy.burst_window_sec,
                )
                for scope, policy in settings.security.rate_limits.policies.items()
            },
        )
    )

    lint_evolution = LintEvolutionConfig(
        enabled=settings.lint_evolution.enabled,
        level1_cooldown_hours=settings.lint_evolution.level1_cooldown_hours,
        level2_cooldown_hours=settings.lint_evolution.level2_cooldown_hours,
        level3_cooldown_hours=settings.lint_evolution.level3_cooldown_hours,
        lock_ttl_minutes=settings.lint_evolution.lock_ttl_minutes,
        error_retry_hours=settings.lint_evolution.error_retry_hours,
        fp_growth_threshold_pct=settings.lint_evolution.fp_growth_threshold_pct,
        canary_rolling_days=settings.lint_evolution.canary_rolling_days,
        canary_baseline_days=settings.lint_evolution.canary_baseline_days,
        canary_max_schema_fields_per_180d=settings.lint_evolution.canary_max_schema_fields_per_180d,
    )

    return AppConfig(
        telegram=telegram,
        tools=tools,
        defaults=defaults,
        mcp=mcp,
        mcp_clients=mcp_clients,
        presets=presets,
        path=path,
        miniapp=miniapp,
        thread_mode=thread_mode,
        webhooks=webhooks,
        scheduler=scheduler,
        security=security,
        lint_evolution=lint_evolution,
    )


__all__ = ["adapt_validated_settings"]
