from __future__ import annotations

from typing import Annotated, Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator, model_validator

NonEmptyStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
PositiveInt = Annotated[int, Field(gt=0)]
PositiveFloat = Annotated[float, Field(gt=0)]
NonNegativeInt = Annotated[int, Field(ge=0)]
NonNegativeFloat = Annotated[float, Field(ge=0)]
PortNumber = Annotated[int, Field(ge=1, le=65535)]


def _normalize_string_or_list(value: Any) -> Any:
    if isinstance(value, str):
        return [value]
    return value


class ConfigModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, validate_default=True)


class TelegramConfigModel(ConfigModel):
    token: NonEmptyStr
    whitelist_chat_ids: list[int] = Field(min_length=1)
    admlist_chat_ids: list[int] = Field(default_factory=list)
    user_workdirs: dict[int, list[NonEmptyStr]] = Field(default_factory=dict)
    user_modes: dict[int, Literal["all"] | list[NonEmptyStr]] = Field(default_factory=dict)
    connection_pool_size: Annotated[int, Field(ge=1)] = 8
    connect_timeout_sec: PositiveFloat = 20.0
    read_timeout_sec: PositiveFloat = 20.0
    write_timeout_sec: PositiveFloat = 20.0
    pool_timeout_sec: PositiveFloat = 10.0
    polling_timeout_sec: Annotated[int, Field(ge=1)] = 5
    poll_interval_sec: NonNegativeFloat = 0.0

    @field_validator("user_workdirs", mode="before")
    @classmethod
    def _normalize_user_workdirs(cls, value: Any) -> Any:
        if value is None or not isinstance(value, dict):
            return value
        normalized: dict[Any, Any] = {}
        for key, item in value.items():
            normalized[key] = [item] if isinstance(item, str) else item
        return normalized

    @field_validator("user_modes", mode="before")
    @classmethod
    def _normalize_user_modes(cls, value: Any) -> Any:
        if value is None or not isinstance(value, dict):
            return value
        normalized: dict[Any, Any] = {}
        for key, item in value.items():
            if isinstance(item, str):
                token = item.strip()
                if not token:
                    continue
                normalized[key] = "all" if token.lower() == "all" else [token]
                continue
            normalized[key] = item
        return normalized


class ToolConfigModel(ConfigModel):
    mode: Literal["headless", "interactive"]
    cmd: list[NonEmptyStr] = Field(min_length=1)
    enabled: bool = True
    headless_cmd: Optional[list[NonEmptyStr]] = Field(default=None, min_length=1)
    resume_cmd: Optional[list[NonEmptyStr]] = Field(default=None, min_length=1)
    image_cmd: Optional[list[NonEmptyStr]] = Field(default=None, min_length=1)
    interactive_cmd: Optional[list[NonEmptyStr]] = Field(default=None, min_length=1)
    prompt_regex: Optional[NonEmptyStr] = None
    resume_regex: Optional[NonEmptyStr] = None
    help_cmd: Optional[NonEmptyStr] = None
    env: Optional[dict[NonEmptyStr, Optional[str]]] = None
    auto_commands: Optional[list[NonEmptyStr]] = Field(default=None, min_length=1)
    separate_stderr: bool = False
    # Claude-only: при fresh-старте добавлять `--no-session-persistence`.
    # По умолчанию выключено — иначе session_transfer/reader_claude не сможет
    # прочитать transcript из ~/.claude/projects и перенести сессию.
    no_session_persistence_on_fresh: bool = False

    @field_validator(
        "cmd",
        "headless_cmd",
        "resume_cmd",
        "image_cmd",
        "interactive_cmd",
        "auto_commands",
        mode="before",
    )
    @classmethod
    def _normalize_command_lists(cls, value: Any) -> Any:
        return _normalize_string_or_list(value)


