"""Tests for M9 fix #3: O(n) rewrite of _extract_balanced_json_objects.

Verifies identical behaviour to the original O(n²) implementation across
nested objects, strings containing braces/escaped quotes, garbage between
objects, and incomplete trailing objects.
"""
from __future__ import annotations

import json

from modes.sdk.runtime.cli_contracts import _extract_balanced_json_objects


def test_single_simple_object() -> None:
    result = _extract_balanced_json_objects('{"key": "value"}')
    assert result == ['{"key": "value"}']


def test_nested_objects() -> None:
    raw = '{"outer": {"inner": 1}}'
    result = _extract_balanced_json_objects(raw)
    assert len(result) == 1
    assert json.loads(result[0]) == {"outer": {"inner": 1}}


def test_two_top_level_objects_separated_by_garbage() -> None:
    raw = 'garbage {"a": 1} more garbage {"b": 2} end'
    result = _extract_balanced_json_objects(raw)
    assert len(result) == 2
    assert json.loads(result[0]) == {"a": 1}
    assert json.loads(result[1]) == {"b": 2}


def test_string_with_braces_inside() -> None:
    raw = '{"msg": "value with { and } braces", "ok": true}'
    result = _extract_balanced_json_objects(raw)
    assert len(result) == 1
    parsed = json.loads(result[0])
    assert parsed["msg"] == "value with { and } braces"
    assert parsed["ok"] is True


def test_string_with_escaped_quotes() -> None:
    raw = r'{"msg": "he said \"hello\"", "n": 42}'
    result = _extract_balanced_json_objects(raw)
    assert len(result) == 1
    parsed = json.loads(result[0])
    assert parsed["n"] == 42
    assert "hello" in parsed["msg"]


def test_incomplete_trailing_object_ignored() -> None:
    raw = '{"complete": 1} {"incomplete": '
    result = _extract_balanced_json_objects(raw)
    assert len(result) == 1
    assert json.loads(result[0]) == {"complete": 1}


def test_valid_object_after_unbalanced_prefix_is_extracted() -> None:
    raw = 'prefix {broken {"ok": true} suffix'
    result = _extract_balanced_json_objects(raw)
    assert len(result) == 1
    assert json.loads(result[0]) == {"ok": True}


def test_no_braces_returns_empty() -> None:
    assert _extract_balanced_json_objects("no braces here") == []


def test_empty_string_returns_empty() -> None:
    assert _extract_balanced_json_objects("") == []


def test_none_like_returns_empty() -> None:
    assert _extract_balanced_json_objects(None) == []  # type: ignore[arg-type]


def test_deeply_nested_object() -> None:
    raw = '{"a": {"b": {"c": {"d": 1}}}}'
    result = _extract_balanced_json_objects(raw)
    assert len(result) == 1
    parsed = json.loads(result[0])
    assert parsed["a"]["b"]["c"]["d"] == 1


def test_multiple_objects_no_garbage_between() -> None:
    raw = '{"x": 1}{"y": 2}{"z": 3}'
    result = _extract_balanced_json_objects(raw)
    assert len(result) == 3
    assert json.loads(result[0]) == {"x": 1}
    assert json.loads(result[1]) == {"y": 2}
    assert json.loads(result[2]) == {"z": 3}


def test_object_with_array_value() -> None:
    raw = '{"items": [1, 2, {"nested": true}]}'
    result = _extract_balanced_json_objects(raw)
    assert len(result) == 1
    parsed = json.loads(result[0])
    assert parsed["items"][2]["nested"] is True


def test_object_with_backslash_before_quote_in_string() -> None:
    # "\\" followed by a quote: the backslash is escaped, so the quote terminates the string
    raw = '{"path": "C:\\\\dir\\\\file", "ok": true}'
    result = _extract_balanced_json_objects(raw)
    assert len(result) == 1
    parsed = json.loads(result[0])
    assert parsed["ok"] is True


def test_tool_call_followed_by_final_payload() -> None:
    tool = json.dumps({"path": "/tmp/file", "offset": 1, "limit": 200})
    final = json.dumps({"approved": True, "summary": "ok", "comments": "done"})
    raw = f"tool payload {tool}\nfinal review {final}"
    result = _extract_balanced_json_objects(raw)
    assert len(result) == 2
    assert json.loads(result[0])["path"] == "/tmp/file"
    assert json.loads(result[1])["approved"] is True
