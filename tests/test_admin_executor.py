from __future__ import annotations

import asyncio
import importlib.util
import sys
import types
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
MODES_ROOT = REPO_ROOT / "modes"
ADMIN_ROOT = MODES_ROOT / "admin"

_modes_pkg = types.ModuleType("modes")
_modes_pkg.__path__ = [str(MODES_ROOT)]
sys.modules.setdefault("modes", _modes_pkg)

_admin_pkg = types.ModuleType("modes.admin")
_admin_pkg.__path__ = [str(ADMIN_ROOT)]
sys.modules.setdefault("modes.admin", _admin_pkg)

_TRANSPORTS_PATH = ADMIN_ROOT / "transports" / "__init__.py"
_TRANSPORTS_SPEC = importlib.util.spec_from_file_location("modes.admin.transports", _TRANSPORTS_PATH)
if _TRANSPORTS_SPEC is None or _TRANSPORTS_SPEC.loader is None:
    raise RuntimeError(f"failed to load admin transports module from {_TRANSPORTS_PATH}")
_TRANSPORTS_MODULE = importlib.util.module_from_spec(_TRANSPORTS_SPEC)
sys.modules[_TRANSPORTS_SPEC.name] = _TRANSPORTS_MODULE
_TRANSPORTS_SPEC.loader.exec_module(_TRANSPORTS_MODULE)

_EXECUTOR_PATH = ADMIN_ROOT / "executor.py"
_EXECUTOR_SPEC = importlib.util.spec_from_file_location("modes.admin.executor_test", _EXECUTOR_PATH)
if _EXECUTOR_SPEC is None or _EXECUTOR_SPEC.loader is None:
    raise RuntimeError(f"failed to load admin executor module from {_EXECUTOR_PATH}")
_EXECUTOR_MODULE = importlib.util.module_from_spec(_EXECUTOR_SPEC)
sys.modules[_EXECUTOR_SPEC.name] = _EXECUTOR_MODULE
_EXECUTOR_SPEC.loader.exec_module(_EXECUTOR_MODULE)

_STATE_STORE_PATH = ADMIN_ROOT / "state_store.py"
_STATE_STORE_SPEC = importlib.util.spec_from_file_location("modes.admin.state_store_test", _STATE_STORE_PATH)
if _STATE_STORE_SPEC is None or _STATE_STORE_SPEC.loader is None:
    raise RuntimeError(f"failed to load admin state_store module from {_STATE_STORE_PATH}")
_STATE_STORE_MODULE = importlib.util.module_from_spec(_STATE_STORE_SPEC)
sys.modules[_STATE_STORE_SPEC.name] = _STATE_STORE_MODULE
_STATE_STORE_SPEC.loader.exec_module(_STATE_STORE_MODULE)

AdminExecutor = _EXECUTOR_MODULE.AdminExecutor
AdminExecutionContext = _EXECUTOR_MODULE.AdminExecutionContext
AdminStateStore = _STATE_STORE_MODULE.AdminStateStore
LocalCommandResult = _TRANSPORTS_MODULE.LocalCommandResult
LocalCommandSpec = _TRANSPORTS_MODULE.LocalCommandSpec


class _FakeLocalTransport:
    def __init__(self) -> None:
        self.calls: list[LocalCommandSpec] = []

    async def run(self, spec: LocalCommandSpec) -> LocalCommandResult:
        self.calls.append(spec)
        return LocalCommandResult(
            action_id=str(spec.action_id or ""),
            returncode=0,
            stdout="REMEDIATION_OK\n",
            stderr="",
            timed_out=False,
            duration_ms=12,
        )


class _NeverSSHTransport:
    async def run(self, _spec):  # type: ignore[no-untyped-def]
        raise AssertionError("ssh transport must not be used in this test")


