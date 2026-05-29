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

_MONITOR_PATH = ADMIN_ROOT / "monitor.py"
_MONITOR_SPEC = importlib.util.spec_from_file_location("modes.admin.monitor_test", _MONITOR_PATH)
if _MONITOR_SPEC is None or _MONITOR_SPEC.loader is None:
    raise RuntimeError(f"failed to load admin monitor module from {_MONITOR_PATH}")
_MONITOR_MODULE = importlib.util.module_from_spec(_MONITOR_SPEC)
sys.modules[_MONITOR_SPEC.name] = _MONITOR_MODULE
_MONITOR_SPEC.loader.exec_module(_MONITOR_MODULE)
AdminMonitor = _MONITOR_MODULE.AdminMonitor

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


class _FakeLocalTransport:
    def __init__(self) -> None:
        self.seen_action_ids: list[str] = []

    async def run(self, spec):  # type: ignore[no-untyped-def]
        action_id = str(getattr(spec, "action_id", "") or "")
        self.seen_action_ids.append(action_id)
        if action_id == "broken_local":
            raise LocalTransportError("local monitor transport failed")
        if action_id == "ok_local":
            return LocalCommandResult(
                action_id=action_id,
                returncode=0,
                stdout='{"cpu": 0.51, "mem_used_mb": 128}',
                stderr="",
                timed_out=False,
                duration_ms=11,
            )
        return LocalCommandResult(
            action_id=action_id,
            returncode=0,
            stdout="",
            stderr="",
            timed_out=False,
            duration_ms=5,
        )


class _FakeSSHTransport:
    def __init__(self) -> None:
        self.seen_action_ids: list[str] = []

    async def run(self, spec):  # type: ignore[no-untyped-def]
        action_id = str(getattr(spec, "action_id", "") or "")
        self.seen_action_ids.append(action_id)
        if action_id == "slow_ssh":
            return SSHCommandResult(
                action_id=action_id,
                host=str(getattr(spec, "host", "") or "srv-timeout"),
                user=str(getattr(spec, "user", "") or "root"),
                port=int(getattr(spec, "port", 22) or 22),
                returncode=-1,
                stdout="",
                stderr="ssh timeout",
                timed_out=True,
                duration_ms=250,
            )
        return SSHCommandResult(
            action_id=action_id,
            host=str(getattr(spec, "host", "") or "srv-ok"),
            user=str(getattr(spec, "user", "") or "root"),
            port=int(getattr(spec, "port", 22) or 22),
            returncode=0,
            stdout="cpu=0.21\nload=1.4\nhealthy=true",
            stderr="",
            timed_out=False,
            duration_ms=18,
        )


class _SlowCountingLocalTransport:
    def __init__(self) -> None:
        self.active = 0
        self.max_active = 0

    async def run(self, spec):  # type: ignore[no-untyped-def]
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        await asyncio.sleep(0.01)
        self.active -= 1
        action_id = str(getattr(spec, "action_id", "") or "")
        return LocalCommandResult(
            action_id=action_id,
            returncode=0,
            stdout="ok=true",
            stderr="",
            timed_out=False,
            duration_ms=10,
        )


def _build_monitor(local_transport: Any | None = None, ssh_transport: Any | None = None) -> AdminMonitor:
    return AdminMonitor(
        local_transport=local_transport or _FakeLocalTransport(),  # type: ignore[arg-type]
        ssh_transport=ssh_transport or _FakeSSHTransport(),  # type: ignore[arg-type]
    )


