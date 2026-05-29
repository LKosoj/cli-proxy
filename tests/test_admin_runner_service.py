from __future__ import annotations

import asyncio
import importlib.util
import sys
import types
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
MODES_ROOT = REPO_ROOT / "modes"
ADMIN_ROOT = MODES_ROOT / "admin"
SDK_ROOT = MODES_ROOT / "sdk"
SDK_RUNTIME_ROOT = SDK_ROOT / "runtime"

_modes_pkg = types.ModuleType("modes")
_modes_pkg.__path__ = [str(MODES_ROOT)]
sys.modules.setdefault("modes", _modes_pkg)

_admin_pkg = types.ModuleType("modes.admin")
_admin_pkg.__path__ = [str(ADMIN_ROOT)]
sys.modules.setdefault("modes.admin", _admin_pkg)

_sdk_pkg = types.ModuleType("modes.sdk")
_sdk_pkg.__path__ = [str(SDK_ROOT)]
sys.modules.setdefault("modes.sdk", _sdk_pkg)

_sdk_runtime_pkg = types.ModuleType("modes.sdk.runtime")
_sdk_runtime_pkg.__path__ = [str(SDK_RUNTIME_ROOT)]
sys.modules.setdefault("modes.sdk.runtime", _sdk_runtime_pkg)

_RUNNER_PATH = ADMIN_ROOT / "runner_service.py"
_RUNNER_SPEC = importlib.util.spec_from_file_location("modes.admin.runner_service_test", _RUNNER_PATH)
if _RUNNER_SPEC is None or _RUNNER_SPEC.loader is None:
    raise RuntimeError(f"failed to load admin runner module from {_RUNNER_PATH}")
_RUNNER_MODULE = importlib.util.module_from_spec(_RUNNER_SPEC)
sys.modules[_RUNNER_SPEC.name] = _RUNNER_MODULE
_RUNNER_SPEC.loader.exec_module(_RUNNER_MODULE)
AdminModeRunnerService = _RUNNER_MODULE.AdminModeRunnerService
AdminExecutorNotifierStepResult = _RUNNER_MODULE.AdminExecutorNotifierStepResult

_ANALYZER_PATH = ADMIN_ROOT / "analyzer.py"
_ANALYZER_SPEC = importlib.util.spec_from_file_location("modes.admin.analyzer_test", _ANALYZER_PATH)
if _ANALYZER_SPEC is None or _ANALYZER_SPEC.loader is None:
    raise RuntimeError(f"failed to load admin analyzer module from {_ANALYZER_PATH}")
_ANALYZER_MODULE = importlib.util.module_from_spec(_ANALYZER_SPEC)
sys.modules[_ANALYZER_SPEC.name] = _ANALYZER_MODULE
_ANALYZER_SPEC.loader.exec_module(_ANALYZER_MODULE)
AdminAnalyzer = _ANALYZER_MODULE.AdminAnalyzer

_EXECUTOR_PATH = ADMIN_ROOT / "executor.py"
_EXECUTOR_SPEC = importlib.util.spec_from_file_location("modes.admin.executor_test", _EXECUTOR_PATH)
if _EXECUTOR_SPEC is None or _EXECUTOR_SPEC.loader is None:
    raise RuntimeError(f"failed to load admin executor module from {_EXECUTOR_PATH}")
_EXECUTOR_MODULE = importlib.util.module_from_spec(_EXECUTOR_SPEC)
sys.modules[_EXECUTOR_SPEC.name] = _EXECUTOR_MODULE
_EXECUTOR_SPEC.loader.exec_module(_EXECUTOR_MODULE)
AdminExecutor = _EXECUTOR_MODULE.AdminExecutor
AdminExecutionResult = _EXECUTOR_MODULE.AdminExecutionResult
LocalCommandSpec = _EXECUTOR_MODULE.LocalCommandSpec

_MONITOR_PATH = ADMIN_ROOT / "monitor.py"
_MONITOR_SPEC = importlib.util.spec_from_file_location("modes.admin.monitor_test", _MONITOR_PATH)
if _MONITOR_SPEC is None or _MONITOR_SPEC.loader is None:
    raise RuntimeError(f"failed to load admin monitor module from {_MONITOR_PATH}")
_MONITOR_MODULE = importlib.util.module_from_spec(_MONITOR_SPEC)
sys.modules[_MONITOR_SPEC.name] = _MONITOR_MODULE
_MONITOR_SPEC.loader.exec_module(_MONITOR_MODULE)
AdminMonitor = _MONITOR_MODULE.AdminMonitor
AdminMonitorSnapshot = _MONITOR_MODULE.AdminMonitorSnapshot
AdminServerSnapshot = _MONITOR_MODULE.AdminServerSnapshot

from modes.admin.snapshot_store import AdminSnapshotStore  # noqa: E402

_STATE_STORE_PATH = ADMIN_ROOT / "state_store.py"
_STATE_STORE_SPEC = importlib.util.spec_from_file_location("modes.admin.state_store_test", _STATE_STORE_PATH)
if _STATE_STORE_SPEC is None or _STATE_STORE_SPEC.loader is None:
    raise RuntimeError(f"failed to load admin state_store module from {_STATE_STORE_PATH}")
_STATE_STORE_MODULE = importlib.util.module_from_spec(_STATE_STORE_SPEC)
sys.modules[_STATE_STORE_SPEC.name] = _STATE_STORE_MODULE
_STATE_STORE_SPEC.loader.exec_module(_STATE_STORE_MODULE)
AdminStateStore = _STATE_STORE_MODULE.AdminStateStore

_TRANSPORTS_PATH = ADMIN_ROOT / "transports" / "__init__.py"
_TRANSPORTS_SPEC = importlib.util.spec_from_file_location("modes.admin.transports", _TRANSPORTS_PATH)
if _TRANSPORTS_SPEC is None or _TRANSPORTS_SPEC.loader is None:
    raise RuntimeError(f"failed to load admin transports module from {_TRANSPORTS_PATH}")
