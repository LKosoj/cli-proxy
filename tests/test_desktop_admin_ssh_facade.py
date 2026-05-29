from __future__ import annotations

import logging
from pathlib import Path
from types import SimpleNamespace

import yaml

from desktop.services.application_facade import ApplicationFacade


def _make_facade_stub(tmp_path: Path) -> SimpleNamespace:
    session = SimpleNamespace(workdir=str(tmp_path))
    session_service = SimpleNamespace(get_session_by_uid=lambda _uid: session)
    return SimpleNamespace(
        session_service=session_service,
        logger=logging.getLogger("test_desktop_admin_ssh_facade"),
    )


def _write_ssh_yaml(tmp_path: Path, hosts: dict) -> None:
    cli_proxy = tmp_path / ".cli-proxy"
    cli_proxy.mkdir(exist_ok=True)
    (cli_proxy / "ssh.yaml").write_text(
        yaml.safe_dump({"hosts": hosts}),
        encoding="utf-8",
    )


def test_get_admin_hosts_includes_local_and_ssh_yaml_aliases(tmp_path) -> None:
    _write_ssh_yaml(
        tmp_path,
        {
            "prod": {"host": "10.0.0.1", "user": "deploy", "port": 22, "auth": "key"},
            "stage": {"host": "10.0.0.2", "user": "deploy", "auth": "key"},
        },
    )
    facade = _make_facade_stub(tmp_path)

    hosts = ApplicationFacade.get_admin_hosts(facade, "sess")

    aliases = [h["alias"] for h in hosts]
    assert "local" in aliases
    assert "prod" in aliases
    assert "stage" in aliases
    local_entry = next(h for h in hosts if h["alias"] == "local")
    assert local_entry["target"] == "local"
    prod_entry = next(h for h in hosts if h["alias"] == "prod")
    assert prod_entry["target"] == "ssh"
    assert prod_entry["host"] == "10.0.0.1"
    assert prod_entry["port"] == 22


def test_save_and_get_admin_monitor_servers_round_trip(tmp_path) -> None:
    facade = _make_facade_stub(tmp_path)

    result = ApplicationFacade.save_admin_monitor_servers(
        facade,
        "sess",
        servers=[
            {"id": "local", "target": "local", "action_id": "scan", "timeout_sec": 10},
            {"id": "prod", "target": "ssh", "action_id": "probe"},
        ],
        enabled=True,
        interval_sec=45,
    )
    assert result == {"ok": True}

    loaded = ApplicationFacade.get_admin_monitor_servers(facade, "sess")
    assert loaded["ok"] is True
    assert loaded["enabled"] is True
    assert loaded["interval_sec"] == 45.0
    by_id = {row["id"]: row for row in loaded["servers"]}
    assert by_id["local"]["target"] == "local"
    assert by_id["local"]["action_id"] == "scan"
    assert by_id["local"]["timeout_sec"] == 10.0
    assert by_id["prod"]["target"] == "ssh"
    assert by_id["prod"]["timeout_sec"] is None


def test_save_admin_monitor_servers_rejects_invalid_input(tmp_path) -> None:
    facade = _make_facade_stub(tmp_path)

    bad_target = ApplicationFacade.save_admin_monitor_servers(
        facade,
        "sess",
        servers=[{"id": "x", "target": "wrong", "action_id": "a"}],
    )
    assert bad_target["ok"] is False
    assert "target" in bad_target["error"]

    bad_action = ApplicationFacade.save_admin_monitor_servers(
        facade,
        "sess",
        servers=[{"id": "local", "target": "local", "action_id": ""}],
    )
    assert bad_action["ok"] is False
    assert "action_id" in bad_action["error"]


def test_save_and_get_admin_actions_ssh_round_trip(tmp_path) -> None:
    facade = _make_facade_stub(tmp_path)

    result = ApplicationFacade.save_admin_actions_ssh(
        facade,
        "sess",
        actions=[
            {
                "action_id": "probe_uptime",
                "argv": ["bash", "-lc", "uptime"],
                "timeout_sec": 15,
                "risk_level": "low",
                "description": "uptime",
            },
            {
                "action_id": "restart_nginx",
                "argv": ["sudo", "systemctl", "restart", "nginx"],
                "timeout_sec": 30,
                "risk_level": "high",
            },
        ],
    )
    assert result == {"ok": True}

    loaded = ApplicationFacade.get_admin_actions_ssh(facade, "sess")
    assert loaded["ok"] is True
    by_id = {item["action_id"]: item for item in loaded["actions"]}
    assert by_id["probe_uptime"]["argv"] == ["bash", "-lc", "uptime"]
    assert by_id["probe_uptime"]["risk_level"] == "low"
    assert by_id["probe_uptime"]["description"] == "uptime"
    assert by_id["restart_nginx"]["risk_level"] == "high"
    assert by_id["restart_nginx"]["description"] == ""


def test_save_and_get_admin_actions_ssh_read_only_round_trip(tmp_path) -> None:
    facade = _make_facade_stub(tmp_path)

    result = ApplicationFacade.save_admin_actions_ssh(
        facade,
        "sess",
        actions=[
            {
                "action_id": "probe_uptime",
                "argv": ["uptime"],
                "timeout_sec": 10,
                "risk_level": "low",
                "read_only": True,
                "description": "readonly",
            },
            {
                "action_id": "restart_nginx",
                "argv": ["sudo", "systemctl", "restart", "nginx"],
                "timeout_sec": 30,
                "risk_level": "high",
            },
        ],
    )
    assert result == {"ok": True}

    loaded = ApplicationFacade.get_admin_actions_ssh(facade, "sess")
    assert loaded["ok"] is True
    by_id = {item["action_id"]: item for item in loaded["actions"]}
    assert by_id["probe_uptime"]["read_only"] is True
    assert by_id["restart_nginx"]["read_only"] is False


def test_save_admin_actions_ssh_rejects_read_only_with_non_low_risk(tmp_path) -> None:
    facade = _make_facade_stub(tmp_path)

    bad = ApplicationFacade.save_admin_actions_ssh(
        facade,
        "sess",
        actions=[
            {
                "action_id": "probe",
                "argv": ["echo"],
                "timeout_sec": 5,
                "risk_level": "medium",
                "read_only": True,
            },
        ],
    )
    assert bad["ok"] is False
    assert "read_only" in bad["error"]


def test_save_admin_actions_ssh_rejects_invalid_input(tmp_path) -> None:
    facade = _make_facade_stub(tmp_path)

    bad_id = ApplicationFacade.save_admin_actions_ssh(
        facade,
        "sess",
        actions=[{"action_id": "", "argv": ["x"], "timeout_sec": 5, "risk_level": "low"}],
    )
    assert bad_id["ok"] is False

    bad_argv = ApplicationFacade.save_admin_actions_ssh(
        facade,
        "sess",
        actions=[{"action_id": "ok", "argv": [], "timeout_sec": 5, "risk_level": "low"}],
    )
    assert bad_argv["ok"] is False

    bad_risk = ApplicationFacade.save_admin_actions_ssh(
        facade,
        "sess",
        actions=[{"action_id": "ok", "argv": ["x"], "timeout_sec": 5, "risk_level": "critical"}],
    )
    assert bad_risk["ok"] is False
