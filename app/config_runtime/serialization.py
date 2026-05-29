from __future__ import annotations

from typing import TYPE_CHECKING, Any

import yaml
from pydantic import ValidationError

from .loader import ValidatedSettings

if TYPE_CHECKING:
    from config import AppConfig, MCPClientServerConfig, ToolConfig


def _serialize_tool(tool: "ToolConfig") -> dict[str, Any]:
    return {
        "mode": tool.mode,
        "cmd": list(tool.cmd),
        "enabled": bool(getattr(tool, "enabled", True)),
        "headless_cmd": list(tool.headless_cmd) if tool.headless_cmd else None,
        "resume_cmd": list(tool.resume_cmd) if tool.resume_cmd else None,
        "image_cmd": list(tool.image_cmd) if tool.image_cmd else None,
        "interactive_cmd": list(tool.interactive_cmd) if tool.interactive_cmd else None,
        "prompt_regex": tool.prompt_regex,
        "resume_regex": tool.resume_regex,
        "help_cmd": tool.help_cmd,
        "env": dict(tool.env) if tool.env is not None else None,
        "auto_commands": list(tool.auto_commands) if tool.auto_commands else None,
        "separate_stderr": tool.separate_stderr,
        "no_session_persistence_on_fresh": tool.no_session_persistence_on_fresh,
    }


def _serialize_mcp_client(client: "MCPClientServerConfig") -> dict[str, Any]:
    return {
        "name": client.name,
        "enabled": client.enabled,
        "transport": client.transport,
        "cmd": list(client.cmd),
        "url": client.url,
        "cwd": client.cwd,
        "env": dict(client.env) if client.env is not None else None,
        "headers": dict(client.headers) if client.headers is not None else None,
        "timeout_ms": client.timeout_ms,
    }


def _serialize_defaults(defaults: Any) -> dict[str, Any]:
    if hasattr(defaults, "model_dump"):
        return defaults.model_dump(mode="python")
    import dataclasses

    return dataclasses.asdict(defaults)


def _serialize_app_config_fallback(config: "AppConfig") -> dict[str, Any]:
    import dataclasses

    return {
        "telegram": dataclasses.asdict(config.telegram),
        "tools": {name: _serialize_tool(tool) for name, tool in (config.tools or {}).items()},
        "defaults": _serialize_defaults(config.defaults),
        "mcp": dataclasses.asdict(config.mcp),
        "miniapp": dataclasses.asdict(config.miniapp),
        "thread_mode": dataclasses.asdict(config.thread_mode),
        "webhooks": dataclasses.asdict(config.webhooks),
        "scheduler": dataclasses.asdict(config.scheduler),
        "security": dataclasses.asdict(config.security),
        "lint_evolution": dataclasses.asdict(config.lint_evolution),
        "mcp_clients": [_serialize_mcp_client(entry) for entry in (config.mcp_clients or [])],
        "presets": [dataclasses.asdict(entry) for entry in (config.presets or [])],
    }


def serialize_validated_settings(settings: ValidatedSettings) -> dict[str, Any]:
    """
    Canonical typed-config contract.

    Rules:
    - Root key order is fixed and stable.
    - External MCP endpoints are serialized only as `mcp_clients`.
    - Nested dict/list order is preserved from the validated model input.
    """

    return {
        "telegram": settings.telegram.model_dump(mode="python"),
        "tools": {name: tool.model_dump(mode="python") for name, tool in settings.tools.items()},
        "defaults": _serialize_defaults(settings.defaults),
        "mcp": settings.mcp.model_dump(mode="python"),
        "miniapp": settings.miniapp.model_dump(mode="python"),
        "thread_mode": settings.thread_mode.model_dump(mode="python"),
        "webhooks": settings.webhooks.model_dump(mode="python"),
        "scheduler": settings.scheduler.model_dump(mode="python"),
        "security": settings.security.model_dump(mode="python"),
        "lint_evolution": settings.lint_evolution.model_dump(mode="python"),
        "mcp_clients": [entry.model_dump(mode="python") for entry in settings.mcp_clients],
        "presets": [entry.model_dump(mode="python") for entry in settings.presets],
    }


def serialize_app_config(config: "AppConfig") -> dict[str, Any]:
    payload = _serialize_app_config_fallback(config)
    try:
        settings = ValidatedSettings.model_validate(payload)
    except ValidationError:
        return payload
    return serialize_validated_settings(settings)


def dump_app_config_yaml(config: "AppConfig") -> str:
    return yaml.safe_dump(serialize_app_config(config), sort_keys=False, allow_unicode=True)


__all__ = [
    "dump_app_config_yaml",
    "serialize_app_config",
    "serialize_validated_settings",
]
