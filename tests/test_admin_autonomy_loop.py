import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

from modes.admin.autonomy_loop import (
    AdminAutonomyLoop,
    DecisionContext,
    RuleBasedDecisionMaker,
)
from modes.admin.autonomy_policy import AutonomyPolicy, load_autonomy_policy
from modes.admin.baseline import apply_scan_result, load_baseline, load_proposed_baseline
from modes.admin.memory import ServerMemory
from modes.admin.runbooks import global_runbooks_dir
from modes.admin.snapshot_store import (
    AdminSnapshotStore,
    SEVERITY_ALARM,
    SEVERITY_INFO,
    SEVERITY_WARN,
)


# --------------------------------------------------------------------
# Helpers / fakes
# --------------------------------------------------------------------


@dataclass
class _FakeResult:
    success: bool
    action_id: str
    server_id: Optional[str]
    target: str
    dry_run: bool
    applied: bool
    exit_code: Optional[int] = 0
    stdout: str = ""
    stderr: str = ""
    duration_ms: int = 0
    command_preview: str = ""
    timed_out: bool = False
    error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "success": self.success,
            "action_id": self.action_id,
            "server_id": self.server_id,
            "target": self.target,
            "dry_run": self.dry_run,
            "applied": self.applied,
            "exit_code": self.exit_code,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "duration_ms": self.duration_ms,
            "command_preview": self.command_preview,
            "timed_out": self.timed_out,
            "error": self.error,
        }


class _RunnerRecorder:
    def __init__(self, *, dry_success=True, real_success=True, raise_on=None):
        self.calls: List[Dict[str, Any]] = []
        self.dry_success = dry_success
        self.real_success = real_success
        self.raise_on = raise_on  # e.g. "dry" / "real"

    async def __call__(
        self, *, workdir: str, action_id: str,
        server_id=None, target_hint=None, dry_run=True, **kwargs,
    ):
        self.calls.append({
            "action_id": action_id,
            "server_id": server_id,
            "target_hint": target_hint,
            "dry_run": dry_run,
        })
        phase = "dry" if dry_run else "real"
        if self.raise_on == phase:
            raise RuntimeError(f"boom-{phase}")
        success = self.dry_success if dry_run else self.real_success
        return _FakeResult(
            success=success, action_id=action_id, server_id=server_id,
            target=target_hint or "local", dry_run=dry_run, applied=not dry_run and success,
            exit_code=0 if success else 2,
        )


def _write_runbook(workdir: Path, *, rb_id: str, tags: list[str], auto_action: Optional[dict]):
    d = global_runbooks_dir(str(workdir))
    d.mkdir(parents=True, exist_ok=True)
    front_lines = [
        f"id: {rb_id}",
        f"title: {rb_id}-title",
        f"tags: {tags}",
    ]
    if auto_action:
        aa_yaml = "\n".join(
            f"  {k}: {v}" for k, v in auto_action.items()
        )
        front_lines.append("auto_action:")
        front_lines.append(aa_yaml)
    body = "---\n" + "\n".join(front_lines) + "\n---\n# body\n"
    (d / f"{rb_id}.md").write_text(body, encoding="utf-8")


def _seed_drift(tmp_path: Path, *, server_id="web-01", check_id="os.kernel", severity=SEVERITY_INFO,
                new_value="6.9", prev_value="6.8"):
    store = AdminSnapshotStore.for_server(str(tmp_path), server_id)
    return store.insert_drift(
        check_id=check_id, severity=severity,
        new_value=new_value, prev_value=prev_value,
        details={"kind": "change"},
    )


def _list_open_drifts(tmp_path: Path, server_id="web-01"):
    store = AdminSnapshotStore.for_server(str(tmp_path), server_id)
    return store.list_drifts(limit=50, include_acknowledged=False)


# --------------------------------------------------------------------
# RuleBasedDecisionMaker
# --------------------------------------------------------------------


def test_rule_based_picks_runbook_with_auto_action(tmp_path):
    from modes.admin.runbooks import load_runbooks, match_runbooks

    _write_runbook(
        tmp_path, rb_id="rb1",
        tags=["os.kernel"],
        auto_action={"action_id": "systemd.reload", "target_hint": "local", "confidence": 0.8},
    )
    rbs = match_runbooks(
        load_runbooks(str(tmp_path)), server_id="web-01", tags=["os.kernel"],
    )
    ctx = DecisionContext(server_id="web-01", drift={"severity": "info"}, runbooks=rbs)
    dm = RuleBasedDecisionMaker()
    decision = asyncio.run(dm.decide(ctx))
    assert decision.action == "execute"
    assert decision.action_id == "systemd.reload"
    assert decision.runbook_id == "rb1"
    assert decision.target_hint == "local"


