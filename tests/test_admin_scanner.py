from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

REPO_ROOT = Path(__file__).resolve().parents[1]
_SCANNER_PATH = REPO_ROOT / "modes" / "admin" / "scanner.py"
_SPEC = importlib.util.spec_from_file_location("modes_admin_scanner_test", _SCANNER_PATH)
if _SPEC is None or _SPEC.loader is None:
    raise RuntimeError(f"failed to load admin scanner module from {_SCANNER_PATH}")
_MODULE = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _MODULE
_SPEC.loader.exec_module(_MODULE)
AdminEnvironmentScanner = _MODULE.AdminEnvironmentScanner


class _FakeLocalTransport:
    def __init__(self, present_action_ids: set[str], stdout_by_action: dict[str, str] | None = None) -> None:
        self.present_action_ids = set(present_action_ids)
        self.stdout_by_action = dict(stdout_by_action or {})
        self.calls: list[str] = []

    async def run(self, spec):  # type: ignore[no-untyped-def]
        action_id = str(spec.action_id)
        self.calls.append(action_id)
        return SimpleNamespace(
            returncode=0 if action_id in self.present_action_ids or action_id in self.stdout_by_action else 1,
            timed_out=False,
            stdout=self.stdout_by_action.get(action_id, ""),
            stderr="",
        )


class _FakeSSHTransport:
    def __init__(self, present_action_ids: set[str], stdout_by_action: dict[str, str] | None = None) -> None:
        self.present_action_ids = set(present_action_ids)
        self.stdout_by_action = dict(stdout_by_action or {})
        self.calls: list[str] = []

    async def run(self, spec):  # type: ignore[no-untyped-def]
        action_id = str(spec.action_id)
        self.calls.append(action_id)
        return SimpleNamespace(
            returncode=0 if action_id in self.present_action_ids or action_id in self.stdout_by_action else 1,
            timed_out=False,
            stdout=self.stdout_by_action.get(action_id, ""),
            stderr="",
        )


def test_admin_environment_scanner_generates_services_from_local_inventory() -> None:
    inventory_stdout = "\n".join(
        [
            "__SYSTEMD__",
            "nginx",
            "mysql",
            "redis",
            "__PROCESSES__",
            "awk\t1",
            "bash\t1",
            "gunicorn\t1",
            "__CONTAINERS__",
        ]
    )
    local_transport = _FakeLocalTransport(
        set(),
        stdout_by_action={"scan_runtime_inventory": inventory_stdout},
    )
    scanner = AdminEnvironmentScanner(
        {"name": "codex", "source": "runtime"},
        local_transport=local_transport,
    )

    result = scanner.scan()

    services = result["services"]
    assert set(services) == {
        "host:runtime",
        "systemd:nginx",
        "systemd:mysql",
        "systemd:redis",
        "process:gunicorn",
    }
    assert result["environment"]["transport"] == "local"
    assert result["environment"]["stack_facts"]["systemd_services"] == ["nginx", "mysql", "redis"]
    assert result["environment"]["stack_facts"]["processes"] == {"gunicorn": 1}
    assert "restart_systemd_mysql" in result["actions"]["remediation"]
    assert "diag_process_gunicorn_status" in result["actions"]["targets"]["local"]
    assert local_transport.calls


def test_admin_environment_scanner_uses_ssh_transport_for_inventory_scan() -> None:
    inventory_stdout = "\n".join(
        [
            "__SYSTEMD__",
            "mysql",
            "__PROCESSES__",
            "pm2\t1",
            "__CONTAINERS__",
            "app\timage:latest\tUp 1 hour",
        ]
    )
    local_transport = _FakeLocalTransport(set())
    ssh_transport = _FakeSSHTransport(
        set(),
        stdout_by_action={"scan_runtime_inventory": inventory_stdout},
    )
    scanner = AdminEnvironmentScanner(
        {
            "name": "codex",
            "target": "ssh",
            "host": "127.0.0.1",
            "user": "root",
            "key_path": "/tmp/id_rsa",
        },
        local_transport=local_transport,
        ssh_transport=ssh_transport,
    )

    result = scanner.scan()

    assert local_transport.calls == []
    assert ssh_transport.calls
    assert result["environment"]["transport"] == "ssh"
    assert set(result["services"]) == {"host:runtime", "systemd:mysql", "process:pm2", "docker_container:app"}
    assert result["actions"]["targets"]["ssh"]["diag_systemd_mysql_status"]["risk_level"] == "low"
    assert "restart_systemd_mysql" in result["actions"]["targets"]["ssh"]
    assert "restart_docker_container_app" in result["actions"]["targets"]["ssh"]
    assert {item["id"] for item in result["generated"]["monitor"]["servers"]} == {
        "scan:host:runtime",
        "scan:systemd:mysql",
        "scan:process:pm2",
        "scan:docker_container:app",
    }
    assert "docker" in result["environment"]["stack_facts"]["container_runtime"]


