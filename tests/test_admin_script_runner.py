from __future__ import annotations

import asyncio
from pathlib import Path
from typing import List

import pytest

from modes.admin.runbook_builder import BuildSpec, ScriptInput, build_runbook_from_scripts, scripts_dir
from modes.admin.runbook_promoter import promote_runbook
from modes.admin.script_runner import (
    DEFAULT_TIMEOUT_SEC,
    MAX_SCRIPT_BYTES,
    ScriptRunnerError,
    run_runbook_step,
)
from modes.admin.transports import (
    LocalCommandResult,
    LocalCommandSpec,
    SSHCommandResult,
    SSHCommandSpec,
)


class FakeLocalTransport:
    def __init__(self, *, returncode: int = 0, stdout: str = "ok\n", stderr: str = "", timed_out: bool = False):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.timed_out = timed_out
        self.calls: List[LocalCommandSpec] = []

    async def run(self, spec: LocalCommandSpec) -> LocalCommandResult:
        self.calls.append(spec)
        return LocalCommandResult(
            action_id=spec.action_id,
            returncode=self.returncode,
            stdout=self.stdout,
            stderr=self.stderr,
            timed_out=self.timed_out,
            duration_ms=5,
        )


class FakeSSHTransport:
    def __init__(self, *, returncode: int = 0, stdout: str = "ok\n", stderr: str = "", timed_out: bool = False):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.timed_out = timed_out
        self.calls: List[SSHCommandSpec] = []

    async def run(self, spec: SSHCommandSpec) -> SSHCommandResult:
        self.calls.append(spec)
        return SSHCommandResult(
            action_id=spec.action_id,
            host=spec.host,
            user=spec.user,
            port=spec.port,
            returncode=self.returncode,
            stdout=self.stdout,
            stderr=self.stderr,
            timed_out=self.timed_out,
            duration_ms=5,
        )


def _build(
    tmp_path: Path, *, rb_id="rb-run", dev="dev-01", target="local",
    body="#!/bin/bash\necho hello\n",
):
    spec = BuildSpec(
        title="Run",
        dev_server_id=dev,
        rb_id=rb_id,
        scripts=[ScriptInput(name="01-main.sh", body=body, target_hint=target)],
        tags=["svc"],
    )
    return build_runbook_from_scripts(str(tmp_path), spec)


def test_dry_run_returns_preview_without_transport_call(tmp_path):
    _build(tmp_path)
    local = FakeLocalTransport()
    res = asyncio.run(run_runbook_step(
        workdir=str(tmp_path), rb_id="rb-run", step_name="01-main",
        server_id="dev-01", dry_run=True, local_transport=local,
    ))
    assert res.dry_run is True
    assert res.applied is False
    assert res.success is True
    assert res.checksum_ok is True
    assert res.command_preview == "bash 01-main.sh"
    assert local.calls == []


def test_local_execution_invokes_local_transport(tmp_path):
    _build(tmp_path)
    local = FakeLocalTransport(returncode=0, stdout="hello\n")
    res = asyncio.run(run_runbook_step(
        workdir=str(tmp_path), rb_id="rb-run", step_name="01-main",
        server_id="dev-01", dry_run=False, local_transport=local,
    ))
    assert res.success is True
    assert res.applied is True
    assert res.exit_code == 0
    assert res.target == "local"
    assert res.stdout == "hello\n"
    assert len(local.calls) == 1
    spec = local.calls[0]
    assert spec.argv[0] == "bash"
    assert spec.argv[1] == "-c"
    assert "echo hello" in spec.argv[2]


def test_local_nonzero_marks_failure(tmp_path):
    _build(tmp_path)
    local = FakeLocalTransport(returncode=7, stderr="boom")
    res = asyncio.run(run_runbook_step(
        workdir=str(tmp_path), rb_id="rb-run", step_name="01-main",
        server_id="dev-01", dry_run=False, local_transport=local,
    ))
    assert res.success is False
    assert res.applied is False
    assert res.exit_code == 7
    assert res.stderr == "boom"