def test_admin_monitor_collects_snapshot_for_multiple_servers(tmp_path) -> None:
    async def _run() -> None:
        monitor = _build_monitor()
        payload = {
            "admin": {
                "monitor": {
                    "servers": [
                        {"id": "local-1", "target": "local", "action_id": "ok_local"},
                        {"id": "ssh-1", "target": "ssh", "action_id": "ok_ssh"},
                    ]
                },
                "actions": {
                    "local": {"ok_local": {"argv": ["echo", "local"]}},
                    "ssh": {
                        "ok_ssh": {
                            "host": "srv-ok",
                            "user": "root",
                            "key_path": "keys/id_rsa",
                            "argv": ["echo", "ssh"],
                        }
                    },
                },
            }
        }

        snapshot = await monitor.collect_snapshot(
            config_payload=payload,
            session_workdir=str(tmp_path),
        )

        assert snapshot.total_servers == 2
        assert snapshot.ok_servers == 2
        assert snapshot.failed_servers == 0
        by_id = {item.server_id: item for item in snapshot.servers}
        assert set(by_id.keys()) == {"local-1", "ssh-1"}
        assert by_id["local-1"].metrics.get("cpu") == 0.51
        assert by_id["local-1"].metrics.get("mem_used_mb") == 128
        assert by_id["ssh-1"].metrics.get("cpu") == 0.21
        assert by_id["ssh-1"].metrics.get("load") == 1.4
        assert by_id["ssh-1"].metrics.get("healthy") is True

    asyncio.run(_run())


def test_admin_monitor_continues_when_single_server_fails_or_times_out(tmp_path) -> None:
    async def _run() -> None:
        local_transport = _FakeLocalTransport()
        ssh_transport = _FakeSSHTransport()
        monitor = _build_monitor(local_transport=local_transport, ssh_transport=ssh_transport)
        payload = {
            "admin": {
                "monitor": {
                    "servers": [
                        {"id": "bad-local", "target": "local", "action_id": "broken_local"},
                        {"id": "slow-ssh", "target": "ssh", "action_id": "slow_ssh"},
                        {"id": "ok-local", "target": "local", "action_id": "ok_local"},
                    ]
                },
                "actions": {
                    "local": {
                        "broken_local": {"argv": ["echo", "broken"]},
                        "ok_local": {"argv": ["echo", "ok"]},
                    },
                    "ssh": {
                        "slow_ssh": {
                            "host": "srv-timeout",
                            "user": "root",
                            "key_path": "keys/id_rsa",
                            "argv": ["echo", "slow"],
                        }
                    },
                },
            }
        }

        snapshot = await monitor.collect_snapshot(
            config_payload=payload,
            session_workdir=str(tmp_path),
        )

        assert snapshot.total_servers == 3
        assert snapshot.ok_servers == 1
        assert snapshot.failed_servers == 2
        by_id = {item.server_id: item for item in snapshot.servers}

        failed_local = by_id["bad-local"]
        assert failed_local.ok is False
        assert "transport failed" in str(failed_local.error or "")

        timed_out_ssh = by_id["slow-ssh"]
        assert timed_out_ssh.ok is False
        assert timed_out_ssh.timed_out is True
        assert timed_out_ssh.returncode == -1

        ok_local = by_id["ok-local"]
        assert ok_local.ok is True
        assert ok_local.metrics.get("cpu") == 0.51

        assert local_transport.seen_action_ids == ["broken_local", "ok_local"]
        assert ssh_transport.seen_action_ids == ["slow_ssh"]

    asyncio.run(_run())


def test_admin_monitor_snapshots_isolated_between_sequential_runs(tmp_path) -> None:
    async def _run() -> None:
        monitor = _build_monitor()
        payload_a = {
            "admin": {
                "monitor": {"servers": [{"id": "server-a", "target": "local", "action_id": "ok_local"}]},
                "actions": {"local": {"ok_local": {"argv": ["echo", "a"]}}},
            }
        }
        payload_b = {
            "admin": {
                "monitor": {"servers": [{"id": "server-b", "target": "ssh", "action_id": "ok_ssh"}]},
                "actions": {
                    "ssh": {
                        "ok_ssh": {
                            "host": "srv-b",
                            "user": "root",
                            "key_path": "keys/id_rsa",
                            "argv": ["echo", "b"],
                        }
                    }
                },
            }
        }

        snapshot_a = await monitor.collect_snapshot(config_payload=payload_a, session_workdir=str(tmp_path))
        snapshot_b = await monitor.collect_snapshot(config_payload=payload_b, session_workdir=str(tmp_path))

        assert snapshot_a.total_servers == 1
        assert snapshot_b.total_servers == 1
        assert snapshot_a.servers[0].server_id == "server-a"
        assert snapshot_b.servers[0].server_id == "server-b"
        assert snapshot_a.servers[0].target == "local"
        assert snapshot_b.servers[0].target == "ssh"

    asyncio.run(_run())


