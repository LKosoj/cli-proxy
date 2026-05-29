import time

import pytest

from modes.admin.snapshot_store import (
    AdminSnapshotStore,
    AdminSnapshotStoreError,
    SEVERITY_ALARM,
    SEVERITY_INFO,
    SEVERITY_WARN,
    admin_root,
    canonical_hash,
    safe_server_id,
    server_dir,
    snapshot_db_path,
)


def _make_store(tmp_path, server_id: str = "web-01") -> AdminSnapshotStore:
    return AdminSnapshotStore.for_server(str(tmp_path), server_id)


def test_safe_server_id_strips_slashes_and_keeps_allowed_chars():
    assert safe_server_id("web-01") == "web-01"
    assert safe_server_id("app_name.v2") == "app_name.v2"
    assert safe_server_id("../evil") == "_.._evil" or "_" in safe_server_id("../evil")
    assert "/" not in safe_server_id("a/b/c")


def test_safe_server_id_rejects_empty_and_dot():
    with pytest.raises(AdminSnapshotStoreError):
        safe_server_id("")
    with pytest.raises(AdminSnapshotStoreError):
        safe_server_id("   ")
    with pytest.raises(AdminSnapshotStoreError):
        safe_server_id(".")


def test_layout_paths(tmp_path):
    root = admin_root(str(tmp_path))
    assert root.as_posix().endswith("/.cli-proxy/.admin")
    sdir = server_dir(str(tmp_path), "web-01")
    assert sdir.as_posix().endswith("/servers/web-01")
    assert snapshot_db_path(str(tmp_path), "web-01").endswith("snapshots.sqlite")


def test_canonical_hash_is_stable_for_reordered_dicts():
    a = {"b": 1, "a": 2}
    b = {"a": 2, "b": 1}
    assert canonical_hash(a) == canonical_hash(b)


def test_insert_and_latest_snapshot_roundtrip(tmp_path):
    store = _make_store(tmp_path)
    snap_id, h = store.insert_snapshot(check_id="disk.pct", value={"pct": 75})
    assert snap_id > 0
    assert len(h) == 16
    latest = store.latest_snapshot("disk.pct")
    assert latest is not None
    assert latest["value"] == {"pct": 75}
    assert latest["value_hash"] == h


def test_snapshots_in_window_respects_limit_and_since(tmp_path):
    store = _make_store(tmp_path)
    now = int(time.time())
    for i in range(5):
        store.insert_snapshot(check_id="load.1m", value=i, ts=now - i * 60)
    rows = store.snapshots_in_window("load.1m", limit=3)
    assert len(rows) == 3
    assert [r["value"] for r in rows] == [0, 1, 2]
    rows2 = store.snapshots_in_window("load.1m", since_ts=now - 90, limit=100)
    assert len(rows2) == 2


def test_all_check_ids(tmp_path):
    store = _make_store(tmp_path)
    store.insert_snapshot(check_id="a", value=1)
    store.insert_snapshot(check_id="b", value=2)
    store.insert_snapshot(check_id="a", value=3)
    assert store.all_check_ids() == ["a", "b"]


def test_insert_drift_and_list_with_severity_filter(tmp_path):
    store = _make_store(tmp_path)
    store.insert_drift(check_id="port.5432", severity=SEVERITY_INFO, new_value="listen")
    store.insert_drift(check_id="user.root", severity=SEVERITY_ALARM, new_value="sudo_added")
    store.insert_drift(check_id="pkg.nginx", severity=SEVERITY_WARN, new_value="1.25")
    all_drifts = store.list_drifts(limit=10)
    assert len(all_drifts) == 3
    warn_up = store.list_drifts(limit=10, severity_min=SEVERITY_WARN)
    assert len(warn_up) == 2
    assert {d["severity"] for d in warn_up} == {SEVERITY_WARN, SEVERITY_ALARM}


def test_ack_drift_marks_as_acknowledged_and_hides_from_open_list(tmp_path):
    store = _make_store(tmp_path)
    drift_id = store.insert_drift(check_id="x", severity=SEVERITY_WARN, new_value="v")
    assert store.ack_drift(drift_id, by="admin") is True
    open_only = store.list_drifts(include_acknowledged=False)
    assert open_only == []
    with_ack = store.list_drifts(include_acknowledged=True)
    assert len(with_ack) == 1
    assert with_ack[0]["acknowledged"] is True
    assert with_ack[0]["ack_by"] == "admin"


def test_ack_drift_invalid_id_returns_false(tmp_path):
    store = _make_store(tmp_path)
    assert store.ack_drift(0) is False
    assert store.ack_drift(999999) is False


def test_drift_stats_counts_only_open(tmp_path):
    store = _make_store(tmp_path)
    did = store.insert_drift(check_id="a", severity=SEVERITY_WARN, new_value="v")
    store.insert_drift(check_id="b", severity=SEVERITY_WARN, new_value="v")
    store.insert_drift(check_id="c", severity=SEVERITY_ALARM, new_value="v")
    store.ack_drift(did)
    stats = store.drift_stats()
    assert stats[SEVERITY_WARN] == 1
    assert stats[SEVERITY_ALARM] == 1


def test_cleanup_retention_deletes_old_snapshots_and_acked_drifts(tmp_path):
    store = _make_store(tmp_path, "db-02")
    now = int(time.time())
    old = now - 40 * 86400
    fresh = now - 5 * 86400
    store.insert_snapshot(check_id="a", value=1, ts=old)
    store.insert_snapshot(check_id="a", value=2, ts=fresh)
    d_old = store.insert_drift(check_id="a", severity=SEVERITY_WARN, new_value="v", ts=old)
    store.insert_drift(check_id="a", severity=SEVERITY_ALARM, new_value="v", ts=old)  # не acked
    store.ack_drift(d_old)
    report = store.cleanup_retention(max_age_days=30)
    assert report["snapshots_deleted"] == 1
    assert report["drifts_deleted"] == 1  # только acked старый
    remaining = store.list_drifts()
    assert len(remaining) == 1
    assert remaining[0]["severity"] == SEVERITY_ALARM


def test_cleanup_retention_zero_days_is_noop(tmp_path):
    store = _make_store(tmp_path)
    store.insert_snapshot(check_id="a", value=1)
    report = store.cleanup_retention(max_age_days=0)
    assert report == {"snapshots_deleted": 0, "drifts_deleted": 0}


def test_meta_set_and_get(tmp_path):
    store = _make_store(tmp_path)
    assert store.get_meta("baseline_accepted_ts") is None
    store.set_meta("baseline_accepted_ts", "12345")
    assert store.get_meta("baseline_accepted_ts") == "12345"


def test_invalid_severity_raises(tmp_path):
    store = _make_store(tmp_path)
    with pytest.raises(AdminSnapshotStoreError):
        store.insert_drift(check_id="a", severity="catastrophic", new_value="v")


def test_two_servers_isolated(tmp_path):
    s1 = _make_store(tmp_path, "web-01")
    s2 = _make_store(tmp_path, "db-02")
    s1.insert_snapshot(check_id="a", value=1)
    s2.insert_snapshot(check_id="a", value=2)
    assert s1.latest_snapshot("a")["value"] == 1
    assert s2.latest_snapshot("a")["value"] == 2
    assert s1.db_path != s2.db_path
