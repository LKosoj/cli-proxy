from __future__ import annotations

import logging
from dataclasses import dataclass, field, replace
from typing import Any, Dict, List, Mapping, Optional

from .snapshot_store import SEVERITY_ALARM, SEVERITY_INFO, SEVERITY_NOISE, SEVERITY_WARN

_log = logging.getLogger(__name__)

_ALLOWED_SEVERITIES = (SEVERITY_NOISE, SEVERITY_INFO, SEVERITY_WARN, SEVERITY_ALARM)

DEFAULT_MAX_ACTIONS_PER_HOUR = 5
DEFAULT_COOLDOWN_SEC = 300
DEFAULT_BASELINE_STABLE_SCANS = 3


@dataclass
class AutonomyPolicy:
    """
    Политика автономии: что агенту разрешено делать самостоятельно.

    Все severities → строки из SEVERITY_{NOISE,INFO,WARN,ALARM}.
    alarm по умолчанию никогда не авто-применяется.
    """

    enabled: bool = False
    auto_apply_severities: List[str] = field(default_factory=lambda: [SEVERITY_INFO])
    auto_exec_actions: List[str] = field(default_factory=list)
    auto_exec_adhoc_commands: List[str] = field(default_factory=list)
    max_actions_per_hour: int = DEFAULT_MAX_ACTIONS_PER_HOUR
    cooldown_sec: int = DEFAULT_COOLDOWN_SEC
    baseline_auto_accept_enabled: bool = False
    baseline_auto_accept_after_stable_scans: int = DEFAULT_BASELINE_STABLE_SCANS
    require_dry_run_success: bool = True
    _per_server: Dict[str, "AutonomyPolicy"] = field(default_factory=dict, repr=False)

    def permits_severity(self, severity: str) -> bool:
        sev = str(severity or "").strip().lower()
        if sev == SEVERITY_ALARM:
            return False  # alarm — всегда ручной разбор
        return sev in {str(s).strip().lower() for s in self.auto_apply_severities}

    def permits_action(self, action_id: str) -> bool:
        aid = str(action_id or "").strip()
        if not aid:
            return False
        return aid in {str(a).strip() for a in self.auto_exec_actions if str(a or "").strip()}

    def permits_adhoc_argv(self, argv: List[str]) -> bool:
        if not argv:
            return False
        head = str(argv[0] or "").strip()
        if not head:
            return False
        allow = {str(c).strip() for c in self.auto_exec_adhoc_commands if str(c or "").strip()}
        return head in allow

    def for_server(self, server_id: str) -> "AutonomyPolicy":
        sid = str(server_id or "").strip()
        if sid and sid in self._per_server:
            override = self._per_server[sid]
            return _merge_policies(self, override)
        return self


def _merge_policies(base: AutonomyPolicy, override: AutonomyPolicy) -> AutonomyPolicy:
    # per-server override: любые явно заданные поля перекрывают базовые.
    # Поскольку dataclass не отличает "не задано" от "=default", используем прагматичный вариант:
    # списки/числа override берутся если они непустые / положительные,
    # булевы — override всегда (per-server явно переопределяет).
    merged = replace(base)
    merged.enabled = override.enabled if override.enabled != AutonomyPolicy.enabled else base.enabled
    if override.auto_apply_severities:
        merged.auto_apply_severities = list(override.auto_apply_severities)
    if override.auto_exec_actions:
        merged.auto_exec_actions = list(override.auto_exec_actions)
    if override.auto_exec_adhoc_commands:
        merged.auto_exec_adhoc_commands = list(override.auto_exec_adhoc_commands)
    if override.max_actions_per_hour and override.max_actions_per_hour > 0:
        merged.max_actions_per_hour = int(override.max_actions_per_hour)
    if override.cooldown_sec and override.cooldown_sec > 0:
        merged.cooldown_sec = int(override.cooldown_sec)
    merged.baseline_auto_accept_enabled = bool(override.baseline_auto_accept_enabled) or base.baseline_auto_accept_enabled
    if override.baseline_auto_accept_after_stable_scans and override.baseline_auto_accept_after_stable_scans > 0:
        merged.baseline_auto_accept_after_stable_scans = int(override.baseline_auto_accept_after_stable_scans)
    merged.require_dry_run_success = bool(override.require_dry_run_success) and base.require_dry_run_success
    return merged


