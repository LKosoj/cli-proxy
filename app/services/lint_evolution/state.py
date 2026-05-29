from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import asdict, dataclass, field
from typing import Any

from modes.sdk.runtime.json_normalizer import loads_safe

from .paths import ensure_directories, project_id_for, state_path

logger = logging.getLogger(__name__)


@dataclass
class LevelState:
    last_run_ts: float = 0.0
    last_success_ts: float = 0.0
    last_error_ts: float = 0.0
    consecutive_failures: int = 0
    error_cooldown_until: float = 0.0
    lock_owner: str = ""
    lock_expires_at: float = 0.0


@dataclass
class ProjectState:
    project_id: str = ""
    schema_version: int = 1
    level1: LevelState = field(default_factory=LevelState)
    level2: LevelState = field(default_factory=LevelState)
    level3: LevelState = field(default_factory=LevelState)


@dataclass
class LintEvolutionState:
    projects: dict[str, ProjectState] = field(default_factory=dict)


def _to_state(payload: dict[str, Any]) -> LintEvolutionState:
    out = LintEvolutionState()
    raw_projects = (payload or {}).get("projects") or {}
    for pid, ps in raw_projects.items():
        levels = {}
        for lvl_key in ("level1", "level2", "level3"):
            lvl = (ps or {}).get(lvl_key) or {}
            levels[lvl_key] = LevelState(
                last_run_ts=float(lvl.get("last_run_ts") or 0.0),
                last_success_ts=float(lvl.get("last_success_ts") or 0.0),
                last_error_ts=float(lvl.get("last_error_ts") or 0.0),
                consecutive_failures=int(lvl.get("consecutive_failures") or 0),
                error_cooldown_until=float(lvl.get("error_cooldown_until") or 0.0),
                lock_owner=str(lvl.get("lock_owner") or ""),
                lock_expires_at=float(lvl.get("lock_expires_at") or 0.0),
            )
        out.projects[str(pid)] = ProjectState(
            project_id=str(ps.get("project_id") or pid),
            schema_version=int(ps.get("schema_version") or 1),
            **levels,
        )
    return out


def load_state(workdir: str) -> LintEvolutionState:
    path = state_path(workdir)
    if not path.exists():
        return LintEvolutionState()
    try:
        return _to_state(loads_safe(path.read_text(encoding="utf-8")))
    except Exception as exc:
        logger.exception("lint_evolution: failed to read state %s: %s", path, exc)
        return LintEvolutionState()


def save_state(workdir: str, state: LintEvolutionState) -> None:
    ensure_directories(workdir)
    path = state_path(workdir)
    payload = {"projects": {pid: asdict(ps) for pid, ps in state.projects.items()}}
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def get_or_create_project(state: LintEvolutionState, workdir: str) -> ProjectState:
    pid = project_id_for(workdir)
    project = state.projects.get(pid)
    if project is None:
        project = ProjectState(project_id=pid)
        state.projects[pid] = project
    return project


def acquire_lock(level: LevelState, *, owner: str, ttl_seconds: float) -> bool:
    now = time.time()
    if level.lock_owner and level.lock_expires_at > now:
        return False
    level.lock_owner = owner
    level.lock_expires_at = now + max(60.0, ttl_seconds)
    return True


def release_lock(level: LevelState, *, owner: str) -> None:
    if level.lock_owner == owner:
        level.lock_owner = ""
        level.lock_expires_at = 0.0


def cooldown_active(level: LevelState, *, cooldown_seconds: float, now: float | None = None) -> bool:
    when = float(now if now is not None else time.time())
    if level.error_cooldown_until > when:
        return True
    if level.last_run_ts <= 0:
        return False
    return (when - level.last_run_ts) < float(cooldown_seconds)


def mark_success(level: LevelState) -> None:
    now = time.time()
    level.last_run_ts = now
    level.last_success_ts = now
    level.consecutive_failures = 0
    level.error_cooldown_until = 0.0


def mark_error(level: LevelState, *, retry_seconds: float) -> None:
    now = time.time()
    level.last_error_ts = now
    level.consecutive_failures += 1
    level.error_cooldown_until = now + max(60.0, retry_seconds)