def test_checksum_mismatch_raises(tmp_path):
    _build(tmp_path)
    sdir = scripts_dir(str(tmp_path), "rb-run")
    (sdir / "01-main.sh").write_text("#!/bin/bash\necho tampered\n", encoding="utf-8")
    with pytest.raises(ScriptRunnerError, match="checksum mismatch"):
        asyncio.run(run_runbook_step(
            workdir=str(tmp_path), rb_id="rb-run", step_name="01-main",
            server_id="dev-01", dry_run=True,
        ))


def test_verify_checksum_false_allows_tampered(tmp_path):
    _build(tmp_path)
    sdir = scripts_dir(str(tmp_path), "rb-run")
    (sdir / "01-main.sh").write_text("#!/bin/bash\necho tampered\n", encoding="utf-8")
    local = FakeLocalTransport()
    res = asyncio.run(run_runbook_step(
        workdir=str(tmp_path), rb_id="rb-run", step_name="01-main",
        server_id="dev-01", dry_run=False, verify_checksum=False,
        local_transport=local,
    ))
    assert res.success is True
    assert res.checksum_ok is None


def test_rejects_unauthorized_server(tmp_path):
    _build(tmp_path)  # servers=[dev-01]
    with pytest.raises(ScriptRunnerError, match="not in runbook.servers"):
        asyncio.run(run_runbook_step(
            workdir=str(tmp_path), rb_id="rb-run", step_name="01-main",
            server_id="prod-01", dry_run=True,
        ))


def test_allows_after_promote(tmp_path):
    _build(tmp_path)
    asyncio.run(promote_runbook(
        str(tmp_path), "rb-run", add_servers=["prod-01"], run_validation=False,
    ))
    local = FakeLocalTransport()
    res = asyncio.run(run_runbook_step(
        workdir=str(tmp_path), rb_id="rb-run", step_name="01-main",
        server_id="prod-01", dry_run=True, local_transport=local,
    ))
    assert res.success is True
    assert res.server_id == "prod-01"


def test_rejects_unknown_runbook(tmp_path):
    with pytest.raises(ScriptRunnerError, match="runbook not found"):
        asyncio.run(run_runbook_step(
            workdir=str(tmp_path), rb_id="rb-nope", step_name="x",
            server_id="dev-01", dry_run=True,
        ))


def test_rejects_unknown_step(tmp_path):
    _build(tmp_path)
    with pytest.raises(ScriptRunnerError, match="not found in runbook"):
        asyncio.run(run_runbook_step(
            workdir=str(tmp_path), rb_id="rb-run", step_name="99-wrong",
            server_id="dev-01", dry_run=True,
        ))


def test_rejects_symlink(tmp_path):
    _build(tmp_path)
    sdir = scripts_dir(str(tmp_path), "rb-run")
    real = sdir / "01-main.sh"
    real_copy = sdir / "real.sh"
    real.rename(real_copy)
    import os as _os
    _os.symlink(real_copy, real)
    with pytest.raises(ScriptRunnerError, match="symlink"):
        asyncio.run(run_runbook_step(
            workdir=str(tmp_path), rb_id="rb-run", step_name="01-main",
            server_id="dev-01", dry_run=True,
        ))


def test_rejects_missing_script(tmp_path):
    _build(tmp_path)
    (scripts_dir(str(tmp_path), "rb-run") / "01-main.sh").unlink()
    with pytest.raises(ScriptRunnerError, match="script missing"):
        asyncio.run(run_runbook_step(
            workdir=str(tmp_path), rb_id="rb-run", step_name="01-main",
            server_id="dev-01", dry_run=True,
        ))


def test_ssh_target_reads_server_cfg(tmp_path):
    _build(tmp_path, target="ssh")
    # Fake admin_cfg с описанием dev-01 как ssh-сервера.
    key = tmp_path / "id_key"
    key.write_text("fake", encoding="utf-8")
    admin_cfg = {
        "servers": {
            "dev-01": {
                "id": "dev-01",
                "target": "ssh",
                "host": "example.com",
                "key_path": str(key),
                "user": "root",
                "port": 2222,
            },
        },
    }
    ssh = FakeSSHTransport(returncode=0)
    res = asyncio.run(run_runbook_step(
        workdir=str(tmp_path), rb_id="rb-run", step_name="01-main",
        server_id="dev-01", dry_run=False, admin_cfg=admin_cfg, ssh_transport=ssh,
    ))
    assert res.success is True
    assert res.target == "ssh"
    assert len(ssh.calls) == 1
    spec = ssh.calls[0]
    assert spec.host == "example.com"
    assert spec.user == "root"
    assert spec.port == 2222
    assert spec.argv[0] == "bash"
    assert spec.argv[1] == "-c"