_TRANSPORTS_MODULE = importlib.util.module_from_spec(_TRANSPORTS_SPEC)
sys.modules[_TRANSPORTS_SPEC.name] = _TRANSPORTS_MODULE
_TRANSPORTS_SPEC.loader.exec_module(_TRANSPORTS_MODULE)
LocalCommandResult = _TRANSPORTS_MODULE.LocalCommandResult
LocalTransportError = _TRANSPORTS_MODULE.LocalTransportError
SSHCommandResult = _TRANSPORTS_MODULE.SSHCommandResult
SSHCommandSpec = _TRANSPORTS_MODULE.SSHCommandSpec


class _FakeMonitor:
    def __init__(self, snapshots: list[Any]) -> None:
        self._snapshots = list(snapshots)
        self.calls: list[dict[str, Any]] = []

    async def collect_snapshot(self, *, config_payload, session_workdir):  # type: ignore[no-untyped-def]
        self.calls.append(
            {
                "config_payload": dict(config_payload or {}),
                "session_workdir": str(session_workdir or ""),
            }
        )
        if not self._snapshots:
            return {"servers": []}
        return self._snapshots.pop(0)


class _FakeAnalyzer:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def analyze(self, *, snapshot, llm_output, cli_output=""):  # type: ignore[no-untyped-def]
        self.calls.append(
            {
                "snapshot": snapshot,
                "llm_output": str(llm_output or ""),
                "cli_output": str(cli_output or ""),
            }
        )
        return {
            "diagnosis": f"snapshot:{str(snapshot.get('id') if isinstance(snapshot, dict) else '')}",
            "confidence": "high",
            "action": "notify_admin",
            "reason": f"seen:{str(llm_output or '')}",
            "urgency": "warning",
        }


class _FakeExecutor:
    def __init__(self, *, call_log: list[str]) -> None:
        self.calls: list[dict[str, Any]] = []
        self._call_log = call_log

    async def apply_analyzer_decision(self, **kwargs):  # type: ignore[no-untyped-def]
        self._call_log.append("executor")
        self.calls.append(dict(kwargs))
        return AdminExecutionResult(
            success=True,
            text="executed",
            returncode=0,
            logged_action_id="analyzer:session-x:notify_admin:1",
        )


class _FakeNotifier:
    def __init__(self, *, call_log: list[str]) -> None:
        self.calls: list[dict[str, Any]] = []
        self._call_log = call_log

    async def notify_action(self, **kwargs):  # type: ignore[no-untyped-def]
        self._call_log.append("notifier_action")
        self.calls.append({"kind": "action", **dict(kwargs)})
        return types.SimpleNamespace(sent=True, muted=False, text="action-notified")

    async def notify_incident(self, **kwargs):  # type: ignore[no-untyped-def]
        self._call_log.append("notifier_incident")
        self.calls.append({"kind": "incident", **dict(kwargs)})
        return types.SimpleNamespace(sent=True, muted=False, text="incident-notified")


class _FakeStateStore:
    def __init__(self) -> None:
        self.action_row = {
            "action_id": "analyzer:session-x:notify_admin:1",
            "session_id": "session-x",
            "payload": {"incident_refs": {"incident_id": "incident:session-x:notify_admin:1"}},
        }
        self.incident_row = {
            "incident_id": "incident:session-x:notify_admin:1",
            "session_id": "session-x",
            "payload": {"decision": {"action": "notify_admin"}},
        }
        self.calls: list[dict[str, Any]] = []

    def get_action(self, action_id: str, *, chat_id: int | None = None):  # type: ignore[no-untyped-def]
        _ = chat_id
        self.calls.append({"fn": "get_action", "action_id": str(action_id or "")})
        return dict(self.action_row)

    def get_incident(self, incident_id: str, *, chat_id: int | None = None):  # type: ignore[no-untyped-def]
        _ = chat_id
        self.calls.append({"fn": "get_incident", "incident_id": str(incident_id or "")})
        return dict(self.incident_row)


class _FakeMessaging:
    async def send_text(self, chat_id: int, text: str, *, md2: bool = True, **kwargs):  # type: ignore[no-untyped-def]
        _ = chat_id, text, md2, kwargs
        return None


class _RecordingMessaging:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    async def send_text(self, chat_id: int, text: str, *, md2: bool = True, **kwargs):  # type: ignore[no-untyped-def]
        self.sent.append(
            {
                "chat_id": int(chat_id),
                "text": str(text or ""),
                "md2": bool(md2),
                "kwargs": dict(kwargs or {}),
            }
        )
        return None


class _MonitorE2ELocalTransport:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def run(self, spec):  # type: ignore[no-untyped-def]
        action_id = str(getattr(spec, "action_id", "") or "")
        self.calls.append(action_id)
        if action_id == "probe_fail_local":
            raise LocalTransportError("local monitor transport failed")
        if action_id == "probe_http_502":
            return LocalCommandResult(
                action_id=action_id,
                returncode=0,
                stdout='{"http_status": 502}',
                stderr="",
                timed_out=False,
                duration_ms=5,
            )
        if action_id == "probe_php_fpm_down":
            return LocalCommandResult(
                action_id=action_id,
                returncode=0,
                stdout='{"php_fpm_state": "down"}',
                stderr="",
                timed_out=False,
                duration_ms=6,
            )
        if action_id == "probe_postgresql_down":
            return LocalCommandResult(
                action_id=action_id,
                returncode=0,
                stdout='{"postgresql_state": "down"}',
                stderr="",
                timed_out=False,
                duration_ms=4,
            )
        return LocalCommandResult(
            action_id=action_id,
            returncode=0,
            stdout='{"ok": true}',
            stderr="",
            timed_out=False,
            duration_ms=3,
        )