class _CountingLocalTransport:
    def __init__(self) -> None:
        self.calls: int = 0

    async def run(self, spec: LocalCommandSpec) -> LocalCommandResult:
        self.calls += 1
        return LocalCommandResult(
            action_id=str(spec.action_id or ""),
            returncode=0,
            stdout="EXEC_OK\n",
            stderr="",
            timed_out=False,
            duration_ms=7,
        )


def test_admin_executor_applies_notify_admin_decision_and_logs_action(tmp_path) -> None:
    async def _run() -> None:
        store = AdminStateStore(str(tmp_path / "state.json"))
        executor = AdminExecutor()
        decision = {
            "diagnosis": "unable_to_parse_llm_response",
            "confidence": "medium",
            "action": "notify_admin",
            "reason": "invalid_json_or_schema",
            "urgency": "warning",
        }

        result = await executor.apply_analyzer_decision(
            decision=decision,
            session_id="sess-admin-1",
            state_store=store,
        )

        assert result.success is True
        assert result.returncode == 0
        assert result.logged_action_id

        row = store.get_action(str(result.logged_action_id))
        assert row is not None
        payload = dict(row.get("payload") or {})
        assert payload.get("source") == "analyzer"
        assert dict(payload.get("decision") or {}).get("action") == "notify_admin"
        assert dict(payload.get("result") or {}).get("success") is True

    asyncio.run(_run())


def test_admin_executor_no_action_skips_action_and_incident_logs(tmp_path) -> None:
    async def _run() -> None:
        store = AdminStateStore(str(tmp_path / "state.json"))
        executor = AdminExecutor()
        decision = {
            "diagnosis": "healthy",
            "confidence": "high",
            "action": "no_action",
            "reason": "rule_engine:all_monitored_checks_ok",
            "urgency": "info",
        }

        result = await executor.apply_analyzer_decision(
            decision=decision,
            session_id="sess-admin-healthy",
            state_store=store,
        )

        assert result.success is True
        assert result.returncode == 0
        assert result.logged_action_id is None
        assert store.list_actions("sess-admin-healthy") == []
        assert store.list_incidents("sess-admin-healthy") == []

    asyncio.run(_run())


def test_admin_executor_persists_incident_and_alert_state_with_sla_metric(tmp_path) -> None:
    async def _run() -> None:
        store = AdminStateStore(str(tmp_path / "state.json"))
        executor = AdminExecutor()
        detected_at_ts = 1_000.0
        notified_at_ts = 1_010.0
        decision = {
            "diagnosis": "unable_to_parse_llm_response",
            "confidence": "medium",
            "action": "notify_admin",
            "reason": "invalid_json_or_schema",
            "urgency": "warning",
            "detected_at_ts": detected_at_ts,
        }

        result = await executor.apply_analyzer_decision(
            decision=decision,
            session_id="sess-admin-sla-1",
            state_store=store,
            now_ts=notified_at_ts,
        )

        assert result.success is True
        incidents = store.list_incidents("sess-admin-sla-1", limit=20)
        assert len(incidents) == 1
        incident = incidents[0]
        incident_payload = dict(incident.get("payload") or {})
        assert incident_payload.get("source") == "analyzer"
        assert dict(incident_payload.get("decision") or {}).get("action") == "notify_admin"
        alert_id = str(incident_payload.get("alert_id") or "")
        assert alert_id

        metrics = dict(incident_payload.get("notification_metrics") or {})
        assert float(metrics.get("detected_at_ts")) == detected_at_ts
        assert float(metrics.get("notified_at_ts")) == notified_at_ts
        assert float(metrics.get("delivery_latency_sec")) <= 15.0
        assert float(metrics.get("sla_target_sec")) == 15.0
        assert bool(metrics.get("sla_met")) is True

        alert_row = store.get_alert_state(alert_id)
        assert alert_row is not None
        alert_payload = dict(alert_row.get("payload") or {})
        assert str(alert_payload.get("incident_id") or "") == str(incident.get("incident_id") or "")
        assert bool(
            dict(alert_payload.get("notification_metrics") or {}).get("sla_met")
        ) is True

    asyncio.run(_run())


