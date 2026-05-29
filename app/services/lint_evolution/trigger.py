from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

from . import (
    autopause,
    canary_metric,
    fingerprints,
    schema_evolver,
    state as state_store,
    weights_regressor,
)
from .canary_metric import CanaryConfig
from .evolver import L1Config, run_level1
from .schema_evolver import L2Config
from .weights_regressor import L3Config

logger = logging.getLogger(__name__)


_LOCK_OWNER = "trigger"
_DEFAULT_LOCK_TTL_SECONDS = 30 * 60.0
_DEFAULT_ERROR_RETRY_SECONDS = 3600.0


@dataclass
class TriggerConfig:
    enabled: bool = True
    level1_cooldown_seconds: float = 24 * 3600.0
    level2_cooldown_seconds: float = 30 * 24 * 3600.0
    level3_cooldown_seconds: float = 30 * 24 * 3600.0
    lock_ttl_seconds: float = _DEFAULT_LOCK_TTL_SECONDS
    error_retry_seconds: float = _DEFAULT_ERROR_RETRY_SECONDS
    l1: L1Config = field(default_factory=L1Config)
    l2: L2Config = field(default_factory=L2Config)
    l3: L3Config = field(default_factory=L3Config)
    canary: CanaryConfig = field(default_factory=CanaryConfig)


@dataclass
class TriggerDecision:
    levels_started: list[int] = field(default_factory=list)
    skipped: dict[int, str] = field(default_factory=dict)
    canary_triggered: list[str] = field(default_factory=list)


SpawnFn = Callable[[Awaitable[Any]], Any]


def maybe_run_evolution(
    *,
    workdir: str,
    project_root: Path,
    config: TriggerConfig,
    spawn: SpawnFn,
    classify_fn: Callable[[str, list[str] | None], Awaitable[dict | None]] | None = None,
    meta_classify_fn: Callable[[list[str], list[str]], Awaitable[list[dict[str, Any]] | None]] | None = None,
    now: float | None = None,
) -> TriggerDecision:
    """Single entrypoint called on session activity. Schedules levels via spawn(coro)."""
    decision = TriggerDecision()
    if not config.enabled:
        decision.skipped[0] = "disabled"
        return decision

    when = float(now if now is not None else time.time())

    # Canary check first — may set autopause flags before we evaluate levels
    try:
        report = canary_metric.evaluate(
            workdir,
            project_id=_project_id(workdir),
            config=config.canary,
            now=when,
        )
        decision.canary_triggered = list(report.triggered)
    except Exception as exc:
        logger.exception("lint_evolution.trigger: canary failed: %s", exc)

    state = state_store.load_state(workdir)
    project = state_store.get_or_create_project(state, workdir)

    if _try_schedule_level(
        decision=decision,
        level=1,
        level_state=project.level1,
        cooldown=config.level1_cooldown_seconds,
        when=when,
        workdir=workdir,
        autopaused=autopause.is_paused(workdir, 1),
        lock_ttl_seconds=config.lock_ttl_seconds,
        coroutine_factory=lambda: _wrap_level1(
            workdir=workdir,
            project=project,
            project_root=project_root,
            classify_fn=classify_fn,
            config=config,
        ),
        spawn=spawn,
        require_signals=True,
        require_callable=classify_fn,
    ):
        pass

    if _try_schedule_level(
        decision=decision,
        level=2,
        level_state=project.level2,
        cooldown=config.level2_cooldown_seconds,
        when=when,
        workdir=workdir,
        autopaused=autopause.is_paused(workdir, 2),
        lock_ttl_seconds=config.lock_ttl_seconds,
        coroutine_factory=lambda: _wrap_level2(
            workdir=workdir,
            project=project,
            project_root=project_root,
            meta_classify_fn=meta_classify_fn,
            config=config,
        ),
        spawn=spawn,
        require_signals=False,
        require_callable=meta_classify_fn,
    ):
        pass

    if _try_schedule_level(
        decision=decision,
        level=3,
        level_state=project.level3,
        cooldown=config.level3_cooldown_seconds,
        when=when,
        workdir=workdir,
        autopaused=autopause.is_paused(workdir, 3),
        lock_ttl_seconds=config.lock_ttl_seconds,
        coroutine_factory=lambda: _wrap_level3(
            workdir=workdir,
            project=project,
            config=config,
        ),
        spawn=spawn,
        require_signals=False,
    ):
        pass

    state_store.save_state(workdir, state)
    return decision


