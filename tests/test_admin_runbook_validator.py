from __future__ import annotations

import asyncio
import os
import shutil
from pathlib import Path

import pytest

from modes.admin.runbook_builder import BuildSpec, ScriptInput, build_runbook_from_scripts, scripts_dir
from modes.admin.runbook_validator import validate_runbook


def _build_simple(tmp_path: Path, *, body="#!/bin/bash\necho ok\n", rb_id=None):
    spec = BuildSpec(
        title="T",
        dev_server_id="dev-01",
        rb_id=rb_id,
        scripts=[ScriptInput(name="01-run.sh", body=body)],
        tags=[],
    )
    return build_runbook_from_scripts(str(tmp_path), spec)


def test_validate_ok_for_freshly_built(tmp_path):
    rb = _build_simple(tmp_path)
    report = asyncio.run(validate_runbook(str(tmp_path), rb.id))
    assert report.ok, report.errors
    assert len(report.checks) == 1
    check = report.checks[0]
    assert check.checksum == "ok"
    if shutil.which("bash"):
        assert check.bash_n == "ok"


def test_validate_fails_on_checksum_mismatch(tmp_path):
    rb = _build_simple(tmp_path)
    path = scripts_dir(str(tmp_path), rb.id) / "01-run.sh"
    path.write_text("#!/bin/bash\necho tampered\n", encoding="utf-8")
    report = asyncio.run(validate_runbook(str(tmp_path), rb.id))
    assert not report.ok
    assert any("mismatch" in c.checksum for c in report.checks)


def test_validate_fails_on_missing_file(tmp_path):
    rb = _build_simple(tmp_path)
    (scripts_dir(str(tmp_path), rb.id) / "01-run.sh").unlink()
    report = asyncio.run(validate_runbook(str(tmp_path), rb.id))
    assert not report.ok
    assert any("missing" in e for e in report.errors)


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash required")
def test_validate_fails_on_bad_bash_syntax(tmp_path):
    body = "#!/bin/bash\nif fi\n"  # заведомо невалидно
    rb = _build_simple(tmp_path, body=body)
    report = asyncio.run(validate_runbook(str(tmp_path), rb.id))
    assert not report.ok
    assert any("bash -n" in e for e in report.errors)


def test_validate_reports_unknown_rb_id(tmp_path):
    report = asyncio.run(validate_runbook(str(tmp_path), "nope"))
    assert not report.ok
    assert any("not found" in e for e in report.errors)


def test_validate_rejects_symlink_in_scripts_dir(tmp_path):
    rb = _build_simple(tmp_path)
    sdir = scripts_dir(str(tmp_path), rb.id)
    # подменяем файл на symlink, указывающий внутрь sdir (сам резолв валидный, но symlink запрещён)
    real = sdir / "01-run.sh"
    real_copy = sdir / "real.sh"
    real.rename(real_copy)
    os.symlink(real_copy, real)
    report = asyncio.run(validate_runbook(str(tmp_path), rb.id))
    assert not report.ok
    assert any("symlink" in e for e in report.errors)


def test_validate_warns_when_checksum_missing(tmp_path):
    rb = _build_simple(tmp_path)
    rb_path = Path(rb.path)
    text = rb_path.read_text(encoding="utf-8")
    # Простой способ — удалить строку checksum:
    lines = [ln for ln in text.splitlines() if "checksum:" not in ln]
    rb_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    report = asyncio.run(validate_runbook(str(tmp_path), rb.id))
    assert any("checksum" in w for w in report.warnings)
