from __future__ import annotations

import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .paths import candidates_dir, ensure_directories


@dataclass
class Candidate:
    rule_kind: str
    proposed_at: float = 0.0
    decision: str = ""
    reason: str = ""
    score: float = 0.0
    classification: dict[str, Any] = field(default_factory=dict)
    examples: list[str] = field(default_factory=list)


def _file(workdir: str, name: str) -> Path:
    ensure_directories(workdir)
    return candidates_dir(workdir) / name


def _load_list(path: Path) -> list[Candidate]:
    if not path.exists():
        return []
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
        return []
    raw = (data or {}).get("candidates") or []
    return [Candidate(**(r or {})) for r in raw if isinstance(r, dict)]


def _save_list(path: Path, items: list[Candidate]) -> None:
    payload = {"candidates": [asdict(c) for c in items]}
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(yaml.safe_dump(payload, allow_unicode=True, sort_keys=False), encoding="utf-8")
    os.replace(tmp, path)


def load_pending(workdir: str) -> list[Candidate]:
    return _load_list(_file(workdir, "pending.yaml"))


def load_rejected(workdir: str) -> list[Candidate]:
    return _load_list(_file(workdir, "rejected.yaml"))


def add_pending(workdir: str, candidate: Candidate) -> None:
    items = load_pending(workdir)
    if any(c.rule_kind == candidate.rule_kind for c in items):
        return
    if not candidate.proposed_at:
        candidate.proposed_at = time.time()
    items.append(candidate)
    _save_list(_file(workdir, "pending.yaml"), items)


def add_rejected(workdir: str, candidate: Candidate) -> None:
    items = load_rejected(workdir)
    if not candidate.proposed_at:
        candidate.proposed_at = time.time()
    items.append(candidate)
    _save_list(_file(workdir, "rejected.yaml"), items)


def has_pending_kind(workdir: str, rule_kind: str) -> bool:
    return any(c.rule_kind == rule_kind for c in load_pending(workdir))