def test_admin_executor_applies_restart_decision_via_transport_and_logs_action(tmp_path) -> None:
    async def _run() -> None:
        store = AdminStateStore(str(tmp_path / "state.json"))
        executor = AdminExecutor()
        local_transport = _FakeLocalTransport()
        approval = {"question": "", "options": []}

        async def _ask_user(question: str, options: list[str]) -> str:
            approval["question"] = str(question or "")
            approval["options"] = list(options or [])
            return "✅ Подтвердить"

        local_spec = LocalCommandSpec(
            action_id="restart_php_fpm",
            argv=("systemctl", "restart", "php-fpm"),
            timeout_sec=10.0,
        )
        decision = {
            "diagnosis": "502_with_php_fpm_down",
            "confidence": "high",
            "action": "restart_php_fpm",
            "reason": "rule_engine:detected_502_and_php_fpm_down",
            "urgency": "critical",
        }

        result = await executor.apply_analyzer_decision(
            decision=decision,
            session_id="sess-admin-2",
            state_store=store,
            local_transport=local_transport,  # type: ignore[arg-type]
            ssh_transport=_NeverSSHTransport(),  # type: ignore[arg-type]
            local_spec=local_spec,
            ask_user=_ask_user,
        )

        assert result.success is True
        assert result.returncode == 0
        assert "RUN action: restart_php_fpm" in str(result.text or "")
        assert len(local_transport.calls) == 1
        assert local_transport.calls[0].action_id == "restart_php_fpm"
        assert approval["question"]
        assert approval["options"] == ["✅ Подтвердить", "⛔ Отклонить"]
        assert result.logged_action_id

        row = store.get_action(str(result.logged_action_id))
        assert row is not None
        payload = dict(row.get("payload") or {})
        assert dict(payload.get("decision") or {}).get("action") == "restart_php_fpm"
        assert dict(payload.get("result") or {}).get("returncode") == 0
        assert bool(dict(payload.get("decision") or {}).get("manual_approval", {}).get("approved")) is True

    asyncio.run(_run())


def test_admin_executor_blocks_risky_action_when_user_rejects_confirmation(tmp_path) -> None:
    async def _run() -> None:
        store = AdminStateStore(str(tmp_path / "state.json"))
        executor = AdminExecutor()
        local_transport = _CountingLocalTransport()
        asked = {"count": 0}

        async def _ask_user(_question: str, _options: list[str]) -> str:
            asked["count"] += 1
            return "⛔ Отклонить"

        result = await executor.apply_analyzer_decision(
            decision={
                "diagnosis": "502_with_php_fpm_down",
                "confidence": "high",
                "action": "restart_php_fpm",
                "reason": "rule_engine:detected_502_and_php_fpm_down",
                "urgency": "critical",
            },
            session_id="sess-admin-risk-1",
            state_store=store,
            local_transport=local_transport,  # type: ignore[arg-type]
            ssh_transport=_NeverSSHTransport(),  # type: ignore[arg-type]
            local_spec=LocalCommandSpec(
                action_id="restart_php_fpm",
                argv=("systemctl", "restart", "php-fpm"),
                timeout_sec=10.0,
            ),
            ask_user=_ask_user,
        )

        assert asked["count"] == 1
        assert result.success is False
        assert "Manual approval required" in str(result.text or "")
        assert local_transport.calls == 0
        assert result.logged_action_id

        row = store.get_action(str(result.logged_action_id))
        assert row is not None
        decision_payload = dict(dict(row.get("payload") or {}).get("decision") or {})
        approval_payload = dict(decision_payload.get("manual_approval") or {})
        assert approval_payload.get("reason") == "rejected_by_user"
        assert bool(approval_payload.get("approved")) is False

    asyncio.run(_run())


