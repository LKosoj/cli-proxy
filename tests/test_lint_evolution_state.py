from __future__ import annotations

import time
from pathlib import Path

from app.services.lint_evolution.state import (
    LevelState,
    acquire_lock,
    cooldown_active,
    get_or_create_project,
    load_state,
    mark_error,
    mark_success,
    release_lock,
    save_state,
)


def test_save_and_load_roundtrip(tmp_path: Path) -> None:
    workdir = str(tmp_path)
    state = load_state(workdir)
    project = get_or_create_project(state, workdir)
    project.level1.last_run_ts = 100.0
    project.level1.consecutive_failures = 2
    project.schema_version = 3
    save_state(workdir, state)

    reloaded = load_state(workdir)
    assert list(reloaded.projects.keys()) == list(state.projects.keys())
    pid = next(iter(reloaded.projects.keys()))
    assert reloaded.projects[pid].level1.last_run_ts == 100.0
    assert reloaded.projects[pid].level1.consecutive_failures == 2
    assert reloaded.projects[pid].schema_version == 3


def test_cooldown_active_respects_window() -> None:
    level = LevelState(last_run_ts=1000.0)
    assert cooldown_active(level, cooldown_seconds=3600, now=1000.0 + 1800.0) is True
    assert cooldown_active(level, cooldown_seconds=3600, now=1000.0 + 3700.0) is False


def test_cooldown_inactive_when_never_ran() -> None:
    level = LevelState()
    assert cooldown_active(level, cooldown_seconds=3600, now=time.time()) is False


def test_error_cooldown_respected() -> None:
    level = LevelState()
    mark_error(level, retry_seconds=3600)
    assert cooldown_active(level, cooldown_seconds=1, now=time.time() + 100) is True


def test_mark_success_clears_failures() -> None:
    level = LevelState(consecutive_failures=5)
    mark_error(level, retry_seconds=60)
    assert level.consecutive_failures == 6
    mark_success(level)
    assert level.consecutive_failures == 0
    assert level.error_cooldown_until == 0.0


def test_acquire_and_release_lock() -> None:
    level = LevelState()
    assert acquire_lock(level, owner="me", ttl_seconds=300) is True
    assert acquire_lock(level, owner="other", ttl_seconds=300) is False
    release_lock(level, owner="me")
    assert acquire_lock(level, owner="other", ttl_seconds=300) is True


def test_lock_expires() -> None:
    level = LevelState()
    acquire_lock(level, owner="me", ttl_seconds=60)
    level.lock_expires_at = 1.0
    assert acquire_lock(level, owner="other", ttl_seconds=300) is True
    assert level.lock_owner == "other"
