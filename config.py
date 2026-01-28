import dataclasses
import os
from typing import Any, Dict, List, Optional

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
    github_token: Optional[str] = None


@dataclasses.dataclass
class AppConfig:
    telegram: TelegramConfig
    tools: Dict[str, ToolConfig]
    defaults: DefaultsConfig
    path: str


def load_config(path: str) -> AppConfig:
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
        github_token=defaults_raw.get("github_token"),
    )

    return AppConfig(telegram=telegram, tools=tools, defaults=defaults, path=path)


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
            "github_token": config.defaults.github_token,
        },
    }

    for name, tool in config.tools.items():
        data["tools"][name] = {
            "mode": tool.mode,
            "cmd": tool.cmd,
            "headless_cmd": tool.headless_cmd,
            "resume_cmd": tool.resume_cmd,
            "interactive_cmd": tool.interactive_cmd,
            "prompt_regex": tool.prompt_regex,
            "resume_regex": tool.resume_regex,
            "help_cmd": tool.help_cmd,
            "env": tool.env,
            "auto_commands": tool.auto_commands,
        }

    with open(config.path, "w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, sort_keys=False, allow_unicode=False)