def test_admin_executor_requests_confirmation_for_low_confidence_decision(tmp_path) -> None:
    async def _run() -> None:
        store = AdminStateStore(str(tmp_path / "state.json"))
        executor = AdminExecutor()
        asked = {"count": 0, "question": "", "options": []}

        async def _ask_user(question: str, options: list[str]) -> str:
            asked["count"] += 1
            asked["question"] = str(question or "")
            asked["options"] = list(options or [])
            return "✅ Подтвердить"

        result = await executor.apply_analyzer_decision(
            decision={
                "diagnosis": "llm_uncertain_signal",
                "confidence": "low",
                "action": "notify_admin",
                "reason": "llm_fallback:low_confidence",
                "urgency": "warning",
            },
            session_id="sess-admin-low-confidence-1",
            state_store=store,
            ask_user=_ask_user,
        )

        assert asked["count"] == 1
        assert asked["question"]
        assert asked["options"] == ["✅ Подтвердить", "⛔ Отклонить"]
        assert result.success is True
        assert result.logged_action_id

        row = store.get_action(str(result.logged_action_id))
        assert row is not None
        decision_payload = dict(dict(row.get("payload") or {}).get("decision") or {})
        approval_payload = dict(decision_payload.get("manual_approval") or {})
        assert approval_payload.get("reason") == "approved_by_user"
        assert bool(approval_payload.get("approved")) is True
        assert "low_confidence" in list(approval_payload.get("triggers") or [])

    asyncio.run(_run())


def test_admin_executor_low_confidence_without_ask_user_uses_safe_default(tmp_path) -> None:
    async def _run() -> None:
        store = AdminStateStore(str(tmp_path / "state.json"))
        executor = AdminExecutor()

        result = await executor.apply_analyzer_decision(
            decision={
                "diagnosis": "llm_uncertain_signal",
                "confidence": "low",
                "action": "notify_admin",
                "reason": "llm_fallback:low_confidence",
                "urgency": "warning",
            },
            session_id="sess-admin-low-confidence-safe-default",
            state_store=store,
            ask_user=None,
        )

        assert result.success is False
        assert "Manual approval required: ask_user_unavailable" in str(result.text or "")
        assert result.logged_action_id

        row = store.get_action(str(result.logged_action_id))
        assert row is not None
        decision_payload = dict(dict(row.get("payload") or {}).get("decision") or {})
        approval_payload = dict(decision_payload.get("manual_approval") or {})
        assert approval_payload.get("reason") == "ask_user_unavailable"
        assert bool(approval_payload.get("approved")) is False

    asyncio.run(_run())


def test_admin_executor_requests_confirmation_for_signal_and_policy_conflicts(tmp_path) -> None:
    async def _run() -> None:
        store = AdminStateStore(str(tmp_path / "state.json"))
        executor = AdminExecutor()
        asked = {"count": 0}

        async def _ask_user(_question: str, _options: list[str]) -> str:
            asked["count"] += 1
            return "✅ Подтвердить"

        result = await executor.apply_analyzer_decision(
            decision={
                "diagnosis": "conflicting_signals_detected",
                "confidence": "medium",
                "action": "notify_admin",
                "reason": "policy_conflict_and_signal_conflict",
                "urgency": "warning",
                "signal_conflict": True,
                "policy_conflict": True,
            },
            session_id="sess-admin-conflict-1",
            state_store=store,
            ask_user=_ask_user,
        )

        assert asked["count"] == 1
        assert result.success is True
        assert result.logged_action_id

        row = store.get_action(str(result.logged_action_id))
        assert row is not None
        decision_payload = dict(dict(row.get("payload") or {}).get("decision") or {})
        approval_payload = dict(decision_payload.get("manual_approval") or {})
        triggers = list(approval_payload.get("triggers") or [])
        assert "signal_conflict" in triggers
        assert "policy_conflict" in triggers

    asyncio.run(_run())


