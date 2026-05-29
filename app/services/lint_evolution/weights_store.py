from __future__ import annotations

import logging
import os
import shutil
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .paths import ensure_directories, rules_dir
from .schemas import template_path

logger = logging.getLogger(__name__)


@dataclass
class WeightsBundle:
    schema_version: int = 1
    thresholds: dict[str, float] = field(default_factory=dict)
    weights: dict[str, float] = field(default_factory=dict)
    generated_by: str = "bootstrap"
    generated_at: float = 0.0


def _active_path(workdir: str) -> Path:
    ensure_directories(workdir)
    return rules_dir(workdir) / "decision_weights.yaml"


def _history_path(workdir: str) -> Path:
    ensure_directories(workdir)
    return rules_dir(workdir) / "weights_history.yaml"


def _to_bundle(payload: dict[str, Any]) -> WeightsBundle:
    return WeightsBundle(
        schema_version=int(payload.get("schema_version") or 1),
        thresholds={str(k): float(v) for k, v in (payload.get("thresholds") or {}).items()},
        weights={str(k): float(v) for k, v in (payload.get("weights") or {}).items()},
        generated_by=str(payload.get("generated_by") or "bootstrap"),
        generated_at=float(payload.get("generated_at") or 0.0),
    )


def bootstrap(workdir: str) -> WeightsBundle:
    active = _active_path(workdir)
    if not active.exists():
        bundled = template_path("decision_weights.yaml")
        shutil.copyfile(bundled, active)
        logger.info("lint_evolution.weights_store: bootstrapped from bundled template")
    return load_active(workdir)


def load_active(workdir: str) -> WeightsBundle:
    bootstrap_if_missing(workdir)
    payload = yaml.safe_load(_active_path(workdir).read_text(encoding="utf-8")) or {}
    bundle = _to_bundle(payload)
    if not bundle.generated_at:
        bundle.generated_at = time.time()
    return bundle


def bootstrap_if_missing(workdir: str) -> None:
    if not _active_path(workdir).exists():
        bootstrap(workdir)


def save_active(workdir: str, bundle: WeightsBundle, *, history_reason: str) -> None:
    bundle.generated_at = time.time()
    payload = asdict(bundle)
    path = _active_path(workdir)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(yaml.safe_dump(payload, allow_unicode=True, sort_keys=False), encoding="utf-8")
    os.replace(tmp, path)
    append_history(workdir, bundle, reason=history_reason)


def append_history(workdir: str, bundle: WeightsBundle, *, reason: str) -> None:
    path = _history_path(workdir)
    history: list[dict[str, Any]] = []
    if path.exists():
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            history = list(data.get("history") or [])
        except yaml.YAMLError:
            history = []
    entry = {
        "ts": bundle.generated_at,
        "reason": reason,
        "schema_version": bundle.schema_version,
        "thresholds": dict(bundle.thresholds),
        "weights": dict(bundle.weights),
        "generated_by": bundle.generated_by,
    }
    history.append(entry)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(yaml.safe_dump({"history": history}, allow_unicode=True, sort_keys=False), encoding="utf-8")
    os.replace(tmp, path)


def history_count(workdir: str) -> int:
    path = _history_path(workdir)
    if not path.exists():
        return 0
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
        return 0
    return len(list(data.get("history") or []))
