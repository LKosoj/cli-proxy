from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import logging
import os
import re
import shlex
import sys
from typing import Any, Dict, Mapping

try:
    from .transports import (
        LocalCommandSpec,
        LocalSubprocessTransport,
        SSHCommandSpec,
        SSHSubprocessTransport,
    )
except ImportError:  # pragma: no cover - fallback for direct module loading in tests
    _TRANSPORTS_DIR = os.path.join(os.path.dirname(__file__), "transports")

    _LOCAL_SPEC = importlib.util.spec_from_file_location(
        "modes_admin_local_transport_direct",
        os.path.join(_TRANSPORTS_DIR, "local.py"),
    )
    if _LOCAL_SPEC is None or _LOCAL_SPEC.loader is None:
        raise
    _LOCAL_MODULE = importlib.util.module_from_spec(_LOCAL_SPEC)
    sys.modules[_LOCAL_SPEC.name] = _LOCAL_MODULE
    _LOCAL_SPEC.loader.exec_module(_LOCAL_MODULE)

    _SSH_SPEC = importlib.util.spec_from_file_location(
        "modes_admin_ssh_transport_direct",
        os.path.join(_TRANSPORTS_DIR, "ssh.py"),
    )
    if _SSH_SPEC is None or _SSH_SPEC.loader is None:
        raise
    _SSH_MODULE = importlib.util.module_from_spec(_SSH_SPEC)
    sys.modules[_SSH_SPEC.name] = _SSH_MODULE
    _SSH_SPEC.loader.exec_module(_SSH_MODULE)

    LocalCommandSpec = _LOCAL_MODULE.LocalCommandSpec
    LocalSubprocessTransport = _LOCAL_MODULE.LocalSubprocessTransport
    SSHCommandSpec = _SSH_MODULE.SSHCommandSpec
    SSHSubprocessTransport = _SSH_MODULE.SSHSubprocessTransport


_LOG = logging.getLogger(__name__)
_RUNTIME_INVENTORY_ACTION_ID = "scan_runtime_inventory"
_MAX_DYNAMIC_SYSTEMD_SERVICES = 80
_MAX_DYNAMIC_PROCESSES = 120
_MAX_DYNAMIC_CONTAINERS = 80
_TRANSIENT_PROCESS_NAMES = {
    "awk",
    "bash",
    "cat",
    "cut",
    "date",
    "df",
    "find",
    "grep",
    "head",
    "journalctl",
    "less",
    "more",
    "paste",
    "pgrep",
    "ps",
    "sed",
    "sh",
    "sleep",
    "sort",
    "ssh",
    "su",
    "sudo",
    "tail",
    "tee",
    "timeout",
    "tr",
    "uname",
    "wc",
    "xargs",
}
_PROTECTED_SYSTEMD_RESTARTS = {
    "containerd",
    "cron",
    "crond",
    "dbus",
    "docker",
    "network",
    "networking",
    "networkmanager",
    "ssh",
    "sshd",
    "systemd-journald",
    "systemd-logind",
    "systemd-networkd",
    "systemd-resolved",
    "systemd-udevd",
}
_RUNTIME_INVENTORY_COMMAND = r"""
printf '__SYSTEMD__\n'
if command -v systemctl >/dev/null 2>&1; then
  systemctl list-units --type=service --state=running --no-legend --no-pager 2>/dev/null \
    | awk '{name=$1; sub(/\.service$/, "", name); print name}' \
    | sort \
    | head -200
fi
printf '__PROCESSES__\n'
if command -v ps >/dev/null 2>&1; then
  ps -eo comm= --no-headers 2>/dev/null \
    | awk 'NF {count[$1]++} END {for (name in count) printf "%s\t%d\n", name, count[name]}' \
    | sort -k2,2nr -k1,1 \
    | head -300
fi
printf '__CONTAINERS__\n'
if command -v docker >/dev/null 2>&1 && docker ps >/dev/null 2>&1; then
  docker ps --format '{{.Names}}\t{{.Image}}\t{{.Status}}' 2>/dev/null | head -200
fi
"""


def _shell_argv(command: str) -> tuple[str, ...]:
    return ("bash", "-lc", str(command or "").strip())


def _is_monitorable_process_name(process_name: str) -> bool:
    normalized = str(process_name or "").strip().lower()
    if not normalized:
        return False
    if normalized in _TRANSIENT_PROCESS_NAMES:
        return False
    return not (normalized.startswith("[") and normalized.endswith("]"))


def _append_unique(items: list[str], value: str) -> None:
    clean = str(value or "").strip()
    if clean and clean not in items:
        items.append(clean)


def _safe_id_fragment(value: str, *, fallback: str = "item", max_len: int = 64) -> str:
    clean = re.sub(r"[^A-Za-z0-9._:-]+", "_", str(value or "").strip())
    clean = clean.strip("._:-")
    if not clean:
        clean = fallback
    if not clean[0].isalnum():
        clean = f"{fallback}_{clean}"
    if len(clean) > max_len:
        digest = hashlib.sha1(clean.encode("utf-8")).hexdigest()[:8]
        clean = f"{clean[:max_len - 9]}_{digest}"
    return clean[:max_len]


def _safe_metric_fragment(value: str, *, fallback: str = "item", max_len: int = 64) -> str:
    clean = re.sub(r"[^A-Za-z0-9]+", "_", str(value or "").strip()).strip("_").lower()
    if not clean:
        clean = fallback
    if not clean[0].isalpha():
        clean = f"{fallback}_{clean}"
    if len(clean) > max_len:
        digest = hashlib.sha1(clean.encode("utf-8")).hexdigest()[:8]
        clean = f"{clean[:max_len - 9]}_{digest}"
    return clean[:max_len]


