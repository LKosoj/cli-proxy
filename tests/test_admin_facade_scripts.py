from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
import yaml

from modes.admin.facade import AdminAutonomyService
from modes.admin.script_sources import ScriptSourceError
from modes.admin.snapshot_store import admin_root


def _write_config(workdir: Path, *, source_dirs=None, servers=None):
    root = admin_root(str(workdir))
    root.mkdir(parents=True, exist_ok=True)
    payload = {
        "admin": {
            "allowlist": {"local": {}, "ssh": {}},
            "monitor": {"servers": servers or [{"server_id": "dev-01", "transport": "local"}]},
            "runbook_sources": [str(p) for p in (source_dirs or [])],
            "runtime": {},
        }
    }
    (root / "config.yaml").write_text(
        yaml.safe_dump(payload, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )


def _write_source_script(workdir: Path, name: str = "01-run.sh", body: str = "#!/bin/bash\necho ok\n") -> Path:
    src = workdir / "sources"
    src.mkdir(parents=True, exist_ok=True)
    path = src / name
    path.write_text(body, encoding="utf-8")
    return path


def test_scan_script_sources_uses_whitelist(tmp_path):
    src = tmp_path / "sources"
    src.mkdir()
    (src / "01-run.sh").write_text("#!/bin/bash\necho ok\n", encoding="utf-8")
    (src / "02-run.bash").write_text("#!/bin/bash\necho b\n", encoding="utf-8")
    _write_config(tmp_path, source_dirs=[src])
    fac = AdminAutonomyService(str(tmp_path))
    files = fac.scan_script_sources(str(src))
    names = [f.name for f in files]
    assert names == ["01-run.sh", "02-run.bash"]


def test_scan_script_sources_outside_whitelist_raises(tmp_path):
    _write_config(tmp_path, source_dirs=[tmp_path / "only"])
    (tmp_path / "only").mkdir()
    other = tmp_path / "other"
    other.mkdir()
    (other / "bad.sh").write_text("#", encoding="utf-8")
    fac = AdminAutonomyService(str(tmp_path))
    with pytest.raises(ScriptSourceError):
        fac.scan_script_sources(str(other))


def test_read_script_from_source(tmp_path):
    src = tmp_path / "sources"
    src.mkdir()
    path = src / "a.sh"
    path.write_text("echo hi\n", encoding="utf-8")
    _write_config(tmp_path, source_dirs=[src])
    fac = AdminAutonomyService(str(tmp_path))
    assert "echo hi" in fac.read_script_from_source(str(path))


def test_create_runbook_from_scripts(tmp_path):
    src = _write_source_script(tmp_path)
    _write_config(tmp_path, source_dirs=[src.parent])
    fac = AdminAutonomyService(str(tmp_path))
    rb = fac.create_runbook_from_scripts(
        title="svc reload",
        dev_server_id="dev-01",
        scripts=[{"source_path": str(src), "target_hint": "local"}],
        tags=["svc"],
    )
    assert rb.id.startswith("rb-")
    assert "dev-01" in rb.servers
    assert rb.metadata["auto_action"]["confidence"] == 0.0


def test_create_runbook_rejects_inline_body_without_source(tmp_path):
    _write_config(tmp_path)
    fac = AdminAutonomyService(str(tmp_path))
    with pytest.raises(Exception, match="source_path"):
        fac.create_runbook_from_scripts(
            title="t",
            dev_server_id="dev-01",
            scripts=[{"name": "a.sh", "body": "echo x\n"}],
        )


def test_create_runbook_rejects_source_outside_whitelist(tmp_path):
    src = _write_source_script(tmp_path)
    # whitelist пустой
    _write_config(tmp_path, source_dirs=[])
    fac = AdminAutonomyService(str(tmp_path))
    with pytest.raises(Exception):
        fac.create_runbook_from_scripts(
            title="t",
            dev_server_id="dev-01",
            scripts=[{"source_path": str(src)}],
        )


def test_validate_runbook_returns_ok_report(tmp_path):
    src = _write_source_script(tmp_path)
    _write_config(tmp_path, source_dirs=[src.parent])
    fac = AdminAutonomyService(str(tmp_path))
    rb = fac.create_runbook_from_scripts(
        title="t",
        dev_server_id="dev-01",
        scripts=[{"source_path": str(src)}],
    )
    report = asyncio.run(fac.validate_runbook(rb.id))
    assert report.ok
    assert report.rb_id == rb.id


def test_promote_runbook_adds_servers(tmp_path):
    src = _write_source_script(tmp_path)
    _write_config(tmp_path, source_dirs=[src.parent], servers=[
        {"server_id": "dev-01", "transport": "local"},
        {"server_id": "prod-01", "transport": "local"},
    ])
    fac = AdminAutonomyService(str(tmp_path))
    rb = fac.create_runbook_from_scripts(
        title="t",
        dev_server_id="dev-01",
        scripts=[{"source_path": str(src)}],
    )
    res = asyncio.run(fac.promote_runbook(rb.id, add_servers=["prod-01"], confidence=0.7))
    assert res.added_servers == ["prod-01"]
    assert res.confidence_after == 0.7
    assert res.validation is not None and res.validation.ok


def test_run_runbook_step_dry_run(tmp_path):
    src = _write_source_script(tmp_path)
    _write_config(tmp_path, source_dirs=[src.parent])
    fac = AdminAutonomyService(str(tmp_path))
    rb = fac.create_runbook_from_scripts(
        title="t",
        dev_server_id="dev-01",
        scripts=[{"source_path": str(src)}],
    )
    res = asyncio.run(fac.run_runbook_step(
        rb_id=rb.id, step_name="01-run", server_id="dev-01", dry_run=True,
    ))
    assert res.dry_run is True
    assert res.success is True