class _ExecutorLocalTransport:
    def __init__(self) -> None:
        self.calls: list[Any] = []

    async def run(self, spec):  # type: ignore[no-untyped-def]
        self.calls.append(spec)
        return LocalCommandResult(
            action_id=str(getattr(spec, "action_id", "") or ""),
            returncode=0,
            stdout="RESTART_OK\n",
            stderr="",
            timed_out=False,
            duration_ms=12,
        )


class _NeverLocalTransport:
    def __init__(self) -> None:
        self.calls: list[Any] = []

    async def run(self, spec):  # type: ignore[no-untyped-def]
        self.calls.append(spec)
        raise AssertionError("local transport must not be used in this test")


class _PinnedSSHTransport:
    def __init__(self) -> None:
        self.calls: list[Any] = []

    async def run(self, spec):  # type: ignore[no-untyped-def]
        self.calls.append(spec)
        action_id = str(getattr(spec, "action_id", "") or "")
        stdout = '{"http_status": 502}' if action_id == "probe_http_502" else "RESTART_OK\n"
        return SSHCommandResult(
            action_id=action_id,
            host=str(getattr(spec, "host", "") or ""),
            user=getattr(spec, "user", None),
            port=int(getattr(spec, "port", 22) or 22),
            returncode=0,
            stdout=stdout,
            stderr="",
            timed_out=False,
            duration_ms=9,
        )


class _CliStepLocalTransport:
    def __init__(self, *, stdout: str = "", stderr: str = "", returncode: int = 0) -> None:
        self.calls: list[Any] = []
        self._stdout = str(stdout or "")
        self._stderr = str(stderr or "")
        self._returncode = int(returncode)

    async def run(self, spec):  # type: ignore[no-untyped-def]
        self.calls.append(spec)
        return LocalCommandResult(
            action_id=str(getattr(spec, "action_id", "") or ""),
            returncode=self._returncode,
            stdout=self._stdout,
            stderr=self._stderr,
            timed_out=False,
            duration_ms=7,
        )


class _NeverSSHTransport:
    async def run(self, _spec):  # type: ignore[no-untyped-def]
        raise AssertionError("ssh transport must not be used in this test")


def _runner_admin_config(
    *,
    remediation_actions: tuple[str, ...] = (),
    allow_secondary_cli: bool = True,
    allow_internet_secondary_cli: bool = True,
    monitor_servers: list[dict[str, Any]] | None = None,
    local_actions: dict[str, Any] | None = None,
    runtime: dict[str, Any] | None = None,
) -> dict[str, Any]:
    admin_payload: dict[str, Any] = {
        "policies": {
            "analyzer": {
                "allow_secondary_cli": allow_secondary_cli,
                "allow_internet_secondary_cli": allow_internet_secondary_cli,
            }
        }
    }
    if monitor_servers is not None:
        admin_payload["monitor"] = {"servers": list(monitor_servers)}
    actions_payload: dict[str, Any] = {}
    if local_actions is not None:
        actions_payload["local"] = dict(local_actions)
    if remediation_actions:
        actions_payload["remediation"] = {
            action_id: {"description": action_id}
            for action_id in remediation_actions
        }
    if actions_payload:
        admin_payload["actions"] = actions_payload
    if runtime is not None:
        admin_payload["runtime"] = dict(runtime)
    return {"admin": admin_payload}


class _SecondaryFeedbackAnalyzer:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def analyze(self, *, snapshot, llm_output, cli_output=""):  # type: ignore[no-untyped-def]
        self.calls.append(
            {
                "snapshot": snapshot,
                "llm_output": str(llm_output or ""),
                "cli_output": str(cli_output or ""),
            }
        )
        if str(cli_output or "").strip():
            return {
                "diagnosis": "final_after_cli",
                "confidence": "medium",
                "action": "clear_cache",
                "reason": "cli_feedback_applied",
                "urgency": "warning",
            }
        return {
            "diagnosis": "need_more_data",
            "confidence": "low",
            "action": "notify_admin",
            "reason": "insufficient_data",
            "urgency": "warning",
            "secondary_cli_command": "echo secondary-diagnostics",
        }


def test_admin_runner_service_passes_monitor_snapshot_into_analyzer() -> None:
    async def _run() -> None:
        snapshot = {"id": "snap-1", "servers": [{"server_id": "s1"}]}
        monitor = _FakeMonitor([snapshot])
        analyzer = _FakeAnalyzer()
        service = AdminModeRunnerService(
            config=types.SimpleNamespace(),
            monitor=monitor,  # type: ignore[arg-type]
            analyzer=analyzer,  # type: ignore[arg-type]
            llm_output_provider=(lambda _snapshot, _cfg: '{"diagnosis":"llm"}'),
            cli_output_provider=(lambda _snapshot, _cfg: '{"diagnosis":"cli"}'),
        )

        step = await service.run_monitor_analyzer_once(
            config_payload={"admin": {"monitor": {"servers": []}}},
            session_workdir="/tmp/admin",
        )

        assert step.snapshot == snapshot
        assert step.decision.get("reason") == 'seen:{"diagnosis":"llm"}'
        assert len(monitor.calls) == 1
        assert monitor.calls[0]["session_workdir"] == "/tmp/admin"
        assert len(analyzer.calls) == 1
        assert analyzer.calls[0]["snapshot"] is snapshot
        assert analyzer.calls[0]["llm_output"] == '{"diagnosis":"llm"}'
        assert analyzer.calls[0]["cli_output"] == '{"diagnosis":"cli"}'

    asyncio.run(_run())


