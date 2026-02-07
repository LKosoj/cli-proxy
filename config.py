import dataclasses
import os
from typing import Any, Dict, List, Optional

import yaml

from dotenv_loader import load_dotenv_near


@dataclasses.dataclass
class TelegramConfig:
    token: str
    whitelist_chat_ids: List[int]


@dataclasses.dataclass
class ToolConfig:
    name: str
    mode: str  # headless | interactive
    cmd: List[str]
    headless_cmd: Optional[List[str]] = None
    resume_cmd: Optional[List[str]] = None
    image_cmd: Optional[List[str]] = None
    interactive_cmd: Optional[List[str]] = None
    prompt_regex: Optional[str] = None
    resume_regex: Optional[str] = None
    help_cmd: Optional[str] = None
    env: Optional[Dict[str, str]] = None
    auto_commands: Optional[List[str]] = None


@dataclasses.dataclass
class DefaultsConfig:
    workdir: str
    idle_timeout_sec: int = 100
    summary_max_chars: int = 4000
    html_filename_prefix: str = "cli-output"
    state_path: str = "state.json"
    toolhelp_path: str = "toolhelp.json"
    openai_api_key: Optional[str] = None
    openai_model: Optional[str] = None
    openai_big_model: Optional[str] = None
    openai_base_url: Optional[str] = None
    # Backward-compat alias for older configs. Prefer openai_big_model.
    big_model_to_use: Optional[str] = None
    zai_api_key: Optional[str] = None
    tavily_api_key: Optional[str] = None
    jina_api_key: Optional[str] = None
    github_token: Optional[str] = None
    log_path: str = "bot.log"
    image_temp_dir: str = ".attachments"
    image_max_mb: int = 10
    memory_max_kb: int = 32
    memory_compact_target_kb: int = 24
    clarification_enabled: bool = True
    clarification_keywords: List[str] = dataclasses.field(
        default_factory=lambda: [
            "уточни",
            "уточните",
            "не ясно",
            "непонятно",
            "какой",
            "какая",
            "какие",
            "какое",
            "сколько",
            "когда",
            "где",
            "почему",
            "зачем",
        ]
    )
    # Manager mode (multi-agent orchestration via CLI + reviewer Agent)
    manager_max_tasks: int = 10
    manager_max_attempts: int = 3
    manager_decompose_timeout_sec: int = 1200
    manager_dev_timeout_sec: int = 3600
    manager_review_timeout_sec: int = 1200
    manager_dev_report_max_chars: int = 20000
    manager_auto_resume: bool = True
    manager_auto_commit: bool = True         # git commit после каждого одобренного шага плана
    manager_debug_log: bool = True           # Сохранять сырые ответы CLI/агентов в .manager/


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
class AppConfig:
    telegram: TelegramConfig
    tools: Dict[str, ToolConfig]
    defaults: DefaultsConfig
    mcp: MCPConfig
    mcp_clients: List[MCPClientServerConfig]
    presets: List[PresetConfig]
    path: str


