from __future__ import annotations

from pathlib import Path

import pytest

from app.services.lint_evolution import autopause


def test_initially_not_paused(tmp_path: Path) -> None:
    workdir = str(tmp_path)
    assert autopause.is_paused(workdir, 1) is False
    assert autopause.is_paused(workdir, 2) is False
    assert autopause.is_paused(workdir, 3) is False


def test_pause_and_resume(tmp_path: Path) -> None:
    workdir = str(tmp_path)
    autopause.pause(workdir, 2, reason="schema_thrash")
    assert autopause.is_paused(workdir, 2) is True
    state = autopause.status(workdir)
    assert state["2"].reason == "schema_thrash"
    assert state["2"].ts > 0

    assert autopause.resume(workdir, 2) is True
    assert autopause.is_paused(workdir, 2) is False
    assert autopause.resume(workdir, 2) is False  # already resumed


def test_independent_levels(tmp_path: Path) -> None:
    workdir = str(tmp_path)
    autopause.pause(workdir, 1, reason="fp_canary")
    autopause.pause(workdir, 3, reason="drift")
    assert autopause.is_paused(workdir, 1) is True
    assert autopause.is_paused(workdir, 2) is False
    assert autopause.is_paused(workdir, 3) is True


def test_invalid_level(tmp_path: Path) -> None:
    workdir = str(tmp_path)
    with pytest.raises(ValueError):
        autopause.pause(workdir, 4, reason="x")
    with pytest.raises(ValueError):
        autopause.is_paused(workdir, 0)
