from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

from app.services.lint_evolution import autopause, fingerprints, state as state_store, trigger
from app.services.lint_evolution.trigger import TriggerConfig


class _Spawner:
    def __init__(self) -> None:
        self.scheduled: list[asyncio.Task] = []

    def __call__(self, coro):
        loop = asyncio.get_event_loop()
        task = loop.create_task(coro)
        self.scheduled.append(task)
        return task


async def _classify(text: str, examples):
    return None  # never invoked: cooldown blocks or no signals; safe fallback


@pytest.mark.asyncio
async def test_disabled_does_nothing(tmp_path: Path) -> None:
    spawner = _Spawner()
    decision = trigger.maybe_run_evolution(
        workdir=str(tmp_path),
        project_root=tmp_path,
        config=TriggerConfig(enabled=False),
        spawn=spawner,
        classify_fn=_classify,
    )
    assert decision.levels_started == []
    assert decision.skipped.get(0) == "disabled"
    assert spawner.scheduled == []


@pytest.mark.asyncio
async def test_autopause_skips_level(tmp_path: Path) -> None:
    workdir = str(tmp_path)
    autopause.pause(workdir, 1, reason="test")
    autopause.pause(workdir, 2, reason="test")
    autopause.pause(workdir, 3, reason="test")
    spawner = _Spawner()
    decision = trigger.maybe_run_evolution(
        workdir=workdir,
        project_root=tmp_path,
        config=TriggerConfig(),
        spawn=spawner,
        classify_fn=_classify,
    )
    assert 1 in decision.skipped and decision.skipped[1] == "autopaused"
    assert 2 in decision.skipped and decision.skipped[2] == "autopaused"
    assert 3 in decision.skipped and decision.skipped[3] == "autopaused"
    assert decision.levels_started == []


@pytest.mark.asyncio
async def test_cooldown_skips_level(tmp_path: Path) -> None:
    workdir = str(tmp_path)
    # Mark level1 as recently-run
    st = state_store.load_state(workdir)
    project = state_store.get_or_create_project(st, workdir)
    import time as _t
    project.level1.last_run_ts = _t.time() - 100  # 100s ago
    state_store.save_state(workdir, st)

    spawner = _Spawner()
    decision = trigger.maybe_run_evolution(
        workdir=workdir,
        project_root=tmp_path,
        config=TriggerConfig(level1_cooldown_seconds=24 * 3600.0),
        spawn=spawner,
        classify_fn=_classify,
    )
    assert decision.skipped.get(1) == "cooldown"
    assert 1 not in decision.levels_started


@pytest.mark.asyncio
async def test_l1_no_new_signals_skips(tmp_path: Path) -> None:
    workdir = str(tmp_path)
    # Pre-set last_run_ts in the future-relative-past, no signals after
    st = state_store.load_state(workdir)
    project = state_store.get_or_create_project(st, workdir)
    project.level1.last_run_ts = 1000.0  # ancient but >0; cooldown fits with override below
    state_store.save_state(workdir, st)

    spawner = _Spawner()
    decision = trigger.maybe_run_evolution(
        workdir=workdir,
        project_root=tmp_path,
        config=TriggerConfig(level1_cooldown_seconds=0.0),
        spawn=spawner,
        classify_fn=_classify,
    )
    assert decision.skipped.get(1) == "no_new_signals"
    assert 1 not in decision.levels_started


@pytest.mark.asyncio
async def test_l1_runs_when_signals_present(tmp_path: Path) -> None:
    workdir = str(tmp_path)
    pid = trigger._project_id(workdir)
    fingerprints.insert_signals(
        workdir,
        [
            fingerprints.SignalRecord(
                project_id=pid,
                ts=1.0,
                source_path="x",
                rule_kind="tests_failing",
                subject_hash="h1",
                weight=1.0,
                raw_text="t",
            )
        ],
    )

    started = asyncio.Event()

    async def classify(text, examples):
        started.set()
        return None  # short-circuits run_level1 early enough; we just verify it was scheduled

    spawner = _Spawner()
    cfg = TriggerConfig(
        level1_cooldown_seconds=0.0,
        level2_cooldown_seconds=10**9,
        level3_cooldown_seconds=10**9,
    )
    decision = trigger.maybe_run_evolution(
        workdir=workdir,
        project_root=tmp_path,
        config=cfg,
        spawn=spawner,
        classify_fn=classify,
    )
    assert 1 in decision.levels_started
    # Drain the scheduled task so it doesn't leak between tests
    for task in spawner.scheduled:
        try:
            await asyncio.wait_for(task, timeout=2.0)
        except Exception:
            pass


@pytest.mark.asyncio
async def test_l2_skipped_without_meta_classifier(tmp_path: Path) -> None:
    workdir = str(tmp_path)
    spawner = _Spawner()
    cfg = TriggerConfig(level1_cooldown_seconds=10**9, level3_cooldown_seconds=10**9)
    decision = trigger.maybe_run_evolution(
        workdir=workdir,
        project_root=tmp_path,
        config=cfg,
        spawn=spawner,
        classify_fn=_classify,
        meta_classify_fn=None,
    )
    assert decision.skipped.get(2) == "no_classifier"


def test_configured_lock_ttl_is_used_for_scheduled_level(tmp_path: Path) -> None:
    workdir = str(tmp_path)
    scheduled = []

    def _capture(coro):
        scheduled.append(coro)
        return object()

    decision = trigger.maybe_run_evolution(
        workdir=workdir,
        project_root=tmp_path,
        config=TriggerConfig(
            level1_cooldown_seconds=10**9,
            level2_cooldown_seconds=10**9,
            level3_cooldown_seconds=0.0,
            lock_ttl_seconds=120.0,
        ),
        spawn=_capture,
        classify_fn=None,
    )

    assert decision.levels_started == [3]
    st = state_store.load_state(workdir)
    project = state_store.get_or_create_project(st, workdir)
    ttl = project.level3.lock_expires_at - time.time()
    assert 0 < ttl <= 120.0
    for coro in scheduled:
        coro.close()