def load_config(path: str) -> AppConfig:
    # Load environment variables from .env near the config file so all plugins/tools can use them.
    # Do not override already provided env vars (e.g. from systemd/docker).
    try:
        load_dotenv_near(path, filename=".env", override=False)
    except Exception:
        pass

    with open(path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)

    telegram_raw = raw.get("telegram", {})
    tools_raw = raw.get("tools", {})
    defaults_raw = raw.get("defaults", {})

    telegram = TelegramConfig(
        token=str(telegram_raw.get("token", "")),
        whitelist_chat_ids=list(telegram_raw.get("whitelist_chat_ids", [])),
    )

    tools: Dict[str, ToolConfig] = {}
    for name, t in tools_raw.items():
        tools[name] = ToolConfig(
            name=name,
            mode=str(t.get("mode", "headless")),
            cmd=list(t.get("cmd", [])),
            headless_cmd=t.get("headless_cmd"),
            resume_cmd=t.get("resume_cmd"),
            image_cmd=t.get("image_cmd"),
            interactive_cmd=t.get("interactive_cmd"),
            prompt_regex=t.get("prompt_regex"),
            resume_regex=t.get("resume_regex"),
            help_cmd=t.get("help_cmd"),
            env=t.get("env"),
            auto_commands=t.get("auto_commands"),
        )

    defaults = DefaultsConfig(
        workdir=str(defaults_raw.get("workdir", os.getcwd())),
        idle_timeout_sec=int(defaults_raw.get("idle_timeout_sec", 100)),
        summary_max_chars=int(defaults_raw.get("summary_max_chars", 4000)),
        html_filename_prefix=str(defaults_raw.get("html_filename_prefix", "cli-output")),
        state_path=str(defaults_raw.get("state_path", "state.json")),
        toolhelp_path=str(defaults_raw.get("toolhelp_path", "toolhelp.json")),
        openai_api_key=defaults_raw.get("openai_api_key"),
        openai_model=defaults_raw.get("openai_model"),
        openai_big_model=defaults_raw.get("openai_big_model"),
        openai_base_url=defaults_raw.get("openai_base_url"),
        big_model_to_use=defaults_raw.get("big_model_to_use"),
        zai_api_key=defaults_raw.get("zai_api_key"),
        tavily_api_key=defaults_raw.get("tavily_api_key"),
        jina_api_key=defaults_raw.get("jina_api_key"),
        github_token=defaults_raw.get("github_token"),
        log_path=str(defaults_raw.get("log_path", "bot.log")),
        image_temp_dir=str(defaults_raw.get("image_temp_dir", ".attachments")),
        image_max_mb=int(defaults_raw.get("image_max_mb", 10)),
        memory_max_kb=int(defaults_raw.get("memory_max_kb", 32)),
        memory_compact_target_kb=int(defaults_raw.get("memory_compact_target_kb", 24)),
        clarification_enabled=bool(defaults_raw.get("clarification_enabled", True)),
        clarification_keywords=list(
            defaults_raw.get(
                "clarification_keywords",
                DefaultsConfig(workdir="").clarification_keywords,
            )
        ),
        manager_max_tasks=int(defaults_raw.get("manager_max_tasks", 10)),
        manager_max_attempts=int(defaults_raw.get("manager_max_attempts", 3)),
        manager_decompose_timeout_sec=int(defaults_raw.get("manager_decompose_timeout_sec", 1200)),
        manager_dev_timeout_sec=int(defaults_raw.get("manager_dev_timeout_sec", 3600)),
        manager_review_timeout_sec=int(defaults_raw.get("manager_review_timeout_sec", 1200)),
        manager_dev_report_max_chars=int(defaults_raw.get("manager_dev_report_max_chars", 20000)),
        manager_auto_resume=bool(defaults_raw.get("manager_auto_resume", True)),
        manager_auto_commit=bool(defaults_raw.get("manager_auto_commit", True)),
        manager_debug_log=bool(defaults_raw.get("manager_debug_log", True)),
    )

    mcp_raw = raw.get("mcp", {})
    mcp = MCPConfig(
        enabled=bool(mcp_raw.get("enabled", False)),
        host=str(mcp_raw.get("host", "127.0.0.1")),
        port=int(mcp_raw.get("port", 8765)),
        token=mcp_raw.get("token"),
    )

    mcp_clients_raw = raw.get("mcp_clients", []) or []
    mcp_clients: List[MCPClientServerConfig] = []
    for entry in mcp_clients_raw:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name", "")).strip()
        if not name:
            continue
        cmd = entry.get("cmd", []) or []
        if isinstance(cmd, str):
            cmd = [cmd]
        if not isinstance(cmd, list):
            cmd = []
        mcp_clients.append(
            MCPClientServerConfig(
                name=name,
                enabled=bool(entry.get("enabled", True)),
                transport=str(entry.get("transport", "stdio")),
                cmd=[str(x) for x in cmd if str(x).strip()],
                url=str(entry.get("url")) if entry.get("url") is not None else None,
                cwd=str(entry.get("cwd")) if entry.get("cwd") is not None else None,
                env=entry.get("env") if isinstance(entry.get("env"), dict) else None,
                headers=entry.get("headers") if isinstance(entry.get("headers"), dict) else None,
                timeout_ms=int(entry.get("timeout_ms", 30_000)),
            )
        )

    # Optional alternative input format (like Claude Desktop): mcp_servers.<name>.url
    mcp_servers_raw = raw.get("mcp_servers", {}) or {}
    if isinstance(mcp_servers_raw, dict):
        for server_name, entry in mcp_servers_raw.items():
            if not isinstance(server_name, str) or not isinstance(entry, dict):
                continue
            url = entry.get("url")
            if not url:
                continue
            mcp_clients.append(
                MCPClientServerConfig(
                    name=server_name,
                    enabled=bool(entry.get("enabled", True)),
                    transport=str(entry.get("transport", "http")),
                    cmd=[],
                    url=str(url),
                    cwd=None,
                    env=None,
                    headers=entry.get("headers") if isinstance(entry.get("headers"), dict) else None,
                    timeout_ms=int(entry.get("timeout_ms", 30_000)),
                )
            )

    presets_raw = raw.get("presets", []) or []
    presets: List[PresetConfig] = []
    for entry in presets_raw:
        name = str(entry.get("name", "")).strip()
        prompt = str(entry.get("prompt", "")).strip()
        if name and prompt:
            presets.append(PresetConfig(name=name, prompt=prompt))

    return AppConfig(
        telegram=telegram,
        tools=tools,
        defaults=defaults,
        mcp=mcp,
        mcp_clients=mcp_clients,
        presets=presets,
        path=path,
    )