def _coerce_severity_list(raw: Any) -> List[str]:
    out: List[str] = []
    if raw is None:
        return out
    if isinstance(raw, str):
        items = [raw]
    elif isinstance(raw, (list, tuple, set)):
        items = list(raw)
    else:
        return out
    for item in items:
        sev = str(item or "").strip().lower()
        if sev in _ALLOWED_SEVERITIES:
            out.append(sev)
    return out


def _coerce_action_list(raw: Any) -> List[str]:
    if raw is None:
        return []
    if isinstance(raw, str):
        return [raw.strip()] if raw.strip() else []
    if isinstance(raw, (list, tuple, set)):
        return [str(a).strip() for a in raw if str(a or "").strip()]
    return []


def _policy_from_mapping(raw: Mapping[str, Any]) -> AutonomyPolicy:
    if not isinstance(raw, Mapping):
        raw = {}
    baseline_cfg = raw.get("baseline_auto_accept") if isinstance(raw.get("baseline_auto_accept"), Mapping) else {}
    return AutonomyPolicy(
        enabled=bool(raw.get("enabled", False)),
        auto_apply_severities=_coerce_severity_list(raw.get("auto_apply_severities")) or [SEVERITY_INFO],
        auto_exec_actions=_coerce_action_list(raw.get("auto_exec_actions")),
        auto_exec_adhoc_commands=_coerce_action_list(raw.get("auto_exec_adhoc_commands")),
        max_actions_per_hour=_coerce_positive_int(
            raw.get("max_actions_per_hour"), default=DEFAULT_MAX_ACTIONS_PER_HOUR,
        ),
        cooldown_sec=_coerce_positive_int(
            raw.get("cooldown_sec"), default=DEFAULT_COOLDOWN_SEC,
        ),
        baseline_auto_accept_enabled=bool(baseline_cfg.get("enabled", False)),
        baseline_auto_accept_after_stable_scans=_coerce_positive_int(
            baseline_cfg.get("after_stable_scans"), default=DEFAULT_BASELINE_STABLE_SCANS,
        ),
        require_dry_run_success=bool(raw.get("require_dry_run_success", True)),
    )


def _coerce_positive_int(value: Any, *, default: int) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return int(default)
    return n if n > 0 else int(default)


def load_autonomy_policy(admin_cfg: Optional[Mapping[str, Any]]) -> AutonomyPolicy:
    """
    Парсит `admin.autonomy` из конфига:

        admin:
          autonomy:
            enabled: true
            auto_apply_severities: [info, warn]
            auto_exec_actions: ["systemd.restart_nginx", ...]
            max_actions_per_hour: 5
            cooldown_sec: 300
            baseline_auto_accept:
              enabled: true
              after_stable_scans: 3
            per_server:
              web-01:
                auto_apply_severities: [info]
                auto_exec_actions: ["systemd.restart_nginx"]
    """
    autonomy_cfg = {}
    if isinstance(admin_cfg, Mapping):
        maybe = admin_cfg.get("autonomy")
        if isinstance(maybe, Mapping):
            autonomy_cfg = dict(maybe)
    policy = _policy_from_mapping(autonomy_cfg)

    per_server_raw = autonomy_cfg.get("per_server") if isinstance(autonomy_cfg.get("per_server"), Mapping) else {}
    per_server: Dict[str, AutonomyPolicy] = {}
    for sid, block in per_server_raw.items():
        sid_clean = str(sid or "").strip()
        if not sid_clean or not isinstance(block, Mapping):
            continue
        per_server[sid_clean] = _policy_from_mapping(block)
    policy._per_server = per_server
    return policy


__all__ = [
    "AutonomyPolicy",
    "DEFAULT_BASELINE_STABLE_SCANS",
    "DEFAULT_COOLDOWN_SEC",
    "DEFAULT_MAX_ACTIONS_PER_HOUR",
    "load_autonomy_policy",
]