def test_admin_environment_scanner_collects_runtime_inventory_without_specific_rules() -> None:
    inventory_stdout = "\n".join(
        [
            "__SYSTEMD__",
            "containerd",
            "docker",
            "ssh",
            "__PROCESSES__",
            "containerd\t1",
            "dockerd\t1",
            "awk\t1",
            "php-fpm\t3",
            "__CONTAINERS__",
            "nextcloud\tanimaccord-nextcloud-fpm:33.0.2-gs1\tUp 31 hours",
        ]
    )
    local_transport = _FakeLocalTransport(
        set(),
        stdout_by_action={"scan_runtime_inventory": inventory_stdout},
    )
    scanner = AdminEnvironmentScanner(
        {"name": "codex", "source": "runtime"},
        local_transport=local_transport,
    )

    result = scanner.scan()
    facts = result["environment"]["stack_facts"]

    assert local_transport.calls[0] == "scan_runtime_inventory"
    assert "docker" in facts["systemd_services"]
    assert facts["processes"]["dockerd"] == 1
    assert facts["processes"]["php-fpm"] == 3
    assert "awk" not in facts["processes"]
    assert facts["container_runtime"] == ["docker", "containerd"]
    assert facts["containers"] == [
        {
            "name": "nextcloud",
            "image": "animaccord-nextcloud-fpm:33.0.2-gs1",
            "status": "Up 31 hours",
        }
    ]