def save_config(config: AppConfig) -> None:
    data: Dict[str, Any] = {
        "telegram": {
            "token": config.telegram.token,
            "whitelist_chat_ids": config.telegram.whitelist_chat_ids,
        },
        "tools": {},
        "defaults": {
            "workdir": config.defaults.workdir,
            "idle_timeout_sec": config.defaults.idle_timeout_sec,
            "summary_max_chars": config.defaults.summary_max_chars,
            "html_filename_prefix": config.defaults.html_filename_prefix,
            "state_path": config.defaults.state_path,
            "toolhelp_path": config.defaults.toolhelp_path,
            "openai_api_key": config.defaults.openai_api_key,
            "openai_model": config.defaults.openai_model,
            "openai_big_model": config.defaults.openai_big_model,
            "openai_base_url": config.defaults.openai_base_url,
            "big_model_to_use": config.defaults.big_model_to_use,
            "zai_api_key": config.defaults.zai_api_key,
            "tavily_api_key": config.defaults.tavily_api_key,
            "jina_api_key": config.defaults.jina_api_key,
            "github_token": config.defaults.github_token,
            "log_path": config.defaults.log_path,
            "image_temp_dir": config.defaults.image_temp_dir,
            "image_max_mb": config.defaults.image_max_mb,
            "memory_max_kb": config.defaults.memory_max_kb,
            "memory_compact_target_kb": config.defaults.memory_compact_target_kb,
            "clarification_enabled": config.defaults.clarification_enabled,
            "clarification_keywords": config.defaults.clarification_keywords,
            "manager_max_tasks": config.defaults.manager_max_tasks,
            "manager_max_attempts": config.defaults.manager_max_attempts,
            "manager_decompose_timeout_sec": config.defaults.manager_decompose_timeout_sec,
            "manager_dev_timeout_sec": config.defaults.manager_dev_timeout_sec,
            "manager_review_timeout_sec": config.defaults.manager_review_timeout_sec,
            "manager_dev_report_max_chars": config.defaults.manager_dev_report_max_chars,
            "manager_auto_resume": config.defaults.manager_auto_resume,
            "manager_auto_commit": config.defaults.manager_auto_commit,
            "manager_debug_log": config.defaults.manager_debug_log,
        },
        "mcp": {
            "enabled": config.mcp.enabled,
            "host": config.mcp.host,
            "port": config.mcp.port,
            "token": config.mcp.token,
        },
        "mcp_clients": [
            {
                "name": s.name,
                "enabled": s.enabled,
                "transport": s.transport,
                "cmd": s.cmd,
                "url": s.url,
                "cwd": s.cwd,
                "env": s.env,
                "headers": s.headers,
                "timeout_ms": s.timeout_ms,
            }
            for s in (config.mcp_clients or [])
        ],
        "presets": [{"name": p.name, "prompt": p.prompt} for p in config.presets],
    }

    for name, tool in config.tools.items():
        data["tools"][name] = {
            "mode": tool.mode,
            "cmd": tool.cmd,
            "headless_cmd": tool.headless_cmd,
            "resume_cmd": tool.resume_cmd,
            "image_cmd": tool.image_cmd,
            "interactive_cmd": tool.interactive_cmd,
            "prompt_regex": tool.prompt_regex,
            "resume_regex": tool.resume_regex,
            "help_cmd": tool.help_cmd,
            "env": tool.env,
            "auto_commands": tool.auto_commands,
        }

    with open(config.path, "w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, sort_keys=False, allow_unicode=False)
