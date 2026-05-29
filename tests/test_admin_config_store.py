from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from app.services.admin_config_service import (
    AdminConfigRevisionConflictError,
    AdminConfigService,
    AdminConfigServiceError,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
_CONFIG_STORE_PATH = REPO_ROOT / "modes" / "admin" / "config_store.py"
_SPEC = importlib.util.spec_from_file_location("modes_admin_config_store_test", _CONFIG_STORE_PATH)
if _SPEC is None or _SPEC.loader is None:
    raise RuntimeError(f"failed to load admin config_store module from {_CONFIG_STORE_PATH}")
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)
AdminConfigStore = _MODULE.AdminConfigStore
AdminConfigStoreError = _MODULE.AdminConfigStoreError


def _admin_config_service_for_workdir(session_workdir: Path, *, session_uid: str = "chat:1:admin"):
    session = SimpleNamespace(id="admin", workdir=str(session_workdir))

    class _Manager:
        sessions_by_chat = {1: {"admin": session}}

        @staticmethod
        def get_by_uid(value: str):
            return session if value == session_uid else None

    return AdminConfigService(SimpleNamespace(manager=_Manager())), session_uid


def test_admin_config_store_uses_session_local_cli_proxy_path(tmp_path) -> None:
    session_workdir = tmp_path / "session"
    session_workdir.mkdir()

    store = AdminConfigStore(str(session_workdir))
    expected = session_workdir / ".cli-proxy" / ".admin" / "config.yaml"

    assert Path(store.config_path) == expected
    assert Path(store.config_path) != (tmp_path / "config.yaml")


def test_admin_config_store_bootstraps_from_template_when_missing(tmp_path) -> None:
    session_workdir = tmp_path / "session"
    session_workdir.mkdir()

    store = AdminConfigStore(str(session_workdir))
    config_path = Path(store.ensure_config())

    assert config_path.exists()
    assert config_path.read_text(encoding="utf-8") == Path(store.template_path).read_text(encoding="utf-8")


def test_admin_config_store_template_includes_runtime_and_generated_blocks(tmp_path) -> None:
    session_workdir = tmp_path / "session"
    session_workdir.mkdir()

    store = AdminConfigStore(str(session_workdir))
    store.ensure_config()

    loaded = store.load_config()
    assert loaded["admin"]["runtime"]["pinned_cli"] == {}
    assert loaded["admin"]["runtime"]["scan_status"] == "not_started"
    assert loaded["admin"]["runbooks"]["templates"] == {}
    assert loaded["admin"]["generated"]["environment"] == {}
    assert loaded["admin"]["generated"]["runbooks"]["templates"] == {}
    assert loaded["admin"]["generated"]["actions"]["targets"] == {"local": {}, "ssh": {}}


def test_admin_config_store_does_not_overwrite_existing_file(tmp_path) -> None:
    session_workdir = tmp_path / "session"
    session_workdir.mkdir()

    store = AdminConfigStore(str(session_workdir))
    config_path = Path(store.ensure_config())
    custom_payload = {"admin": {"monitor": {"enabled": False, "interval_sec": 5}, "intent": "custom"}}
    config_path.write_text(yaml.safe_dump(custom_payload, sort_keys=False), encoding="utf-8")

    store.ensure_config()
    loaded = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert loaded == custom_payload


def test_admin_config_store_load_config_reads_session_file(tmp_path) -> None:
    session_workdir = tmp_path / "session"
    session_workdir.mkdir()

    store = AdminConfigStore(str(session_workdir))
    config_path = Path(store.ensure_config())
    custom_payload = {"admin": {"monitor": {"enabled": True, "interval_sec": 11}, "intent": "status"}}
    config_path.write_text(yaml.safe_dump(custom_payload, sort_keys=False), encoding="utf-8")

    loaded = store.load_config()
    assert loaded == custom_payload


def test_admin_config_store_isolated_between_sequential_sessions(tmp_path) -> None:
    workdir_a = tmp_path / "wd_a"
    workdir_b = tmp_path / "wd_b"
    workdir_a.mkdir()
    workdir_b.mkdir()

    store_a = AdminConfigStore(str(workdir_a))
    store_b = AdminConfigStore(str(workdir_b))
    path_a = Path(store_a.ensure_config())
    path_b = Path(store_b.ensure_config())

    payload_a = {"admin": {"intent": "monitoring", "monitor": {"enabled": True}}}
    payload_b = {"admin": {"intent": "maintenance", "monitor": {"enabled": False}}}
    path_a.write_text(yaml.safe_dump(payload_a, sort_keys=False), encoding="utf-8")
    path_b.write_text(yaml.safe_dump(payload_b, sort_keys=False), encoding="utf-8")

    assert store_a.load_config() == payload_a
    assert store_b.load_config() == payload_b
    assert path_a != path_b