def test_admin_executor_persists_override_and_skips_repeated_ask_user(tmp_path) -> None:
    async def _run() -> None:
        store = AdminStateStore(str(tmp_path / "state.json"))
        executor = AdminExecutor()
        ask_user_calls = {"count": 0}

        async def _ask_user(_question: str, _options: list[str]) -> str:
            ask_user_calls["count"] += 1
            return "✅ Подтвердить"

        decision = {
            "diagnosis": "502_with_php_fpm_down",
            "confidence": "high",
            "action": "restart_php_fpm",
            "reason": "rule_engine:detected_502_and_php_fpm_down",
            "urgency": "critical",
        }
        local_spec = LocalCommandSpec(
            action_id="restart_php_fpm",
            argv=("systemctl", "restart", "php-fpm"),
            timeout_sec=10.0,
        )

        first = await executor.apply_analyzer_decision(
            decision=decision,
            session_id="sess-admin-override-1",
            state_store=store,
            local_transport=_FakeLocalTransport(),  # type: ignore[arg-type]
            ssh_transport=_NeverSSHTransport(),  # type: ignore[arg-type]
            local_spec=local_spec,
            ask_user=_ask_user,
        )
        assert first.success is True
        assert ask_user_calls["count"] == 1
        first_row = store.get_action(str(first.logged_action_id))
        assert first_row is not None
        first_decision = dict(dict(first_row.get("payload") or {}).get("decision") or {})
        assert dict(first_decision.get("manual_approval") or {}).get("reason") == "approved_by_user"

        second = await executor.apply_analyzer_decision(
            decision=decision,
            session_id="sess-admin-override-1",
            state_store=store,
            local_transport=_FakeLocalTransport(),  # type: ignore[arg-type]
            ssh_transport=_NeverSSHTransport(),  # type: ignore[arg-type]
            local_spec=local_spec,
            ask_user=_ask_user,
        )
        assert second.success is True
        assert ask_user_calls["count"] == 1
        second_row = store.get_action(str(second.logged_action_id))
        assert second_row is not None
        second_decision = dict(dict(second_row.get("payload") or {}).get("decision") or {})
        manual = dict(second_decision.get("manual_approval") or {})
        assert manual.get("reason") == "approved_override_used"
        override_id = str(manual.get("override_id") or "")
        assert override_id
        override_row = store.get_approved_override(override_id)
        assert override_row is not None
        override_payload = dict(override_row.get("payload") or {})
        assert bool(override_payload.get("approved")) is True
        assert str(override_payload.get("hash") or "")

    asyncio.run(_run())


def test_admin_executor_override_hash_depends_on_action_parameters(tmp_path) -> None:
    async def _run() -> None:
        store = AdminStateStore(str(tmp_path / "state.json"))
        executor = AdminExecutor()
        ask_user_calls = {"count": 0}

        async def _ask_user(_question: str, _options: list[str]) -> str:
            ask_user_calls["count"] += 1
            return "✅ Подтвердить"

        decision = {
            "diagnosis": "manual_remediation_needed",
            "confidence": "high",
            "action": "restart_nginx",
            "reason": "rule_engine:risky_restart",
            "urgency": "critical",
        }
        first_spec = LocalCommandSpec(
            action_id="restart_nginx",
            argv=("systemctl", "restart", "nginx"),
            timeout_sec=5.0,
        )
        second_spec = LocalCommandSpec(
            action_id="restart_nginx",
            argv=("systemctl", "reload", "nginx"),
            timeout_sec=5.0,
        )

        first = await executor.apply_analyzer_decision(
            decision=decision,
            session_id="sess-admin-override-2",
            state_store=store,
            local_transport=_FakeLocalTransport(),  # type: ignore[arg-type]
            ssh_transport=_NeverSSHTransport(),  # type: ignore[arg-type]
            local_spec=first_spec,
            ask_user=_ask_user,
        )
        assert first.success is True
        first_override_ctx = executor._build_override_context(  # type: ignore[attr-defined]
            decision_payload=executor._normalize_analyzer_decision(decision),  # type: ignore[attr-defined]
            session_id="sess-admin-override-2",
            chat_id=0,
            local_spec=first_spec,
            ssh_spec=None,
        )
        first_override_id = str(first_override_ctx.get("override_id") or "")
        assert first_override_id

        second = await executor.apply_analyzer_decision(
            decision=decision,
            session_id="sess-admin-override-2",
            state_store=store,
            local_transport=_FakeLocalTransport(),  # type: ignore[arg-type]
            ssh_transport=_NeverSSHTransport(),  # type: ignore[arg-type]
            local_spec=second_spec,
            ask_user=_ask_user,
        )
        assert second.success is True
        second_override_ctx = executor._build_override_context(  # type: ignore[attr-defined]
            decision_payload=executor._normalize_analyzer_decision(decision),  # type: ignore[attr-defined]
            session_id="sess-admin-override-2",
            chat_id=0,
            local_spec=second_spec,
            ssh_spec=None,
        )
        second_override_id = str(second_override_ctx.get("override_id") or "")
        assert second_override_id
        assert second_override_id != first_override_id
        assert ask_user_calls["count"] == 2

    asyncio.run(_run())


