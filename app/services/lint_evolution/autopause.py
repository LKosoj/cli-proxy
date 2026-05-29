from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import asdict, dataclass, field

from modes.sdk.runtime.json_normalizer import loads_safe

from .paths import autopause_path, ensure_directories

logger = logging.getLogger(__name__)


_VALID_LEVELS = (1, 2, 3)


@dataclass
class LevelPause:
    paused: bool = False
    reason: str = ""
    ts: float = 0.0


@dataclass
class AutopauseState:
    levels: dict[str, LevelPause] = field(default_factory=dict)


def _load(workdir: str) -> AutopauseState:
    path = autopause_path(workdir)
    if not path.exists():
        return AutopauseState()
    try:
        data = loads_safe(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, ValueError):
        return AutopauseState()
    levels: dict[str, LevelPause] = {}
    for key, payload in (data.get("levels") or {}).items():
        if not isinstance(payload, dict):
            continue
        levels[str(key)] = LevelPause(
            paused=bool(payload.get("paused")),
            reason=str(payload.get("reason") or ""),
            ts=float(payload.get("ts") or 0.0),
        )
    return AutopauseState(levels=levels)


def _save(workdir: str, state: AutopauseState) -> None:
    ensure_directories(workdir)
    path = autopause_path(workdir)
    payload = {"levels": {k: asdict(v) for k, v in state.levels.items()}}
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _check_level(level: int) -> str:
    if level not in _VALID_LEVELS:
        raise ValueError(f"unsupported level: {level!r}")
    return str(level)


def is_paused(workdir: str, level: int) -> bool:
    key = _check_level(level)
    state = _load(workdir)
    entry = state.levels.get(key)
    return bool(entry and entry.paused)


def pause(workdir: str, level: int, *, reason: str) -> None:
    key = _check_level(level)
    state = _load(workdir)
    state.levels[key] = LevelPause(paused=True, reason=reason, ts=time.time())
    _save(workdir, state)
    logger.warning("lint_evolution.autopause: level=%d paused (%s)", level, reason)


def resume(workdir: str, level: int) -> bool:
    key = _check_level(level)
    state = _load(workdir)
    entry = state.levels.get(key)
    if not entry or not entry.paused:
        return False
    state.levels[key] = LevelPause(paused=False, reason="", ts=time.time())
    _save(workdir, state)
    logger.info("lint_evolution.autopause: level=%d resumed", level)
    return True


def status(workdir: str) -> dict[str, LevelPause]:
    return _load(workdir).levels
