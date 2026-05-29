from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services import lint_evolution_runtime
from app.services.lint_evolution import autopause, fingerprints
from app.services.lint_evolution.paths import project_id_for


@dataclass
class _LintCfg:
    enabled: bool = False
    level1_cooldown_hours: float = 24.0
    level2_cooldown_hours: float = 24.0 * 30
    level3_cooldown_hours: float = 24.0 * 30
    lock_ttl_minutes: float = 30.0
    error_retry_hours: float = 1.0
    fp_growth_threshold_pct: float = 50.0
    canary_rolling_days: float = 7.0
    canary_baseline_days: float = 30.0
    canary_max_schema_fields_per_180d: int = 3


def test_build_trigger_config_translates_units() -> None:
    tc = lint_evolution_runtime.build_trigger_config(
        _LintCfg(
            enabled=True,
            level1_cooldown_hours=2.0,
            lock_ttl_minutes=15.0,
            error_retry_hours=0.5,
            canary_rolling_days=3.0,
        )
    )
    assert tc.enabled is True
    assert tc.level1_cooldown_seconds == pytest.approx(2.0 * 3600.0)
    assert tc.lock_ttl_seconds == pytest.approx(15.0 * 60.0)
    assert tc.error_retry_seconds == pytest.approx(0.5 * 3600.0)
    assert tc.canary.rolling_window_days == pytest.approx(3.0)


def test_make_session_hook_returns_none_when_disabled() -> None:
    hook = lint_evolution_runtime.make_session_hook(_LintCfg(enabled=False))
    assert hook is None


@pytest.mark.asyncio
async def test_session_hook_runs_canary_and_returns_quickly(tmp_path: Path) -> None:
    import time as _time
    workdir = str(tmp_path)
    pid = project_id_for(workdir)
    # Seed outcomes anchored to real time.time() so canary's default now=time.time() sees them.
    now = _time.time()
    # Baseline ~25 days ago: low fp rate (~10%)
    for i in range(2):
        fingerprints.insert_outcome(workdir, project_id=pid, rule_id="r", outcome="reverted", ts=now - 86400 * 25 + i)
    for i in range(18):
        fingerprints.insert_outcome(workdir, project_id=pid, rule_id="r", outcome="committed", ts=now - 86400 * 25 + 2 + i)
    # Rolling ~2 days ago: high fp rate (~80%)
    for i in range(16):
        fingerprints.insert_outcome(workdir, project_id=pid, rule_id="r", outcome="reverted", ts=now - 86400 * 2 + i)
    for i in range(4):
        fingerprints.insert_outcome(workdir, project_id=pid, rule_id="r", outcome="committed", ts=now - 86400 * 2 + 16 + i)

    hook = lint_evolution_runtime.make_session_hook(_LintCfg(enabled=True))
    assert hook is not None

    session = SimpleNamespace(workdir=workdir)
    # Hook is sync; spawn schedules tasks on the running loop. Canary runs synchronously.
    hook(session)
    assert autopause.is_paused(workdir, 1) is True
    assert autopause.is_paused(workdir, 2) is True


def test_session_hook_no_workdir_is_noop(tmp_path: Path) -> None:
    hook = lint_evolution_runtime.make_session_hook(_LintCfg(enabled=True))
    assert hook is not None
    # Session without workdir should not raise.
    hook(SimpleNamespace())
    hook(SimpleNamespace(workdir=""))