class DefaultsConfigModel(ConfigModel):
    workdir: NonEmptyStr
    idle_timeout_sec: PositiveInt = 100
    summary_max_chars: PositiveInt = 4000
    html_filename_prefix: NonEmptyStr = "cli-output"
    state_path: NonEmptyStr = "state.json"
    desktop_state_path: NonEmptyStr = "desktop_state.json"
    toolhelp_path: NonEmptyStr = "toolhelp.json"
    openai_api_key: Optional[NonEmptyStr] = None
    openai_model: Optional[NonEmptyStr] = None
    openai_big_model: Optional[NonEmptyStr] = None
    openai_base_url: Optional[NonEmptyStr] = None
    zai_api_key: Optional[NonEmptyStr] = None
    tavily_api_key: Optional[NonEmptyStr] = None
    jina_api_key: Optional[NonEmptyStr] = None
    github_token: Optional[NonEmptyStr] = None
    gemini_oauth_client_secret: Optional[NonEmptyStr] = None
    log_path: NonEmptyStr = "bot.log"
    image_temp_dir: NonEmptyStr = ".cli-proxy/.attachments"
    image_max_mb: PositiveInt = 10
    memory_max_kb: PositiveInt = 32
    memory_compact_target_kb: PositiveInt = 24
    memory_events_enabled: bool = False
    memory_native_cli_hooks_enabled: bool = False
    memory_outcomes_enabled: bool = False
    memory_dreaming_enabled: bool = False
    memory_events_retention_days: PositiveInt = 30
    memory_events_max_payload_chars: PositiveInt = 6000
    memory_events_redaction_enabled: bool = True
    memory_dreaming_batch_size: PositiveInt = 20
    clarification_enabled: bool = True
    pending_input_confirmation_enabled: bool = True
    default_cli: Optional[NonEmptyStr] = None
    clarification_keywords: list[NonEmptyStr] = Field(
        default_factory=lambda: [
            "уточни",
            "уточните",
            "не ясно",
            "непонятно",
        ]
    )
    manager_max_tasks: Annotated[int, Field(ge=1)] = 10
    manager_max_attempts: Annotated[int, Field(ge=1)] = 3
    manager_decompose_timeout_sec: PositiveInt = 1200
    manager_dev_timeout_sec: PositiveInt = 3600
    manager_review_timeout_sec: PositiveInt = 1200
    analyst_use_cli_timeout_sec: PositiveInt = 3600
    webmaster_use_cli_timeout_sec: PositiveInt = 3600
    webmaster_validation_max_fix_iterations: Annotated[int, Field(ge=1)] = 2
    manager_dev_report_max_chars: PositiveInt = 20000
    manager_auto_resume: bool = True
    manager_auto_commit: bool = True
    manager_response_archive: bool = True
    cli_json_stream_archive_enabled: bool = False
    assistant_preview_enabled: bool = False
    codebase_mapper_usage: Literal["auto", "enabled", "disabled"] = "auto"
    run_artifacts_enabled: bool = True
    run_artifacts_retention_days: PositiveInt = 30
    run_doctor_enabled: bool = True
    run_boundary_validation_enabled: bool = True
    run_metrics_enabled: bool = True
    tool_disclosure: Literal["full", "progressive"] = "full"
    context_window_tokens: PositiveInt = 128_000
    context_reserve_tokens: PositiveInt = 8000
    summarization_threshold: Annotated[float, Field(ge=0.0, le=1.0)] = 0.75
    llm_trace_enabled: bool = False
    skill_discovery_mode: Literal["off", "suggest", "auto"] = "suggest"
    skill_install_policy: Literal["manual", "admin_approve", "allowlisted_auto"] = "manual"
    skill_registry_paths: list[NonEmptyStr] = Field(default_factory=lambda: [".cli-proxy/skills"])
    skill_allowlisted_sources: list[NonEmptyStr] = Field(
        default_factory=lambda: [
            "local:global-registry",
            "local:project-registry",
            "path:absolute",
            "registry:npx-skills",
            "ref:owner-repo-skill",
        ]
    )
    cli_routing: Optional[dict[NonEmptyStr, list[NonEmptyStr]]] = None


class MCPConfigModel(ConfigModel):
    enabled: bool = False
    host: NonEmptyStr = "127.0.0.1"
    port: PortNumber = 8765
    token: Optional[NonEmptyStr] = None


class MCPClientConfigModel(ConfigModel):
    name: NonEmptyStr
    enabled: bool = True
    transport: Literal["stdio", "http"] = "stdio"
    cmd: list[NonEmptyStr] = Field(default_factory=list)
    url: Optional[NonEmptyStr] = None
    cwd: Optional[NonEmptyStr] = None
    env: Optional[dict[NonEmptyStr, Optional[str]]] = None
    headers: Optional[dict[NonEmptyStr, NonEmptyStr]] = None
    timeout_ms: PositiveInt = 30000

    @field_validator("cmd", mode="before")
    @classmethod
    def _normalize_cmd(cls, value: Any) -> Any:
        if value is None:
            return []
        return _normalize_string_or_list(value)

    @model_validator(mode="after")
    def _validate_transport_requirements(self) -> "MCPClientConfigModel":
        if self.transport == "stdio" and not self.cmd:
            raise ValueError("cmd is required when transport='stdio'")
        if self.transport == "http" and self.url is None:
            raise ValueError("url is required when transport='http'")
        return self


class PresetConfigModel(ConfigModel):
    name: NonEmptyStr
    prompt: NonEmptyStr


class MiniAppConfigModel(ConfigModel):
    enabled: bool = False
    bind_host: NonEmptyStr = "127.0.0.1"
    bind_port: PortNumber = 8088
    base_path: NonEmptyStr = "/cli-proxy"
    public_url: str = ""
    max_edit_file_size_kb: PositiveInt = 5120
    enable_delete: bool = True

    @field_validator("base_path")
    @classmethod
    def _validate_base_path(cls, value: str) -> str:
        if not value.startswith("/"):
            raise ValueError("base_path must start with '/'")
        return value


