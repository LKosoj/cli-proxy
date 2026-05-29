from __future__ import annotations

import copy
import logging
import os
from pathlib import Path
from typing import Any, Mapping

import yaml
from pydantic import ValidationError

from app.services.dotenv_loader import parse_dotenv

from .models import AppConfigModel

ValidatedSettings = AppConfigModel

ENV_OVERRIDE_PREFIX = "CLI_PROXY__"
LEGACY_ENV_ALIASES: dict[str, tuple[str | int, ...]] = {
    "OPENAI_API_KEY": ("defaults", "openai_api_key"),
    "OPENAI_MODEL": ("defaults", "openai_model"),
    "OPENAI_BIG_MODEL": ("defaults", "openai_big_model"),
    "OPENAI_BASE_URL": ("defaults", "openai_base_url"),
    "ZAI_API_KEY": ("defaults", "zai_api_key"),
    "TAVILY_API_KEY": ("defaults", "tavily_api_key"),
    "JINA_API_KEY": ("defaults", "jina_api_key"),
    "GITHUB_TOKEN": ("defaults", "github_token"),
}


def _read_yaml_config(path: str) -> dict[str, Any]:
    config_path = Path(path)
    try:
        content = config_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError(f"cannot read config file: {exc}") from exc

    try:
        payload = yaml.safe_load(content)
    except yaml.YAMLError as exc:
        raise ValueError(f"invalid YAML: {exc}") from exc

    if payload is None:
        return {}
    if not isinstance(payload, dict):
        raise ValueError("config root must be a mapping")
    return payload


def _read_dotenv_values(config_path: str, env_path: str | None = None) -> dict[str, str]:
    dotenv_path = Path(env_path) if env_path else Path(config_path).resolve().parent / ".env"
    if not dotenv_path.exists():
        return {}
    try:
        content = dotenv_path.read_text(encoding="utf-8")
    except OSError:
        return {}
    return parse_dotenv(content)


def _parse_override_value(raw: str) -> Any:
    if raw == "":
        return ""
    try:
        return yaml.safe_load(raw)
    except yaml.YAMLError:
        return raw


def _normalize_override_tokens(tokens: list[str]) -> list[str | int]:
    normalized: list[str | int] = []
    preserve_case = False
    for token in tokens:
        raw_item = str(token or "").strip()
        item = raw_item if preserve_case else raw_item.lower()
        if not item:
            continue
        normalized.append(item if preserve_case else (int(item) if item.isdigit() else item))
        env_is_tool_name = (
            not preserve_case
            and item == "env"
            and len(normalized) == 2
            and normalized[0] == "tools"
        )
        if not preserve_case and item == "env" and not env_is_tool_name:
            preserve_case = True
    return normalized


def _set_nested_value(target: dict[str | int, Any], path: tuple[str | int, ...], value: Any) -> None:
    current: dict[str | int, Any] = target
    for key in path[:-1]:
        next_value = current.get(key)
        if not isinstance(next_value, dict):
            next_value = {}
            current[key] = next_value
        current = next_value
    current[path[-1]] = value


def _deep_merge(base: dict[str, Any], overrides: dict[str | int, Any]) -> dict[str, Any]:
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _deep_merge(base[key], value)
            continue
        base[key] = value
    return base


def _build_env_overrides(
    config_path: str,
    *,
    env_path: str | None = None,
    environ: Mapping[str, str] | None = None,
) -> dict[str | int, Any]:
    # Build a unified env view where process env wins over .env for the same variable name.
    env_view: dict[str, str] = _read_dotenv_values(config_path, env_path=env_path)
    env_view.update(dict(os.environ if environ is None else environ))

    overrides: dict[str | int, Any] = {}

    # Legacy aliases are backward-compatible env overrides for defaults.*.
    # They overlay YAML values just like other env-based overrides.
    for env_name, path in LEGACY_ENV_ALIASES.items():
        if env_name not in env_view:
            continue
        _set_nested_value(overrides, path, _parse_override_value(env_view[env_name]))

    # CLI_PROXY__* remains the most explicit mechanism and is applied after legacy aliases,
    # so it wins if both target the same final config path.
    for env_name, env_value in env_view.items():
        if not str(env_name or "").upper().startswith(ENV_OVERRIDE_PREFIX):
            continue
        suffix = env_name[len(ENV_OVERRIDE_PREFIX):]
        tokens = _normalize_override_tokens(suffix.split("__"))
        if not tokens:
            continue
        _set_nested_value(overrides, tuple(tokens), _parse_override_value(env_value))

    return overrides


def _format_validation_errors(exc: ValidationError) -> str:
    lines: list[str] = []
    for error in exc.errors():
        location = ".".join(str(part) for part in error.get("loc", ())) or "<root>"
        message = str(error.get("msg", "validation error"))
        lines.append(f"- {location}: {message}")
    return "\n".join(lines)


def load_validated_settings(
    path: str,
    *,
    env_path: str | None = None,
    environ: Mapping[str, str] | None = None,
    logger: logging.Logger | None = None,
) -> ValidatedSettings:
    log = logger or logging.getLogger(__name__)
    config_path = str(path)

    try:
        payload = _read_yaml_config(config_path)
        overrides = _build_env_overrides(config_path, env_path=env_path, environ=environ)
        # Precedence (lowest -> highest):
        # 1. config.yaml
        # 2. legacy env aliases from .env/process env (process env wins over .env)
        # 3. CLI_PROXY__* overrides from .env/process env (most explicit; process env wins over .env)
        merged_payload = _deep_merge(copy.deepcopy(payload), overrides) if overrides else copy.deepcopy(payload)
        return ValidatedSettings.model_validate(merged_payload)
    except ValidationError as exc:
        log.error("validated config load failed path=%s\n%s", config_path, _format_validation_errors(exc))
    except ValueError as exc:
        log.error("validated config load failed path=%s\n- <root>: %s", config_path, exc)

    raise SystemExit(1)


__all__ = [
    "ENV_OVERRIDE_PREFIX",
    "ValidatedSettings",
    "load_validated_settings",
]
