import asyncio
from pathlib import Path

import pytest
import yaml

from modes.admin.plugin_tools import (
    AdminToolError,
    ActionRunResult,
    build_server_dossier,
    find_allowlisted_action,
    resolve_workdir,
    run_allowlisted_action,
    write_escalation,
)
from modes.admin.snapshot_store import SEVERITY_WARN, admin_root


class _FakeSession:
    def __init__(self, workdir: str):
        self.workdir = workdir


class _FakeLocalResult:
    def __init__(self, *, rc=0, stdout="", stderr="", timed_out=False):
        self.returncode = rc
        self.stdout = stdout
        self.stderr = stderr
        self.timed_out = timed_out


class _FakeLocalTransport:
    def __init__(self, result=None, *, raise_exc=None):
        self.result = result or _FakeLocalResult(stdout="ok\n")
        self.raise_exc = raise_exc
        self.calls = []

    async def run(self, spec):
        self.calls.append(spec)
        if self.raise_exc is not None:
            raise self.raise_exc
        return self.result


class _FakeStateStore:
    def __init__(self):
        self.records = []

    def record_incident(self, *, incident_id: str, payload):
        self.records.append({"incident_id": incident_id, "payload": dict(payload)})


def _write_admin_config(workdir: Path, extra_local=None, extra_ssh=None):
    admin_dir = admin_root(str(workdir))
    admin_dir.mkdir(parents=True, exist_ok=True)
    local_actions = {
        "clear_logs": {
            "argv": ["bash", "-lc", "echo cleared"],
            "timeout_sec": 10,
            "risk_level": "low",
        }
    }
    if extra_local:
        local_actions.update(extra_local)
    ssh_actions = dict(extra_ssh or {})
    payload = {
        "admin": {
            "allowlist": {"local": local_actions, "ssh": ssh_actions},
            "runtime": {},
        }
    }
    (admin_dir / "config.yaml").write_text(
        yaml.safe_dump(payload, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )


def test_resolve_workdir_from_session(tmp_path):
    ctx = {"session": _FakeSession(str(tmp_path))}
    assert resolve_workdir(ctx) == str(tmp_path)


def test_resolve_workdir_from_cwd_fallback(tmp_path):
    ctx = {"cwd": str(tmp_path)}
    assert resolve_workdir(ctx) == str(tmp_path)


def test_resolve_workdir_missing_raises():
    with pytest.raises(AdminToolError):
        resolve_workdir({})


def test_find_allowlisted_action_matches_local():
    admin = {"allowlist": {"local": {"a": {"argv": ["true"], "timeout_sec": 5}}}}
    spec = find_allowlisted_action(admin, action_id="a")
    assert spec["target"] == "local"
    assert spec["action_id"] == "a"
    assert spec["argv"] == ["true"]


def test_find_allowlisted_action_rejects_missing():
    with pytest.raises(AdminToolError):
        find_allowlisted_action({"allowlist": {"local": {}}}, action_id="nope")


def test_find_allowlisted_action_invalid_hint():
    with pytest.raises(AdminToolError):
        find_allowlisted_action({}, action_id="a", target_hint="xxx")


def test_find_allowlisted_action_respects_hint():
    admin = {
        "allowlist": {
            "local": {"same_id": {"argv": ["echo", "local"]}},
            "ssh": {"same_id": {"argv": ["echo", "ssh"], "host": "h", "key_path": "k"}},
        }
    }
    ssh_spec = find_allowlisted_action(admin, action_id="same_id", target_hint="ssh")
    assert ssh_spec["argv"] == ["echo", "ssh"]
    local_spec = find_allowlisted_action(admin, action_id="same_id", target_hint="local")
    assert local_spec["argv"] == ["echo", "local"]


def test_run_allowlisted_action_dry_run_returns_preview(tmp_path):
    _write_admin_config(tmp_path)

    async def _go():
        transport = _FakeLocalTransport()
        result = await run_allowlisted_action(
            workdir=str(tmp_path),
            action_id="clear_logs",
            server_id="web-01",
            dry_run=True,
            local_transport=transport,
        )
        assert result.dry_run is True
        assert result.applied is False
        assert result.success is True
        assert transport.calls == []
        mem_path = admin_root(str(tmp_path)) / "servers" / "web-01" / "memory" / "notes.md"
        assert mem_path.exists()
        assert "DRY-RUN" in mem_path.read_text(encoding="utf-8")

    asyncio.run(_go())


def test_run_allowlisted_action_executes_local_success(tmp_path):
    _write_admin_config(tmp_path)

    async def _go():
        transport = _FakeLocalTransport(_FakeLocalResult(rc=0, stdout="done\n"))
        result = await run_allowlisted_action(
            workdir=str(tmp_path),
            action_id="clear_logs",
            server_id="web-01",
            dry_run=False,
            local_transport=transport,
        )
        assert result.success is True
        assert result.applied is True
        assert result.exit_code == 0
        assert "done" in result.stdout
        assert len(transport.calls) == 1
        mem = (admin_root(str(tmp_path)) / "servers" / "web-01" / "memory" / "notes.md").read_text(encoding="utf-8")
        assert "EXECUTED" in mem

    asyncio.run(_go())


def test_run_allowlisted_action_failure_returns_error(tmp_path):
    _write_admin_config(tmp_path)

    async def _go():
        transport = _FakeLocalTransport(raise_exc=RuntimeError("boom"))
        result = await run_allowlisted_action(
            workdir=str(tmp_path),
            action_id="clear_logs",
            server_id="web-01",
            dry_run=False,
            local_transport=transport,
        )
        assert result.success is False
        assert result.applied is False
        assert result.error == "boom"
        mem = (admin_root(str(tmp_path)) / "servers" / "web-01" / "memory" / "notes.md").read_text(encoding="utf-8")
        assert "ERROR" in mem

    asyncio.run(_go())


def test_run_allowlisted_action_non_zero_exit_is_failure(tmp_path):
    _write_admin_config(tmp_path)

    async def _go():
        transport = _FakeLocalTransport(_FakeLocalResult(rc=2, stderr="nope"))
        result = await run_allowlisted_action(
            workdir=str(tmp_path),
            action_id="clear_logs",
            server_id="web-01",
            dry_run=False,
            local_transport=transport,
        )
        assert result.success is False
        assert result.applied is False
        assert result.exit_code == 2

    asyncio.run(_go())


def test_run_allowlisted_action_rejects_unknown_action(tmp_path):
    _write_admin_config(tmp_path)

    async def _go():
        with pytest.raises(AdminToolError):
            await run_allowlisted_action(
                workdir=str(tmp_path),
                action_id="does_not_exist",
                server_id="web-01",
                dry_run=True,
            )

    asyncio.run(_go())


def test_run_allowlisted_action_ssh_requires_host_key(tmp_path):
    _write_admin_config(tmp_path, extra_ssh={
        "remote_check": {"argv": ["uptime"]}  # no host/key
    })

    async def _go():
        with pytest.raises(AdminToolError):
            await run_allowlisted_action(
                workdir=str(tmp_path),
                action_id="remote_check",
                server_id="db-01",
                target_hint="ssh",
                dry_run=False,
            )

    asyncio.run(_go())


def test_write_escalation_records_incident_and_note(tmp_path):
    store = _FakeStateStore()
    result = write_escalation(
        workdir=str(tmp_path),
        state_store=store,
        server_id="web-01",
        reason="nginx down and I cannot fix",
        urgency="high",
        context={"last_action": "systemctl restart nginx"},
    )
    assert result["incident_id"].startswith("esc-")
    assert result["urgency"] == "high"
    assert len(store.records) == 1
    notes = (admin_root(str(tmp_path)) / "servers" / "web-01" / "memory" / "notes.md").read_text(encoding="utf-8")
    assert "ESCALATION" in notes


def test_write_escalation_rejects_empty_reason(tmp_path):
    with pytest.raises(AdminToolError):
        write_escalation(
            workdir=str(tmp_path), state_store=_FakeStateStore(),
            server_id="x", reason="   ", urgency="low",
        )


def test_write_escalation_rejects_invalid_urgency(tmp_path):
    with pytest.raises(AdminToolError):
        write_escalation(
            workdir=str(tmp_path), state_store=_FakeStateStore(),
            server_id="x", reason="ok", urgency="catastrophic",
        )


def test_build_server_dossier_collects_sections(tmp_path):
    # предварительно создаём baseline и memory
    from modes.admin.baseline import apply_scan_result
    from modes.admin.memory import ServerMemory
    from modes.admin.snapshot_store import AdminSnapshotStore

    apply_scan_result(str(tmp_path), "web-01", {"server_id": "web-01", "checks": {"os.kernel": "6.8"}})
    mem = ServerMemory(str(tmp_path), "web-01")
    mem.update_fact("service_manager", "systemd")
    mem.append_note("nginx notes", source="agent")
    store = AdminSnapshotStore.for_server(str(tmp_path), "web-01")
    store.insert_drift(check_id="x", severity=SEVERITY_WARN, new_value="v")

    dossier = build_server_dossier(
        workdir=str(tmp_path),
        server_id="web-01",
        alert_tags=["nginx"],
    )
    assert dossier["server_id"] == "web-01"
    assert dossier["baseline"]["checks"]["os.kernel"] == "6.8"
    assert dossier["facts"]["service_manager"] == "systemd"
    assert any("nginx notes" in n["text"] for n in dossier["recent_notes"])
    assert dossier["open_drifts_summary"][SEVERITY_WARN] == 1
    assert isinstance(dossier["recent_drifts"], list)
    assert isinstance(dossier["runbooks"], list)


def test_action_run_result_to_dict_roundtrip():
    res = ActionRunResult(
        success=True, action_id="a", server_id="s", target="local",
        dry_run=False, applied=True, exit_code=0,
        stdout="out", stderr="", duration_ms=100, command_preview="bash -lc echo",
    )
    d = res.to_dict()
    assert d["success"] is True
    assert d["action_id"] == "a"
    assert d["command_preview"] == "bash -lc echo"
