import dataclasses
import logging
from typing import Any, Dict, List, Optional, Union

from app.services.dotenv_loader import load_dotenv_near


logger = logging.getLogger(__name__)


@dataclasses.dataclass
class TelegramConfig:
    token: str
    whitelist_chat_ids: List[int]
    # Administrators: full access to all bot features (including /files and /git).
    admlist_chat_ids: List[int] = dataclasses.field(default_factory=list)
    # Per-user allowed project roots (absolute paths). Users may only switch between these roots.
    # Keys are chat_id.
    user_workdirs: Dict[int, List[str]] = dataclasses.field(default_factory=dict)
    # Per-user allowed modes (for non-admin users), key: chat_id.
    # Value:
    # - "all" -> all registered modes + virtual direct CLI access (`direct_cli`)
    # - ["agent", "analyst", "direct_cli"] -> only listed modes / direct CLI
    # - not set -> no modes
    user_modes: Dict[int, Union[str, List[str]]] = dataclasses.field(default_factory=dict)
    # Per-user language preference. Key: Telegram user_id (int).
    # Supported values: "ru", "en", "zh", "de".
    # Written by bot on first contact (auto-detect) and on explicit user change.
    user_languages: Dict[int, str] = dataclasses.field(default_factory=dict)
    # python-telegram-bot uses httpx under the hood. Defaults are quite low and can cause
    # intermittent ConnectTimeout/TimedOut errors on unstable networks.
    connection_pool_size: int = 8
    connect_timeout_sec: float = 20.0
    read_timeout_sec: float = 20.0
    write_timeout_sec: float = 20.0
    pool_timeout_sec: float = 10.0
    # Polling settings for python-telegram-bot (getUpdates long polling).
    # timeout controls how long Telegram may hold the connection open when there are no updates.
    # poll_interval is a sleep between successive polls (keep at 0 for best responsiveness).
    polling_timeout_sec: int = 5
    poll_interval_sec: float = 0.0


@dataclasses.dataclass
class ToolConfig:
    name: str
    mode: str  # headless | interactive
    cmd: List[str]
    # Logical switch for tool availability (in addition to being installed on PATH).
    # If missing in YAML, defaults to True.
    enabled: bool = True
    headless_cmd: Optional[List[str]] = None
    resume_cmd: Optional[List[str]] = None
    image_cmd: Optional[List[str]] = None
    interactive_cmd: Optional[List[str]] = None
    prompt_regex: Optional[str] = None
    resume_regex: Optional[str] = None
    help_cmd: Optional[str] = None
    env: Optional[Dict[str, str]] = None
    auto_commands: Optional[List[str]] = None
    separate_stderr: bool = False
    no_session_persistence_on_fresh: bool = False