def test_admin_monitor_limits_concurrent_polls(tmp_path) -> None:
    async def _run() -> None:
        transport = _SlowCountingLocalTransport()
        monitor = AdminMonitor(
            local_transport=transport,  # type: ignore[arg-type]
            max_concurrent_polls=2,
        )
        servers = [
            {"id": f"local-{idx}", "target": "local", "action_id": f"ok_local_{idx}"}
            for idx in range(8)
        ]
        actions = {
            item["action_id"]: {"argv": ["echo", str(item["id"])]}
            for item in servers
        }
        payload = {
            "admin": {
                "monitor": {"servers": servers},
                "actions": {"local": actions},
            }
        }

        snapshot = await monitor.collect_snapshot(
            config_payload=payload,
            session_workdir=str(tmp_path),
        )

        assert snapshot.total_servers == 8
        assert snapshot.ok_servers == 8
        assert transport.max_active <= 2

    asyncio.run(_run())


def test_admin_monitor_resolves_ssh_host_from_ssh_yaml(tmp_path) -> None:
    import yaml as _yaml

    cli_proxy_dir = tmp_path / ".cli-proxy"
    cli_proxy_dir.mkdir()
    (cli_proxy_dir / "ssh.yaml").write_text(
        _yaml.safe_dump(
            {
                "hosts": {
                    "srv-test": {
                        "host": "srv-test.example.com",
                        "user": "deploy",
                        "port": 2222,
                        "auth": "key",
                        "key_file": "/abs/keys/id_rsa",
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    spec = AdminMonitor._build_ssh_command_spec(
        action_id="probe",
        action_payload={"argv": ["echo", "ok"], "timeout_sec": 5},
        server_id="srv-test",
        session_workdir=str(tmp_path),
        timeout_override_sec=None,
    )

    assert spec.host == "srv-test.example.com"
    assert spec.user == "deploy"
    assert spec.port == 2222
    assert spec.key_path == "/abs/keys/id_rsa"
    assert spec.argv == ("echo", "ok")


def test_admin_monitor_resolves_password_ssh_host_from_ssh_yaml(tmp_path) -> None:
    import yaml as _yaml

    cli_proxy_dir = tmp_path / ".cli-proxy"
    cli_proxy_dir.mkdir()
    (cli_proxy_dir / "ssh.yaml").write_text(
        _yaml.safe_dump(
            {
                "hosts": {
                    "srv-test": {
                        "host": "srv-test.example.com",
                        "user": "deploy",
                        "port": 2222,
                        "auth": "password",
                        "password_env": "SSH_SRV_TEST_PASSWORD",
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    (cli_proxy_dir / "ssh.env").write_text("SSH_SRV_TEST_PASSWORD=secret\n", encoding="utf-8")

    spec = AdminMonitor._build_ssh_command_spec(
        action_id="probe",
        action_payload={"argv": ["echo", "ok"], "timeout_sec": 5},
        server_id="srv-test",
        session_workdir=str(tmp_path),
        timeout_override_sec=None,
    )

    assert spec.host == "srv-test.example.com"
    assert spec.user == "deploy"
    assert spec.port == 2222
    assert spec.key_path == ""
    assert spec.password == "secret"
    assert spec.argv == ("echo", "ok")


def test_admin_monitor_ssh_resolver_raises_when_alias_missing(tmp_path) -> None:
    cli_proxy_dir = tmp_path / ".cli-proxy"
    cli_proxy_dir.mkdir()
    (cli_proxy_dir / "ssh.yaml").write_text("hosts: {}\n", encoding="utf-8")

    import pytest

    with pytest.raises(ValueError, match="ssh.yaml"):
        AdminMonitor._build_ssh_command_spec(
            action_id="probe",
            action_payload={"argv": ["echo", "ok"], "timeout_sec": 5},
            server_id="missing",
            session_workdir=str(tmp_path),
            timeout_override_sec=None,
        )
