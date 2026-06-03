from __future__ import annotations

from pathlib import Path

import pytest

from modes.sdd.decisions import append_decision, load_decisions


def test_load_missing_returns_empty(tmp_path: Path) -> None:
    assert load_decisions(str(tmp_path)) == ""


def test_load_empty_workdir_returns_empty() -> None:
    assert load_decisions("") == ""
    assert load_decisions(None) == ""


def test_append_creates_and_loads(tmp_path: Path) -> None:
    written = append_decision(
        str(tmp_path),
        feature_slug="power-operation",
        out_of_scope=["logarithms", "roots"],
        plan_decisions=["reuse existing arithmetic dispatcher"],
    )
    assert written is True
    text = load_decisions(str(tmp_path))
    assert "## power-operation" in text
    assert "logarithms" in text
    assert "reuse existing arithmetic dispatcher" in text


def test_append_idempotent_per_slug(tmp_path: Path) -> None:
    assert append_decision(str(tmp_path), feature_slug="addition", out_of_scope=["x"]) is True
    first = load_decisions(str(tmp_path))
    assert append_decision(str(tmp_path), feature_slug="addition", out_of_scope=["y"]) is False
    assert load_decisions(str(tmp_path)) == first  # no growth, no duplicate


def test_append_multiple_features_accumulate(tmp_path: Path) -> None:
    append_decision(str(tmp_path), feature_slug="addition", out_of_scope=["overflow"])
    append_decision(str(tmp_path), feature_slug="subtraction", out_of_scope=["underflow"])
    text = load_decisions(str(tmp_path))
    assert "## addition" in text
    assert "## subtraction" in text


def test_append_empty_slug_rejected(tmp_path: Path) -> None:
    assert append_decision(str(tmp_path), feature_slug="  ") is False
    assert load_decisions(str(tmp_path)) == ""


def test_append_handles_no_lists(tmp_path: Path) -> None:
    assert append_decision(str(tmp_path), feature_slug="bare") is True
    text = load_decisions(str(tmp_path))
    assert "## bare" in text
    assert "(none)" in text


def test_append_rejects_symlinked_file_outside_workdir(tmp_path: Path) -> None:
    cli_dir = tmp_path / ".cli-proxy"
    cli_dir.mkdir()
    outside = tmp_path.parent / f"{tmp_path.name}-evil.md"
    outside.write_text("evil\n", encoding="utf-8")
    link = cli_dir / "decisions.md"
    try:
        link.symlink_to(outside)
    except (NotImplementedError, OSError):
        pytest.skip("symlinks are not available on this filesystem")
    assert append_decision(str(tmp_path), feature_slug="x", out_of_scope=["a"]) is False
    assert outside.read_text(encoding="utf-8") == "evil\n"