def _project_id(workdir: str) -> str:
    from .paths import project_id_for

    return project_id_for(workdir)


def _try_schedule_level(
    *,
    decision: TriggerDecision,
    level: int,
    level_state,
    cooldown: float,
    when: float,
    workdir: str,
    autopaused: bool,
    lock_ttl_seconds: float,
    coroutine_factory: Callable[[], Awaitable[Any] | None],
    spawn: SpawnFn,
    require_signals: bool,
    require_callable: Any = True,
) -> bool:
    if autopaused:
        decision.skipped[level] = "autopaused"
        return False
    if state_store.cooldown_active(level_state, cooldown_seconds=cooldown, now=when):
        decision.skipped[level] = "cooldown"
        return False
    if not require_callable:
        decision.skipped[level] = "no_classifier"
        return False
    if require_signals:
        recent = fingerprints.signals_count_since(
            workdir,
            project_id=_project_id(workdir),
            since_ts=level_state.last_run_ts,
        )
        if recent <= 0 and level_state.last_run_ts > 0:
            decision.skipped[level] = "no_new_signals"
            return False
    if not state_store.acquire_lock(level_state, owner=_LOCK_OWNER, ttl_seconds=lock_ttl_seconds):
        decision.skipped[level] = "locked"
        return False

    coro = coroutine_factory()
    if coro is None:
        state_store.release_lock(level_state, owner=_LOCK_OWNER)
        decision.skipped[level] = "no_classifier"
        return False
    try:
        spawn(coro)
    except Exception as exc:
        logger.exception("lint_evolution.trigger: spawn level=%d failed: %s", level, exc)
        state_store.release_lock(level_state, owner=_LOCK_OWNER)
        decision.skipped[level] = "spawn_error"
        return False
    decision.levels_started.append(level)
    return True


async def _wrap_level1(
    *,
    workdir: str,
    project,
    project_root: Path,
    classify_fn,
    config: TriggerConfig,
):
    if classify_fn is None:
        state_store.release_lock(project.level1, owner=_LOCK_OWNER)
        return
    try:
        await run_level1(
            workdir=workdir,
            project_id=project.project_id,
            project_root=project_root,
            classify_fn=classify_fn,
            config=config.l1,
        )
        state_store.mark_success(project.level1)
    except Exception as exc:
        logger.exception("lint_evolution.trigger: level1 failed: %s", exc)
        state_store.mark_error(project.level1, retry_seconds=config.error_retry_seconds)
    finally:
        state_store.release_lock(project.level1, owner=_LOCK_OWNER)
        _persist(workdir, project)


async def _wrap_level2(
    *,
    workdir: str,
    project,
    project_root: Path,
    meta_classify_fn,
    config: TriggerConfig,
):
    if meta_classify_fn is None:
        state_store.release_lock(project.level2, owner=_LOCK_OWNER)
        return
    try:
        await schema_evolver.run_level2(
            workdir=workdir,
            project_id=project.project_id,
            project_root=project_root,
            meta_classify_fn=meta_classify_fn,
            config=config.l2,
        )
        state_store.mark_success(project.level2)
    except Exception as exc:
        logger.exception("lint_evolution.trigger: level2 failed: %s", exc)
        state_store.mark_error(project.level2, retry_seconds=config.error_retry_seconds)
    finally:
        state_store.release_lock(project.level2, owner=_LOCK_OWNER)
        _persist(workdir, project)


async def _wrap_level3(
    *,
    workdir: str,
    project,
    config: TriggerConfig,
):
    try:
        weights_regressor.run_level3(
            workdir=workdir,
            project_id=project.project_id,
            config=config.l3,
        )
        state_store.mark_success(project.level3)
    except Exception as exc:
        logger.exception("lint_evolution.trigger: level3 failed: %s", exc)
        state_store.mark_error(project.level3, retry_seconds=config.error_retry_seconds)
    finally:
        state_store.release_lock(project.level3, owner=_LOCK_OWNER)
        _persist(workdir, project)


def _persist(workdir: str, project) -> None:
    state = state_store.load_state(workdir)
    state.projects[project.project_id] = project
    state_store.save_state(workdir, state)
