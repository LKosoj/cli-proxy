from __future__ import annotations

from pathlib import Path

import pytest

from app.services.lint_evolution import schema_store


def test_bootstrap_creates_active_and_history(tmp_path: Path) -> None:
    workdir = str(tmp_path)
    schema_store.bootstrap_schema(workdir)
    schema = schema_store.load_active_schema(workdir)
    assert "rule_kind" in (schema.get("properties") or {})
    history = list(Path(workdir, ".cli-proxy", "lint_evolution", "schemas", "history").glob("*.json"))
    assert any(h.name == "classification_v1.json" for h in history)
    state = schema_store.load_state(workdir)
    assert state.active_version == 1


def test_extend_schema_bumps_version(tmp_path: Path) -> None:
    workdir = str(tmp_path)
    new_version = schema_store.extend_schema(
        workdir,
        schema_store.FieldSpec(name="environment_specific", type="bool", rationale="emerged from notes"),
        reason="bump test",
    )
    assert new_version == 2
    schema = schema_store.load_active_schema(workdir)
    assert schema["properties"]["environment_specific"] == {"type": "boolean"}
    assert "environment_specific" in schema["required"]
    assert schema["title"].endswith("v2")
    history = list(Path(workdir, ".cli-proxy", "lint_evolution", "schemas", "history").glob("*.json"))
    assert any(h.name == "classification_v2.json" for h in history)


def test_extend_schema_rejects_invalid_type(tmp_path: Path) -> None:
    workdir = str(tmp_path)
    with pytest.raises(ValueError):
        schema_store.extend_schema(workdir, schema_store.FieldSpec(name="x", type="string"))


def test_extend_schema_rejects_invalid_name(tmp_path: Path) -> None:
    workdir = str(tmp_path)
    with pytest.raises(ValueError):
        schema_store.extend_schema(workdir, schema_store.FieldSpec(name="bad-name", type="bool"))


def test_extend_schema_rejects_duplicate_field(tmp_path: Path) -> None:
    workdir = str(tmp_path)
    schema_store.extend_schema(workdir, schema_store.FieldSpec(name="my_field", type="bool"))
    with pytest.raises(ValueError):
        schema_store.extend_schema(workdir, schema_store.FieldSpec(name="my_field", type="bool"))


def test_enum_field_requires_values(tmp_path: Path) -> None:
    workdir = str(tmp_path)
    with pytest.raises(ValueError):
        schema_store.extend_schema(workdir, schema_store.FieldSpec(name="kind", type="enum", values=[]))
    schema_store.extend_schema(
        workdir, schema_store.FieldSpec(name="kind2", type="enum", values=["a", "b"])
    )
    schema = schema_store.load_active_schema(workdir)
    assert schema["properties"]["kind2"] == {"type": "string", "enum": ["a", "b"]}


def test_proposals_round_trip(tmp_path: Path) -> None:
    workdir = str(tmp_path)
    schema_store.append_proposal(workdir, {"proposed_name": "a", "decision": "propose"})
    schema_store.append_proposal(workdir, {"proposed_name": "b", "decision": "reject"})
    items = schema_store.load_proposals(workdir)
    assert len(items) == 2
    assert items[0]["proposed_name"] == "a"


def test_deprecated_round_trip(tmp_path: Path) -> None:
    workdir = str(tmp_path)
    schema_store.append_deprecated(workdir, name="old_field", reason="unused 60d", since_version=3)
    items = schema_store.load_deprecated(workdir)
    assert items[0]["name"] == "old_field"
    assert items[0]["since_version"] == 3
