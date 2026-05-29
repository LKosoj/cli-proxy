from __future__ import annotations
import json

import pytest

from modes.sdk.runtime import json_normalizer as normalizer_mod
from modes.sdk.runtime.json_normalizer import (
    JSONSchemaValidationError,
    extract_json_object,
    loads_safe,
    parse_normalize_validate,
)


def test_extract_json_object_handles_fence_and_surrounding_text() -> None:
    raw = 'prefix\n```json\n{"a": 1, "nested": {"b": 2}}\n```\nsuffix'
    assert extract_json_object(raw) == '{"a": 1, "nested": {"b": 2}}'


def test_extract_json_object_ignores_braces_inside_string() -> None:
    raw = 'text before {"msg":"value with } brace", "ok": true} text after'
    assert extract_json_object(raw) == '{"msg":"value with } brace", "ok": true}'


def test_extract_json_object_skips_non_json_placeholders_in_code_fence() -> None:
    raw = (
        "prefix\n"
        "```bash\n"
        "GET /api/v1/students/{student_id}/lessons\n"
        "```\n"
        "```json\n"
        '{"ok": true, "count": 1}\n'
        "```\n"
        "suffix"
    )
    assert extract_json_object(raw) == '{"ok": true, "count": 1}'


def test_loads_safe_repairs_invalid_control_character() -> None:
    raw = '{"message":"line1\nline2"}'
    data = loads_safe(raw, strict_first=True)
    assert data["message"] == "line1\nline2"


def test_loads_safe_repairs_invalid_escape_sequence() -> None:
    raw = '{"message":"bad\\\\qescape"}'
    data = loads_safe(raw, strict_first=True)
    assert data["message"] == "bad\\qescape"


def test_loads_safe_strict_first_keeps_valid_document_with_code_fence_placeholders() -> None:
    raw = (
        '{"_sessions_by_chat":{"1":{"sessions":{"s1":{"active_cli":"codex","workdir":"/repo",'
        '"summary":"```bash\\nGET /api/v1/students/{student_id}/lessons\\n```"}}}}}'
    )
    data = loads_safe(raw, strict_first=True)
    assert data["_sessions_by_chat"]["1"]["sessions"]["s1"]["active_cli"] == "codex"


def test_loads_safe_fallback_skips_placeholder_object_from_bash_fence() -> None:
    raw = (
        "not a json payload\n"
        "```bash\n"
        "GET /api/v1/students/{student_id}/lessons\n"
        "```\n"
        "```json\n"
        '{"result": "ok"}\n'
        "```\n"
    )
    data = loads_safe(raw, strict_first=False)
    assert data == {"result": "ok"}


def test_loads_safe_fallback_skips_inline_placeholder_before_json() -> None:
    raw = 'GET /api/v1/students/{student_id}/lessons final {"result": "ok"}'
    data = loads_safe(raw, strict_first=False)
    assert data == {"result": "ok"}


def test_parse_normalize_validate_applies_schema_defaults() -> None:
    schema = {
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "enabled": {"type": "boolean", "default": False},
            "limit": {"type": "integer", "default": 3},
        },
        "required": ["name"],
        "additionalProperties": False,
    }
    result = parse_normalize_validate('{"name":"demo"}', schema)
    assert result == {"name": "demo", "enabled": False, "limit": 3}


def test_parse_normalize_validate_copies_mutable_schema_defaults() -> None:
    schema = {
        "type": "object",
        "properties": {
            "items": {"type": "array", "items": {"type": "string"}, "default": []},
        },
        "additionalProperties": False,
    }
    first = parse_normalize_validate("{}", schema)
    first["items"].append("leak")
    second = parse_normalize_validate("{}", schema)
    assert second == {"items": []}
    assert first["items"] is not second["items"]


def test_parse_normalize_validate_raises_specific_schema_error() -> None:
    schema = {
        "type": "object",
        "properties": {"count": {"type": "integer"}},
        "required": ["count"],
        "additionalProperties": False,
    }
    with pytest.raises(JSONSchemaValidationError):
        parse_normalize_validate('{"count":"bad"}', schema)


def test_parse_normalize_validate_strict_document_rejects_wrapped_json() -> None:
    schema = {
        "type": "object",
        "properties": {"count": {"type": "integer"}},
        "required": ["count"],
        "additionalProperties": False,
    }
    raw = "prefix {\"count\": 1} suffix"
    with pytest.raises(json.JSONDecodeError):
        parse_normalize_validate(raw, schema, strict_json_document=True)


def test_parse_normalize_validate_strict_document_accepts_explicit_json_fence() -> None:
    schema = {
        "type": "object",
        "properties": {"count": {"type": "integer"}},
        "required": ["count"],
        "additionalProperties": False,
    }
    raw = "prefix\n```json\n{\"count\": 1}\n```\nsuffix"
    parsed = parse_normalize_validate(raw, schema, strict_json_document=True)
    assert parsed == {"count": 1}


def test_parse_normalize_validate_uses_later_schema_matching_object_in_mixed_output() -> None:
    schema = {
        "type": "object",
        "properties": {
            "approved": {"type": "boolean"},
            "summary": {"type": "string"},
            "comments": {"type": "string"},
        },
        "required": ["approved", "summary", "comments"],
        "additionalProperties": True,
    }
    raw = (
        'tool payload {"path":"/tmp/ajax_snapshot.h","offset":1,"limit":200}\n'
        'final review {"approved":true,"summary":"ok","comments":"done"}'
    )
    parsed = parse_normalize_validate(raw, schema)
    assert parsed["approved"] is True
    assert parsed["summary"] == "ok"
    assert parsed["comments"] == "done"


def test_parse_normalize_validate_repairs_later_schema_matching_candidate() -> None:
    schema = {
        "type": "object",
        "properties": {"result": {"type": "string"}},
        "required": ["result"],
        "additionalProperties": False,
    }
    raw = 'tool {"path":"/tmp"}\nfinal {"result":"line1\nline2"}'

    parsed = parse_normalize_validate(raw, schema)

    assert parsed == {"result": "line1\nline2"}


def test_parse_normalize_validate_logs_parse_exceptions(monkeypatch) -> None:
    calls: list[tuple] = []

    def _fake_exception(msg, *args, **kwargs):
        calls.append((msg, args, kwargs))

    monkeypatch.setattr(normalizer_mod.logger, "exception", _fake_exception)
    with pytest.raises(json.JSONDecodeError):
        parse_normalize_validate("not json", {"type": "object"})
    assert calls


def test_parse_normalize_validate_logs_validation_exceptions(monkeypatch) -> None:
    calls: list[tuple] = []

    def _fake_exception(msg, *args, **kwargs):
        calls.append((msg, args, kwargs))

    monkeypatch.setattr(normalizer_mod.logger, "exception", _fake_exception)
    schema = {
        "type": "object",
        "properties": {"count": {"type": "integer"}},
        "required": ["count"],
        "additionalProperties": False,
    }
    with pytest.raises(JSONSchemaValidationError):
        parse_normalize_validate('{"count":"bad"}', schema)
    assert calls