@dataclasses.dataclass
class DefaultsConfig:
    workdir: str
    idle_timeout_sec: int = 100
    summary_max_chars: int = 4000
    html_filename_prefix: str = "cli-output"
    state_path: str = "state.json"
    desktop_state_path: str = "desktop_state.json"
    toolhelp_path: str = "toolhelp.json"
    openai_api_key: Optional[str] = None
    openai_model: Optional[str] = None
    openai_big_model: Optional[str] = None
    openai_base_url: Optional[str] = None
    zai_api_key: Optional[str] = None
    tavily_api_key: Optional[str] = None
    jina_api_key: Optional[str] = None
    github_token: Optional[str] = None
    gemini_oauth_client_secret: Optional[str] = None
    log_path: str = "bot.log"
    image_temp_dir: str = ".cli-proxy/.attachments"
    image_max_mb: int = 10
    memory_max_kb: int = 32
    memory_compact_target_kb: int = 24
    memory_events_enabled: bool = False
    memory_native_cli_hooks_enabled: bool = False
    memory_outcomes_enabled: bool = False
    memory_dreaming_enabled: bool = False
    memory_events_retention_days: int = 30
    memory_events_max_payload_chars: int = 6000
    memory_events_redaction_enabled: bool = True
    memory_dreaming_batch_size: int = 20
    clarification_enabled: bool = True
    pending_input_confirmation_enabled: bool = True
    # Default CLI to activate when creating a new session (if not specified explicitly).
    # If not set, runtime falls back to "qwen".
    default_cli: Optional[str] = None
    # Global default language for users without explicit preference.
    # Used by Desktop (single-user) and as final fallback in Telegram.
    # Supported values: "ru", "en", "zh", "de". Default: "ru".
    default_language: str = "ru"
    clarification_keywords: List[str] = dataclasses.field(
        default_factory=lambda: [
            "уточни",
            "уточните",
            "не ясно",
            "непонятно",
        ]
    )
    # Per-language clarification keywords. Used by needs_clarification heuristic.
    # Falls back to legacy clarification_keywords if language not found here.
    # TODO: deprecate clarification_keywords once all languages are covered by clarification_keywords_by_lang.
    clarification_keywords_by_lang: Dict[str, List[str]] = dataclasses.field(
        default_factory=lambda: {
            "ru": ["уточни", "уточните", "не ясно", "непонятно"],
            "en": ["clarify", "unclear", "not clear", "please clarify", "i need clarification"],
            "zh": ["请澄清", "不清楚", "需要澄清", "能说清楚吗"],
            "de": ["unklar", "bitte präzisieren", "nicht klar", "klären sie", "erläutern sie"],
        }
    )
    # Manager mode (multi-agent orchestration via CLI + reviewer Agent)
    manager_max_tasks: int = 10
    manager_max_attempts: int = 3
    manager_decompose_timeout_sec: int = 1200
    manager_dev_timeout_sec: int = 3600
    manager_review_timeout_sec: int = 1200
    analyst_use_cli_timeout_sec: int = 3600
    webmaster_use_cli_timeout_sec: int = 3600
    webmaster_validation_max_fix_iterations: int = 2
    manager_dev_report_max_chars: int = 20000
    manager_auto_resume: bool = True
    manager_auto_commit: bool = True         # git commit после каждого одобренного шага плана
    manager_response_archive: bool = True    # Сохранять сырые ответы CLI/агентов в .cli-proxy/.manager/
    cli_json_stream_archive_enabled: bool = False
    assistant_preview_enabled: bool = False
    # Codebase mapper usage mode:
    # - "auto": currently behaves same as enabled
    # - "enabled": mapper mode is allowed to run map generation/update
    # - "disabled": mapper mode returns disabled status and does not generate/update map
    codebase_mapper_usage: str = "auto"
    run_artifacts_enabled: bool = True
    run_artifacts_retention_days: int = 30
    run_doctor_enabled: bool = True
    run_boundary_validation_enabled: bool = True
    run_metrics_enabled: bool = True
    skill_discovery_mode: str = "suggest"
    skill_install_policy: str = "manual"
    skill_registry_paths: List[str] = dataclasses.field(default_factory=lambda: [".cli-proxy/skills"])
    skill_allowlisted_sources: List[str] = dataclasses.field(
        default_factory=lambda: [
            "local:global-registry",
            "local:project-registry",
            "path:absolute",
            "registry:npx-skills",
            "ref:owner-repo-skill",
        ]
    )
    # Progressive Disclosure: "full" sends all tool schemas to LLM;
    # "progressive" sends summaries + get_tool_details meta-tool.
    tool_disclosure: str = "full"
    # Token counting and context management.
    context_window_tokens: int = 128_000
    context_reserve_tokens: int = 8000
    summarization_threshold: float = 0.75
    # LLM trace logging (llm_request/llm_response/tool_execution events in EVENTS.jsonl).
    llm_trace_enabled: bool = False
    # CLI routing priorities by work type for Manager/Analyst modes.
    # Optional: if missing, runtime uses hardcoded defaults (see cli_routing.py).
    cli_routing: Optional[Dict[str, List[str]]] = None


@dataclasses.dataclass
class MCPConfig:
    enabled: bool = False
    host: str = "127.0.0.1"
    port: int = 8765
    token: Optional[str] = None


@dataclasses.dataclass
class MCPClientServerConfig:
    """
    Configuration for connecting to external MCP servers (client-side).
    Transport currently supported: stdio.
    """

    name: str
    enabled: bool = True
    transport: str = "stdio"  # stdio | http
    cmd: List[str] = dataclasses.field(default_factory=list)
    url: Optional[str] = None
    cwd: Optional[str] = None
    env: Optional[Dict[str, str]] = None
    headers: Optional[Dict[str, str]] = None
    timeout_ms: int = 30_000


@dataclasses.dataclass
class PresetConfig:
    name: str
    prompt: str


@dataclasses.dataclass
class MiniAppConfig:
    enabled: bool = False
    bind_host: str = "127.0.0.1"
    bind_port: int = 8088
    base_path: str = "/cli-proxy"
    public_url: str = ""
    max_edit_file_size_kb: int = 5120
    enable_delete: bool = True


