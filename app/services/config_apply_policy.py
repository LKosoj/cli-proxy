from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from app.services.app_runtime_service import AppRuntimeService

ApplyMode = Literal["hot_reload", "restart_required", "not_supported"]
ConfigSurface = Literal["runtime", "ui_only", "operator_file"]


@dataclass(frozen=True)
class ConfigApplyPolicy:
    path_pattern: str
    apply_mode: ApplyMode
    surface: ConfigSurface
    secret: bool = False
    note: str = ""


_DEFAULT_SECRET_FIELDS = frozenset(
    {
        "defaults.openai_api_key",
        "defaults.zai_api_key",
        "defaults.github_token",
        "defaults.tavily_api_key",
        "defaults.jina_api_key",
        "defaults.gemini_oauth_client_secret",
    }
)

_RESTART_REQUIRED_FIELDS = frozenset(AppRuntimeService.RESTART_REQUIRED_FIELDS)
_RELOADABLE_FIELDS = frozenset(AppRuntimeService.RELOADABLE_FIELDS) | frozenset(
    f"defaults.{field}" for field in AppRuntimeService.RELOADABLE_DEFAULTS_FIELDS
)


def _policy(
    path_pattern: str,
    apply_mode: ApplyMode,
    surface: ConfigSurface = "runtime",
    *,
    secret: bool = False,
    note: str = "",
) -> ConfigApplyPolicy:
    return ConfigApplyPolicy(
        path_pattern=path_pattern,
        apply_mode=apply_mode,
        surface=surface,
        secret=secret,
        note=note,
    )


def _normalize_path(path: str) -> str:
    return ".".join(part.strip() for part in str(path or "").strip().split(".") if part.strip())


def _prefix(path: str, prefix: str) -> bool:
    return path == prefix or path.startswith(f"{prefix}.")


def classify_config_path(path: str) -> ConfigApplyPolicy:
    normalized = _normalize_path(path)
    if not normalized:
        return _policy("", "not_supported", "operator_file", note="empty config path")

    if normalized == "telegram.token":
        return _policy("telegram.token", "restart_required", secret=True)
    if normalized in _RESTART_REQUIRED_FIELDS:
        return _policy(normalized, "restart_required", secret=normalized in _DEFAULT_SECRET_FIELDS)
    if normalized in _RELOADABLE_FIELDS:
        return _policy(
            normalized,
            "hot_reload",
            secret=normalized in _DEFAULT_SECRET_FIELDS or normalized in {"mcp.token", "webhooks.secret_token"},
        )

    if normalized in _DEFAULT_SECRET_FIELDS:
        return _policy(
            normalized,
            "hot_reload",
            secret=True,
            note="secret field applied by AppRuntimeService defaults hot-reload path",
        )

    if _prefix(normalized, "tools"):
        return _policy("tools.*", "hot_reload", secret=".env." in normalized or normalized.endswith(".env"))
    if _prefix(normalized, "presets"):
        return _policy("presets.*", "hot_reload")
    if _prefix(normalized, "security"):
        return _policy("security.*", "hot_reload")
    if _prefix(normalized, "lint_evolution"):
        return _policy("lint_evolution.*", "hot_reload")

    if _prefix(normalized, "scheduler"):
        return _policy("scheduler.*", "restart_required")
    if _prefix(normalized, "thread_mode"):
        return _policy("thread_mode.*", "restart_required")
    if _prefix(normalized, "mcp_clients"):
        return _policy("mcp_clients.*", "restart_required")
    if _prefix(normalized, "webhooks"):
        return _policy("webhooks.*", "restart_required", secret=normalized == "webhooks.secret_token")
    if _prefix(normalized, "miniapp"):
        return _policy("miniapp.*", "restart_required")
    if _prefix(normalized, "mcp"):
        return _policy("mcp.*", "restart_required", secret=normalized == "mcp.token")
    if _prefix(normalized, "telegram"):
        return _policy("telegram.*", "restart_required")
    if _prefix(normalized, "defaults"):
        return _policy(
            "defaults.*",
            "restart_required",
            secret=normalized in _DEFAULT_SECRET_FIELDS,
            note="defaults path is restart-required unless AppRuntimeService explicitly reloads it",
        )

    return _policy(normalized, "not_supported", "operator_file", note="unknown config path")