def test_rule_based_ignores_low_confidence_auto_action(tmp_path):
    from modes.admin.runbooks import load_runbooks

    _write_runbook(
        tmp_path, rb_id="rb_weak",
        tags=["os.kernel"],
        auto_action={"action_id": "x", "confidence": 0.1},
    )
    rbs = load_runbooks(str(tmp_path))
    ctx = DecisionContext(server_id="web-01", drift={"severity": "info"}, runbooks=rbs)
    dm = RuleBasedDecisionMaker(min_confidence_for_execute=0.5)
    decision = asyncio.run(dm.decide(ctx))
    assert decision.action in ("ignore", "escalate")


def test_rule_based_escalates_on_warn_without_runbook():
    ctx = DecisionContext(server_id="web-01", drift={"severity": SEVERITY_WARN}, runbooks=[])
    dm = RuleBasedDecisionMaker()
    decision = asyncio.run(dm.decide(ctx))
    assert decision.action == "escalate"


def test_rule_based_ignores_on_info_without_runbook():
    ctx = DecisionContext(server_id="web-01", drift={"severity": SEVERITY_INFO}, runbooks=[])
    dm = RuleBasedDecisionMaker()
    decision = asyncio.run(dm.decide(ctx))
    assert decision.action == "ignore"


# --------------------------------------------------------------------
# Loop: policy gates
# --------------------------------------------------------------------


def test_loop_does_nothing_when_policy_disabled(tmp_path):
    _seed_drift(tmp_path)
    drifts = _list_open_drifts(tmp_path)
    loop = AdminAutonomyLoop(str(tmp_path), AutonomyPolicy(enabled=False))
    results = asyncio.run(loop.process_server_drifts("web-01", drifts))
    assert results == []


def test_loop_alarm_always_escalated_even_if_permitted(tmp_path):
    # даже если severity в auto_apply, alarm принудительно escalate
    policy = load_autonomy_policy({
        "autonomy": {
            "enabled": True,
            "auto_apply_severities": ["alarm", "info", "warn"],  # alarm присутствует
            "auto_exec_actions": ["x.y"],
        }
    })
    _write_runbook(
        tmp_path, rb_id="rb_alarm",
        tags=["network.listen"],
        auto_action={"action_id": "x.y", "target_hint": "local", "confidence": 0.9},
    )
    _seed_drift(tmp_path, check_id="network.listen", severity=SEVERITY_ALARM)
    drifts = _list_open_drifts(tmp_path)
    runner = _RunnerRecorder()
    loop = AdminAutonomyLoop(str(tmp_path), policy, action_runner=runner)
    results = asyncio.run(loop.process_server_drifts("web-01", drifts))
    assert len(results) == 1
    r = results[0]
    # runner не должен быть вызван — alarm никогда не auto-applies
    assert runner.calls == []
    assert r["decision"]["action"] == "escalate"
    assert r["gated_reason"] and "alarm" in r["gated_reason"].lower()


def test_loop_gates_action_not_in_allowlist(tmp_path):
    policy = load_autonomy_policy({
        "autonomy": {
            "enabled": True,
            "auto_apply_severities": ["info"],
            "auto_exec_actions": ["different.action"],  # runbook's action_id не здесь
        }
    })
    _write_runbook(
        tmp_path, rb_id="rb_y",
        tags=["os.kernel"],
        auto_action={"action_id": "systemd.reload", "target_hint": "local"},
    )
    _seed_drift(tmp_path)
    drifts = _list_open_drifts(tmp_path)
    runner = _RunnerRecorder()
    loop = AdminAutonomyLoop(str(tmp_path), policy, action_runner=runner)
    results = asyncio.run(loop.process_server_drifts("web-01", drifts))
    r = results[0]
    assert runner.calls == []
    assert r["decision"]["action"] == "escalate"
    assert "auto_exec_actions" in r["gated_reason"]