def test_admin_executor_analyzer_decisions_isolated_between_sequential_runs(tmp_path) -> None:
    async def _run() -> None:
        store = AdminStateStore(str(tmp_path / "state.json"))
        executor = AdminExecutor()

        async def _ask_user(_question: str, _options: list[str]) -> str:
            return "✅ Подтвердить"

        first = await executor.apply_analyzer_decision(
            decision={
                "diagnosis": "postgresql_down",
                "confidence": "high",
                "action": "restart_postgresql",
                "reason": "rule_engine:postgresql_controlled_restart_required",
                "urgency": "critical",
            },
            session_id="sess-admin-3",
            state_store=store,
            local_transport=_FakeLocalTransport(),  # type: ignore[arg-type]
            ssh_transport=_NeverSSHTransport(),  # type: ignore[arg-type]
            local_spec=LocalCommandSpec(
                action_id="restart_postgresql",
                argv=("systemctl", "restart", "postgresql"),
                timeout_sec=10.0,
            ),
            ask_user=_ask_user,
        )
        second = await executor.apply_analyzer_decision(
            decision={
                "diagnosis": "unable_to_parse_llm_response",
                "confidence": "medium",
                "action": "notify_admin",
                "reason": "invalid_json_or_schema",
                "urgency": "warning",
            },
            session_id="sess-admin-3",
            state_store=store,
        )

        assert first.success is True
        assert second.success is True
        assert first.logged_action_id and second.logged_action_id
        assert first.logged_action_id != second.logged_action_id

        row_first = store.get_action(str(first.logged_action_id))
        row_second = store.get_action(str(second.logged_action_id))
        assert row_first is not None and row_second is not None
        assert dict(row_first.get("payload") or {}).get("decision", {}).get("action") == "restart_postgresql"
        assert dict(row_second.get("payload") or {}).get("decision", {}).get("action") == "notify_admin"

    asyncio.run(_run())


def test_admin_executor_dry_run_blocks_execution_and_logs_intent(tmp_path) -> None:
    async def _run() -> None:
        store = AdminStateStore(str(tmp_path / "state.json"))
        executor = AdminExecutor()
        local_transport = _CountingLocalTransport()
        context = AdminExecutionContext(
            command="run",
            action_id="restart_nginx",
            target="local",
            dry_run=True,
            check_only=False,
            session_id="sess-security-1",
            chat_id=1,
            flags={},
        )
        local_spec = LocalCommandSpec(
            action_id="restart_nginx",
            argv=("systemctl", "restart", "nginx"),
            timeout_sec=5.0,
        )

        result = await executor.execute(
            context=context,
            local_transport=local_transport,  # type: ignore[arg-type]
            ssh_transport=_NeverSSHTransport(),  # type: ignore[arg-type]
            local_spec=local_spec,
            state_store=store,
            now_ts=1_000.0,
        )

        assert result.success is True
        assert "DRY-RUN action: restart_nginx" in str(result.text or "")
        assert local_transport.calls == 0
        assert result.logged_action_id
        logged = store.get_action(str(result.logged_action_id))
        assert logged is not None
        payload = dict(logged.get("payload") or {})
        assert payload.get("source") == "executor"
        assert payload.get("event") == "dry_run_intent"

    asyncio.run(_run())