def test_admin_runner_service_persists_monitor_snapshot_history(tmp_path) -> None:
    async def _run() -> None:
        snapshot = AdminMonitorSnapshot(
            created_at_ts=1000.0,
            total_servers=1,
            ok_servers=1,
            failed_servers=0,
            servers=(
                AdminServerSnapshot(
                    server_id="scan:host:runtime",
                    target="local",
                    action_id="diag_host_runtime",
                    ok=True,
                    timed_out=False,
                    returncode=0,
                    duration_ms=11,
                    metrics={"host_load_1m": 1.25, "host_tcp_syn_recv": 0},
                    error=None,
                    collected_at_ts=1001.0,
                ),
            ),
        )
        monitor = _FakeMonitor([snapshot])
        analyzer = _FakeAnalyzer()
        service = AdminModeRunnerService(
            config=types.SimpleNamespace(),
            monitor=monitor,  # type: ignore[arg-type]
            analyzer=analyzer,  # type: ignore[arg-type]
            llm_output_provider=(lambda _snapshot, _cfg: '{"diagnosis":"llm"}'),
        )

        await service.run_monitor_analyzer_once(
            config_payload={"admin": {"monitor": {"servers": []}}},
            session_workdir=str(tmp_path),
        )

        store = AdminSnapshotStore.for_server(str(tmp_path), "scan:host:runtime")
        latest = store.latest_snapshot("diag_host_runtime")
        assert latest is not None
        assert latest["value"]["metrics"]["host_load_1m"] == 1.25
        assert latest["value"]["metrics"]["host_tcp_syn_recv"] == 0

    asyncio.run(_run())


def test_admin_runner_service_builds_pinned_ssh_spec_with_password_env(tmp_path) -> None:
    cli_proxy_dir = tmp_path / ".cli-proxy"
    cli_proxy_dir.mkdir()
    (cli_proxy_dir / "ssh.env").write_text("SSH_ADMIN_PASSWORD=secret\n", encoding="utf-8")

    spec = AdminModeRunnerService._build_pinned_ssh_spec(
        action_id="probe",
        argv=("echo", "ok"),
        timeout_sec=5,
        pinned_cli={
            "target": "ssh",
            "host": "admin.example.com",
            "user": "root",
            "port": 2222,
            "password_env": "SSH_ADMIN_PASSWORD",
        },
        session_workdir=str(tmp_path),
    )

    assert spec.host == "admin.example.com"
    assert spec.user == "root"
    assert spec.port == 2222
    assert spec.key_path == ""
    assert spec.password == "secret"


def test_admin_runner_service_analyzer_generates_decision_based_on_snapshot_in_background_loop() -> None:
    async def _run() -> None:
        monitor = _FakeMonitor(
            [
                {
                    "servers": [
                        {"metrics": {"http_status": 502}},
                        {"metrics": {"php_fpm_state": "down"}},
                    ]
                }
            ]
        )
        analyzer = AdminAnalyzer()
        service = AdminModeRunnerService(
            config=types.SimpleNamespace(),
            monitor=monitor,  # type: ignore[arg-type]
            analyzer=analyzer,  # type: ignore[arg-type]
            llm_output_provider=(
                lambda _snapshot, _cfg: (
                    '{"diagnosis":"llm_detected_upstream_failure",'
                    '"confidence":"high",'
                    '"action":"restart_php_fpm",'
                    '"reason":"llm_snapshot_detected_php_issue",'
                    '"urgency":"critical"}'
                )
            ),
        )

        task = asyncio.create_task(
            service.run_monitor_analyzer_loop(
                config_payload=_runner_admin_config(
                    remediation_actions=("restart_php_fpm",),
                    allow_secondary_cli=False,
                    monitor_servers=[],
                ),
                session_workdir="/tmp/admin",
                interval_sec=0.01,
                max_iterations=1,
            )
        )
        results = await task

        assert len(results) == 1
        decision = dict(results[0].decision or {})
        assert decision.get("action") == "restart_php_fpm"
        assert decision.get("reason") == "llm_snapshot_detected_php_issue"

    asyncio.run(_run())


def test_admin_runner_service_sequential_runs_are_isolated() -> None:
    async def _run() -> None:
        monitor = _FakeMonitor(
            [
                {"id": "snap-a", "servers": [{"metrics": {"postgresql_state": "down"}}]},
                {"id": "snap-b", "servers": []},
            ]
        )
        analyzer = AdminAnalyzer()
        service = AdminModeRunnerService(
            config=types.SimpleNamespace(),
            monitor=monitor,  # type: ignore[arg-type]
            analyzer=analyzer,  # type: ignore[arg-type]
            llm_output_provider=(
                lambda snapshot, _cfg: (
                    '{"diagnosis":"fallback","confidence":"low","action":"notify_admin","reason":"from_llm","urgency":"warning"}'
                    if snapshot.get("id") == "snap-b"
                    else (
                        '{"diagnosis":"db_restart","confidence":"high","action":"restart_postgresql",'
                        '"reason":"db_from_llm","urgency":"critical"}'
                    )
                )
            ),
        )

        config_payload = _runner_admin_config(
            remediation_actions=("restart_postgresql",),
            allow_secondary_cli=False,
        )
        first = await service.run_monitor_analyzer_once(config_payload=config_payload, session_workdir="/tmp/a")
        second = await service.run_monitor_analyzer_once(config_payload=config_payload, session_workdir="/tmp/b")

        assert first.decision.get("action") == "restart_postgresql"
        assert first.decision.get("reason") == "db_from_llm"
        assert second.decision.get("action") == "notify_admin"
        assert second.decision.get("reason") == "from_llm"
        assert first.snapshot.get("id") == "snap-a"
        assert second.snapshot.get("id") == "snap-b"

    asyncio.run(_run())