@pytest.mark.parametrize(
    ("admin_payload", "expected_fragment"),
    [
        ({"runtime": {"pinned_cli": "codex"}}, "admin.runtime.pinned_cli"),
        ({"generated": ["auto"]}, "admin.generated"),
    ],
)
def test_admin_config_store_rejects_invalid_runtime_schema(
    tmp_path,
    admin_payload: dict,
    expected_fragment: str,
) -> None:
    session_workdir = tmp_path / "session"
    session_workdir.mkdir()

    store = AdminConfigStore(str(session_workdir))
    config_path = Path(store.ensure_config())
    config_path.write_text(yaml.safe_dump({"admin": admin_payload}, sort_keys=False), encoding="utf-8")

    with pytest.raises(AdminConfigStoreError) as excinfo:
        store.load_config()

    assert expected_fragment in str(excinfo.value)


def test_admin_config_service_contract_get_and_save_yaml_without_miniapp_context(tmp_path) -> None:
    session_workdir = tmp_path / "session"
    session_workdir.mkdir()
    service, session_uid = _admin_config_service_for_workdir(session_workdir)

    initial = service.get_yaml(session_uid)
    parsed = yaml.safe_load(initial["yaml"])
    parsed["admin"]["monitor"]["interval_sec"] = 45

    saved = service.save_yaml(
        session_uid,
        yaml.safe_dump(parsed, sort_keys=False),
        expected_revision=initial["revision"],
    )
    reloaded = AdminConfigStore(str(session_workdir)).load_config()

    assert initial["config_path"].endswith(".cli-proxy/.admin/config.yaml")
    assert saved["revision"] != initial["revision"]
    assert reloaded["admin"]["monitor"]["interval_sec"] == 45


def test_admin_config_service_supports_desktop_session_service_lookup(tmp_path) -> None:
    session_workdir = tmp_path / "session"
    session_workdir.mkdir()
    session = SimpleNamespace(id="admin", workdir=str(session_workdir))
    service = AdminConfigService(
        SimpleNamespace(session_service=SimpleNamespace(get_session_by_uid=lambda _uid: session))
    )

    loaded = service.load_config("desktop-session", effective=False)

    assert loaded["admin"]["monitor"]["enabled"] is True


def test_admin_config_service_rejects_stale_yaml_revision(tmp_path) -> None:
    session_workdir = tmp_path / "session"
    session_workdir.mkdir()
    service, session_uid = _admin_config_service_for_workdir(session_workdir)
    initial = service.get_yaml(session_uid)

    with pytest.raises(AdminConfigRevisionConflictError) as excinfo:
        service.save_yaml(session_uid, initial["yaml"], expected_revision="stale")

    assert excinfo.value.status == 409
    assert str(excinfo.value) == "revision mismatch"


def test_admin_config_service_contract_monitor_servers_get_and_save(tmp_path) -> None:
    session_workdir = tmp_path / "session"
    session_workdir.mkdir()
    service, session_uid = _admin_config_service_for_workdir(session_workdir)

    saved = service.save_monitor_servers(
        session_uid,
        {
            "servers": [
                {
                    "id": "local-main",
                    "target": "local",
                    "action_id": "clear_logs",
                    "timeout_sec": "12.5",
                }
            ],
            "enabled": False,
            "interval_sec": "60",
        },
    )
    reloaded = service.get_monitor_servers(session_uid)

    assert saved == reloaded
    assert reloaded == {
        "servers": [
            {
                "id": "local-main",
                "target": "local",
                "action_id": "clear_logs",
                "timeout_sec": 12.5,
            }
        ],
        "interval_sec": 60.0,
        "enabled": False,
    }


def test_admin_config_service_validation_error_path(tmp_path) -> None:
    session_workdir = tmp_path / "session"
    session_workdir.mkdir()
    service, session_uid = _admin_config_service_for_workdir(session_workdir)

    with pytest.raises(AdminConfigServiceError) as yaml_exc:
        service.save_yaml(session_uid, "[]")
    with pytest.raises(AdminConfigServiceError) as monitor_exc:
        service.save_monitor_servers(
            session_uid,
            {"servers": [{"id": "bad", "target": "remote", "action_id": "clear_logs"}]},
        )

    assert yaml_exc.value.status == 400
    assert "admin config must be a YAML mapping" in str(yaml_exc.value)
    assert monitor_exc.value.status == 400
    assert str(monitor_exc.value) == "target must be local|ssh: bad"