def test_loop_executes_when_everything_permits(tmp_path):
    policy = load_autonomy_policy({
        "autonomy": {
            "enabled": True,
            "auto_apply_severities": ["info"],
            "auto_exec_actions": ["systemd.reload"],
        }
    })
    _write_runbook(
        tmp_path, rb_id="rb_ok",
        tags=["os.kernel"],
        auto_action={"action_id": "systemd.reload", "target_hint": "local"},
    )
    _seed_drift(tmp_path)
    drifts = _list_open_drifts(tmp_path)
    runner = _RunnerRecorder(dry_success=True, real_success=True)
    loop = AdminAutonomyLoop(str(tmp_path), policy, action_runner=runner)
    results = asyncio.run(loop.process_server_drifts("web-01", drifts))
    assert len(results) == 1
    r = results[0]
    assert r["applied"] is True
    assert r["decision"]["action"] == "execute"
    # dry + real
    assert len(runner.calls) == 2
    assert runner.calls[0]["dry_run"] is True
    assert runner.calls[1]["dry_run"] is False


def test_loop_dry_run_failure_denies_real(tmp_path):
    policy = load_autonomy_policy({
        "autonomy": {
            "enabled": True,
            "auto_apply_severities": ["info"],
            "auto_exec_actions": ["systemd.reload"],
        }
    })
    _write_runbook(
        tmp_path, rb_id="rb_dry",
        tags=["os.kernel"],
        auto_action={"action_id": "systemd.reload", "target_hint": "local"},
    )
    _seed_drift(tmp_path)
    drifts = _list_open_drifts(tmp_path)
    runner = _RunnerRecorder(dry_success=False)
    loop = AdminAutonomyLoop(str(tmp_path), policy, action_runner=runner)
    results = asyncio.run(loop.process_server_drifts("web-01", drifts))
    r = results[0]
    assert r["applied"] is False
    # только dry-run был — real не вызвали
    assert len(runner.calls) == 1
    assert runner.calls[0]["dry_run"] is True


def test_loop_rate_limit_blocks(tmp_path):
    # pre-seed action window с полным лимитом
    policy = load_autonomy_policy({
        "autonomy": {
            "enabled": True,
            "auto_apply_severities": ["info"],
            "auto_exec_actions": ["systemd.reload"],
            "max_actions_per_hour": 1,
        }
    })
    _write_runbook(
        tmp_path, rb_id="rb_rate",
        tags=["os.kernel"],
        auto_action={"action_id": "systemd.reload", "target_hint": "local"},
    )
    _seed_drift(tmp_path)
    drifts = _list_open_drifts(tmp_path)

    # Полный лимит: один недавний action
    import json
    import time as _t
    store = AdminSnapshotStore.for_server(str(tmp_path), "web-01")
    store.set_meta("autonomy:recent_action_timestamps", json.dumps([_t.time()]))

    runner = _RunnerRecorder()
    loop = AdminAutonomyLoop(str(tmp_path), policy, action_runner=runner)
    results = asyncio.run(loop.process_server_drifts("web-01", drifts))
    r = results[0]
    assert runner.calls == []
    assert "rate-limited" in r["gated_reason"]


def test_loop_cooldown_blocks(tmp_path):
    import time as _t
    policy = load_autonomy_policy({
        "autonomy": {
            "enabled": True,
            "auto_apply_severities": ["info"],
            "auto_exec_actions": ["systemd.reload"],
            "cooldown_sec": 600,
        }
    })
    _write_runbook(
        tmp_path, rb_id="rb_cd",
        tags=["os.kernel"],
        auto_action={"action_id": "systemd.reload", "target_hint": "local"},
    )
    _seed_drift(tmp_path, check_id="os.kernel")
    store = AdminSnapshotStore.for_server(str(tmp_path), "web-01")
    store.set_meta("autonomy:last_action_ts:os.kernel", str(_t.time()))
    drifts = _list_open_drifts(tmp_path)
    runner = _RunnerRecorder()
    loop = AdminAutonomyLoop(str(tmp_path), policy, action_runner=runner)
    results = asyncio.run(loop.process_server_drifts("web-01", drifts))
    r = results[0]
    assert runner.calls == []
    assert "cooldown" in r["gated_reason"].lower()


