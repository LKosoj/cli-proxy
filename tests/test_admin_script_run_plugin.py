from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

from agent.plugins.admin_script_run import AdminScriptRunTool
from modes.admin.runbook_builder import BuildSpec, ScriptInput, build_runbook_from_scripts, scripts_dir


def _build(tmp_path: Path, *, rb_id="rb-plug", body="#!/bin/bash\necho plug\n"):
    return build_runbook_from_scripts(
        str(tmp_path),
        BuildSpec(
            title="Plug",
            dev_server_id="dev-01",
            rb_id=rb_id,
            scripts=[ScriptInput(name="01-do.sh", body=body)],
        ),
    )


def _ctx(tmp_path: Path):
    return {"session": SimpleNamespace(workdir=str(tmp_path))}


def test_spec_basic_fields():
    tool = AdminScriptRunTool()
    spec = tool.get_spec()
    assert spec.name == "admin_script_run"
    assert spec.risk_level == "high"
    assert "rb_id" in spec.parameters["required"]
    assert "step_name" in spec.parameters["required"]
    assert "server_id" in spec.parameters["required"]


def test_dry_run_returns_preview(tmp_path):
    _build(tmp_path)
    tool = AdminScriptRunTool()
    result = asyncio.run(tool.execute(
        {"rb_id": "rb-plug", "step_name": "01-do", "server_id": "dev-01"},
        _ctx(tmp_path),
    ))
    assert result["success"] is True
    assert result["dry_run"] is True
    assert result["applied"] is False
    assert "[DRY-RUN]" in result["output"]
    assert result["checksum_ok"] is True


def test_missing_args_error(tmp_path):
    tool = AdminScriptRunTool()
    result = asyncio.run(tool.execute({"rb_id": "rb-x"}, _ctx(tmp_path)))
    assert result["success"] is False
    assert "required" in result["error"]


def test_unknown_rb_returns_error_payload(tmp_path):
    tool = AdminScriptRunTool()
    result = asyncio.run(tool.execute(
        {"rb_id": "rb-nope", "step_name": "x", "server_id": "dev-01"},
        _ctx(tmp_path),
    ))
    assert result["success"] is False
    assert "runbook not found" in result["error"]


def test_unauthorized_server_error(tmp_path):
    _build(tmp_path)
    tool = AdminScriptRunTool()
    result = asyncio.run(tool.execute(
        {"rb_id": "rb-plug", "step_name": "01-do", "server_id": "prod-01"},
        _ctx(tmp_path),
    ))
    assert result["success"] is False
    assert "not in runbook.servers" in result["error"]


def test_tampered_checksum_error(tmp_path):
    _build(tmp_path)
    sdir = scripts_dir(str(tmp_path), "rb-plug")
    (sdir / "01-do.sh").write_text("#!/bin/bash\necho x\n", encoding="utf-8")
    tool = AdminScriptRunTool()
    result = asyncio.run(tool.execute(
        {"rb_id": "rb-plug", "step_name": "01-do", "server_id": "dev-01"},
        _ctx(tmp_path),
    ))
    assert result["success"] is False
    assert "checksum" in result["error"]


def test_no_session_in_ctx(tmp_path):
    tool = AdminScriptRunTool()
    result = asyncio.run(tool.execute(
        {"rb_id": "rb-plug", "step_name": "01-do", "server_id": "dev-01"},
        {},  # no session, no cwd
    ))
    assert result["success"] is False
    assert "workdir" in result["error"]


def test_timeout_sec_coerced(tmp_path):
    _build(tmp_path)
    tool = AdminScriptRunTool()
    # просто проверяем что не падает на передаче timeout_sec в dry-run
    result = asyncio.run(tool.execute(
        {
            "rb_id": "rb-plug",
            "step_name": "01-do",
            "server_id": "dev-01",
            "timeout_sec": 7,
        },
        _ctx(tmp_path),
    ))
    assert result["success"] is True
