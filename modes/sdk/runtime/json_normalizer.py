from __future__ import annotations

import copy
import json
import logging
import re
from typing import Any

from jsonschema import ValidationError
from jsonschema import validators

_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*\n?(.*?)\n?\s*```", re.DOTALL | re.IGNORECASE)
_JSON_ONLY_FENCE_RE = re.compile(r"```json\s*\n?(.*?)\n?\s*```", re.DOTALL | re.IGNORECASE)
logger = logging.getLogger(__name__)


class JSONSchemaValidationError(ValueError):
    """Schema validation error raised by JSON normalizer."""

    def __init__(self, message: str, *, path: str = ""):
        super().__init__(message)
        self.path = path


def _extract_outermost_object(text: str) -> str:
    s = str(text or "")
    start = s.find("{")
    if start < 0:
        return ""

    depth = 0
    in_string = False
    escaped = False
    begin = -1

    for idx, ch in enumerate(s[start:], start=start):
        if escaped:
            escaped = False
            continue

        if in_string:
            if ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue

        if ch == '"':
            in_string = True
            continue

        if ch == "{":
            if depth == 0:
                begin = idx
            depth += 1
            continue

        if ch == "}" and depth > 0:
            depth -= 1
            if depth == 0 and begin >= 0:
                return s[begin:idx + 1]

    return ""


def _iter_outermost_objects(text: str):
    s = str(text or "")
    depth = 0
    in_string = False
    escaped = False
    begin = -1

    for idx, ch in enumerate(s):
        if escaped:
            escaped = False
            continue

        if in_string:
            if ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue

        if ch == '"':
            in_string = True
            continue

        if ch == "{":
            if depth == 0:
                begin = idx
            depth += 1
            continue

        if ch == "}" and depth > 0:
            depth -= 1
            if depth == 0 and begin >= 0:
                yield s[begin:idx + 1]
                begin = -1


def _sanitize_json_candidate(raw: str) -> str:
    s = str(raw or "")
    out: list[str] = []
    in_string = False
    escaped = False
    i = 0

    while i < len(s):
        ch = s[i]
        code = ord(ch)

        if in_string:
            if escaped:
                if ch in {'"', "\\", "/", "b", "f", "n", "r", "t", "u"}:
                    out.append(ch)
                else:
                    # Invalid escape, keep literal backslash.
                    out.append("\\")
                    out.append(ch)
                escaped = False
                i += 1
                continue

            if ch == "\\":
                out.append(ch)
                escaped = True
                i += 1
                continue

            if ch == '"':
                out.append(ch)
                in_string = False
                i += 1
                continue

            if code < 0x20:
                out.append(f"\\u{code:04x}")
                i += 1
                continue

            out.append(ch)
            i += 1
            continue

        if ch == '"':
            in_string = True
            out.append(ch)
            i += 1
            continue

        if code < 0x20 and ch not in {"\n", "\r", "\t"}:
            i += 1
            continue

        out.append(ch)
        i += 1

    return "".join(out)


def _looks_like_json_object(candidate: str) -> bool:
    s = str(candidate or "").strip()
    if not s.startswith("{") or not s.endswith("}"):
        return False
    inner = s[1:-1].strip()
    if not inner:
        return True
    # Heuristic: valid JSON objects use quoted keys (or at least contain key-value separator).
    # This filters placeholders like "{student_id}" extracted from non-JSON code fences.
    return (":" in inner) and ('"' in inner)


def extract_json_object(raw: str) -> str:
    s = str(raw or "").strip()
    if not s:
        return ""

    for match in _JSON_FENCE_RE.finditer(s):
        inner = str(match.group(1) or "").strip()
        if not inner:
            continue
        found = _extract_outermost_object(inner)
        if found and _looks_like_json_object(found):
            return found

    found = _extract_outermost_object(s)
    if found and _looks_like_json_object(found):
        return found

    return s


def _iter_json_object_candidates(raw: str) -> list[str]:
    s = str(raw or "").strip()
    if not s:
        return []

    candidates: list[str] = []
    seen: set[str] = set()

    def _append(found: str) -> None:
        if not found or not _looks_like_json_object(found) or found in seen:
            return
        candidates.append(found)
        seen.add(found)

    for match in _JSON_FENCE_RE.finditer(s):
        inner = str(match.group(1) or "").strip()
        if not inner:
            continue
        for found in _iter_outermost_objects(inner):
            _append(found)

    for found in _iter_outermost_objects(s):
        _append(found)

    return candidates


def _extract_json_from_json_fence(raw: str) -> str:
    s = str(raw or "").strip()
    if not s:
        return ""

    for match in _JSON_ONLY_FENCE_RE.finditer(s):
        inner = str(match.group(1) or "").strip()
        if not inner:
            continue
        found = _extract_outermost_object(inner)
        if found and _looks_like_json_object(found):
            return found
    return ""


def loads_safe(raw: str, strict_first: bool = True) -> Any:
    source = str(raw or "").strip()
    if not source:
        raise json.JSONDecodeError("empty json payload", source, 0)

    if strict_first:
        try:
            # Prefer parsing the full payload first. This preserves valid JSON
            # documents that may contain markdown/code-fence fragments inside
            # string fields (for example persisted state summaries).
            return json.loads(source)
        except json.JSONDecodeError:
            pass

    candidate = extract_json_object(source)
    to_parse = candidate if candidate else source

    try:
        return json.loads(to_parse)
    except json.JSONDecodeError:
        pass

    for candidate in _iter_json_object_candidates(source):
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            try:
                return json.loads(_sanitize_json_candidate(candidate))
            except json.JSONDecodeError:
                continue

    repaired = _sanitize_json_candidate(to_parse)
    return json.loads(repaired)


def _validator_with_defaults(schema: dict[str, Any]):
    validator_cls = validators.validator_for(schema)
    validator_cls.check_schema(schema)
    validate_props = validator_cls.VALIDATORS["properties"]

    def set_defaults(validator, properties, instance, schema_obj):
        if isinstance(instance, dict):
            for prop, subschema in properties.items():
                if prop not in instance and isinstance(subschema, dict) and "default" in subschema:
                    instance[prop] = copy.deepcopy(subschema["default"])
        yield from validate_props(validator, properties, instance, schema_obj)

    return validators.extend(validator_cls, {"properties": set_defaults})


def _normalize_payload_inner(payload: dict, schema: dict) -> dict[str, Any]:
    data = dict(payload)
    validator_cls = _validator_with_defaults(schema)
    validator = validator_cls(schema)
    validator.validate(data)
    return data


def normalize_payload(payload: dict, schema: dict) -> dict[str, Any]:
    if not isinstance(payload, dict):
        try:
            raise TypeError("payload must be dict")
        except TypeError:
            logger.exception("normalize_payload invalid payload type type=%s", type(payload).__name__)
            raise
    if not isinstance(schema, dict):
        try:
            raise TypeError("schema must be dict")
        except TypeError:
            logger.exception("normalize_payload invalid schema type type=%s", type(schema).__name__)
            raise

    try:
        return _normalize_payload_inner(payload, schema)
    except ValidationError as exc:
        path = ".".join(str(x) for x in exc.absolute_path)
        logger.exception(
            "normalize_payload schema validation failed path=%s message=%s payload=%r schema_keys=%s",
            path,
            exc.message,
            payload,
            sorted(schema.keys()),
        )
        raise JSONSchemaValidationError(exc.message, path=path) from exc
    except Exception:
        logger.exception(
            "normalize_payload failed payload=%r schema_keys=%s",
            payload,
            sorted(schema.keys()) if isinstance(schema, dict) else [],
        )
        raise


def _try_schema_matching_candidate(raw: str, schema: dict) -> dict[str, Any] | None:
    for candidate in _iter_json_object_candidates(raw):
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            try:
                parsed = json.loads(_sanitize_json_candidate(candidate))
            except json.JSONDecodeError:
                continue
        if not isinstance(parsed, dict):
            continue
        try:
            return _normalize_payload_inner(parsed, schema)
        except ValidationError:
            continue
    return None


def parse_normalize_validate(
    raw: str,
    schema: dict,
    *,
    strict_json_document: bool = False,
    log_validation_errors: bool = True,
) -> dict[str, Any]:
    source = str(raw or "").strip()
    try:
        if strict_json_document:
            try:
                parsed = json.loads(source)
            except json.JSONDecodeError:
                # Strict mode still accepts explicit ```json ...``` wrappers
                # used by some LLMs around the final JSON report.
                fenced = _extract_json_from_json_fence(source)
                if not fenced:
                    raise
                parsed = json.loads(fenced)
        else:
            try:
                parsed = json.loads(source)
            except json.JSONDecodeError:
                parsed = loads_safe(source, strict_first=False)
    except Exception:
        logger.exception("parse_normalize_validate parse failed raw_preview=%r", str(raw or "")[:500])
        raise

    if not isinstance(parsed, dict):
        try:
            raise TypeError("parsed payload must be JSON object")
        except TypeError:
            logger.exception(
                "parse_normalize_validate parsed payload is not object type=%s raw_preview=%r",
                type(parsed).__name__,
                str(raw or "")[:500],
            )
            raise

    try:
        return _normalize_payload_inner(parsed, schema)
    except ValidationError as exc:
        if not strict_json_document:
            # Try candidate matching even when parsed_from_full_document=True
            # This handles cases where source contains multiple JSON objects
            # and the first parsed object doesn't match the schema
            matched = _try_schema_matching_candidate(source, schema)
            if matched is not None:
                return matched
        path = ".".join(str(x) for x in exc.absolute_path)
        if log_validation_errors:
            logger.exception(
                "parse_normalize_validate validation failed payload=%r schema_keys=%s",
                parsed,
                sorted(schema.keys()) if isinstance(schema, dict) else [],
            )
        raise JSONSchemaValidationError(exc.message, path=path) from exc
    except Exception:
        logger.exception(
            "parse_normalize_validate validation failed payload=%r schema_keys=%s",
            parsed,
            sorted(schema.keys()) if isinstance(schema, dict) else [],
        )
        raise