def test_admin_runner_service_executes_secondary_cli_and_redirects_feedback_to_analyzer() -> None:
    async def _run() -> None:
        snapshot = {"id": "snap-feedback", "servers": [{"server_id": "srv-1", "metrics": {}}]}
        monitor = _FakeMonitor([snapshot])
        analyzer = _SecondaryFeedbackAnalyzer()
        cli_transport = _CliStepLocalTransport(stdout="diag stdout\n", stderr="diag stderr\n", returncode=0)
        service = AdminModeRunnerService(
            config=types.SimpleNamespace(),
            monitor=monitor,  # type: ignore[arg-type]
            analyzer=analyzer,  # type: ignore[arg-type]
            cli_step_local_transport=cli_transport,  # type: ignore[arg-type]
            cli_step_ssh_transport=_NeverSSHTransport(),  # type: ignore[arg-type]
        )

        step = await service.run_monitor_analyzer_once(
            config_payload={"admin": {"runtime": {"pinned_cli": {"name": "codex"}}}},
            session_workdir="/tmp/admin-feedback",
        )

        assert step.decision["action"] == "clear_cache"
        assert step.decision["reason"] == "cli_feedback_applied"
        assert len(analyzer.calls) == 2
        assert analyzer.calls[0]["cli_output"] == ""
        assert '"secondary_cli_feedback"' in analyzer.calls[1]["cli_output"]
        assert '"stdout": "diag stdout\\n"' in analyzer.calls[1]["cli_output"]
        assert '"stderr": "diag stderr\\n"' in analyzer.calls[1]["cli_output"]
        assert len(cli_transport.calls) == 1
        assert str(cli_transport.calls[0].action_id) == "secondary_cli_step"
        assert tuple(cli_transport.calls[0].argv) == ("bash", "-lc", "echo secondary-diagnostics")

    asyncio.run(_run())


def test_admin_runner_service_real_analyzer_finalizes_after_secondary_cli_feedback() -> None:
    async def _run() -> None:
        snapshot = {"servers": [{"server_id": "srv-1", "metrics": {"cpu_usage_pct": 99}}]}
        monitor = _FakeMonitor([snapshot])
        cli_transport = _CliStepLocalTransport(stdout="top snapshot\n", stderr="", returncode=0)
        service = AdminModeRunnerService(
            config=types.SimpleNamespace(),
            monitor=monitor,  # type: ignore[arg-type]
            analyzer=AdminAnalyzer(),  # type: ignore[arg-type]
            cli_step_local_transport=cli_transport,  # type: ignore[arg-type]
            cli_step_ssh_transport=_NeverSSHTransport(),  # type: ignore[arg-type]
        )

        step = await service.run_monitor_analyzer_once(
            config_payload={"admin": {"runtime": {"pinned_cli": {"name": "codex"}}}},
            session_workdir="/tmp/admin-feedback",
        )

        decision = dict(step.decision or {})
        assert decision["action"] == "notify_admin"
        assert decision["confidence"] == "medium"
        assert decision["reason"] == "cli_post_analysis:diagnostic_output_captured"
        assert "secondary_cli_command" not in decision
        assert len(cli_transport.calls) == 1

    asyncio.run(_run())


def test_admin_runner_service_name_only_pinned_cli_does_not_rewrite_monitor_targets() -> None:
    service = AdminModeRunnerService(config=types.SimpleNamespace())
    payload = {
        "admin": {
            "runtime": {"pinned_cli": {"name": "codex"}},
            "monitor": {
                "servers": [
                    {"id": "remote", "target": "ssh", "action_id": "diag_host_health"},
                ],
            },
            "allowlist": {
                "ssh": {
                    "diag_host_health": {"argv": ["bash", "-lc", "uptime"], "timeout_sec": 15},
                },
            },
        },
    }

    rewritten = service._build_monitor_config_for_pinned_cli(config_payload=payload)

    assert rewritten["admin"]["monitor"]["servers"] == [
        {"id": "remote", "target": "ssh", "action_id": "diag_host_health"},
    ]


def test_admin_runner_service_uses_generated_pinned_cli_for_monitor_rewrite() -> None:
    service = AdminModeRunnerService(config=types.SimpleNamespace())
    payload = {
        "admin": {
            "runtime": {"pinned_cli": {"name": "codex"}},
            "generated": {
                "environment": {
                    "pinned_cli": {
                        "target": "ssh",
                        "transport": "ssh",
                        "host": "server.example.com",
                        "user": "ops",
                        "port": 2222,
                        "password_env": "SSH_PASSWORD",
                    }
                }
            },
            "monitor": {
                "servers": [
                    {"id": "scan:host:runtime", "target": "ssh", "action_id": "diag_host_runtime"},
                ],
            },
            "actions": {
                "ssh": {
                    "diag_host_runtime": {"argv": ["bash", "-lc", "uptime"], "timeout_sec": 15},
                },
            },
        },
    }

    rewritten = service._build_monitor_config_for_pinned_cli(config_payload=payload)
    action_payload = rewritten["admin"]["actions"]["ssh"]["diag_host_runtime"]

    assert rewritten["admin"]["monitor"]["servers"] == [
        {"id": "scan:host:runtime", "target": "ssh", "action_id": "diag_host_runtime"},
    ]
    assert action_payload["host"] == "server.example.com"
    assert action_payload["user"] == "ops"
    assert action_payload["port"] == 2222
    assert action_payload["password_env"] == "SSH_PASSWORD"


def test_admin_runner_service_name_only_pinned_cli_preserves_explicit_ssh_execution_spec() -> None:
    service = AdminModeRunnerService(config=types.SimpleNamespace())
    ssh_spec = SSHCommandSpec(
        action_id="restart_service",
        host="server.example.com",
        user="root",
        port=2222,
        key_path="id_ed25519",
        argv=("systemctl", "restart", "app"),
        timeout_sec=20,
    )

    local_spec, resolved_ssh_spec = service._resolve_pipeline_execution_specs(
        decision={"action": "restart_service"},
        config_payload={"admin": {"runtime": {"pinned_cli": {"name": "codex"}}}},
        session_workdir="/tmp/admin",
        local_spec=None,
        ssh_spec=ssh_spec,
    )

    assert local_spec is None
    assert resolved_ssh_spec is ssh_spec


