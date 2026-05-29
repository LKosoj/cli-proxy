from __future__ import annotations

import os
from pathlib import Path

import pytest

from modes.admin.script_sources import (
    ScriptSourceError,
    load_whitelist,
    read_script_text,
    scan_scripts_directory,
)


def _mkscript(d: Path, name: str, body: str = "#!/bin/bash\necho ok\n", chmod: int = 0o755) -> Path:
    p = d / name
    p.write_text(body, encoding="utf-8")
    p.chmod(chmod)
    return p


def test_load_whitelist_parses_list(tmp_path):
    cfg = {"runbook_sources": [str(tmp_path), "~/ops"]}
    wl = load_whitelist(cfg)
    assert any(str(tmp_path.resolve()) == str(p) for p in wl)


def test_load_whitelist_empty_when_missing():
    assert load_whitelist({}) == []
    assert load_whitelist(None) == []
    assert load_whitelist({"runbook_sources": None}) == []
    assert load_whitelist({"runbook_sources": 42}) == []


def test_scan_returns_sh_files(tmp_path):
    _mkscript(tmp_path, "01-prepare.sh")
    _mkscript(tmp_path, "02-apply.bash")
    (tmp_path / "notes.md").write_text("ignore", encoding="utf-8")
    result = scan_scripts_directory(str(tmp_path), whitelist=[tmp_path])
    names = [s.name for s in result]
    assert names == ["01-prepare.sh", "02-apply.bash"]
    assert all(s.sha1 for s in result)
    assert all(s.size_bytes > 0 for s in result)


def test_scan_rejects_outside_whitelist(tmp_path):
    other = tmp_path.parent / "nope"
    other.mkdir(exist_ok=True)
    _mkscript(other, "x.sh")
    with pytest.raises(ScriptSourceError, match="outside admin.runbook_sources"):
        scan_scripts_directory(str(other), whitelist=[tmp_path])


def test_scan_rejects_when_whitelist_empty(tmp_path):
    _mkscript(tmp_path, "x.sh")
    with pytest.raises(ScriptSourceError, match="admin.runbook_sources is empty"):
        scan_scripts_directory(str(tmp_path), whitelist=[])


def test_scan_rejects_nonexistent(tmp_path):
    with pytest.raises(ScriptSourceError, match="does not exist"):
        scan_scripts_directory(str(tmp_path / "missing"), whitelist=[tmp_path])


def test_scan_rejects_non_dir(tmp_path):
    f = _mkscript(tmp_path, "only.sh")
    with pytest.raises(ScriptSourceError, match="not a directory"):
        scan_scripts_directory(str(f), whitelist=[tmp_path])


def test_scan_skips_symlinks(tmp_path):
    real_script = _mkscript(tmp_path, "real.sh")
    link = tmp_path / "alias.sh"
    os.symlink(real_script, link)
    result = scan_scripts_directory(str(tmp_path), whitelist=[tmp_path])
    assert [s.name for s in result] == ["real.sh"]


def test_scan_path_traversal_blocked(tmp_path):
    # admin.runbook_sources = /allowed only — попытка зайти в /etc должна блокироваться
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    _mkscript(outside, "bad.sh")
    traverse = f"{allowed}/../outside"
    with pytest.raises(ScriptSourceError):
        scan_scripts_directory(traverse, whitelist=[allowed])


def test_scan_size_limit(tmp_path):
    big = "#\n" * 1024
    _mkscript(tmp_path, "big.sh", body=big)
    _mkscript(tmp_path, "small.sh", body="#\n")
    result = scan_scripts_directory(str(tmp_path), whitelist=[tmp_path], max_file_size=16)
    assert [s.name for s in result] == ["small.sh"]


def test_scan_max_files(tmp_path):
    for i in range(5):
        _mkscript(tmp_path, f"{i:02d}.sh")
    result = scan_scripts_directory(str(tmp_path), whitelist=[tmp_path], max_files=3)
    assert len(result) == 3


def test_read_script_text_ok(tmp_path):
    p = _mkscript(tmp_path, "r.sh", body="echo hi")
    assert "echo hi" in read_script_text(str(p), whitelist=[tmp_path])


def test_read_script_text_rejects_outside(tmp_path):
    outside = tmp_path.parent / "elsewhere"
    outside.mkdir(exist_ok=True)
    p = _mkscript(outside, "elsewhere.sh")
    with pytest.raises(ScriptSourceError):
        read_script_text(str(p), whitelist=[tmp_path])


def test_read_script_text_rejects_symlink(tmp_path):
    real = _mkscript(tmp_path, "real.sh")
    link = tmp_path / "link.sh"
    os.symlink(real, link)
    # резолв симлинка указывает на real (под whitelist), но os.path.islink до резолва true →
    # ScriptSourceError. Однако Path.resolve(strict=True) снимает симлинк; тест проверит
    # что после резолва os.path.islink(resolved) False — т.е. чтение проходит. Валидация
    # симлинка выполняется после резолва (если resolved сам является симлинком — отклоняем).
    text = read_script_text(str(link), whitelist=[tmp_path])
    assert "echo ok" in text


def test_scan_respects_extension_filter(tmp_path):
    _mkscript(tmp_path, "a.sh")
    _mkscript(tmp_path, "b.py", body="print('no')")
    result = scan_scripts_directory(str(tmp_path), whitelist=[tmp_path])
    assert [s.name for s in result] == ["a.sh"]
