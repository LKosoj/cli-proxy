import dataclasses
import os
from typing import Any, Dict, List, Optional, Union

import yaml


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
    openai_base_url: Optional[str] = None
    zai_api_key: Optional[str] = None
    tavily_api_key: Optional[str] = None
    jina_api_key: Optional[str] = None
    github_token: Optional[str] = None
    log_path: str = "bot.log"
    mtproto_output_dir: str = ".mtproto"
    mtproto_cleanup_days: int = 5
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


@dataclasses.dataclass
class MTProtoTarget:
    title: str
    peer: Union[int, str]


@dataclasses.dataclass
class MTProtoConfig:
    enabled: bool = False
    api_id: Optional[int] = None
    api_hash: Optional[str] = None
    session_string: Optional[str] = None
    session_path: str = "mtproto.session"
    targets: List[MTProtoTarget] = dataclasses.field(default_factory=list)


@dataclasses.dataclass
class MCPConfig:
    enabled: bool = False
    host: str = "127.0.0.1"
    port: int = 8765
    token: Optional[str] = None


@dataclasses.dataclass
class PresetConfig:
    name: str
    prompt: str


@dataclasses.dataclass
class AppConfig:
    telegram: TelegramConfig
    tools: Dict[str, ToolConfig]
    defaults: DefaultsConfig
    mtproto: MTProtoConfig
    mcp: MCPConfig
    presets: List[PresetConfig]
    path: str


def load_config(path: str) -> AppConfig:
    with open(path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)

    telegram_raw = raw.get("telegram", {})
    tools_raw = raw.get("tools", {})
    defaults_raw = raw.get("defaults", {})
    mtproto_raw = raw.get("mtproto", {})

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
        openai_base_url=defaults_raw.get("openai_base_url"),
        zai_api_key=defaults_raw.get("zai_api_key"),
        tavily_api_key=defaults_raw.get("tavily_api_key"),
        jina_api_key=defaults_raw.get("jina_api_key"),
        github_token=defaults_raw.get("github_token"),
        log_path=str(defaults_raw.get("log_path", "bot.log")),
        mtproto_output_dir=str(defaults_raw.get("mtproto_output_dir", ".mtproto")),
        mtproto_cleanup_days=int(defaults_raw.get("mtproto_cleanup_days", 5)),
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
    )

    targets: List[MTProtoTarget] = []
    for entry in mtproto_raw.get("targets", []) or []:
        title = str(entry.get("title") or entry.get("name") or "").strip()
        peer_val = entry.get("peer", entry.get("id", entry.get("username")))
        if title and peer_val is not None:
            if isinstance(peer_val, str) and peer_val.lstrip("-").isdigit():
                peer: Union[int, str] = int(peer_val)
            else:
                peer = peer_val
            targets.append(MTProtoTarget(title=title, peer=peer))

    api_id_raw = mtproto_raw.get("api_id")
    api_id = None
    if api_id_raw is not None:
        try:
            api_id = int(api_id_raw)
        except Exception:
            api_id = None

    mtproto = MTProtoConfig(
        enabled=bool(mtproto_raw.get("enabled", False)),
        api_id=api_id,
        api_hash=mtproto_raw.get("api_hash"),
        session_string=mtproto_raw.get("session_string"),
        session_path=str(mtproto_raw.get("session_path", "mtproto.session")),
        targets=targets,
    )

    mcp_raw = raw.get("mcp", {})
    mcp = MCPConfig(
        enabled=bool(mcp_raw.get("enabled", False)),
        host=str(mcp_raw.get("host", "127.0.0.1")),
        port=int(mcp_raw.get("port", 8765)),
        token=mcp_raw.get("token"),
    )

    presets_raw = raw.get("presets", []) or []
    presets: List[PresetConfig] = []
    for entry in presets_raw:
        name = str(entry.get("name", "")).strip()
        prompt = str(entry.get("prompt", "")).strip()
        if name and prompt:
            presets.append(PresetConfig(name=name, prompt=prompt))

    return AppConfig(telegram=telegram, tools=tools, defaults=defaults, mtproto=mtproto, mcp=mcp, presets=presets, path=path)


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
            "openai_base_url": config.defaults.openai_base_url,
            "zai_api_key": config.defaults.zai_api_key,
            "tavily_api_key": config.defaults.tavily_api_key,
            "jina_api_key": config.defaults.jina_api_key,
            "github_token": config.defaults.github_token,
            "log_path": config.defaults.log_path,
            "mtproto_output_dir": config.defaults.mtproto_output_dir,
            "mtproto_cleanup_days": config.defaults.mtproto_cleanup_days,
            "image_temp_dir": config.defaults.image_temp_dir,
            "image_max_mb": config.defaults.image_max_mb,
            "memory_max_kb": config.defaults.memory_max_kb,
            "memory_compact_target_kb": config.defaults.memory_compact_target_kb,
            "clarification_enabled": config.defaults.clarification_enabled,
            "clarification_keywords": config.defaults.clarification_keywords,
        },
        "mtproto": {
            "enabled": config.mtproto.enabled,
            "api_id": config.mtproto.api_id,
            "api_hash": config.mtproto.api_hash,
            "session_string": config.mtproto.session_string,
            "session_path": config.mtproto.session_path,
            "targets": [
                {"title": t.title, "peer": t.peer} for t in config.mtproto.targets
            ],
        },
        "mcp": {
            "enabled": config.mcp.enabled,
            "host": config.mcp.host,
            "port": config.mcp.port,
            "token": config.mcp.token,
        },
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