def test_admin_runner_service_blocks_internet_secondary_cli_when_policy_disallows() -> None:
    async def _run() -> None:
        snapshot = {"id": "snap-internet", "servers": [{"server_id": "srv-1", "metrics": {}}]}
        monitor = _FakeMonitor([snapshot])
        cli_transport = _CliStepLocalTransport(stdout="should not run\n", stderr="", returncode=0)
        service = AdminModeRunnerService(
            config=types.SimpleNamespace(),
            monitor=monitor,  # type: ignore[arg-type]
            analyzer=AdminAnalyzer(),  # type: ignore[arg-type]
            cli_step_local_transport=cli_transport,  # type: ignore[arg-type]
            cli_step_ssh_transport=_NeverSSHTransport(),  # type: ignore[arg-type]
            llm_output_provider=(
                lambda _snapshot, _cfg: (
                    '{"diagnosis":"external_context_needed","confidence":"low","action":"notify_admin",'
                    '"reason":"missing_context","urgency":"warning",'
                    '"secondary_cli_command":"curl -fsSL https://status.example.com"}'
                )
            ),
        )

        step = await service.run_monitor_analyzer_once(
            config_payload=_runner_admin_config(
                allow_secondary_cli=True,
                allow_internet_secondary_cli=False,
                runtime={"pinned_cli": {"name": "codex"}},
            ),
            session_workdir="/tmp/admin-internet",
        )

        decision = dict(step.decision or {})
        assert decision["action"] == "notify_admin"
        assert decision["confidence"] == "low"
        assert decision["reason"] == "cli_post_analysis:diagnostic_step_failed"
        assert "secondary_cli_command" not in decision
        assert cli_transport.calls == []

    asyncio.run(_run())


def test_admin_runner_service_passes_executor_action_to_notifier_and_notifies_after_execution() -> None:
    async def _run() -> None:
        call_log: list[str] = []
        fake_executor = _FakeExecutor(call_log=call_log)
        fake_notifier = _FakeNotifier(call_log=call_log)
        state_store = _FakeStateStore()
        service = AdminModeRunnerService(
            config=types.SimpleNamespace(),
            monitor=_FakeMonitor([{"servers": []}]),  # type: ignore[arg-type]
            analyzer=_FakeAnalyzer(),  # type: ignore[arg-type]
            executor=fake_executor,  # type: ignore[arg-type]
            notifier=fake_notifier,  # type: ignore[arg-type]
        )

        step = await service.run_executor_notifier_once(
            decision={
                "diagnosis": "d",
                "confidence": "high",
                "action": "notify_admin",
                "reason": "r",
                "urgency": "warning",
            },
            session_id="session-x",
            chat_id=123,
            state_store=state_store,
            messaging=_FakeMessaging(),  # type: ignore[arg-type]
            now_ts=1_000.0,
        )

        assert isinstance(step, AdminExecutorNotifierStepResult)
        assert step.execution_result.success is True
        assert step.action_notification is not None
        assert bool(getattr(step.action_notification, "sent", False)) is True
        assert step.incident_notification is not None
        assert bool(getattr(step.incident_notification, "sent", False)) is True

        assert call_log == ["executor", "notifier_action", "notifier_incident"]
        assert len(fake_executor.calls) == 1
        assert fake_executor.calls[0]["decision"]["action"] == "notify_admin"

        action_call = next(call for call in fake_notifier.calls if call["kind"] == "action")
        assert action_call["session_id"] == "session-x"
        assert action_call["chat_id"] == 123
        assert dict(action_call["action_row"]).get("action_id") == "analyzer:session-x:notify_admin:1"

    asyncio.run(_run())


def test_admin_runner_service_e2e_502_php_fpm_down_restart_then_notify(tmp_path) -> None:
    async def _run() -> None:
        store = AdminStateStore(str(tmp_path / "state.json"))
        monitor_transport = _MonitorE2ELocalTransport()
        monitor = AdminMonitor(
            local_transport=monitor_transport,  # type: ignore[arg-type]
            ssh_transport=_NeverSSHTransport(),  # type: ignore[arg-type]
        )
        service = AdminModeRunnerService(
            config=types.SimpleNamespace(),
            monitor=monitor,  # type: ignore[arg-type]
            analyzer=AdminAnalyzer(),  # type: ignore[arg-type]
            executor=AdminExecutor(),  # type: ignore[arg-type]
            llm_output_provider=(
                lambda _snapshot, _cfg: (
                    '{"diagnosis":"llm_detected_php_issue","confidence":"high","action":"restart_php_fpm",'
                    '"reason":"llm_restart_php","urgency":"critical"}'
                )
            ),
        )
        messaging = _RecordingMessaging()
        remediation_transport = _ExecutorLocalTransport()
        approval = {"count": 0}

        async def _ask_user(_question: str, _options: list[str]) -> str:
            approval["count"] += 1
            return "✅ Подтвердить"

        step = await service.run_pipeline_once(
            config_payload=_runner_admin_config(
                remediation_actions=("restart_php_fpm",),
                allow_secondary_cli=False,
                monitor_servers=[
                    {"id": "web-http", "target": "local", "action_id": "probe_http_502"},
                    {"id": "web-php", "target": "local", "action_id": "probe_php_fpm_down"},
                ],
                local_actions={
                    "probe_http_502": {"argv": ["echo", "http"]},
                    "probe_php_fpm_down": {"argv": ["echo", "php"]},
                },
            ),
            session_workdir=str(tmp_path),
            session_id="sess-e2e-502",
            chat_id=101,
            state_store=store,
            messaging=messaging,  # type: ignore[arg-type]
            ask_user=_ask_user,
            local_transport=remediation_transport,  # type: ignore[arg-type]
            ssh_transport=_NeverSSHTransport(),  # type: ignore[arg-type]
            local_spec=LocalCommandSpec(
                action_id="restart_php_fpm",
                argv=("systemctl", "restart", "php-fpm"),
                timeout_sec=10.0,
            ),
            now_ts=1_000.0,
        )

        decision = dict(step.monitor_analyzer.decision or {})
        assert decision.get("action") == "restart_php_fpm"
        assert decision.get("reason") == "llm_restart_php"
        assert approval["count"] == 1
        assert len(remediation_transport.calls) == 1
        assert str(remediation_transport.calls[0].action_id) == "restart_php_fpm"
        assert step.executor_notifier.execution_result.success is True
        assert bool(getattr(step.executor_notifier.action_notification, "sent", False)) is True
        assert bool(getattr(step.executor_notifier.incident_notification, "sent", False)) is True
        assert len(messaging.sent) == 2
        assert "*🛡 Admin Action*" in str(messaging.sent[0].get("text") or "")
        assert "*🛡 Admin Incident*" in str(messaging.sent[1].get("text") or "")
        incidents = store.list_incidents("sess-e2e-502", limit=10)
        assert any(
            dict(item.get("payload") or {}).get("decision", {}).get("action") == "restart_php_fpm"
            for item in incidents
        )

    asyncio.run(_run())