@dataclasses.dataclass
class ThreadModeConfig:
    enabled: bool = True
    mode: str = "private"
    topics_chat_id: Optional[int] = None
    topic_title_prefix: str = ""
    inactivity_ttl_sec: int = 86400


@dataclasses.dataclass
class WebhooksConfig:
    enabled: bool = True
    path: str = "/webhooks/telegram"
    public_base_url: Optional[str] = None
    secret_token: Optional[str] = None
    request_timeout_sec: float = 30.0
    max_payload_bytes: int = 1048576


@dataclasses.dataclass
class SchedulerConfig:
    enabled: bool = False
    timezone: str = "UTC"
    tick_interval_sec: int = 60
    max_concurrent_jobs: int = 1
    job_timeout_sec: int = 3600
    misfire_grace_sec: int = 30


@dataclasses.dataclass
class SecurityRateLimitPolicyConfig:
    limit: int
    window_sec: float
    burst_limit: Optional[int] = None
    burst_window_sec: Optional[float] = None


@dataclasses.dataclass
class SecurityRateLimitsConfig:
    enabled: bool = False
    backend: str = "sqlite"
    sqlite_path: Optional[str] = None
    default: Optional[SecurityRateLimitPolicyConfig] = None
    policies: Dict[str, SecurityRateLimitPolicyConfig] = dataclasses.field(default_factory=dict)


@dataclasses.dataclass
class SecurityConfig:
    rate_limits: SecurityRateLimitsConfig = dataclasses.field(default_factory=SecurityRateLimitsConfig)


@dataclasses.dataclass
class LintEvolutionConfig:
    enabled: bool = False
    level1_cooldown_hours: float = 24.0
    level2_cooldown_hours: float = 24.0 * 30
    level3_cooldown_hours: float = 24.0 * 30
    lock_ttl_minutes: float = 30.0
    error_retry_hours: float = 1.0
    fp_growth_threshold_pct: float = 50.0
    canary_rolling_days: float = 7.0
    canary_baseline_days: float = 30.0
    canary_max_schema_fields_per_180d: int = 3


@dataclasses.dataclass
class SSHHostConfig:
    """Configuration for a single remote SSH host within a project."""

    host: str
    user: str
    auth: str = "key"  # "key" | "password"
    port: int = 22
    key_file: Optional[str] = None
    key_passphrase_env: Optional[str] = None
    password_env: Optional[str] = None
    sudo: bool = False
    sudo_password_env: Optional[str] = None
    idle_timeout_sec: int = 1200
    allowed_chat_ids: Optional[List[int]] = None
    roles: List[str] = dataclasses.field(default_factory=list)
    description: str = ""
    remote_project_root: Optional[str] = None


@dataclasses.dataclass
class AppConfig:
    telegram: TelegramConfig
    tools: Dict[str, ToolConfig]
    defaults: DefaultsConfig
    mcp: MCPConfig
    mcp_clients: List[MCPClientServerConfig]
    presets: List[PresetConfig]
    path: str
    miniapp: MiniAppConfig = dataclasses.field(default_factory=MiniAppConfig)
    thread_mode: ThreadModeConfig = dataclasses.field(default_factory=ThreadModeConfig)
    webhooks: WebhooksConfig = dataclasses.field(default_factory=WebhooksConfig)
    scheduler: SchedulerConfig = dataclasses.field(default_factory=SchedulerConfig)
    security: SecurityConfig = dataclasses.field(default_factory=SecurityConfig)
    lint_evolution: LintEvolutionConfig = dataclasses.field(default_factory=LintEvolutionConfig)


def load_config(path: str) -> AppConfig:
    # Load environment variables from .env near the config file so all plugins/tools can use them.
    # Do not override already provided env vars (e.g. from systemd/docker).
    try:
        load_dotenv_near(path, filename=".env", override=False)
    except Exception:
        logger.exception("failed to load .env near config path=%s", path)

    from app.config_runtime.adapter import adapt_validated_settings
    from app.config_runtime.loader import load_validated_settings

    return adapt_validated_settings(load_validated_settings(path), path=path)


def app_config_to_dict(config: AppConfig) -> Dict[str, Any]:
    from app.config_runtime.serialization import serialize_app_config

    return serialize_app_config(config)


def save_config(config: AppConfig) -> None:
    from app.config_runtime.serialization import dump_app_config_yaml

    with open(config.path, "w", encoding="utf-8") as f:
        f.write(dump_app_config_yaml(config))
