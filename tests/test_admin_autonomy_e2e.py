"""End-to-end проверка полного autonomy-тика через AdminAutonomyService.

Сценарий:
    1. Сконфигурировать admin с одним сервером и включённой автономией.
    2. Подменить scanner так, чтобы первый скан зафиксировал baseline,
       а второй — породил drift по известному check_id.
    3. Настроить runbook с auto_action, разрешённый в allowlist политики.
    4. Вызвать facade.run_autonomy_tick() дважды.
    5. Убедиться, что loop выполнил action и записал observability-счётчики.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from modes.admin.autonomy_loop import (
    LLMDecisionMaker,
    RuleBasedDecisionMaker,
    AutonomyDecision,
    DecisionContext,
)
from modes.admin.baseline import AdminBaselineScanner, ServerSpec
from modes.admin.facade import AdminAutonomyService
from modes.admin.reconciliation import AdminReconciler
from modes.admin.runbooks import global_runbooks_dir
from modes.admin.snapshot_store import AdminSnapshotStore, admin_root


class _StubScanner(AdminBaselineScanner):
    """Скан-стаб: отдаёт заранее подготовленные профили по очереди."""

    def __init__(self, profiles: List[Dict[str, Any]]) -> None:
        # не вызываем super().__init__ — не нужны реальные транспорты
        self._profiles = list(profiles)
        self._idx = 0
        self._checks = []

    async def scan(self, server: ServerSpec) -> Dict[str, Any]:
        if self._idx >= len(self._profiles):
            profile = self._profiles[-1]
        else:
            profile = self._profiles[self._idx]
            self._idx += 1
        return {
            "server_id": server.server_id,
            "scanned_at": "now",
            "checks": dict(profile),
            "errors": {},
        }


class _FakeToolResult:
    def __init__(self, *, success: bool, action_id: str, dry_run: bool) -> None:
        self._d = {
            "success": success,
            "action_id": action_id,
            "dry_run": dry_run,
            "applied": not dry_run and success,
            "exit_code": 0 if success else 1,
            "stdout": "ok" if success else "",
            "stderr": "",
            "duration_ms": 1,
            "command_preview": f"fake {action_id}",
            "timed_out": False,
            "error": None,
            "target": "local",
        }

    def to_dict(self) -> Dict[str, Any]:
        return dict(self._d)


class _ActionRecorder:
    def __init__(self) -> None:
        self.calls: List[Dict[str, Any]] = []

    async def __call__(
        self, *, workdir: str, action_id: str,
        server_id: Optional[str] = None, target_hint: Optional[str] = None,
        dry_run: bool = True, **kwargs: Any,
    ) -> _FakeToolResult:
        self.calls.append({
            "action_id": action_id,
            "server_id": server_id,
            "target_hint": target_hint,
            "dry_run": dry_run,
        })
        return _FakeToolResult(success=True, action_id=action_id, dry_run=dry_run)


def _write_admin_config(workdir: Path, *, autonomy: Dict[str, Any]) -> None:
    root = admin_root(str(workdir))
    root.mkdir(parents=True, exist_ok=True)
    cfg = {
        "admin": {
            "monitor": {
                "servers": [{"server_id": "web-01", "transport": "local"}],
            },
            "autonomy": autonomy,
        }
    }
    (root / "config.yaml").write_text(yaml.safe_dump(cfg), encoding="utf-8")


def _write_runbook(workdir: Path, *, rb_id: str, tags: List[str], auto_action: Dict[str, Any]) -> None:
    d = global_runbooks_dir(str(workdir))
    d.mkdir(parents=True, exist_ok=True)
    front = [f"id: {rb_id}", f"title: {rb_id}", f"tags: {tags}"]
    front.append("auto_action:")
    for k, v in auto_action.items():
        front.append(f"  {k}: {v}")
    body = "---\n" + "\n".join(front) + "\n---\n# body\n"
    (d / f"{rb_id}.md").write_text(body, encoding="utf-8")


def _make_service(
    tmp_path: Path,
    *,
    scanner: _StubScanner,
) -> AdminAutonomyService:
    def _factory() -> AdminReconciler:
        return AdminReconciler(str(tmp_path), scanner=scanner)

    return AdminAutonomyService(str(tmp_path), reconciler_factory=_factory)


def test_e2e_autonomy_tick_executes_action_and_updates_counters(tmp_path):
    """Полный цикл: baseline → drift → runbook match → execute → counters."""
    _write_admin_config(
        tmp_path,
        autonomy={
            "enabled": True,
            "auto_apply_severities": ["info", "warn"],
            "auto_exec_actions": ["systemd.reload"],
            "max_actions_per_hour": 10,
            "cooldown_sec": 1,
        },
    )
    _write_runbook(
        tmp_path,
        rb_id="rb_kernel",
        tags=["os.kernel"],
        auto_action={"action_id": "systemd.reload", "target_hint": "local", "confidence": 0.9},
    )

    # Первый скан → baseline, второй → drift по os.kernel
    scanner = _StubScanner([
        {"os.kernel": "6.8"},
        {"os.kernel": "6.9"},
    ])
    runner = _ActionRecorder()
    svc = _make_service(tmp_path, scanner=scanner)

    # Первый tick: создаст baseline, drift'ов ещё нет.
    r1 = asyncio.run(svc.run_autonomy_tick(action_runner=runner))
    assert r1["policy_enabled"] is True
    assert r1["decisions"] == []

    # Второй tick: появится drift, loop должен выполнить action.
    r2 = asyncio.run(svc.run_autonomy_tick(action_runner=runner))
    assert r2["policy_enabled"] is True
    assert len(r2["decisions"]) >= 1
    executed = [d for d in r2["decisions"] if d["applied"]]
    assert len(executed) == 1, r2["decisions"]
    assert executed[0]["decision"]["action_id"] == "systemd.reload"

    # dry + real = 2 вызова.
    assert len(runner.calls) == 2
    assert runner.calls[0]["dry_run"] is True
    assert runner.calls[1]["dry_run"] is False

    # Observability: счётчики записаны.
    status = svc.autonomy_status()
    assert status["policy"]["enabled"] is True
    counters = status["counters"]
    assert counters["tick_count"] >= 2
    assert counters["actions_executed_total"] >= 1
    assert counters["last_tick_ts"] > 0


def test_e2e_autonomy_tick_respects_allowlist_and_escalates(tmp_path):
    """auto_exec_actions не содержит action_id runbook'а → escalate, runner не вызывается."""
    _write_admin_config(
        tmp_path,
        autonomy={
            "enabled": True,
            "auto_apply_severities": ["info", "warn"],
            "auto_exec_actions": ["different.action"],
        },
    )
    _write_runbook(
        tmp_path,
        rb_id="rb_block",
        tags=["os.kernel"],
        auto_action={"action_id": "systemd.reload", "target_hint": "local", "confidence": 0.9},
    )
    scanner = _StubScanner([
        {"os.kernel": "6.8"},
        {"os.kernel": "6.9"},
    ])
    runner = _ActionRecorder()
    svc = _make_service(tmp_path, scanner=scanner)

    asyncio.run(svc.run_autonomy_tick(action_runner=runner))
    r2 = asyncio.run(svc.run_autonomy_tick(action_runner=runner))
    assert len(r2["decisions"]) >= 1
    # Runner ни разу не должен быть вызван — action не в allowlist
    assert runner.calls == []
    denied = [d for d in r2["decisions"] if d.get("gated_reason") and "auto_exec_actions" in d["gated_reason"]]
    assert denied, r2["decisions"]

    status = svc.autonomy_status()
    assert status["counters"]["escalations_total"] >= 1
    assert status["counters"]["denied_by_policy_total"] >= 1