class AdminEnvironmentScanner:
    """Admin environment scanner bound to the session pinned CLI."""

    def __init__(
        self,
        pinned_cli: Mapping[str, Any] | str | None = None,
        *,
        local_transport: Any | None = None,
        ssh_transport: Any | None = None,
        secrets_workdir: str = "",
        timeout_sec: float = 10.0,
    ) -> None:
        self.pinned_cli = self._normalize_pinned_cli(pinned_cli)
        self._local_transport = local_transport or LocalSubprocessTransport()
        self._ssh_transport = ssh_transport or SSHSubprocessTransport()
        self._secrets_workdir = str(secrets_workdir or "").strip()
        self._timeout_sec = float(timeout_sec or 10.0)

    def scan(self) -> Dict[str, Any]:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(self.scan_async())
        raise RuntimeError("AdminEnvironmentScanner.scan() requires sync context; use scan_async() instead")

    async def scan_async(self) -> Dict[str, Any]:
        transport_kind = self._resolve_transport_kind()
        runtime_inventory = await self._collect_runtime_inventory(transport_kind=transport_kind)
        services: Dict[str, Any] = {}
        diagnostics_checks: Dict[str, Any] = {}
        incident_rules: Dict[str, Any] = {}
        monitor_servers: list[Dict[str, Any]] = []
        remediation_actions: Dict[str, Any] = {}
        target_actions: Dict[str, Any] = {}
        server_roles: list[str] = []
        stack_facts: Dict[str, Any] = {
            "host": [],
            "web_stack": [],
            "app_runtime": [],
            "databases": [],
            "helpers": [],
            "security": [],
            "container_runtime": list(runtime_inventory.get("container_runtime", [])),
            "detected_services": [],
            "systemd_services": list(runtime_inventory.get("systemd_services", [])),
            "processes": dict(runtime_inventory.get("processes", {})),
            "containers": list(runtime_inventory.get("containers", [])),
        }

        self._merge_dynamic_inventory_monitors(
            runtime_inventory=runtime_inventory,
            transport_kind=transport_kind,
            services=services,
            diagnostics_checks=diagnostics_checks,
            incident_rules=incident_rules,
            monitor_servers=monitor_servers,
            remediation_actions=remediation_actions,
            target_actions=target_actions,
            server_roles=server_roles,
            stack_facts=stack_facts,
        )

        analyzer_policy = {
            "allow_secondary_cli": True,
            "allow_internet_secondary_cli": True,
            "require_secondary_confirmation_on_low_confidence": True,
            "require_secondary_confirmation_on_risky_action": True,
            "require_secondary_confirmation_on_signal_conflict": True,
            "require_secondary_confirmation_on_policy_conflict": True,
            "require_secondary_confirmation_before_remediation": True,
            "risky_actions": sorted(
                action_id
                for action_id, payload in remediation_actions.items()
                if str(payload.get("risk_level") or "").strip().lower() in {"high", "critical"}
            ),
        }
        executor_policy = {
            "mandatory_notify_actions": list(analyzer_policy["risky_actions"]),
            "auto_actions_per_hour": 6,
            "cooldown_sec": 120,
        }

        environment = {
            "pinned_cli": dict(self.pinned_cli),
            "transport": transport_kind,
            "services": services,
            "server_roles": server_roles,
            "stack_facts": stack_facts,
        }
        runbooks = {
            "templates": self._build_generated_runbook_templates(
                services=services,
                transport_kind=transport_kind,
            )
        }
        summary = {
            "transport": transport_kind,
            "detected_services": sorted(services.keys()),
            "server_roles": list(server_roles),
        }
        generated = {
            "environment": environment,
            "diagnostics": {"checks": diagnostics_checks},
            "incidents": {"rules": incident_rules},
            "actions": {
                "remediation": remediation_actions,
                "targets": {
                    transport_kind: target_actions,
                },
            },
            "policies": {
                "analyzer": analyzer_policy,
                "executor": executor_policy,
            },
            "runbooks": runbooks,
            "monitor": {
                "enabled": bool(monitor_servers),
                "interval_sec": 30,
                "servers": monitor_servers,
            },
            "scan_meta": {
                "transport": transport_kind,
                "service_count": len(services),
                "detected_services": sorted(services.keys()),
            },
        }

        return {
            "services": services,
            "pinned_cli": dict(self.pinned_cli),
            "environment": environment,
            "diagnostics": {"checks": diagnostics_checks},
            "incidents": {"rules": incident_rules},
            "actions": {
                "remediation": remediation_actions,
                "targets": {
                    transport_kind: target_actions,
                },
            },
            "policies": {
                "analyzer": analyzer_policy,
                "executor": executor_policy,
            },
            "runbooks": runbooks,
            "monitor": {
                "enabled": bool(monitor_servers),
                "interval_sec": 30,
                "servers": monitor_servers,
            },
            "scan_meta": {
                "transport": transport_kind,
                "service_count": len(services),
            },
            "scan_summary": summary,
            "generated": generated,
        }

    @staticmethod
    def _build_generated_runbook_templates(
        *,
        services: Mapping[str, Any],
        transport_kind: str,
    ) -> Dict[str, Any]:
        templates: Dict[str, Any] = {}
        for service_id, raw_payload in sorted(dict(services or {}).items()):
            if not isinstance(raw_payload, Mapping):
                continue
            check_action_id = str(raw_payload.get("check_action_id") or "").strip()
            inspect_action_id = str(raw_payload.get("inspect_action_id") or check_action_id).strip()
            if not inspect_action_id:
                continue
            fragment = _safe_metric_fragment(service_id, fallback="service")
            runbook_id = f"inspect_{fragment}"
            steps: list[Dict[str, Any]] = [
                {
                    "name": "inspect",
                    "action_id": inspect_action_id,
                    "target": str(raw_payload.get("transport") or transport_kind).strip() or transport_kind,
                    "risk_level": "low",
                }
            ]
            remediation_action_id = str(raw_payload.get("remediation_action_id") or "").strip()
            if remediation_action_id:
                steps.append(
                    {
                        "name": "remediate",
                        "action_id": remediation_action_id,
                        "target": str(raw_payload.get("transport") or transport_kind).strip() or transport_kind,
                        "requires_approval": True,
                    }
                )
            templates[runbook_id] = {
                "id": runbook_id,
                "title": f"Inspect {service_id}",
                "service": str(service_id),
                "source": "runtime_inventory",
                "steps": steps,
            }
        return templates

    def _merge_dynamic_inventory_monitors(
        self,
        *,
        runtime_inventory: Mapping[str, Any],
        transport_kind: str,
        services: Dict[str, Any],
        diagnostics_checks: Dict[str, Any],
        incident_rules: Dict[str, Any],
        monitor_servers: list[Dict[str, Any]],
        remediation_actions: Dict[str, Any],
        target_actions: Dict[str, Any],
        server_roles: list[str],
        stack_facts: Dict[str, Any],
    ) -> None:
        self._merge_host_runtime_monitor(
            transport_kind=transport_kind,
            services=services,
            diagnostics_checks=diagnostics_checks,
            incident_rules=incident_rules,
            monitor_servers=monitor_servers,
            target_actions=target_actions,
            server_roles=server_roles,
            stack_facts=stack_facts,
        )
        self._merge_dynamic_systemd_monitors(
            runtime_inventory=runtime_inventory,
            transport_kind=transport_kind,
            services=services,
            diagnostics_checks=diagnostics_checks,
            incident_rules=incident_rules,
            monitor_servers=monitor_servers,
            remediation_actions=remediation_actions,
            target_actions=target_actions,
            server_roles=server_roles,
            stack_facts=stack_facts,
        )
        self._merge_dynamic_process_monitors(
            runtime_inventory=runtime_inventory,
            transport_kind=transport_kind,
            services=services,
            diagnostics_checks=diagnostics_checks,
            incident_rules=incident_rules,
            monitor_servers=monitor_servers,
            target_actions=target_actions,
            server_roles=server_roles,
            stack_facts=stack_facts,
        )
        self._merge_fail2ban_monitor(
            runtime_inventory=runtime_inventory,
            transport_kind=transport_kind,
            services=services,
            diagnostics_checks=diagnostics_checks,
            incident_rules=incident_rules,
            monitor_servers=monitor_servers,
            target_actions=target_actions,
            server_roles=server_roles,
            stack_facts=stack_facts,
        )
        self._merge_dynamic_container_monitors(
            runtime_inventory=runtime_inventory,
            transport_kind=transport_kind,
            services=services,
            diagnostics_checks=diagnostics_checks,
            incident_rules=incident_rules,
            monitor_servers=monitor_servers,
            remediation_actions=remediation_actions,
            target_actions=target_actions,
            server_roles=server_roles,
            stack_facts=stack_facts,
        )

    def _merge_host_runtime_monitor(
        self,
        *,
        transport_kind: str,
        services: Dict[str, Any],
        diagnostics_checks: Dict[str, Any],
        incident_rules: Dict[str, Any],
        monitor_servers: list[Dict[str, Any]],
        target_actions: Dict[str, Any],
        server_roles: list[str],
        stack_facts: Dict[str, Any],
    ) -> None:
        service_id = "host:runtime"
        if service_id in services:
            return
        self._add_dynamic_service(
            service_id=service_id,
            category="host",
            role="host",
            stack_group="host",
            transport_kind=transport_kind,
            action_id="diag_host_runtime",
            check_command=self._host_runtime_command(),
            inspect_command=self._host_inspect_command(),
            services=services,
            diagnostics_checks=diagnostics_checks,
            monitor_servers=monitor_servers,
            target_actions=target_actions,
            server_roles=server_roles,
            stack_facts=stack_facts,
        )
        self._add_threshold_rule(
            incident_rules=incident_rules,
            rule_id="host_runtime_thresholds",
            service_id=service_id,
            check_id=f"check:{service_id}",
            runbook_id="inspect_host_runtime",
            fallback_action="notify_admin",
            thresholds=[
                self._threshold("host_memory_used_pct", "gte", 90, "saturation.memory_high"),
                self._threshold("host_disk_root_pct", "gte", 90, "saturation.disk_high"),
                self._threshold("host_inode_root_pct", "gte", 90, "saturation.inodes_high"),
                self._threshold("host_systemd_failed_units", "gte", 1, "availability.systemd_failed_units"),
                self._threshold("host_tcp_syn_recv", "gte", 200, "network.syn_flood_suspected", urgency="critical"),
                self._threshold("host_tcp_established", "gte", 5000, "network.connection_pressure"),
            ],
        )

    def _merge_dynamic_systemd_monitors(
        self,
        *,
        runtime_inventory: Mapping[str, Any],
        transport_kind: str,
        services: Dict[str, Any],
        diagnostics_checks: Dict[str, Any],
        incident_rules: Dict[str, Any],
        monitor_servers: list[Dict[str, Any]],
        remediation_actions: Dict[str, Any],
        target_actions: Dict[str, Any],
        server_roles: list[str],
        stack_facts: Dict[str, Any],
    ) -> None:
        for service_name in list(runtime_inventory.get("systemd_services", []) or [])[:_MAX_DYNAMIC_SYSTEMD_SERVICES]:
            service_name = str(service_name or "").strip().removesuffix(".service")
            if not service_name or service_name in services:
                continue
            fragment = _safe_id_fragment(service_name, fallback="service")
            metric_fragment = _safe_metric_fragment(service_name, fallback="service")
            service_id = f"systemd:{fragment}"
            if service_id in services:
                continue
            action_id = f"diag_systemd_{fragment}_status"
            check_command = self._systemd_status_command(
                service_name=service_name,
                metric_key=f"systemd_{metric_fragment}_state",
            )

            self._add_dynamic_service(
                service_id=service_id,
                category="systemd",
                role="service",
                stack_group="systemd",
                transport_kind=transport_kind,
                action_id=action_id,
                check_command=check_command,
                inspect_command=self._systemd_inspect_command(service_name=service_name),
                services=services,
                diagnostics_checks=diagnostics_checks,
                monitor_servers=monitor_servers,
                target_actions=target_actions,
                server_roles=server_roles,
                stack_facts=stack_facts,
            )

            remediation_id = ""
            if service_name.lower() not in _PROTECTED_SYSTEMD_RESTARTS:
                remediation_id = f"restart_systemd_{fragment}"
                remediation_actions[remediation_id] = {
                    "service": service_id,
                    "target": transport_kind,
                    "action_id": remediation_id,
                    "risk_level": "high",
                    "description": f"Restart systemd service {service_name}",
                }
                services[service_id]["remediation_action_id"] = remediation_id
                target_actions[remediation_id] = {
                    "argv": list(_shell_argv(f"systemctl restart {shlex.quote(service_name)}")),
                    "timeout_sec": 45,
                    "risk_level": "high",
                }
            incident_rules[f"systemd_{fragment}_down"] = {
                "id": f"rule:systemd_{fragment}_down",
                "service": service_id,
                "check_id": f"check:{service_id}",
                "runbook_id": f"inspect_systemd_{_safe_metric_fragment(fragment, fallback='service')}",
                "thresholds": [
                    self._threshold(f"systemd_{metric_fragment}_state", "in", ["down"], "availability.service_down"),
                    self._threshold(
                        f"systemd_{metric_fragment}_restart_count",
                        "gte",
                        3,
                        "runtime.service_restart_loop",
                    ),
                    self._threshold(
                        f"systemd_{metric_fragment}_exit_status",
                        "gte",
                        1,
                        "runtime.service_exit_error",
                    ),
                ],
                "recommended_action": remediation_id or "notify_admin",
                "fallback_action": "notify_admin",
                "requires_manual_approval": bool(remediation_id),
            }

    def _merge_dynamic_process_monitors(
        self,
        *,
        runtime_inventory: Mapping[str, Any],
        transport_kind: str,
        services: Dict[str, Any],
        diagnostics_checks: Dict[str, Any],
        incident_rules: Dict[str, Any],
        monitor_servers: list[Dict[str, Any]],
        target_actions: Dict[str, Any],
        server_roles: list[str],
        stack_facts: Dict[str, Any],
    ) -> None:
        process_items = list((runtime_inventory.get("processes", {}) or {}).items())
        process_items.sort(key=lambda item: (-int(item[1] or 0), str(item[0])))
        for process_name, _count in process_items[:_MAX_DYNAMIC_PROCESSES]:
            process_name = str(process_name or "").strip()
            if not _is_monitorable_process_name(process_name):
                continue
            fragment = _safe_id_fragment(process_name, fallback="process")
            metric_fragment = _safe_metric_fragment(process_name, fallback="process")
            service_id = f"process:{fragment}"
            if service_id in services:
                continue
            action_id = f"diag_process_{fragment}_status"
            check_command = self._process_status_command(
                process_name=process_name,
                metric_key=f"process_{metric_fragment}_state",
                count_key=f"process_{metric_fragment}_count",
            )
            self._add_dynamic_service(
                service_id=service_id,
                category="process",
                role="process",
                stack_group="process",
                transport_kind=transport_kind,
                action_id=action_id,
                check_command=check_command,
                inspect_command=self._process_inspect_command(process_name=process_name),
                services=services,
                diagnostics_checks=diagnostics_checks,
                monitor_servers=monitor_servers,
                target_actions=target_actions,
                server_roles=server_roles,
                stack_facts=stack_facts,
            )
            self._add_threshold_rule(
                incident_rules=incident_rules,
                rule_id=f"process_{fragment}_thresholds",
                service_id=service_id,
                check_id=f"check:{service_id}",
                runbook_id=f"inspect_process_{_safe_metric_fragment(fragment, fallback='process')}",
                fallback_action="notify_admin",
                thresholds=[
                    self._threshold(f"process_{metric_fragment}_state", "in", ["down"], "availability.process_down"),
                    self._threshold(f"process_{metric_fragment}_count", "lte", 0, "availability.process_down"),
                    self._threshold(
                        f"process_{metric_fragment}_cpu_capacity_pct",
                        "gte",
                        90,
                        "saturation.process_cpu_high",
                    ),
                ],
            )

    def _merge_fail2ban_monitor(
        self,
        *,
        runtime_inventory: Mapping[str, Any],
        transport_kind: str,
        services: Dict[str, Any],
        diagnostics_checks: Dict[str, Any],
        incident_rules: Dict[str, Any],
        monitor_servers: list[Dict[str, Any]],
        target_actions: Dict[str, Any],
        server_roles: list[str],
        stack_facts: Dict[str, Any],
    ) -> None:
        if not self._inventory_has_fail2ban(runtime_inventory):
            return
        service_id = "security:fail2ban"
        if service_id in services:
            return
        self._add_dynamic_service(
            service_id=service_id,
            category="security",
            role="security",
            stack_group="security",
            transport_kind=transport_kind,
            action_id="diag_fail2ban_status",
            check_command=self._fail2ban_status_command(),
            inspect_command=self._fail2ban_inspect_command(),
            services=services,
            diagnostics_checks=diagnostics_checks,
            monitor_servers=monitor_servers,
            target_actions=target_actions,
            server_roles=server_roles,
            stack_facts=stack_facts,
        )
        incident_rules["fail2ban_security_activity"] = {
            "id": "rule:fail2ban_security_activity",
            "service": service_id,
            "check_id": f"check:{service_id}",
            "trigger_metric_prefix": "fail2ban_jail_",
            "runbook_id": "inspect_security_fail2ban",
            "thresholds": [
                self._threshold_suffix("_currently_failed", "gte", 50, "security.bruteforce_suspected"),
                self._threshold_suffix("_currently_banned", "gte", 20, "security.fail2ban_ban_surge"),
            ],
            "recommended_action": "notify_admin",
            "fallback_action": "notify_admin",
            "requires_manual_approval": False,
        }

    def _merge_dynamic_container_monitors(
        self,
        *,
        runtime_inventory: Mapping[str, Any],
        transport_kind: str,
        services: Dict[str, Any],
        diagnostics_checks: Dict[str, Any],
        incident_rules: Dict[str, Any],
        monitor_servers: list[Dict[str, Any]],
        remediation_actions: Dict[str, Any],
        target_actions: Dict[str, Any],
        server_roles: list[str],
        stack_facts: Dict[str, Any],
    ) -> None:
        for container in list(runtime_inventory.get("containers", []) or [])[:_MAX_DYNAMIC_CONTAINERS]:
            if not isinstance(container, Mapping):
                continue
            container_name = str(container.get("name") or "").strip()
            if not container_name:
                continue
            fragment = _safe_id_fragment(container_name, fallback="container")
            metric_fragment = _safe_metric_fragment(container_name, fallback="container")
            service_id = f"docker_container:{fragment}"
            if service_id in services:
                continue
            action_id = f"diag_docker_container_{fragment}_status"
            check_command = self._docker_container_status_command(
                container_name=container_name,
                metric_key=f"docker_container_{metric_fragment}_state",
            )
            self._add_dynamic_service(
                service_id=service_id,
                category="docker_container",
                role="container",
                stack_group="containers",
                transport_kind=transport_kind,
                action_id=action_id,
                check_command=check_command,
                inspect_command=self._docker_inspect_command(container_name=container_name),
                services=services,
                diagnostics_checks=diagnostics_checks,
                monitor_servers=monitor_servers,
                target_actions=target_actions,
                server_roles=server_roles,
                stack_facts=stack_facts,
            )

            remediation_id = f"restart_docker_container_{fragment}"
            remediation_actions[remediation_id] = {
                "service": service_id,
                "target": transport_kind,
                "action_id": remediation_id,
                "risk_level": "high",
                "description": f"Restart Docker container {container_name}",
            }
            services[service_id]["remediation_action_id"] = remediation_id
            target_actions[remediation_id] = {
                "argv": list(_shell_argv(f"docker restart {shlex.quote(container_name)}")),
                "timeout_sec": 45,
                "risk_level": "high",
            }
            incident_rules[f"docker_container_{fragment}_down"] = {
                "id": f"rule:docker_container_{fragment}_down",
                "service": service_id,
                "check_id": f"check:{service_id}",
                "runbook_id": f"inspect_docker_container_{_safe_metric_fragment(fragment, fallback='container')}",
                "thresholds": [
                    self._threshold(
                        f"docker_container_{metric_fragment}_state",
                        "in",
                        ["down"],
                        "availability.container_down",
                    ),
                    self._threshold(
                        f"docker_container_{metric_fragment}_health",
                        "in",
                        ["unhealthy"],
                        "availability.container_unhealthy",
                    ),
                    self._threshold(
                        f"docker_container_{metric_fragment}_restart_count",
                        "gte",
                        3,
                        "runtime.container_restart_loop",
                    ),
                    self._threshold(
                        f"docker_container_{metric_fragment}_oom_killed",
                        "in",
                        [True, "true"],
                        "runtime.container_oom_killed",
                        urgency="critical",
                    ),
                ],
                "recommended_action": remediation_id,
                "fallback_action": "notify_admin",
                "requires_manual_approval": True,
            }

    @staticmethod
    def _threshold(
        metric: str,
        op: str,
        value: Any,
        incident_type: str,
        *,
        urgency: str = "warning",
        confidence: str = "low",
    ) -> Dict[str, Any]:
        return {
            "metric": str(metric),
            "op": str(op),
            "value": value,
            "incident_type": str(incident_type),
            "urgency": str(urgency),
            "confidence": str(confidence),
        }

    @staticmethod
    def _threshold_suffix(
        metric_suffix: str,
        op: str,
        value: Any,
        incident_type: str,
        *,
        urgency: str = "warning",
        confidence: str = "low",
    ) -> Dict[str, Any]:
        payload = AdminEnvironmentScanner._threshold(
            "",
            op,
            value,
            incident_type,
            urgency=urgency,
            confidence=confidence,
        )
        payload.pop("metric", None)
        payload["metric_suffix"] = str(metric_suffix)
        return payload

    @staticmethod
    def _add_threshold_rule(
        *,
        incident_rules: Dict[str, Any],
        rule_id: str,
        service_id: str,
        check_id: str,
        runbook_id: str,
        fallback_action: str,
        thresholds: list[Dict[str, Any]],
        recommended_action: str = "",
    ) -> None:
        incident_rules[str(rule_id)] = {
            "id": f"rule:{rule_id}",
            "service": str(service_id),
            "check_id": str(check_id),
            "runbook_id": str(runbook_id),
            "thresholds": list(thresholds),
            "recommended_action": str(recommended_action or fallback_action),
            "fallback_action": str(fallback_action),
            "requires_manual_approval": bool(recommended_action and recommended_action != fallback_action),
        }

    @staticmethod
    def _add_dynamic_service(
        *,
        service_id: str,
        category: str,
        role: str,
        stack_group: str,
        transport_kind: str,
        action_id: str,
        check_command: str,
        inspect_command: str = "",
        services: Dict[str, Any],
        diagnostics_checks: Dict[str, Any],
        monitor_servers: list[Dict[str, Any]],
        target_actions: Dict[str, Any],
        server_roles: list[str],
        stack_facts: Dict[str, Any],
    ) -> None:
        inspect_action_id = ""
        if str(inspect_command or "").strip():
            inspect_action_id = f"inspect_{_safe_metric_fragment(service_id, fallback='service')}"
        services[service_id] = {
            "category": category,
            "role": role,
            "stack_group": stack_group,
            "check_action_id": action_id,
            "inspect_action_id": inspect_action_id,
            "remediation_action_id": None,
            "transport": transport_kind,
            "source": "runtime_inventory",
        }
        _append_unique(server_roles, role)
        bucket = stack_facts.get(category)
        if not isinstance(bucket, list):
            bucket = []
            stack_facts[category] = bucket
        _append_unique(bucket, service_id)
        detected_services = stack_facts.get("detected_services", [])
        if isinstance(detected_services, list):
            _append_unique(detected_services, service_id)

        diagnostics_checks[f"{service_id}_health"] = {
            "id": f"check:{service_id}",
            "service": service_id,
            "target": transport_kind,
            "action_id": action_id,
            "category": category,
            "source": "runtime_inventory",
        }
        monitor_servers.append(
            {
                "id": f"scan:{service_id}",
                "target": transport_kind,
                "action_id": action_id,
            }
        )
        target_actions[action_id] = {
            "argv": list(_shell_argv(check_command)),
            "timeout_sec": 20,
            "risk_level": "low",
        }
        if inspect_action_id:
            target_actions[inspect_action_id] = {
                "argv": list(_shell_argv(inspect_command)),
                "timeout_sec": 45,
                "risk_level": "low",
                "read_only": True,
            }

    @staticmethod
    def _inventory_has_fail2ban(runtime_inventory: Mapping[str, Any]) -> bool:
        systemd_services = {
            str(item or "").strip().lower().removesuffix(".service")
            for item in list(runtime_inventory.get("systemd_services", []) or [])
        }
        processes = {
            str(name or "").strip().lower()
            for name in dict(runtime_inventory.get("processes", {}) or {}).keys()
        }
        return "fail2ban" in systemd_services or "fail2ban-server" in processes

    @staticmethod
    def _host_runtime_command() -> str:
        return (
            "load1=$(awk '{print $1}' /proc/loadavg 2>/dev/null || echo 0); "
            "load5=$(awk '{print $2}' /proc/loadavg 2>/dev/null || echo 0); "
            "cpu_count=$(getconf _NPROCESSORS_ONLN 2>/dev/null || nproc 2>/dev/null || echo 1); "
            "mem_total=$(awk '/MemTotal:/ {print int($2)}' /proc/meminfo 2>/dev/null || echo 0); "
            "mem_avail=$(awk '/MemAvailable:/ {print int($2)}' /proc/meminfo 2>/dev/null || echo 0); "
            "mem_used_pct=$(awk -v t=${mem_total:-0} -v a=${mem_avail:-0} "
            "'BEGIN {if (t > 0) printf \"%.2f\", ((t-a)*100/t); else print 0}'); "
            "disk_root_pct=$(df -P / 2>/dev/null | awk 'NR==2 {gsub(\"%\", \"\", $5); print $5+0}'); "
            "inode_root_pct=$(df -Pi / 2>/dev/null | awk 'NR==2 {gsub(\"%\", \"\", $5); print $5+0}'); "
            "if command -v ss >/dev/null 2>&1; then "
            "tcp_established=$(ss -Htan state established 2>/dev/null | wc -l | tr -d ' '); "
            "tcp_syn_recv=$(ss -Htan state syn-recv 2>/dev/null | wc -l | tr -d ' '); "
            "tcp_listen=$(ss -Hltn 2>/dev/null | wc -l | tr -d ' '); "
            "else tcp_established=0; tcp_syn_recv=0; tcp_listen=0; fi; "
            "if command -v systemctl >/dev/null 2>&1; then "
            "systemd_failed_units=$(systemctl --failed --no-legend --no-pager 2>/dev/null "
            "| awk 'NF {c++} END {print c+0}'); "
            "else systemd_failed_units=0; fi; "
            "printf 'host_load_1m=%s\\n"
            "host_load_5m=%s\\n"
            "host_cpu_count=%s\\n"
            "host_memory_used_pct=%s\\n"
            "host_disk_root_pct=%s\\n"
            "host_inode_root_pct=%s\\n"
            "host_tcp_established=%s\\n"
            "host_tcp_syn_recv=%s\\n"
            "host_tcp_listen=%s\\n"
            "host_systemd_failed_units=%s\\n' "
            "\"${load1:-0}\" \"${load5:-0}\" \"${cpu_count:-1}\" \"${mem_used_pct:-0}\" "
            "\"${disk_root_pct:-0}\" \"${inode_root_pct:-0}\" \"${tcp_established:-0}\" "
            "\"${tcp_syn_recv:-0}\" \"${tcp_listen:-0}\" \"${systemd_failed_units:-0}\""
        )

    @staticmethod
    def _fail2ban_status_command() -> str:
        return (
            "if ! command -v fail2ban-client >/dev/null 2>&1; then "
            "printf 'fail2ban_state=missing\\nfail2ban_jail_count=0\\n'; exit 0; fi; "
            "status=$(fail2ban-client status 2>/dev/null || true); "
            "jails=$(printf '%s\\n' \"$status\" | sed -n 's/.*Jail list:[[:space:]]*//p' "
            "| tr ',' ' ' | xargs 2>/dev/null); "
            "jail_count=$(printf '%s\\n' \"$jails\" | awk '{print NF+0}'); "
            "state=$(test ${jail_count:-0} -gt 0 && echo running || echo unknown); "
            "printf 'fail2ban_state=%s\\nfail2ban_jail_count=%s\\n' \"$state\" \"${jail_count:-0}\"; "
            "for jail in $jails; do "
            "safe=$(printf '%s' \"$jail\" | tr -c 'A-Za-z0-9' '_' "
            "| sed 's/^_*//;s/_*$//' | tr '[:upper:]' '[:lower:]'); "
            "test -n \"$safe\" || safe=jail; "
            "jail_status=$(fail2ban-client status \"$jail\" 2>/dev/null || true); "
            "currently_failed=$(printf '%s\\n' \"$jail_status\" "
            "| sed -n 's/.*Currently failed:[[:space:]]*//p' | awk 'NR==1 {print int($1)}'); "
            "total_failed=$(printf '%s\\n' \"$jail_status\" "
            "| sed -n 's/.*Total failed:[[:space:]]*//p' | awk 'NR==1 {print int($1)}'); "
            "currently_banned=$(printf '%s\\n' \"$jail_status\" "
            "| sed -n 's/.*Currently banned:[[:space:]]*//p' | awk 'NR==1 {print int($1)}'); "
            "total_banned=$(printf '%s\\n' \"$jail_status\" "
            "| sed -n 's/.*Total banned:[[:space:]]*//p' | awk 'NR==1 {print int($1)}'); "
            "printf 'fail2ban_jail_%s_currently_failed=%s\\n"
            "fail2ban_jail_%s_total_failed=%s\\n"
            "fail2ban_jail_%s_currently_banned=%s\\n"
            "fail2ban_jail_%s_total_banned=%s\\n' "
            "\"$safe\" \"${currently_failed:-0}\" "
            "\"$safe\" \"${total_failed:-0}\" "
            "\"$safe\" \"${currently_banned:-0}\" "
            "\"$safe\" \"${total_banned:-0}\"; "
            "done"
        )

    @staticmethod
    def _host_inspect_command() -> str:
        return (
            "uptime || true; "
            "free -m 2>/dev/null || true; "
            "df -h 2>/dev/null || true; "
            "df -ih 2>/dev/null || true; "
            "ss -s 2>/dev/null || true; "
            "systemctl --failed --no-pager 2>/dev/null || true; "
            "journalctl -p warning -n 120 --no-pager 2>/dev/null || true"
        )

    @staticmethod
    def _systemd_inspect_command(*, service_name: str) -> str:
        quoted = shlex.quote(str(service_name or "").strip())
        return (
            f"systemctl status {quoted} --no-pager 2>/dev/null || true; "
            f"journalctl -u {quoted} -n 120 --no-pager 2>/dev/null || true"
        )

    @staticmethod
    def _process_inspect_command(*, process_name: str) -> str:
        quoted = shlex.quote(str(process_name or "").strip())
        grep_pattern = shlex.quote(str(process_name or "").strip()[:80])
        return (
            f"pgrep -a -x {quoted} 2>/dev/null || true; "
            f"ps -C {quoted} -o pid,ppid,pcpu,pmem,rss,etimes,args 2>/dev/null || true; "
            f"journalctl -n 120 --no-pager 2>/dev/null | grep -i -- {grep_pattern} | tail -n 80 || true"
        )

    @staticmethod
    def _docker_inspect_command(*, container_name: str) -> str:
        quoted = shlex.quote(str(container_name or "").strip())
        return (
            f"docker inspect {quoted} 2>/dev/null || true; "
            f"docker logs --tail 120 {quoted} 2>&1 || true"
        )

    @staticmethod
    def _fail2ban_inspect_command() -> str:
        return (
            "if command -v fail2ban-client >/dev/null 2>&1; then "
            "fail2ban-client status 2>/dev/null || true; "
            "jails=$(fail2ban-client status 2>/dev/null | sed -n 's/.*Jail list:[[:space:]]*//p' "
            "| tr ',' ' ' | xargs 2>/dev/null); "
            "for jail in $jails; do fail2ban-client status \"$jail\" 2>/dev/null || true; done; "
            "fi; "
            "journalctl -u fail2ban -n 120 --no-pager 2>/dev/null || true"
        )

    @staticmethod
    def _systemd_status_command(*, service_name: str, metric_key: str) -> str:
        quoted = shlex.quote(str(service_name or "").strip())
        metric_base = metric_key.removesuffix("_state")
        return (
            f"unit={quoted}; "
            "raw_state=$(systemctl is-active \"$unit\" 2>/dev/null || echo down); "
            "state=$(test \"$raw_state\" = active && echo running || echo down); "
            "active_state=$(systemctl show \"$unit\" -p ActiveState --value 2>/dev/null || echo unknown); "
            "sub_state=$(systemctl show \"$unit\" -p SubState --value 2>/dev/null || echo unknown); "
            "result=$(systemctl show \"$unit\" -p Result --value 2>/dev/null || echo unknown); "
            "restart_count=$(systemctl show \"$unit\" -p NRestarts --value 2>/dev/null || echo 0); "
            "exit_status=$(systemctl show \"$unit\" -p ExecMainStatus --value 2>/dev/null || echo 0); "
            f"printf '{metric_key}=%s\\n"
            f"{metric_base}_active_state=%s\\n"
            f"{metric_base}_sub_state=%s\\n"
            f"{metric_base}_result=%s\\n"
            f"{metric_base}_restart_count=%s\\n"
            f"{metric_base}_exit_status=%s\\n' "
            "\"$state\" \"${active_state:-unknown}\" \"${sub_state:-unknown}\" "
            "\"${result:-unknown}\" \"${restart_count:-0}\" \"${exit_status:-0}\""
        )

    @staticmethod
    def _process_status_command(*, process_name: str, metric_key: str, count_key: str) -> str:
        pattern = shlex.quote(re.escape(str(process_name or "").strip()))
        metric_base = metric_key.removesuffix("_state")
        return (
            f"pids=$(pgrep -x {pattern} 2>/dev/null | paste -sd, -); "
            "count=$(printf '%s' \"$pids\" | awk -F, '{print ($1 == \"\" ? 0 : NF)}'); "
            "state=$(test ${count:-0} -gt 0 && echo running || echo down); "
            f"printf '{metric_key}=%s\\n{count_key}=%s\\n' \"$state\" \"${{count:-0}}\"; "
            "if test ${count:-0} -gt 0; then "
            "first_pid=${pids%%,*}; "
            "cpu_pct=$(ps -p \"$pids\" -o pcpu= 2>/dev/null | awk '{s+=$1} END {printf \"%.2f\", s+0}'); "
            "cpu_count=$(getconf _NPROCESSORS_ONLN 2>/dev/null || nproc 2>/dev/null || echo 1); "
            "cpu_capacity_pct=$(awk -v cpu=\"${cpu_pct:-0}\" -v cores=\"${cpu_count:-1}\" "
            "'BEGIN {if (cores <= 0) cores = 1; printf \"%.2f\", cpu / cores}'); "
            "rss_kb=$(ps -p \"$pids\" -o rss= 2>/dev/null | awk '{s+=$1} END {printf \"%d\", s+0}'); "
            "oldest_seconds=$(ps -p \"$pids\" -o etimes= 2>/dev/null | awk 'NR==1 {print int($1)}'); "
            "cmd=$(ps -p \"$first_pid\" -o args= 2>/dev/null | tr '\\n' ' ' | sed 's/[[:space:]]\\+/ /g' | cut -c1-160); "
            f"printf '{metric_base}_pids=%s\\n"
            f"{metric_base}_cpu_pct=%s\\n"
            f"{metric_base}_cpu_capacity_pct=%s\\n"
            f"{metric_base}_rss_kb=%s\\n"
            f"{metric_base}_oldest_seconds=%s\\n"
            f"{metric_base}_sample_cmd=%s\\n' "
            "\"$pids\" \"${cpu_pct:-0}\" \"${cpu_capacity_pct:-0}\" "
            "\"${rss_kb:-0}\" \"${oldest_seconds:-0}\" \"${cmd:-}\"; "
            "fi"
        )

    @staticmethod
    def _docker_container_status_command(*, container_name: str, metric_key: str) -> str:
        quoted = shlex.quote(str(container_name or "").strip())
        metric_base = metric_key.removesuffix("_state")
        return (
            f"container={quoted}; "
            "raw_state=$(docker inspect -f '{{.State.Status}}' \"$container\" 2>/dev/null || echo missing); "
            "state=$(test \"$raw_state\" = running && echo running || echo down); "
            "health=$(docker inspect -f "
            "'{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "
            "\"$container\" 2>/dev/null || echo unknown); "
            "restart_count=$(docker inspect -f '{{.RestartCount}}' \"$container\" 2>/dev/null || echo 0); "
            "exit_code=$(docker inspect -f '{{.State.ExitCode}}' \"$container\" 2>/dev/null || echo 0); "
            "oom_killed=$(docker inspect -f '{{.State.OOMKilled}}' \"$container\" 2>/dev/null || echo false); "
            f"printf '{metric_key}=%s\\n"
            f"{metric_base}_raw_state=%s\\n"
            f"{metric_base}_health=%s\\n"
            f"{metric_base}_restart_count=%s\\n"
            f"{metric_base}_exit_code=%s\\n"
            f"{metric_base}_oom_killed=%s\\n' "
            "\"$state\" \"${raw_state:-missing}\" \"${health:-unknown}\" "
            "\"${restart_count:-0}\" \"${exit_code:-0}\" \"${oom_killed:-false}\""
        )

    @staticmethod
    def _normalize_pinned_cli(pinned_cli: Mapping[str, Any] | str | None) -> Dict[str, Any]:
        if isinstance(pinned_cli, Mapping):
            normalized = dict(pinned_cli)
            name = str(normalized.get("name") or "").strip()
            if name:
                normalized["name"] = name
            elif "name" in normalized:
                normalized.pop("name", None)
            return normalized
        name = str(pinned_cli or "").strip()
        return {"name": name} if name else {}

    def _resolve_transport_kind(self) -> str:
        raw_target = str(self.pinned_cli.get("target") or self.pinned_cli.get("transport") or "").strip().lower()
        if raw_target == "ssh":
            return "ssh"
        if self.pinned_cli.get("host") and (
            self.pinned_cli.get("key_path")
            or self.pinned_cli.get("password")
            or self.pinned_cli.get("password_env")
        ):
            return "ssh"
        return "local"

    async def _collect_runtime_inventory(self, *, transport_kind: str) -> Dict[str, Any]:
        try:
            if transport_kind == "ssh":
                result = await self._ssh_transport.run(
                    self._build_ssh_command_spec(
                        action_id=_RUNTIME_INVENTORY_ACTION_ID,
                        command=_RUNTIME_INVENTORY_COMMAND,
                    )
                )
            else:
                result = await self._local_transport.run(
                    self._build_local_command_spec(
                        action_id=_RUNTIME_INVENTORY_ACTION_ID,
                        command=_RUNTIME_INVENTORY_COMMAND,
                    )
                )
        except Exception:
            _LOG.exception("admin scanner: failed to collect runtime inventory transport=%s", transport_kind)
            return {}
        if int(getattr(result, "returncode", -1)) != 0 or bool(getattr(result, "timed_out", False)):
            return {}
        return self._parse_runtime_inventory(str(getattr(result, "stdout", "") or ""))

    @staticmethod
    def _parse_runtime_inventory(raw: str) -> Dict[str, Any]:
        section = ""
        systemd_services: list[str] = []
        processes: Dict[str, int] = {}
        containers: list[Dict[str, str]] = []

        for raw_line in str(raw or "").splitlines():
            line = raw_line.strip()
            if not line:
                continue
            if line in {"__SYSTEMD__", "__PROCESSES__", "__CONTAINERS__"}:
                section = line
                continue
            if section == "__SYSTEMD__":
                _append_unique(systemd_services, line.removesuffix(".service"))
                continue
            if section == "__PROCESSES__":
                name, _, count_raw = line.partition("\t")
                name = name.strip()
                if not _is_monitorable_process_name(name):
                    continue
                try:
                    count = int(str(count_raw or "1").strip() or "1")
                except ValueError:
                    count = 1
                processes[name] = max(1, count)
                continue
            if section == "__CONTAINERS__":
                name, image, status = (line.split("\t", 2) + ["", ""])[:3]
                name = name.strip()
                if not name:
                    continue
                containers.append(
                    {
                        "name": name,
                        "image": image.strip(),
                        "status": status.strip(),
                    }
                )

        systemd_set = set(systemd_services)
        process_set = set(processes)
        container_runtime: list[str] = []
        if "docker" in systemd_set or "dockerd" in process_set or containers:
            _append_unique(container_runtime, "docker")
        if "containerd" in systemd_set or "containerd" in process_set:
            _append_unique(container_runtime, "containerd")

        return {
            "systemd_services": systemd_services,
            "processes": processes,
            "containers": containers,
            "container_runtime": container_runtime,
        }

    def _build_local_command_spec(self, *, action_id: str, command: str) -> LocalCommandSpec:
        return LocalCommandSpec(
            action_id=action_id,
            argv=_shell_argv(command),
            timeout_sec=self._timeout_sec,
        )

    def _build_ssh_command_spec(self, *, action_id: str, command: str) -> SSHCommandSpec:
        host = str(self.pinned_cli.get("host") or "").strip()
        key_path = str(self.pinned_cli.get("key_path") or "").strip()
        password = str(self.pinned_cli.get("password") or "")
        password_env = str(self.pinned_cli.get("password_env") or "").strip()
        if not password and password_env:
            from app.services.ssh_config_loader import load_ssh_secrets, resolve_ssh_secret

            password = str(resolve_ssh_secret(load_ssh_secrets(self._secrets_workdir), password_env) or "")
        if not host or (not key_path and not password):
            raise ValueError("ssh pinned_cli requires host and key_path/password_env")
        raw_port = self.pinned_cli.get("port", 22)
        port = int(raw_port or 22)
        if key_path:
            key_path = os.path.expanduser(key_path)
            if self._secrets_workdir and not key_path.startswith("/"):
                key_path = os.path.abspath(os.path.join(self._secrets_workdir, key_path))
        options_raw = self.pinned_cli.get("options", ())
        options: tuple[str, ...]
        if isinstance(options_raw, (list, tuple)):
            options = tuple(str(item).strip() for item in options_raw if str(item).strip())
        else:
            options = ()
        return SSHCommandSpec(
            action_id=action_id,
            host=host,
            user=str(self.pinned_cli.get("user") or "").strip() or None,
            port=port,
            key_path=key_path,
            argv=_shell_argv(command),
            timeout_sec=self._timeout_sec,
            options=options,
            password=password or None,
        )


__all__ = ["AdminEnvironmentScanner"]
