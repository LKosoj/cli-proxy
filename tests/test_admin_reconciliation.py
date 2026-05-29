import asyncio
import time

from modes.admin.baseline import (
    AdminBaselineScanner,
    BaselineCheck,
    ServerSpec,
    apply_scan_result,
    load_baseline,
)
from modes.admin.memory import ServerMemory
from modes.admin.reconciliation import AdminReconciler
from modes.admin.snapshot_store import (
    AdminSnapshotStore,
    SEVERITY_ALARM,
    SEVERITY_INFO,
    SEVERITY_WARN,
)


class _FakeResult:
    def __init__(self, stdout="", *, timed_out=False):
        self.stdout = stdout
        self.stderr = ""
        self.returncode = 0
        self.timed_out = timed_out
        self.duration_ms = 1


class _Transport:
    def __init__(self, responses):
        self.responses = dict(responses)

    async def run(self, spec):
        key = spec.action_id.split(":", 1)[-1]
        payload = self.responses.get(key, "")
        return _FakeResult(stdout=str(payload))


def _scanner(responses):
    checks = [
        BaselineCheck(id="os.kernel", command="uname -r", parser=lambda s: s.strip()),
        BaselineCheck(id="systemd.running", command="s",
                      parser=lambda s: sorted(
                          line.strip() for line in s.splitlines() if line.strip()
                      )),
        BaselineCheck(id="network.listen", command="n",
                      parser=lambda s: sorted(
                          line.strip() for line in s.splitlines() if line.strip()
                      )),
    ]
    return AdminBaselineScanner(checks=checks, local_transport=_Transport(responses))


def test_first_reconcile_creates_baseline_and_no_drifts(tmp_path):
    scanner = _scanner({
        "os.kernel": "6.8",
        "systemd.running": "nginx.service\nssh.service",
        "network.listen": "0.0.0.0:22",
    })
    reconciler = AdminReconciler(str(tmp_path), scanner=scanner)

    async def _go():
        spec = ServerSpec(server_id="web-01", transport="local")
        report = await reconciler.reconcile_server(spec)
        assert report.ok is True
        assert report.baseline_present is True
        assert report.drifts_written == 0
        assert report.snapshots_written == 3
        base = load_baseline(str(tmp_path), "web-01")
        assert base["checks"]["os.kernel"] == "6.8"

    asyncio.run(_go())


def test_second_reconcile_detects_drifts_and_persists(tmp_path):
    apply_scan_result(str(tmp_path), "web-01", {
        "server_id": "web-01",
        "checks": {
            "os.kernel": "6.8",
            "systemd.running": ["nginx.service", "ssh.service"],
            "network.listen": ["0.0.0.0:22"],
        },
    })

    scanner = _scanner({
        "os.kernel": "6.9",
        "systemd.running": "nginx.service\nssh.service",
        "network.listen": "0.0.0.0:22\n0.0.0.0:9000",
    })
    reconciler = AdminReconciler(str(tmp_path), scanner=scanner)

    async def _go():
        spec = ServerSpec(server_id="web-01", transport="local")
        report = await reconciler.reconcile_server(spec)
        assert report.ok is True
        assert report.drifts_written >= 2  # kernel + new listener
        assert report.drifts_by_severity.get(SEVERITY_INFO, 0) >= 1
        assert report.drifts_by_severity.get(SEVERITY_ALARM, 0) >= 1
        store = AdminSnapshotStore.for_server(str(tmp_path), "web-01")
        open_drifts = store.list_drifts(include_acknowledged=False)
        assert len(open_drifts) >= 2
        severities = {d["severity"] for d in open_drifts}
        assert SEVERITY_ALARM in severities

    asyncio.run(_go())


def test_alarm_hook_invoked_on_alarm(tmp_path):
    apply_scan_result(str(tmp_path), "web-01", {
        "server_id": "web-01",
        "checks": {"network.listen": ["0.0.0.0:22"]},
    })
    scanner = _scanner({
        "os.kernel": "",
        "systemd.running": "",
        "network.listen": "0.0.0.0:22\n0.0.0.0:9000",
    })
    captured = {}

    def hook(server_id, drifts):
        captured["server_id"] = server_id
        captured["count"] = len(drifts)

    reconciler = AdminReconciler(str(tmp_path), scanner=scanner, alarm_hook=hook)

    async def _go():
        await reconciler.reconcile_server(ServerSpec(server_id="web-01", transport="local"))

    asyncio.run(_go())
    assert captured.get("server_id") == "web-01"
    assert captured.get("count") and captured["count"] >= 1


def test_tick_aggregates_reports_for_multiple_servers(tmp_path):
    scanner = _scanner({"os.kernel": "6.8", "systemd.running": "", "network.listen": ""})
    reconciler = AdminReconciler(str(tmp_path), scanner=scanner)

    async def _go():
        tick = await reconciler.tick([
            ServerSpec(server_id="web-01", transport="local"),
            ServerSpec(server_id="db-02", transport="local"),
        ])
        summary = tick.summary()
        assert summary["servers_total"] == 2
        assert summary["servers_ok"] == 2

    asyncio.run(_go())


def test_tick_keeps_going_on_single_server_failure(tmp_path):
    class _FailingScanner:
        async def scan(self, spec):
            if spec.server_id == "bad":
                raise RuntimeError("scanner boom")
            return {"server_id": spec.server_id, "checks": {"os.kernel": "6.8"}}

    reconciler = AdminReconciler(str(tmp_path), scanner=_FailingScanner())  # type: ignore

    async def _go():
        tick = await reconciler.tick([
            ServerSpec(server_id="good", transport="local"),
            ServerSpec(server_id="bad", transport="local"),
        ])
        assert tick.summary()["servers_failed"] == 1
        assert tick.summary()["servers_ok"] == 1

    asyncio.run(_go())


def test_daily_maintenance_cleans_and_compacts(tmp_path):
    # populate old snapshots & many notes
    store = AdminSnapshotStore.for_server(str(tmp_path), "web-01")
    old_ts = int(time.time()) - 40 * 86400
    for i in range(5):
        store.insert_snapshot(check_id="a", value=i, ts=old_ts)
    did = store.insert_drift(check_id="a", severity=SEVERITY_WARN, new_value="v", ts=old_ts)
    store.ack_drift(did)

    mem = ServerMemory(str(tmp_path), "web-01")
    for i in range(10):
        mem.append_note(f"note {i}", ts=f"2026-04-22T10:{i:02d}:00Z")

    reconciler = AdminReconciler(
        str(tmp_path),
        retention_days=30,
        memory_compact_threshold=5,
        memory_compact_keep_tail=2,
    )
    report = reconciler.daily_maintenance(["web-01"])
    entry = report.servers["web-01"]
    assert entry["cleanup"]["snapshots_deleted"] == 5
    assert entry["cleanup"]["drifts_deleted"] == 1
    assert entry["memory_compact"]["compacted"] is True
    assert entry["memory_compact"]["collapsed"] > 0
    assert store.get_meta("last_daily_maintenance_ts") is not None


def test_reconciler_with_no_servers_is_noop(tmp_path):
    reconciler = AdminReconciler(str(tmp_path))

    async def _go():
        tick = await reconciler.tick([])
        assert tick.summary()["servers_total"] == 0

    asyncio.run(_go())
    report = reconciler.daily_maintenance([])
    assert report.servers == {}
