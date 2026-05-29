"""Bot-side glue between input_routing.py and lint_evolution.trigger.

Builds a TriggerConfig from AppConfig.lint_evolution, wires fire-and-forget spawn
through the running asyncio loop, and exposes a callable suitable for the
ModeInputRoutingService.lint_evolution_hook slot.

L1/L2 require an active-CLI classifier; until that integration lands, both are
left disabled here (classify_fn=None / meta_classify_fn=None) and only canary +
L3 + state bookkeeping run on session activity.
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any, Callable, Optional

from app.services.lint_evolution import trigger
from app.services.lint_evolution.canary_metric import CanaryConfig
from app.services.lint_evolution.trigger import TriggerConfig

logger = logging.getLogger(__name__)


def build_trigger_config(lint_cfg: Any) -> TriggerConfig:
    """Translate the AppConfig.lint_evolution dataclass into a TriggerConfig."""
    return TriggerConfig(
        enabled=bool(getattr(lint_cfg, "enabled", False)),
        level1_cooldown_seconds=float(getattr(lint_cfg, "level1_cooldown_hours", 24.0)) * 3600.0,
        level2_cooldown_seconds=float(getattr(lint_cfg, "level2_cooldown_hours", 24.0 * 30)) * 3600.0,
        level3_cooldown_seconds=float(getattr(lint_cfg, "level3_cooldown_hours", 24.0 * 30)) * 3600.0,
        lock_ttl_seconds=float(getattr(lint_cfg, "lock_ttl_minutes", 30.0)) * 60.0,
        error_retry_seconds=float(getattr(lint_cfg, "error_retry_hours", 1.0)) * 3600.0,
        canary=CanaryConfig(
            fp_growth_threshold_pct=float(getattr(lint_cfg, "fp_growth_threshold_pct", 50.0)),
            rolling_window_days=float(getattr(lint_cfg, "canary_rolling_days", 7.0)),
            baseline_window_days=float(getattr(lint_cfg, "canary_baseline_days", 30.0)),
            schema_max_fields_per_180d=int(getattr(lint_cfg, "canary_max_schema_fields_per_180d", 3)),
        ),
    )


def _spawn_via_running_loop(coro) -> Any:
    """Schedule a coroutine on whichever asyncio loop is currently running."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        logger.debug("lint_evolution: no running loop, dropping coroutine")
        coro.close()
        return None
    return loop.create_task(coro)


def make_session_hook(lint_cfg: Any) -> Optional[Callable[[Any], None]]:
    """Return a callable suitable for ModeInputRoutingService.lint_evolution_hook.

    Returns None when the feature is disabled — caller must handle None.
    The callable is sync (not async): it inspects the session and schedules a
    fire-and-forget background task via the running asyncio loop. Never blocks
    the user's reply path.
    """
    cfg = build_trigger_config(lint_cfg)
    if not cfg.enabled:
        return None

    def _hook(session: Any) -> None:
        workdir = str(getattr(session, "workdir", "") or "").strip()
        if not workdir:
            return
        try:
            project_root = Path(workdir)
            trigger.maybe_run_evolution(
                workdir=workdir,
                project_root=project_root,
                config=cfg,
                spawn=_spawn_via_running_loop,
                classify_fn=None,
                meta_classify_fn=None,
            )
        except Exception:
            logger.exception("lint_evolution: maybe_run_evolution failed")

    return _hook


__all__ = ["build_trigger_config", "make_session_hook"]