def test_admin_runner_service_e2e_postgresql_down_controlled_restart_then_notify(tmp_path) -> None:
    async def _run() -> None:
        store = AdminStateStore(str(tmp_path / "state.json"))
        monitor_transport = _MonitorE2ELocalTransport()
        monitor = AdminMonitor(
            local_transport=monitor_transport,  # type: ignore[arg-type]
            ssh_transport=_NeverSSHTransport(),  # type: ignore[arg-type]
        )
        service = AdminModeRunnerService(
            config=types.SimpleNamespace(),
            monitor=monitor,  # type: ignore[arg-type]
            analyzer=AdminAnalyzer(),  # type: ignore[arg-type]
            executor=AdminExecutor(),  # type: ignore[arg-type]
            llm_output_provider=(
                lambda _snapshot, _cfg: (
                    '{"diagnosis":"llm_detected_db_issue","confidence":"high","action":"restart_postgresql",'
                    '"reason":"llm_restart_db","urgency":"critical"}'
                )
            ),
        )
        messaging = _RecordingMessaging()
        remediation_transport = _ExecutorLocalTransport()
        approval = {"count": 0}

        async def _ask_user(_question: str, _options: list[str]) -> str:
            approval["count"] += 1
            return "✅ Подтвердить"

        step = await service.run_pipeline_once(
            config_payload=_runner_admin_config(
                remediation_actions=("restart_postgresql",),
                allow_secondary_cli=False,
                monitor_servers=[
                    {"id": "db-1", "target": "local", "action_id": "probe_postgresql_down"},
                ],
                local_actions={
                    "probe_postgresql_down": {"argv": ["echo", "db"]},
                },
            ),
            session_workdir=str(tmp_path),
            session_id="sess-e2e-pg",
            chat_id=102,
            state_store=store,
            messaging=messaging,  # type: ignore[arg-type]
            ask_user=_ask_user,
            local_transport=remediation_transport,  # type: ignore[arg-type]
            ssh_transport=_NeverSSHTransport(),  # type: ignore[arg-type]
            local_spec=LocalCommandSpec(
                action_id="restart_postgresql",
                argv=("systemctl", "restart", "postgresql"),
                timeout_sec=10.0,
            ),
            now_ts=2_000.0,
        )

        decision = dict(step.monitor_analyzer.decision or {})
        assert decision.get("action") == "restart_postgresql"
        assert decision.get("reason") == "llm_restart_db"
        assert approval["count"] == 1
        assert len(remediation_transport.calls) == 1
        assert str(remediation_transport.calls[0].action_id) == "restart_postgresql"
        assert "RUN action: restart_postgresql" in str(step.executor_notifier.execution_result.text or "")
        assert bool(getattr(step.executor_notifier.action_notification, "sent", False)) is True
        assert bool(getattr(step.executor_notifier.incident_notification, "sent", False)) is True
        assert len(messaging.sent) == 2
        actions = store.list_actions("sess-e2e-pg", limit=20)
        assert any(
            bool(dict(item.get("payload") or {}).get("decision", {}).get("manual_approval", {}).get("approved"))
            for item in actions
        )

    asyncio.run(_run())