def test_admin_executor_rejects_action_in_cooldown(tmp_path) -> None:
    async def _run() -> None:
        store = AdminStateStore(str(tmp_path / "state.json"))
        executor = AdminExecutor()
        local_transport = _CountingLocalTransport()
        session_id = "sess-security-2"
        store.create_action(
            "seed-cooldown-1",
            session_id=session_id,
            chat_id=1,
            payload={
                "source": "executor",
                "event": "executed_success",
                "request": {"action_id": "restart_nginx"},
                "logged_at_ts": 995.0,
            },
        )
        context = AdminExecutionContext(
            command="run",
            action_id="restart_nginx",
            target="local",
            dry_run=False,
            check_only=False,
            session_id=session_id,
            chat_id=1,
            flags={"cooldown_sec": 10},
        )
        local_spec = LocalCommandSpec(
            action_id="restart_nginx",
            argv=("systemctl", "restart", "nginx"),
            timeout_sec=5.0,
        )

        result = await executor.execute(
            context=context,
            local_transport=local_transport,  # type: ignore[arg-type]
            ssh_transport=_NeverSSHTransport(),  # type: ignore[arg-type]
            local_spec=local_spec,
            state_store=store,
            now_ts=1_000.0,
        )

        assert result.success is False
        assert result.returncode == -1
        assert "SECURITY POLICY BLOCKED: cooldown" in str(result.text or "")
        assert local_transport.calls == 0

    asyncio.run(_run())


def test_admin_executor_rejects_action_when_rate_limit_exceeded(tmp_path) -> None:
    async def _run() -> None:
        store = AdminStateStore(str(tmp_path / "state.json"))
        executor = AdminExecutor()
        local_transport = _CountingLocalTransport()
        session_id = "sess-security-3"
        store.create_action(
            "seed-rate-1",
            session_id=session_id,
            chat_id=1,
            payload={
                "source": "executor",
                "event": "executed_success",
                "request": {"action_id": "restart_nginx"},
                "logged_at_ts": 998.0,
            },
        )
        store.create_action(
            "seed-rate-2",
            session_id=session_id,
            chat_id=1,
            payload={
                "source": "executor",
                "event": "executed_failed",
                "request": {"action_id": "restart_nginx"},
                "logged_at_ts": 999.0,
            },
        )
        context = AdminExecutionContext(
            command="run",
            action_id="restart_nginx",
            target="local",
            dry_run=False,
            check_only=False,
            session_id=session_id,
            chat_id=1,
            flags={"rate_limit_max": 2, "rate_limit_window_sec": 10},
        )
        local_spec = LocalCommandSpec(
            action_id="restart_nginx",
            argv=("systemctl", "restart", "nginx"),
            timeout_sec=5.0,
        )

        result = await executor.execute(
            context=context,
            local_transport=local_transport,  # type: ignore[arg-type]
            ssh_transport=_NeverSSHTransport(),  # type: ignore[arg-type]
            local_spec=local_spec,
            state_store=store,
            now_ts=1_000.0,
        )

        assert result.success is False
        assert result.returncode == -1
        assert "SECURITY POLICY BLOCKED: rate_limit" in str(result.text or "")
        assert local_transport.calls == 0

    asyncio.run(_run())