def test_merge_generated_config_preserves_manual(tmp_path) -> None:
    session_workdir = tmp_path / "session"
    session_workdir.mkdir()

    store = AdminConfigStore(str(session_workdir))
    config_path = Path(store.ensure_config())
    config_path.write_text(
        yaml.safe_dump(
            {
                "admin": {
                    "monitor": {
                        "enabled": True,
                        "interval_sec": 11,
                        "servers": [{"id": "manual", "target": "local", "action_id": "clear_logs"}],
                    },
                    "notifications": {"level": "info"},
                    "runtime": {"pinned_cli": {"name": "codex"}},
                    "actions": {
                        "local": {
                            "clear_logs": {"argv": ["bash", "-lc", "echo manual"], "timeout_sec": 11},
                        },
                        "remediation": {
                            "clear_logs": {"target": "local", "risk_level": "medium"},
                        },
                    },
                    "generated": {
                        "actions": {"targets": {"local": {"diag_old": {"argv": ["bash", "-lc", "true"]}}}},
                    },
                }
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    merged = store.merge_generated_config(
        {
            "environment": {
                "services": {
                    "mysql": {
                        "category": "database",
                        "transport": "local",
                    }
                }
            },
            "diagnostics": {
                "checks": {
                    "mysql_health": {
                        "id": "check:mysql",
                        "service": "mysql",
                    }
                }
            },
            "incidents": {
                "rules": {
                    "mysql_down": {
                        "id": "rule:mysql_down",
                        "recommended_action": "restart_mysql",
                    }
                }
            },
            "actions": {
                "targets": {
                    "local": {
                        "diag_mysql_status": {"argv": ["bash", "-lc", "systemctl status mysql"], "timeout_sec": 20},
                    }
                },
                "remediation": {
                    "restart_mysql": {
                        "target": "local",
                        "risk_level": "critical",
                    }
                },
            },
            "monitor": {
                "servers": [
                    {"id": "scan:mysql", "target": "local", "action_id": "diag_mysql_status"},
                ]
            },
            "runbooks": {
                "templates": {
                    "inspect_mysql": {
                        "id": "inspect_mysql",
                        "steps": [{"action_id": "diag_mysql_status"}],
                    }
                }
            },
        }
    )
    reloaded = store.load_config()
    effective = store.load_effective_config()

    for payload in (merged, reloaded):
        assert payload["admin"]["monitor"] == {
            "enabled": True,
            "interval_sec": 11,
            "servers": [{"id": "manual", "target": "local", "action_id": "clear_logs"}],
        }
        assert payload["admin"]["notifications"] == {"level": "info"}
        assert payload["admin"]["runtime"] == {"pinned_cli": {"name": "codex"}}
        assert payload["admin"]["generated"]["environment"]["services"]["mysql"]["category"] == "database"
        assert payload["admin"]["generated"]["actions"]["remediation"]["restart_mysql"]["risk_level"] == "critical"
        assert payload["admin"]["generated"]["runbooks"]["templates"]["inspect_mysql"]["id"] == "inspect_mysql"
        assert payload["admin"]["generated"]["monitor"]["servers"] == [
            {"id": "scan:mysql", "target": "local", "action_id": "diag_mysql_status"},
        ]

    assert effective["admin"]["environment"]["services"]["mysql"]["transport"] == "local"
    assert effective["admin"]["actions"]["local"]["clear_logs"]["timeout_sec"] == 11
    assert effective["admin"]["actions"]["local"]["diag_mysql_status"]["timeout_sec"] == 20
    assert effective["admin"]["actions"]["remediation"]["restart_mysql"]["risk_level"] == "critical"
    assert effective["admin"]["runbooks"]["templates"]["inspect_mysql"]["steps"][0]["action_id"] == "diag_mysql_status"
    assert effective["admin"]["monitor"]["servers"] == [
        {"id": "scan:mysql", "target": "local", "action_id": "diag_mysql_status"},
        {"id": "manual", "target": "local", "action_id": "clear_logs"},
    ]


def test_effective_config_ignores_generated_inventory_for_other_manual_transport(tmp_path) -> None:
    session_workdir = tmp_path / "session"
    session_workdir.mkdir()

    store = AdminConfigStore(str(session_workdir))
    config_path = Path(store.ensure_config())
    manual_server = {
        "id": "remote",
        "target": "ssh",
        "action_id": "diag_host_health",
        "host": "server.example.com",
        "user": "root",
        "password_env": "SSH_PASSWORD",
    }
    config_path.write_text(
        yaml.safe_dump(
            {
                "admin": {
                    "monitor": {
                        "enabled": True,
                        "interval_sec": 30,
                        "servers": [manual_server],
                    },
                    "runtime": {"pinned_cli": {"name": "codex"}},
                    "actions": {
                        "ssh": {
                            "diag_host_health": {"argv": ["bash", "-lc", "uptime"], "timeout_sec": 15},
                        },
                    },
                    "generated": {
                        "environment": {
                            "transport": "local",
                            "services": {
                                "systemd:bot": {
                                    "category": "systemd",
                                    "transport": "local",
                                },
                            },
                        },
                        "actions": {
                            "targets": {
                                "local": {
                                    "diag_systemd_bot_status": {
                                        "argv": ["bash", "-lc", "systemctl is-active bot"],
                                        "timeout_sec": 20,
                                    },
                                },
                            },
                        },
                        "monitor": {
                            "servers": [
                                {
                                    "id": "scan:systemd:bot",
                                    "target": "local",
                                    "action_id": "diag_systemd_bot_status",
                                },
                            ],
                        },
                    },
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    effective = store.load_effective_config(validate=False)

    assert effective["admin"]["monitor"]["servers"] == [manual_server]
    assert "systemd:bot" not in effective["admin"].get("environment", {}).get("services", {})
    assert "diag_systemd_bot_status" not in effective["admin"].get("actions", {}).get("local", {})
    assert effective["admin"]["generated"]["environment"]["transport"] == "local"