def test_ssh_target_requires_ssh_server_cfg(tmp_path):
    _build(tmp_path, target="ssh")
    admin_cfg = {
        "servers": {
            "dev-01": {"id": "dev-01", "target": "local"},  # target mismatch
        },
    }
    with pytest.raises(ScriptRunnerError, match="admin.servers.dev-01.target"):
        asyncio.run(run_runbook_step(
            workdir=str(tmp_path), rb_id="rb-run", step_name="01-main",
            server_id="dev-01", dry_run=False, admin_cfg=admin_cfg,
        ))


def test_ssh_target_missing_server_cfg(tmp_path):
    _build(tmp_path, target="ssh")
    with pytest.raises(ScriptRunnerError, match="not found in admin.servers"):
        asyncio.run(run_runbook_step(
            workdir=str(tmp_path), rb_id="rb-run", step_name="01-main",
            server_id="dev-01", dry_run=False, admin_cfg={"servers": {}},
        ))


def test_max_script_bytes(tmp_path):
    huge = "echo x\n" * (MAX_SCRIPT_BYTES // 5)  # > MAX
    _build(tmp_path, body=huge)
    with pytest.raises(ScriptRunnerError, match="exceeds MAX_SCRIPT_BYTES"):
        asyncio.run(run_runbook_step(
            workdir=str(tmp_path), rb_id="rb-run", step_name="01-main",
            server_id="dev-01", dry_run=True,
        ))


def test_timeout_sec_passed_to_transport(tmp_path):
    _build(tmp_path)
    local = FakeLocalTransport()
    asyncio.run(run_runbook_step(
        workdir=str(tmp_path), rb_id="rb-run", step_name="01-main",
        server_id="dev-01", dry_run=False, local_transport=local, timeout_sec=5.0,
    ))
    assert local.calls[0].timeout_sec == 5.0


def test_default_timeout_used(tmp_path):
    _build(tmp_path)
    local = FakeLocalTransport()
    asyncio.run(run_runbook_step(
        workdir=str(tmp_path), rb_id="rb-run", step_name="01-main",
        server_id="dev-01", dry_run=False, local_transport=local,
    ))
    assert local.calls[0].timeout_sec == DEFAULT_TIMEOUT_SEC


def test_wildcard_in_servers_is_ignored(tmp_path):
    """Если кто-то руками поставил `*` в rb.servers, это не должно авторизовать всех."""
    _build(tmp_path)
    # Руками ломаем servers: записываем wildcard-паттерн.
    rb_path = __import__("modes.admin.runbooks", fromlist=["global_runbooks_dir"]).global_runbooks_dir(str(tmp_path)) / "rb-run.md"
    text = rb_path.read_text(encoding="utf-8")
    text = text.replace("- dev-01", "- '*'")
    rb_path.write_text(text, encoding="utf-8")
    with pytest.raises(ScriptRunnerError, match="not in runbook.servers"):
        asyncio.run(run_runbook_step(
            workdir=str(tmp_path), rb_id="rb-run", step_name="01-main",
            server_id="prod-999", dry_run=True,
        ))


def test_verify_checksum_false_writes_audit_note(tmp_path):
    from modes.admin.memory import ServerMemory
    _build(tmp_path)
    local = FakeLocalTransport()
    asyncio.run(run_runbook_step(
        workdir=str(tmp_path), rb_id="rb-run", step_name="01-main",
        server_id="dev-01", dry_run=False, verify_checksum=False,
        local_transport=local,
    ))
    notes = ServerMemory(str(tmp_path), "dev-01").get_notes()
    assert "INTEGRITY-OFF" in notes