def test_admin_executor_requires_notify_before_restart_postgresql(tmp_path) -> None:
    async def _run() -> None:
        store = AdminStateStore(str(tmp_path / "state.json"))
        executor = AdminExecutor()
        local_transport = _CountingLocalTransport()
        context = AdminExecutionContext(
            command="run",
            action_id="restart_postgresql",
            target="local",
            dry_run=False,
            check_only=False,
            session_id="sess-security-4",
            chat_id=1,
            flags={"mandatory_notify_actions": ["restart_postgresql"]},
        )
        local_spec = LocalCommandSpec(
            action_id="restart_postgresql",
            argv=("systemctl", "restart", "postgresql"),
            timeout_sec=5.0,
        )

        blocked = await executor.execute(
            context=context,
            local_transport=local_transport,  # type: ignore[arg-type]
            ssh_transport=_NeverSSHTransport(),  # type: ignore[arg-type]
            local_spec=local_spec,
            state_store=store,
            now_ts=1_000.0,
        )
        assert blocked.success is False
        assert "SECURITY POLICY BLOCKED: mandatory_notify" in str(blocked.text or "")
        assert local_transport.calls == 0

        allowed = await executor.execute(
            context=AdminExecutionContext(
                command="run",
                action_id="restart_postgresql",
                target="local",
                dry_run=False,
                check_only=False,
                session_id="sess-security-4",
                chat_id=1,
                flags={
                    "mandatory_notify_actions": ["restart_postgresql"],
                    "notify_sent": True,
                },
            ),
            local_transport=local_transport,  # type: ignore[arg-type]
            ssh_transport=_NeverSSHTransport(),  # type: ignore[arg-type]
            local_spec=local_spec,
            state_store=store,
            now_ts=1_001.0,
        )
        assert allowed.success is True
        assert local_transport.calls == 1

    asyncio.run(_run())


def test_admin_executor_rejects_action_outside_maintenance_window(tmp_path) -> None:
    async def _run() -> None:
        executor = AdminExecutor()
        local_transport = _CountingLocalTransport()
        context = AdminExecutionContext(
            command="run",
            action_id="restart_nginx",
            target="local",
            dry_run=False,
            check_only=False,
            session_id="sess-security-6",
            chat_id=1,
            flags={
                "maintenance_window": {
                    "enabled": True,
                    "start_ts": 2_000.0,
                    "end_ts": 3_000.0,
                    "actions": ["restart_nginx"],
                }
            },
        )
        local_spec = LocalCommandSpec(
            action_id="restart_nginx",
            argv=("systemctl", "restart", "nginx"),
            timeout_sec=5.0,
        )

        result = await executor.execute(
            context=context,
            local_transport=local_transport,  # type: ignore[arg-type]
            ssh_transport=_NeverSSHTransport(),  # type: ignore[arg-type]
            local_spec=local_spec,
            state_store=None,
            now_ts=1_000.0,
        )

        assert result.success is False
        assert "SECURITY POLICY BLOCKED: maintenance_window" in str(result.text or "")
        assert local_transport.calls == 0

    asyncio.run(_run())


def test_admin_executor_does_not_duplicate_allowlist_validation(tmp_path) -> None:
    async def _run() -> None:
        executor = AdminExecutor()
        local_transport = _CountingLocalTransport()
        context = AdminExecutionContext(
            command="run",
            action_id="action_not_checked_by_allowlist_in_executor",
            target="local",
            dry_run=False,
            check_only=False,
            session_id="sess-security-5",
            chat_id=1,
            flags={},
        )
        local_spec = LocalCommandSpec(
            action_id="action_not_checked_by_allowlist_in_executor",
            argv=("echo", "ok"),
            timeout_sec=5.0,
        )

        result = await executor.execute(
            context=context,
            local_transport=local_transport,  # type: ignore[arg-type]
            ssh_transport=_NeverSSHTransport(),  # type: ignore[arg-type]
            local_spec=local_spec,
            state_store=None,
            now_ts=1_000.0,
        )

        assert result.success is True
        assert local_transport.calls == 1

    asyncio.run(_run())