class ThreadModeConfigModel(ConfigModel):
    enabled: bool = True
    mode: Literal["private", "group"] = "private"
    topics_chat_id: Optional[int] = None
    topic_title_prefix: str = ""
    inactivity_ttl_sec: PositiveInt = 86400

    @model_validator(mode="after")
    def _validate_group_mode_requirements(self) -> "ThreadModeConfigModel":
        if self.mode == "group" and self.topics_chat_id is None:
            raise ValueError("topics_chat_id is required when mode='group'")
        return self


class WebhooksConfigModel(ConfigModel):
    enabled: bool = True
    path: NonEmptyStr = "/webhooks/telegram"
    public_base_url: Optional[NonEmptyStr] = None
    secret_token: Optional[NonEmptyStr] = None
    request_timeout_sec: PositiveFloat = 30.0
    max_payload_bytes: PositiveInt = 1048576

    @field_validator("path")
    @classmethod
    def _validate_path(cls, value: str) -> str:
        if not value.startswith("/"):
            raise ValueError("path must start with '/'")
        return value


class SchedulerConfigModel(ConfigModel):
    enabled: bool = False
    timezone: NonEmptyStr = "UTC"
    tick_interval_sec: PositiveInt = 60
    max_concurrent_jobs: Annotated[int, Field(ge=1)] = 1
    job_timeout_sec: PositiveInt = 3600
    misfire_grace_sec: NonNegativeInt = 30


class SecurityRateLimitPolicyConfigModel(ConfigModel):
    limit: Annotated[int, Field(ge=1)]
    window_sec: PositiveFloat
    burst_limit: Optional[Annotated[int, Field(ge=1)]] = None
    burst_window_sec: Optional[PositiveFloat] = None

    @model_validator(mode="after")
    def _validate_burst_limits(self) -> "SecurityRateLimitPolicyConfigModel":
        if self.burst_limit is None and self.burst_window_sec is None:
            return self
        if self.burst_limit is None or self.burst_window_sec is None:
            raise ValueError("burst_limit and burst_window_sec must be set together")
        if self.burst_limit > self.limit:
            raise ValueError("burst_limit must be less than or equal to limit")
        if self.burst_window_sec > self.window_sec:
            raise ValueError("burst_window_sec must be less than or equal to window_sec")
        return self


class SecurityRateLimitsConfigModel(ConfigModel):
    enabled: bool = False
    backend: Literal["sqlite"] = "sqlite"
    sqlite_path: Optional[NonEmptyStr] = None
    default: Optional[SecurityRateLimitPolicyConfigModel] = None
    policies: dict[NonEmptyStr, SecurityRateLimitPolicyConfigModel] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _validate_enabled_policy_presence(self) -> "SecurityRateLimitsConfigModel":
        if self.enabled and self.default is None and not self.policies:
            raise ValueError("default or policies is required when rate_limits.enabled=true")
        return self


class SecurityConfigModel(ConfigModel):
    rate_limits: SecurityRateLimitsConfigModel = Field(default_factory=SecurityRateLimitsConfigModel)


class LintEvolutionConfigModel(ConfigModel):
    enabled: bool = False
    level1_cooldown_hours: NonNegativeFloat = 24.0
    level2_cooldown_hours: NonNegativeFloat = 24.0 * 30
    level3_cooldown_hours: NonNegativeFloat = 24.0 * 30
    lock_ttl_minutes: PositiveFloat = 30.0
    error_retry_hours: NonNegativeFloat = 1.0
    fp_growth_threshold_pct: NonNegativeFloat = 50.0
    canary_rolling_days: PositiveFloat = 7.0
    canary_baseline_days: PositiveFloat = 30.0
    canary_max_schema_fields_per_180d: NonNegativeInt = 3


class AppConfigModel(ConfigModel):
    telegram: TelegramConfigModel
    tools: dict[NonEmptyStr, ToolConfigModel] = Field(default_factory=dict)
    defaults: DefaultsConfigModel
    mcp: MCPConfigModel = Field(default_factory=MCPConfigModel)
    mcp_clients: list[MCPClientConfigModel] = Field(default_factory=list)
    presets: list[PresetConfigModel] = Field(default_factory=list)
    miniapp: MiniAppConfigModel = Field(default_factory=MiniAppConfigModel)
    thread_mode: ThreadModeConfigModel = Field(default_factory=ThreadModeConfigModel)
    webhooks: WebhooksConfigModel = Field(default_factory=WebhooksConfigModel)
    scheduler: SchedulerConfigModel = Field(default_factory=SchedulerConfigModel)
    security: SecurityConfigModel = Field(default_factory=SecurityConfigModel)
    lint_evolution: LintEvolutionConfigModel = Field(default_factory=LintEvolutionConfigModel)
