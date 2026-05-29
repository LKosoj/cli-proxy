import asyncio

import pytest
import yaml

from modes.admin.baseline import apply_scan_result
from modes.admin.facade import AdminAutonomyService, parse_server_specs
from modes.admin.memory import ServerMemory
from modes.admin.runbooks import global_runbooks_dir
from modes.admin.snapshot_store import SEVERITY_ALARM, SEVERITY_WARN, admin_root


def _write_config(workdir, servers):
    root = admin_root(str(workdir))
    root.mkdir(parents=True, exist_ok=True)
    payload = {
        "admin": {
            "allowlist": {"local": {}, "ssh": {}},
            "monitor": {"servers": servers},
            "runtime": {},
        }
    }
    (root / "config.yaml").write_text(
        yaml.safe_dump(payload, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )


def test_parse_server_specs_basic():
    admin_cfg = {
        "monitor": {
            "servers": [
                {"server_id": "web-01", "transport": "local", "tags": ["web"]},
                {"id": "db-02", "transport": "ssh", "host": "1.2.3.4",
                 "user": "root", "key_path": "/keys/id", "password_env": "SSH_DB_PASS", "port": 2222},
                {"name": "mal/bad"},  # invalid id — sanitized
            ]
        }
    }
    specs = parse_server_specs(admin_cfg)
    assert len(specs) == 3
    assert specs[0].server_id == "web-01"
    assert specs[0].tags == ["web"]
    assert specs[1].server_id == "db-02"
    assert specs[1].host == "1.2.3.4"
    assert specs[1].port == 2222
    assert specs[1].password_env == "SSH_DB_PASS"
    assert "/" not in specs[2].server_id


def test_parse_server_specs_unknown_transport_falls_back_to_local():
    specs = parse_server_specs({"monitor": {"servers": [{"server_id": "x", "transport": "carrier_pigeon"}]}})
    assert specs[0].transport == "local"


def test_list_servers_empty_when_no_config(tmp_path):
    fac = AdminAutonomyService(str(tmp_path))
    assert fac.list_servers() == []


def test_list_servers_returns_summaries(tmp_path):
    _write_config(tmp_path, [{"server_id": "web-01", "tags": ["web"]}])
    fac = AdminAutonomyService(str(tmp_path))
    summaries = fac.list_servers()
    assert len(summaries) == 1
    s = summaries[0]
    assert s.server_id == "web-01"
    assert s.baseline_present is False
    assert s.status() == "no_baseline"


def test_server_summary_reflects_baseline_and_drifts(tmp_path):
    _write_config(tmp_path, [{"server_id": "web-01"}])
    apply_scan_result(str(tmp_path), "web-01", {"server_id": "web-01", "checks": {"a": 1}})
    from modes.admin.snapshot_store import AdminSnapshotStore
    store = AdminSnapshotStore.for_server(str(tmp_path), "web-01")
    store.insert_drift(check_id="x", severity=SEVERITY_ALARM, new_value="v")
    fac = AdminAutonomyService(str(tmp_path))
    s = fac.get_server_summary("web-01")
    assert s is not None
    assert s.baseline_present is True
    assert s.open_drifts[SEVERITY_ALARM] == 1
    assert s.status() == "alarm"


def test_server_summary_for_unknown_returns_none(tmp_path):
    _write_config(tmp_path, [{"server_id": "web-01"}])
    fac = AdminAutonomyService(str(tmp_path))
    assert fac.get_server_summary("does-not-exist") is None


def test_orphan_server_on_disk_is_surfaced(tmp_path):
    _write_config(tmp_path, [])
    apply_scan_result(str(tmp_path), "web-01", {"server_id": "web-01", "checks": {"a": 1}})
    fac = AdminAutonomyService(str(tmp_path))
    s = fac.get_server_summary("web-01")
    assert s is not None
    assert s.baseline_present is True


def test_get_baseline_and_accept_discard_cycle(tmp_path):
    _write_config(tmp_path, [{"server_id": "web-01"}])
    apply_scan_result(str(tmp_path), "web-01", {"server_id": "web-01", "checks": {"a": 1}})
    apply_scan_result(str(tmp_path), "web-01", {"server_id": "web-01", "checks": {"a": 2}})
    fac = AdminAutonomyService(str(tmp_path))
    info = fac.get_baseline("web-01")
    assert info["has_proposed"] is True
    fac.discard_baseline_proposal("web-01")
    assert fac.get_baseline("web-01")["has_proposed"] is False

    apply_scan_result(str(tmp_path), "web-01", {"server_id": "web-01", "checks": {"a": 3}})
    fac.accept_baseline("web-01")
    info = fac.get_baseline("web-01")
    assert info["has_proposed"] is False
    assert info["has_prev"] is True
    assert info["baseline"]["checks"]["a"] == 3


def test_memory_facts_and_notes_through_facade(tmp_path):
    _write_config(tmp_path, [{"server_id": "web-01"}])
    fac = AdminAutonomyService(str(tmp_path))
    fac.update_memory_fact("web-01", key="service_manager", value="systemd", by="test")
    fac.append_memory_note("web-01", "nginx reload ok", source="manual", tags=["nginx"])
    mem = fac.get_memory("web-01")
    assert mem["facts"]["service_manager"] == "systemd"
    assert "nginx reload ok" in mem["notes_text"]
    assert mem["stats"]["entries"] == 1
    assert fac.delete_memory_fact("web-01", "service_manager") is True


def test_compact_memory_through_facade(tmp_path):
    fac = AdminAutonomyService(str(tmp_path))
    # direct call to memory for keep_tail control; facade wraps it
    memory = ServerMemory(str(tmp_path), "web-01")
    for i in range(60):
        memory.append_note(f"n{i}", source="auto", ts=f"2026-04-22T10:{i % 60:02d}:00Z")
    result = fac.compact_memory("web-01", force=True)
    assert result["compacted"] is True


def test_drift_list_and_ack_through_facade(tmp_path):
    _write_config(tmp_path, [{"server_id": "web-01"}])
    from modes.admin.snapshot_store import AdminSnapshotStore
    store = AdminSnapshotStore.for_server(str(tmp_path), "web-01")
    did = store.insert_drift(check_id="a", severity=SEVERITY_WARN, new_value="v")
    fac = AdminAutonomyService(str(tmp_path))
    drifts = fac.list_drifts("web-01", open_only=True)
    assert len(drifts) == 1
    fac.ack_drift("web-01", did, by="me")
    assert fac.list_drifts("web-01", open_only=True) == []


def test_runbooks_list_through_facade(tmp_path):
    d = global_runbooks_dir(str(tmp_path))
    d.mkdir(parents=True, exist_ok=True)
    (d / "r.md").write_text(
        "---\nid: r\ntitle: Some\nservers: [\"web-*\"]\ntags: [\"disk\"]\n---\n# body\n",
        encoding="utf-8",
    )
    fac = AdminAutonomyService(str(tmp_path))
    rbs = fac.list_runbooks(server_id="web-01", tags=["disk"])
    assert len(rbs) == 1
    summary = fac.list_runbook_summary(server_id="web-01", tags=["disk"])
    assert summary[0]["id"] == "r"
    rb = fac.get_runbook("r")
    assert rb is not None
    assert rb.title == "Some"


def test_rescan_server_with_custom_reconciler(tmp_path):
    _write_config(tmp_path, [{"server_id": "web-01"}])

    class _StubScanner:
        async def scan(self, spec):
            return {"server_id": spec.server_id, "checks": {"os.kernel": "6.8"}}

    from modes.admin.reconciliation import AdminReconciler

    def factory():
        return AdminReconciler(str(tmp_path), scanner=_StubScanner())  # type: ignore

    fac = AdminAutonomyService(str(tmp_path), reconciler_factory=factory)

    async def _go():
        report = await fac.rescan_server("web-01")
        assert report.ok is True
        assert report.baseline_present is True

    asyncio.run(_go())


def test_global_summary_aggregates_statuses(tmp_path):
    _write_config(tmp_path, [
        {"server_id": "web-01"},
        {"server_id": "web-02"},
    ])
    apply_scan_result(str(tmp_path), "web-01", {"server_id": "web-01", "checks": {"a": 1}})
    fac = AdminAutonomyService(str(tmp_path))
    summary = fac.global_summary()
    assert summary["server_count"] == 2
    assert summary["statuses"]["ok"] == 1
    assert summary["statuses"]["no_baseline"] == 1


def test_workdir_is_required(tmp_path):
    with pytest.raises(ValueError):
        AdminAutonomyService("")
