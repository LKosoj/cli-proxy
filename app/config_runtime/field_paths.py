from __future__ import annotations

from .models import (
    ConfigModel,
    DefaultsConfigModel,
    LintEvolutionConfigModel,
    MCPClientConfigModel,
    MCPConfigModel,
    MiniAppConfigModel,
    PresetConfigModel,
    SchedulerConfigModel,
    SecurityRateLimitPolicyConfigModel,
    SecurityRateLimitsConfigModel,
    TelegramConfigModel,
    ThreadModeConfigModel,
    ToolConfigModel,
    WebhooksConfigModel,
)


def _model_field_paths(prefix: str, model_cls: type[ConfigModel]) -> set[str]:
    return {f"{prefix}.{field}" for field in model_cls.model_fields}


RUNTIME_CONFIG_FIELD_PATHS = frozenset(
    path
    for prefix, model_cls in (
        ("telegram", TelegramConfigModel),
        ("tools.*", ToolConfigModel),
        ("defaults", DefaultsConfigModel),
        ("mcp", MCPConfigModel),
        ("mcp_clients.*", MCPClientConfigModel),
        ("presets.*", PresetConfigModel),
        ("miniapp", MiniAppConfigModel),
        ("thread_mode", ThreadModeConfigModel),
        ("webhooks", WebhooksConfigModel),
        ("scheduler", SchedulerConfigModel),
        ("security.rate_limits", SecurityRateLimitsConfigModel),
        ("security.rate_limits.default", SecurityRateLimitPolicyConfigModel),
        ("security.rate_limits.policies.*", SecurityRateLimitPolicyConfigModel),
        ("lint_evolution", LintEvolutionConfigModel),
    )
    for path in _model_field_paths(prefix, model_cls)
)


__all__ = ["RUNTIME_CONFIG_FIELD_PATHS"]