def test_loop_audit_note_written_on_execute(tmp_path):
    policy = load_autonomy_policy({
        "autonomy": {
            "enabled": True,
            "auto_apply_severities": ["info"],
            "auto_exec_actions": ["systemd.reload"],
        }
    })
    _write_runbook(
        tmp_path, rb_id="rb_audit",
        tags=["os.kernel"],
        auto_action={"action_id": "systemd.reload", "target_hint": "local"},
    )
    _seed_drift(tmp_path)
    drifts = _list_open_drifts(tmp_path)
    runner = _RunnerRecorder()
    loop = AdminAutonomyLoop(str(tmp_path), policy, action_runner=runner)
    asyncio.run(loop.process_server_drifts("web-01", drifts))
    notes = ServerMemory(str(tmp_path), "web-01").get_notes()
    assert "AUTO-EXEC" in notes


# --------------------------------------------------------------------
# Baseline auto-accept
# --------------------------------------------------------------------


def test_baseline_auto_accept_counts_stable_scans(tmp_path):
    policy = load_autonomy_policy({
        "autonomy": {
            "enabled": True,
            "baseline_auto_accept": {"enabled": True, "after_stable_scans": 3},
        }
    })
    # создать baseline + proposed через повторное apply_scan_result
    apply_scan_result(str(tmp_path), "web-01", {"server_id": "web-01", "checks": {"a": 1}})
    apply_scan_result(str(tmp_path), "web-01", {"server_id": "web-01", "checks": {"a": 2}})
    assert load_proposed_baseline(str(tmp_path), "web-01") is not None

    loop = AdminAutonomyLoop(str(tmp_path), policy)
    # первые два чистых скана — не принять
    assert loop.maybe_auto_accept_baseline("web-01", drifts_this_tick=0) is None
    assert loop.maybe_auto_accept_baseline("web-01", drifts_this_tick=0) is None
    # третий — принимает
    accepted = loop.maybe_auto_accept_baseline("web-01", drifts_this_tick=0)
    assert accepted == "web-01"
    assert load_proposed_baseline(str(tmp_path), "web-01") is None
    # у baseline теперь должен быть a=2
    assert load_baseline(str(tmp_path), "web-01")["checks"]["a"] == 2


def test_baseline_auto_accept_resets_on_drift(tmp_path):
    policy = load_autonomy_policy({
        "autonomy": {
            "enabled": True,
            "baseline_auto_accept": {"enabled": True, "after_stable_scans": 2},
        }
    })
    apply_scan_result(str(tmp_path), "web-01", {"server_id": "web-01", "checks": {"a": 1}})
    apply_scan_result(str(tmp_path), "web-01", {"server_id": "web-01", "checks": {"a": 2}})

    loop = AdminAutonomyLoop(str(tmp_path), policy)
    loop.maybe_auto_accept_baseline("web-01", drifts_this_tick=0)  # +1
    # drift появился — счётчик сбрасывается
    loop.maybe_auto_accept_baseline("web-01", drifts_this_tick=1)
    # теперь ещё раз clean — счётчик с 0 идёт
    assert loop.maybe_auto_accept_baseline("web-01", drifts_this_tick=0) is None
    # нужен ещё один clean чтобы accept
    assert loop.maybe_auto_accept_baseline("web-01", drifts_this_tick=0) == "web-01"


def test_baseline_auto_accept_disabled_when_policy_off(tmp_path):
    policy = load_autonomy_policy({
        "autonomy": {
            "enabled": True,
            "baseline_auto_accept": {"enabled": False},
        }
    })
    apply_scan_result(str(tmp_path), "web-01", {"server_id": "web-01", "checks": {"a": 1}})
    apply_scan_result(str(tmp_path), "web-01", {"server_id": "web-01", "checks": {"a": 2}})
    loop = AdminAutonomyLoop(str(tmp_path), policy)
    for _ in range(10):
        assert loop.maybe_auto_accept_baseline("web-01", drifts_this_tick=0) is None


# --------------------------------------------------------------------
# Integration-ish: facade.run_autonomy_tick
# --------------------------------------------------------------------


def test_facade_autonomy_tick_disabled_returns_shape(tmp_path):
    from modes.admin.facade import AdminAutonomyService
    import yaml
    from modes.admin.snapshot_store import admin_root
    root = admin_root(str(tmp_path))
    root.mkdir(parents=True, exist_ok=True)
    (root / "config.yaml").write_text(
        yaml.safe_dump({"admin": {"autonomy": {"enabled": False}, "monitor": {"servers": []}}}),
        encoding="utf-8",
    )
    svc = AdminAutonomyService(str(tmp_path))
    result = asyncio.run(svc.run_autonomy_tick())
    assert result["policy_enabled"] is False
    assert "tick" in result
    assert result["decisions"] == []