def test_e2e_autonomy_disabled_returns_shape_and_does_nothing(tmp_path):
    """enabled=false → loop не стартует, но tick возвращает разумный shape."""
    _write_admin_config(tmp_path, autonomy={"enabled": False})
    scanner = _StubScanner([{"os.kernel": "6.8"}, {"os.kernel": "6.9"}])
    runner = _ActionRecorder()
    svc = _make_service(tmp_path, scanner=scanner)

    asyncio.run(svc.run_autonomy_tick(action_runner=runner))
    r = asyncio.run(svc.run_autonomy_tick(action_runner=runner))
    assert r["policy_enabled"] is False
    assert r["decisions"] == []
    assert runner.calls == []


def test_e2e_autonomy_disabled_updates_last_tick_ts_but_not_count(tmp_path):
    """При policy.enabled=False — last_tick_ts обновляется (scan состоялся),
    tick_count НЕ инкрементируется (autonomy loop не запускался)."""
    _write_admin_config(tmp_path, autonomy={"enabled": False})
    scanner = _StubScanner([{"os.kernel": "6.8"}, {"os.kernel": "6.9"}])
    svc = _make_service(tmp_path, scanner=scanner)

    before = svc.autonomy_status()["counters"]
    assert before["last_tick_ts"] == 0
    assert before["tick_count"] == 0

    asyncio.run(svc.run_autonomy_tick())

    after = svc.autonomy_status()["counters"]
    assert after["last_tick_ts"] > 0, "last_tick_ts должен обновляться даже при disabled policy"
    assert after["tick_count"] == 0, "tick_count должен инкрементироваться только при реальном прогоне loop"