def test_admin_environment_scanner_generates_actions_from_runtime_inventory() -> None:
    inventory_stdout = "\n".join(
        [
            "__SYSTEMD__",
            "custom-worker",
            "ssh",
            "__PROCESSES__",
            "custom-bin\t2",
            "__CONTAINERS__",
            "nextcloud\tanimaccord-nextcloud-fpm:33.0.2-gs1\tUp 31 hours",
        ]
    )
    local_transport = _FakeLocalTransport(
        set(),
        stdout_by_action={"scan_runtime_inventory": inventory_stdout},
    )
    scanner = AdminEnvironmentScanner(
        {"name": "codex", "source": "runtime"},
        local_transport=local_transport,
    )

    result = scanner.scan()
    services = result["services"]
    target_actions = result["actions"]["targets"]["local"]
    remediation = result["actions"]["remediation"]
    incident_rules = result["incidents"]["rules"]
    monitor_ids = {item["id"] for item in result["generated"]["monitor"]["servers"]}
    runbook_templates = result["generated"]["runbooks"]["templates"]

    assert "systemd:custom-worker" in services
    assert "host:runtime" in services
    assert "process:custom-bin" in services
    assert "docker_container:nextcloud" in services
    assert "diag_host_runtime" in target_actions
    assert "inspect_host_runtime" in target_actions
    host_command = " ".join(target_actions["diag_host_runtime"]["argv"])
    assert "host_load_1m" in host_command
    assert "host_tcp_syn_recv" in host_command
    assert "host_systemd_failed_units" in host_command
    assert "diag_systemd_custom-worker_status" in target_actions
    assert "inspect_systemd_custom_worker" in target_actions
    systemd_command = " ".join(target_actions["diag_systemd_custom-worker_status"]["argv"])
    assert "systemd_custom_worker_active_state" in systemd_command
    assert "systemd_custom_worker_sub_state" in systemd_command
    assert "systemd_custom_worker_restart_count" in systemd_command
    assert "restart_systemd_custom-worker" in target_actions
    assert "restart_systemd_custom-worker" in remediation
    assert "diag_process_custom-bin_status" in target_actions
    assert "inspect_process_custom_bin" in target_actions
    process_command = " ".join(target_actions["diag_process_custom-bin_status"]["argv"])
    assert "process_custom_bin_cpu_pct" in process_command
    assert "process_custom_bin_cpu_capacity_pct" in process_command
    assert "process_custom_bin_rss_kb" in process_command
    assert "process_custom_bin_sample_cmd" in process_command
    assert "restart_process_custom-bin" not in target_actions
    assert "diag_docker_container_nextcloud_status" in target_actions
    assert "inspect_docker_container_nextcloud" in target_actions
    docker_command = " ".join(target_actions["diag_docker_container_nextcloud_status"]["argv"])
    assert "docker_container_nextcloud_health" in docker_command
    assert "docker_container_nextcloud_restart_count" in docker_command
    assert "docker_container_nextcloud_exit_code" in docker_command
    assert "restart_docker_container_nextcloud" in target_actions
    assert "restart_docker_container_nextcloud" in remediation
    assert runbook_templates["inspect_host_runtime"]["steps"][0]["action_id"] == "inspect_host_runtime"
    assert (
        runbook_templates["inspect_systemd_custom_worker"]["steps"][0]["action_id"]
        == "inspect_systemd_custom_worker"
    )
    assert runbook_templates["inspect_docker_container_nextcloud"]["steps"][1]["requires_approval"] is True
    assert incident_rules["host_runtime_thresholds"]["thresholds"]
    assert {
        threshold["incident_type"]
        for threshold in incident_rules["systemd_custom-worker_down"]["thresholds"]
    } >= {"availability.service_down", "runtime.service_restart_loop", "runtime.service_exit_error"}
    assert {
        threshold["incident_type"]
        for threshold in incident_rules["process_custom-bin_thresholds"]["thresholds"]
    } >= {"availability.process_down", "saturation.process_cpu_high"}
    assert any(
        threshold["metric"] == "process_custom_bin_cpu_capacity_pct"
        and threshold["incident_type"] == "saturation.process_cpu_high"
        for threshold in incident_rules["process_custom-bin_thresholds"]["thresholds"]
    )
    assert {
        threshold["incident_type"]
        for threshold in incident_rules["docker_container_nextcloud_down"]["thresholds"]
    } >= {"availability.container_down", "availability.container_unhealthy", "runtime.container_restart_loop"}
    assert "restart_systemd_ssh" not in target_actions
    assert {
        "scan:host:runtime",
        "scan:systemd:custom-worker",
        "scan:process:custom-bin",
        "scan:docker_container:nextcloud",
    }.issubset(monitor_ids)


def test_admin_environment_scanner_generates_fail2ban_security_probe() -> None:
    inventory_stdout = "\n".join(
        [
            "__SYSTEMD__",
            "fail2ban",
            "__PROCESSES__",
            "fail2ban-server\t1",
            "__CONTAINERS__",
        ]
    )
    local_transport = _FakeLocalTransport(
        set(),
        stdout_by_action={"scan_runtime_inventory": inventory_stdout},
    )
    scanner = AdminEnvironmentScanner(
        {"name": "codex", "source": "runtime"},
        local_transport=local_transport,
    )

    result = scanner.scan()
    services = result["services"]
    target_actions = result["actions"]["targets"]["local"]
    monitor_ids = {item["id"] for item in result["generated"]["monitor"]["servers"]}
    runbook_templates = result["generated"]["runbooks"]["templates"]

    assert "security:fail2ban" in services
    assert "diag_fail2ban_status" in target_actions
    assert "inspect_security_fail2ban" in target_actions
    command = " ".join(target_actions["diag_fail2ban_status"]["argv"])
    assert "fail2ban_jail_count" in command
    assert "currently_failed" in command
    assert "currently_banned" in command
    assert "fail2ban_security_activity" in result["incidents"]["rules"]
    assert runbook_templates["inspect_security_fail2ban"]["steps"][0]["action_id"] == "inspect_security_fail2ban"
    assert "scan:security:fail2ban" in monitor_ids


def test_admin_environment_scanner_has_no_static_service_rules() -> None:
    assert not hasattr(_MODULE, "_SERVICE_SCAN_RULES")
    assert "systemctl list-units" in _MODULE._RUNTIME_INVENTORY_COMMAND
    assert "ps -eo comm=" in _MODULE._RUNTIME_INVENTORY_COMMAND
    assert "docker ps --format" in _MODULE._RUNTIME_INVENTORY_COMMAND
