from modes.admin.drift import (
    DEFAULT_DRIFT_RULES,
    compare_baselines,
    drifts_summary,
)
from modes.admin.snapshot_store import (
    SEVERITY_ALARM,
    SEVERITY_INFO,
    SEVERITY_NOISE,
    SEVERITY_WARN,
)


def _profile(checks):
    return {"checks": dict(checks)}


def test_no_drift_when_baselines_equal():
    base = _profile({"os.kernel": "6.8", "systemd.running": ["nginx.service", "ssh.service"]})
    cur = _profile({"os.kernel": "6.8", "systemd.running": ["ssh.service", "nginx.service"]})
    assert compare_baselines(base, cur) == []


def test_kernel_change_is_info():
    base = _profile({"os.kernel": "6.8"})
    cur = _profile({"os.kernel": "6.9"})
    drifts = compare_baselines(base, cur)
    assert len(drifts) == 1
    assert drifts[0].severity == SEVERITY_INFO
    assert drifts[0].kind == "change"
    assert drifts[0].details == {"from": "6.8", "to": "6.9"}


def test_hostname_change_is_warn():
    drifts = compare_baselines(
        _profile({"os.hostname": "web-01"}),
        _profile({"os.hostname": "web-02"}),
    )
    assert len(drifts) == 1
    assert drifts[0].severity == SEVERITY_WARN


def test_new_listener_is_alarm():
    base = _profile({"network.listen": ["0.0.0.0:22"]})
    cur = _profile({"network.listen": ["0.0.0.0:22", "0.0.0.0:9000"]})
    drifts = compare_baselines(base, cur)
    assert any(
        d.severity == SEVERITY_ALARM
        and d.kind == "added"
        and d.details.get("added") == ["0.0.0.0:9000"]
        for d in drifts
    )


def test_removed_listener_is_info():
    base = _profile({"network.listen": ["0.0.0.0:22", "0.0.0.0:9000"]})
    cur = _profile({"network.listen": ["0.0.0.0:22"]})
    drifts = compare_baselines(base, cur)
    assert any(
        d.severity == SEVERITY_INFO and d.kind == "removed"
        for d in drifts
    )


def test_new_user_is_alarm():
    base = _profile({"users.regular": ["ubuntu"]})
    cur = _profile({"users.regular": ["ubuntu", "evil"]})
    drifts = compare_baselines(base, cur)
    assert any(
        d.severity == SEVERITY_ALARM
        and d.details.get("added") == ["evil"]
        for d in drifts
    )


def test_added_systemd_unit_is_warn():
    base = _profile({"systemd.running": ["ssh.service"]})
    cur = _profile({"systemd.running": ["ssh.service", "cron.service"]})
    drifts = compare_baselines(base, cur)
    assert any(
        d.check_id == "systemd.running"
        and d.kind == "added"
        and d.severity == SEVERITY_WARN
        for d in drifts
    )


def test_package_version_change_is_info():
    base = _profile({"packages.sample": {"nginx": "1.25", "openssh-server": "9.0"}})
    cur = _profile({"packages.sample": {"nginx": "1.26", "openssh-server": "9.0"}})
    drifts = compare_baselines(base, cur)
    change_records = [d for d in drifts if d.kind == "change"]
    assert len(change_records) == 1
    assert change_records[0].severity == SEVERITY_INFO
    assert "nginx" in change_records[0].details["changed"]


def test_added_package_is_info():
    base = _profile({"packages.sample": {"nginx": "1.25"}})
    cur = _profile({"packages.sample": {"nginx": "1.25", "htop": "3.0"}})
    drifts = compare_baselines(base, cur)
    added = [d for d in drifts if d.kind == "added"]
    assert len(added) == 1
    assert "htop" in added[0].details["added"]


def test_crontab_change_is_warn():
    base = _profile({"crontab.root": "0 3 * * * /backup.sh"})
    cur = _profile({"crontab.root": "0 3 * * * /backup.sh\n* * * * * /evil.sh"})
    drifts = compare_baselines(base, cur)
    assert len(drifts) == 1
    assert drifts[0].severity == SEVERITY_WARN


def test_disk_space_noise_severity():
    base = _profile({"disk.space": {"/": "45%"}})
    cur = _profile({"disk.space": {"/": "46%"}})
    drifts = compare_baselines(base, cur)
    assert all(d.severity == SEVERITY_NOISE for d in drifts)


def test_unknown_check_is_ignored_without_rule():
    base = _profile({"custom.check": "a"})
    cur = _profile({"custom.check": "b"})
    assert compare_baselines(base, cur) == []


def test_custom_rule_overrides_default():
    base = _profile({"os.kernel": "6.8"})
    cur = _profile({"os.kernel": "6.9"})
    custom = {"os.kernel": {"change": SEVERITY_ALARM}}
    drifts = compare_baselines(base, cur, rules=custom)
    assert drifts[0].severity == SEVERITY_ALARM


def test_missing_check_in_current_triggers_removed_for_list():
    base = _profile({"systemd.running": ["ssh.service"]})
    cur = _profile({})
    drifts = compare_baselines(base, cur)
    assert any(d.kind == "removed" for d in drifts)


def test_drifts_summary_counts():
    base = _profile({
        "os.kernel": "6.8",
        "network.listen": ["0.0.0.0:22"],
        "users.regular": ["ubuntu"],
    })
    cur = _profile({
        "os.kernel": "6.9",
        "network.listen": ["0.0.0.0:22", "0.0.0.0:9000"],
        "users.regular": ["ubuntu", "evil"],
    })
    drifts = compare_baselines(base, cur)
    summary = drifts_summary(drifts)
    assert summary[SEVERITY_INFO] == 1
    assert summary[SEVERITY_ALARM] == 2


def test_default_rules_covers_standard_checks():
    expected = {
        "os.kernel", "os.hostname", "os.os_release",
        "systemd.running", "network.listen", "mounts", "disk.space",
        "users.regular", "packages.sample", "crontab.root",
    }
    assert expected.issubset(DEFAULT_DRIFT_RULES.keys())