# --------------------------------------------------------------------
# LLMDecisionMaker unit tests (лёгкие, без реального LLM).
# --------------------------------------------------------------------


def test_llm_decision_maker_parses_valid_response():
    async def caller(_prompt: str) -> str:
        return (
            '{"action":"execute","action_id":"systemd.reload","target_hint":"local",'
            '"runbook_id":"rb1","reason":"match","confidence":0.85}'
        )

    dm = LLMDecisionMaker(caller, fallback=RuleBasedDecisionMaker())
    ctx = DecisionContext(server_id="web-01", drift={"severity": "info"}, runbooks=[])
    decision = asyncio.run(dm.decide(ctx))
    assert decision.action == "execute"
    assert decision.action_id == "systemd.reload"
    assert decision.runbook_id == "rb1"
    assert 0.0 <= decision.confidence <= 1.0


def test_llm_decision_maker_falls_back_on_timeout():
    async def caller(_prompt: str) -> str:
        await asyncio.sleep(5)
        return "{}"

    rb = RuleBasedDecisionMaker()
    dm = LLMDecisionMaker(caller, timeout_sec=0.05, fallback=rb)
    ctx = DecisionContext(server_id="web-01", drift={"severity": "warn"}, runbooks=[])
    decision = asyncio.run(dm.decide(ctx))
    # Rule-based на warn без runbook'а даёт escalate.
    assert decision.action == "escalate"


def test_llm_decision_maker_falls_back_on_non_json_response():
    async def caller(_prompt: str) -> str:
        return "this is not json"

    dm = LLMDecisionMaker(caller, fallback=RuleBasedDecisionMaker())
    ctx = DecisionContext(server_id="web-01", drift={"severity": "info"}, runbooks=[])
    decision = asyncio.run(dm.decide(ctx))
    # Rule-based на info без runbook'а даёт ignore.
    assert decision.action == "ignore"


def test_llm_decision_maker_strips_code_fences():
    async def caller(_prompt: str) -> str:
        return '```json\n{"action":"ignore","reason":"stub"}\n```'

    dm = LLMDecisionMaker(caller, fallback=RuleBasedDecisionMaker())
    ctx = DecisionContext(server_id="web-01", drift={"severity": "info"}, runbooks=[])
    decision = asyncio.run(dm.decide(ctx))
    assert decision.action == "ignore"


def test_llm_decision_maker_coerces_execute_without_action_id_to_escalate():
    """Defense-in-depth: execute без action_id сразу вырождается в escalate."""

    async def caller(_prompt: str) -> str:
        return '{"action":"execute","reason":"no action_id provided"}'

    dm = LLMDecisionMaker(caller, fallback=RuleBasedDecisionMaker())
    ctx = DecisionContext(server_id="web-01", drift={"severity": "info"}, runbooks=[])
    decision = asyncio.run(dm.decide(ctx))
    assert decision.action == "escalate"
    assert decision.action_id is None
    assert "coerced" in decision.reason


def test_e2e_llm_execute_on_alarm_is_gated(tmp_path):
    """Даже если LLM говорит execute на alarm, policy-gate обязан escalate'ить."""
    from modes.admin.snapshot_store import SEVERITY_ALARM
    from modes.admin.autonomy_loop import AdminAutonomyLoop
    from modes.admin.autonomy_policy import load_autonomy_policy

    policy = load_autonomy_policy({
        "autonomy": {
            "enabled": True,
            # даже если явно прописан alarm и action в allowlist — не должен выполниться
            "auto_apply_severities": ["alarm", "info", "warn"],
            "auto_exec_actions": ["systemd.reload"],
        }
    })

    # LLM "зловредный" — настаивает на execute alarm
    class _HostileLLM:
        async def decide(self, ctx):
            return AutonomyDecision(
                action="execute", action_id="systemd.reload",
                reason="llm insists", confidence=1.0, target_hint="local",
            )

    store = AdminSnapshotStore.for_server(str(tmp_path), "web-01")
    store.insert_drift(
        check_id="network.listen", severity=SEVERITY_ALARM,
        new_value="9999", prev_value="8080", details={"kind": "change"},
    )
    runner = _ActionRecorder()
    loop = AdminAutonomyLoop(
        str(tmp_path), policy, decision_maker=_HostileLLM(), action_runner=runner,
    )
    drifts = store.list_drifts(limit=5, include_acknowledged=False)
    results = asyncio.run(loop.process_server_drifts("web-01", drifts))
    assert runner.calls == []  # alarm НИКОГДА не выполняется
    assert len(results) == 1
    assert results[0]["decision"]["action"] == "escalate"
    assert "alarm" in results[0]["gated_reason"].lower()