def test_admin_runner_service_e2e_single_server_failure_does_not_stop_others(tmp_path) -> None:
    async def _run() -> None:
        store = AdminStateStore(str(tmp_path / "state.json"))
        monitor_transport = _MonitorE2ELocalTransport()
        monitor = AdminMonitor(
            local_transport=monitor_transport,  # type: ignore[arg-type]
            ssh_transport=_NeverSSHTransport(),  # type: ignore[arg-type]
        )
        service = AdminModeRunnerService(
            config=types.SimpleNamespace(),
            monitor=monitor,  # type: ignore[arg-type]
            analyzer=AdminAnalyzer(),  # type: ignore[arg-type]
            executor=AdminExecutor(),  # type: ignore[arg-type]
            llm_output_provider=(
                lambda _snapshot, _cfg: (
                    '{"diagnosis":"llm_failover_php","confidence":"high","action":"restart_php_fpm",'
                    '"reason":"llm_failover_restart","urgency":"critical"}'
                )
            ),
        )
        messaging = _RecordingMessaging()
        remediation_transport = _ExecutorLocalTransport()

        async def _ask_user(_question: str, _options: list[str]) -> str:
            return "✅ Подтвердить"

        step = await service.run_pipeline_once(
            config_payload=_runner_admin_config(
                remediation_actions=("restart_php_fpm",),
                allow_secondary_cli=False,
                monitor_servers=[
                    {"id": "broken-node", "target": "local", "action_id": "probe_fail_local"},
                    {"id": "web-http", "target": "local", "action_id": "probe_http_502"},
                    {"id": "web-php", "target": "local", "action_id": "probe_php_fpm_down"},
                ],
                local_actions={
                    "probe_fail_local": {"argv": ["echo", "fail"]},
                    "probe_http_502": {"argv": ["echo", "http"]},
                    "probe_php_fpm_down": {"argv": ["echo", "php"]},
                },
            ),
            session_workdir=str(tmp_path),
            session_id="sess-e2e-failover",
            chat_id=103,
            state_store=store,
            messaging=messaging,  # type: ignore[arg-type]
            ask_user=_ask_user,
            local_transport=remediation_transport,  # type: ignore[arg-type]
            ssh_transport=_NeverSSHTransport(),  # type: ignore[arg-type]
            local_spec=LocalCommandSpec(
                action_id="restart_php_fpm",
                argv=("systemctl", "restart", "php-fpm"),
                timeout_sec=10.0,
            ),
            now_ts=3_000.0,
        )

        snapshot = step.monitor_analyzer.snapshot
        decision = dict(step.monitor_analyzer.decision or {})
        assert int(snapshot.total_servers) == 3
        assert int(snapshot.failed_servers) == 1
        assert int(snapshot.ok_servers) == 2
        by_id = {item.server_id: item for item in tuple(snapshot.servers or ())}
        assert by_id["broken-node"].ok is False
        assert "transport failed" in str(by_id["broken-node"].error or "")
        assert decision.get("action") == "restart_php_fpm"
        assert decision.get("reason") == "llm_failover_restart"
        assert len(remediation_transport.calls) == 1
        assert bool(getattr(step.executor_notifier.action_notification, "sent", False)) is True
        assert bool(getattr(step.executor_notifier.incident_notification, "sent", False)) is True
        assert len(messaging.sent) == 2

    asyncio.run(_run())


def test_admin_runner_service_pipeline_uses_pinned_cli_for_checks_and_execution(tmp_path) -> None:
    async def _run() -> None:
        store = AdminStateStore(str(tmp_path / "state.json"))
        local_transport = _NeverLocalTransport()
        ssh_transport = _PinnedSSHTransport()
        monitor = AdminMonitor(
            local_transport=local_transport,  # type: ignore[arg-type]
            ssh_transport=ssh_transport,  # type: ignore[arg-type]
        )
        service = AdminModeRunnerService(
            config=types.SimpleNamespace(),
            monitor=monitor,  # type: ignore[arg-type]
            analyzer=AdminAnalyzer(),  # type: ignore[arg-type]
            executor=AdminExecutor(),  # type: ignore[arg-type]
            llm_output_provider=(
                lambda _snapshot, _cfg: (
                    '{"diagnosis":"llm_detected_php_issue","confidence":"high","action":"restart_php_fpm",'
                    '"reason":"llm_restart_php","urgency":"critical"}'
                )
            ),
        )
        messaging = _RecordingMessaging()

        async def _ask_user(_question: str, _options: list[str]) -> str:
            return "✅ Подтвердить"

        step = await service.run_pipeline_once(
            config_payload=_runner_admin_config(
                remediation_actions=("restart_php_fpm",),
                allow_secondary_cli=False,
                monitor_servers=[
                    {"id": "web-http", "target": "local", "action_id": "probe_http_502"},
                ],
                local_actions={
                    "probe_http_502": {"argv": ["echo", "http"]},
                },
                runtime={
                    "pinned_cli": {
                        "name": "ssh-admin",
                        "target": "ssh",
                        "host": "admin.example.com",
                        "user": "root",
                        "port": 2222,
                        "key_path": "id_ed25519",
                        "options": ["StrictHostKeyChecking=no"],
                    }
                },
            ),
            session_workdir=str(tmp_path),
            session_id="sess-pinned-cli",
            chat_id=104,
            state_store=store,
            messaging=messaging,  # type: ignore[arg-type]
            ask_user=_ask_user,
            local_transport=local_transport,  # type: ignore[arg-type]
            ssh_transport=ssh_transport,  # type: ignore[arg-type]
            local_spec=LocalCommandSpec(
                action_id="restart_php_fpm",
                argv=("systemctl", "restart", "php-fpm"),
                timeout_sec=10.0,
            ),
        )

        decision = dict(step.monitor_analyzer.decision or {})
        assert decision.get("action") == "restart_php_fpm"
        assert decision.get("reason") == "llm_restart_php"
        assert local_transport.calls == []
        assert len(ssh_transport.calls) == 2

        check_spec = ssh_transport.calls[0]
        assert str(check_spec.action_id) == "probe_http_502"
        assert tuple(check_spec.argv) == ("echo", "http")
        assert str(check_spec.host) == "admin.example.com"
        assert str(check_spec.user or "") == "root"
        assert int(check_spec.port) == 2222
        assert str(check_spec.key_path).endswith("id_ed25519")
        assert tuple(check_spec.options) == ("StrictHostKeyChecking=no",)

        exec_spec = ssh_transport.calls[1]
        assert str(exec_spec.action_id) == "restart_php_fpm"
        assert tuple(exec_spec.argv) == ("systemctl", "restart", "php-fpm")
        assert str(exec_spec.host) == "admin.example.com"
        assert str(exec_spec.user or "") == "root"
        assert int(exec_spec.port) == 2222
        assert step.executor_notifier.execution_result.success is True
        assert "admin.example.com:2222" in str(step.executor_notifier.execution_result.text or "")
        assert bool(getattr(step.executor_notifier.action_notification, "sent", False)) is True
        assert bool(getattr(step.executor_notifier.incident_notification, "sent", False)) is True

    asyncio.run(_run())
