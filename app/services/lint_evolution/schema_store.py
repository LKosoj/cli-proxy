from __future__ import annotations

import json
import logging
import os
import time
from copy import deepcopy
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import yaml

from modes.sdk.runtime.json_normalizer import loads_safe

from .paths import ensure_directories, schemas_dir
from .schemas import classification_schema_v1

logger = logging.getLogger(__name__)


_ALLOWED_TYPES: frozenset[str] = frozenset({"enum", "bool", "number"})


@dataclass
class FieldSpec:
    name: str
    type: str
    values: list[str] = field(default_factory=list)
    added_in_version: int = 1
    rationale: str = ""


@dataclass
class SchemaState:
    active_version: int = 1
    last_bump_ts: float = 0.0


def _active_dir(workdir: str) -> Path:
    ensure_directories(workdir)
    p = schemas_dir(workdir) / "active"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _history_dir(workdir: str) -> Path:
    ensure_directories(workdir)
    p = schemas_dir(workdir) / "history"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _schemas_root(workdir: str) -> Path:
    ensure_directories(workdir)
    p = schemas_dir(workdir)
    p.mkdir(parents=True, exist_ok=True)
    return p


def _state_file(workdir: str) -> Path:
    return _schemas_root(workdir) / "schema_state.yaml"


def _proposals_file(workdir: str) -> Path:
    return _schemas_root(workdir) / "proposals.yaml"


def _deprecated_file(workdir: str) -> Path:
    return _schemas_root(workdir) / "deprecated.yaml"


def _active_schema_file(workdir: str) -> Path:
    return _active_dir(workdir) / "classification.json"


def load_state(workdir: str) -> SchemaState:
    path = _state_file(workdir)
    if not path.exists():
        return SchemaState()
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
        return SchemaState()
    return SchemaState(
        active_version=int(data.get("active_version") or 1),
        last_bump_ts=float(data.get("last_bump_ts") or 0.0),
    )


def save_state(workdir: str, state: SchemaState) -> None:
    path = _state_file(workdir)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(yaml.safe_dump(asdict(state), allow_unicode=True, sort_keys=False), encoding="utf-8")
    os.replace(tmp, path)


def load_active_schema(workdir: str) -> dict[str, Any]:
    path = _active_schema_file(workdir)
    if not path.exists():
        bootstrap_schema(workdir)
    return loads_safe(path.read_text(encoding="utf-8"))


def bootstrap_schema(workdir: str) -> None:
    """Initialize active+history with bundled v1 schema."""
    active = _active_schema_file(workdir)
    if active.exists():
        return
    schema = classification_schema_v1()
    active.write_text(json.dumps(schema, ensure_ascii=False, indent=2), encoding="utf-8")
    history = _history_dir(workdir) / "classification_v1.json"
    if not history.exists():
        history.write_text(json.dumps(schema, ensure_ascii=False, indent=2), encoding="utf-8")
    save_state(workdir, SchemaState(active_version=1, last_bump_ts=time.time()))


def existing_field_names(workdir: str) -> list[str]:
    schema = load_active_schema(workdir)
    return list((schema.get("properties") or {}).keys())


def days_since_last_bump(workdir: str, *, now: float | None = None) -> float:
    state = load_state(workdir)
    when = float(now if now is not None else time.time())
    if state.last_bump_ts <= 0:
        return float("inf")
    return (when - state.last_bump_ts) / 86400.0


def extend_schema(workdir: str, field_spec: FieldSpec, *, reason: str = "") -> int:
    """Add a new field to the active schema, bump version, append history entry. Returns new version."""
    if field_spec.type not in _ALLOWED_TYPES:
        raise ValueError(f"unsupported field type: {field_spec.type!r}")
    if not field_spec.name or not field_spec.name.isidentifier():
        raise ValueError(f"invalid field name: {field_spec.name!r}")
    schema = load_active_schema(workdir)
    properties = schema.setdefault("properties", {})
    if field_spec.name in properties:
        raise ValueError(f"field {field_spec.name!r} already exists")
    properties[field_spec.name] = _build_field_schema(field_spec)
    required = list(schema.get("required") or [])
    if field_spec.name not in required:
        required.append(field_spec.name)
    schema["required"] = required

    state = load_state(workdir)
    new_version = state.active_version + 1
    schema["title"] = f"lint_evolution.classification.v{new_version}"

    active_path = _active_schema_file(workdir)
    tmp = active_path.with_suffix(active_path.suffix + ".tmp")
    tmp.write_text(json.dumps(schema, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, active_path)

    history_path = _history_dir(workdir) / f"classification_v{new_version}.json"
    history_path.write_text(json.dumps(schema, ensure_ascii=False, indent=2), encoding="utf-8")

    state.active_version = new_version
    state.last_bump_ts = time.time()
    save_state(workdir, state)
    logger.info("lint_evolution.schema_store: extended schema → v%d (%s)", new_version, reason)
    return new_version


def _build_field_schema(field_spec: FieldSpec) -> dict[str, Any]:
    if field_spec.type == "bool":
        return {"type": "boolean"}
    if field_spec.type == "number":
        return {"type": "number"}
    if field_spec.type == "enum":
        values = list(field_spec.values or [])
        if not values:
            raise ValueError("enum field requires non-empty values")
        return {"type": "string", "enum": values}
    raise ValueError(f"unsupported field type: {field_spec.type!r}")


def load_proposals(workdir: str) -> list[dict[str, Any]]:
    path = _proposals_file(workdir)
    if not path.exists():
        return []
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return [dict(p) for p in (data.get("proposals") or []) if isinstance(p, dict)]


def save_proposals(workdir: str, items: list[dict[str, Any]]) -> None:
    path = _proposals_file(workdir)
    payload = {"proposals": items}
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(yaml.safe_dump(payload, allow_unicode=True, sort_keys=False), encoding="utf-8")
    os.replace(tmp, path)


def append_proposal(workdir: str, proposal: dict[str, Any]) -> None:
    proposals = load_proposals(workdir)
    proposals.append(deepcopy(proposal))
    save_proposals(workdir, proposals)


def load_deprecated(workdir: str) -> list[dict[str, Any]]:
    path = _deprecated_file(workdir)
    if not path.exists():
        return []
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return [dict(p) for p in (data.get("fields") or []) if isinstance(p, dict)]


def append_deprecated(workdir: str, *, name: str, reason: str, since_version: int) -> None:
    items = load_deprecated(workdir)
    items.append({"name": name, "reason": reason, "since_version": int(since_version), "ts": time.time()})
    path = _deprecated_file(workdir)
    payload = {"fields": items}
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(yaml.safe_dump(payload, allow_unicode=True, sort_keys=False), encoding="utf-8")
    os.replace(tmp, path)
