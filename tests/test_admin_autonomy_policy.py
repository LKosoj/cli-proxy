from modes.admin.autonomy_policy import (
    DEFAULT_COOLDOWN_SEC,
    DEFAULT_MAX_ACTIONS_PER_HOUR,
    AutonomyPolicy,
    load_autonomy_policy,
)
from modes.admin.snapshot_store import (
    SEVERITY_ALARM,
    SEVERITY_INFO,
    SEVERITY_WARN,
)


def test_defaults_when_empty_cfg():
    p = load_autonomy_policy({})
    assert p.enabled is False
    assert p.auto_apply_severities == [SEVERITY_INFO]
    assert p.auto_exec_actions == []
    assert p.max_actions_per_hour == DEFAULT_MAX_ACTIONS_PER_HOUR
    assert p.cooldown_sec == DEFAULT_COOLDOWN_SEC
    assert p.baseline_auto_accept_enabled is False
    assert p.require_dry_run_success is True


def test_parse_full_config():
    cfg = {
        "autonomy": {
            "enabled": True,
            "auto_apply_severities": ["info", "warn"],
            "auto_exec_actions": ["systemd.restart_nginx", "systemd.reload"],
            "max_actions_per_hour": 3,
            "cooldown_sec": 120,
            "baseline_auto_accept": {"enabled": True, "after_stable_scans": 5},
            "per_server": {
                "web-01": {
                    "auto_apply_severities": ["info"],
                    "auto_exec_actions": ["systemd.reload"],
                },
            },
        }
    }
    p = load_autonomy_policy(cfg)
    assert p.enabled is True
    assert set(p.auto_apply_severities) == {"info", "warn"}
    assert "systemd.restart_nginx" in p.auto_exec_actions
    assert p.max_actions_per_hour == 3
    assert p.cooldown_sec == 120
    assert p.baseline_auto_accept_enabled is True
    assert p.baseline_auto_accept_after_stable_scans == 5

    # per-server merge
    p_web = p.for_server("web-01")
    assert p_web.enabled is True  # from base
    assert p_web.auto_apply_severities == ["info"]  # override
    assert p_web.auto_exec_actions == ["systemd.reload"]  # override

    # unknown server → base
    p_other = p.for_server("db-99")
    assert p_other is p  # base identity


def test_permits_severity_blocks_alarm_always():
    p = load_autonomy_policy({
        "autonomy": {"enabled": True, "auto_apply_severities": ["alarm"]},
    })
    # alarm — всегда False, даже если явно прописано
    assert p.permits_severity(SEVERITY_ALARM) is False


def test_permits_severity_respects_list():
    p = load_autonomy_policy({
        "autonomy": {"enabled": True, "auto_apply_severities": ["info"]},
    })
    assert p.permits_severity(SEVERITY_INFO) is True
    assert p.permits_severity(SEVERITY_WARN) is False


def test_permits_action_whitelist():
    p = load_autonomy_policy({
        "autonomy": {"enabled": True, "auto_exec_actions": ["a.b", "c.d"]},
    })
    assert p.permits_action("a.b") is True
    assert p.permits_action("c.d") is True
    assert p.permits_action("x.y") is False
    assert p.permits_action("") is False


def test_bad_severities_are_filtered_out():
    p = load_autonomy_policy({
        "autonomy": {"enabled": True, "auto_apply_severities": ["garbage", "info"]},
    })
    assert p.auto_apply_severities == ["info"]


def test_non_positive_ints_fallback_to_defaults():
    p = load_autonomy_policy({
        "autonomy": {
            "enabled": True,
            "max_actions_per_hour": -1,
            "cooldown_sec": 0,
        }
    })
    assert p.max_actions_per_hour == DEFAULT_MAX_ACTIONS_PER_HOUR
    assert p.cooldown_sec == DEFAULT_COOLDOWN_SEC


def test_for_server_returns_self_when_no_override():
    p = AutonomyPolicy(enabled=True)
    assert p.for_server("") is p
    assert p.for_server("unknown") is p


def test_permits_adhoc_argv_whitelist():
    p = load_autonomy_policy({
        "autonomy": {
            "enabled": True,
            "auto_exec_adhoc_commands": ["ls", "df", "uptime"],
        }
    })
    assert p.permits_adhoc_argv(["ls", "-la"]) is True
    assert p.permits_adhoc_argv(["df", "-h"]) is True
    assert p.permits_adhoc_argv(["uptime"]) is True
    assert p.permits_adhoc_argv(["rm", "-rf", "/"]) is False
    assert p.permits_adhoc_argv([]) is False
    assert p.permits_adhoc_argv([""]) is False


def test_permits_adhoc_argv_empty_allowlist():
    p = AutonomyPolicy(enabled=True)
    assert p.permits_adhoc_argv(["ls"]) is False


def test_per_server_override_adhoc_commands():
    p = load_autonomy_policy({
        "autonomy": {
            "enabled": True,
            "auto_exec_adhoc_commands": ["ls"],
            "per_server": {
                "web-01": {"auto_exec_adhoc_commands": ["systemctl", "journalctl"]},
            },
        }
    })
    p_web = p.for_server("web-01")
    assert p_web.permits_adhoc_argv(["systemctl", "status"]) is True
    assert p_web.permits_adhoc_argv(["journalctl", "-xe"]) is True
    # Override — base `ls` полностью замещается списком override
    assert p_web.permits_adhoc_argv(["ls"]) is False


def test_adhoc_commands_default_empty():
    p = load_autonomy_policy({"autonomy": {"enabled": True}})
    assert p.auto_exec_adhoc_commands == []
