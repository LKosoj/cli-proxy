from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

import jsonschema

from modes.sdk.runtime.json_normalizer import loads_safe

logger = logging.getLogger(__name__)

InvokeFn = Callable[[str], Awaitable[str]]

_JSON_FENCE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL | re.IGNORECASE)
_JSON_OBJECT = re.compile(r"\{.*\}", re.DOTALL)


_SYSTEM_RULES = """\
You are a lint-rule CLASSIFIER. Your only job is to fill the JSON schema below
based on review feedback text. You DO NOT recommend apply/reject — that decision
is made by deterministic Python downstream.

CONSERVATIVE DEFAULTS (mandatory):
- If unsure about ANY field, pick the safest value:
    false_positive_risk: "high"
    scope_subjectivity: "subjective"
    test_generatable: false
    detector_type: "llm_only"
- Style/formatting issues belong to ruff/black; classify category="style" only when truly stylistic.
- Use rule_kind="__unknown__" if no canonical kind matches; explain in `notes`.
- If the proposed detector cannot be expressed as regex/ast/shell, set detector_type="llm_only".
- Use `notes` for anything that does not fit the schema; max 500 chars.

OUTPUT: a single JSON object that validates against the schema. No prose, no preamble.
"""


def build_prompt(*, signal_text: str, schema: dict, recent_examples: list[str] | None = None) -> str:
    parts = [_SYSTEM_RULES, "\nSCHEMA:\n", json.dumps(schema, ensure_ascii=False, indent=2)]
    parts.append("\n\nINPUT REVIEW COMMENT:\n")
    parts.append(signal_text.strip())
    if recent_examples:
        parts.append("\n\nADDITIONAL OCCURRENCES (same rule_kind):\n")
        for i, ex in enumerate(recent_examples[:5], 1):
            parts.append(f"\n{i}. {ex.strip()}\n")
    parts.append("\n\nReturn ONLY the JSON object.")
    return "".join(parts)


def extract_json(text: str) -> dict | None:
    if not text:
        return None
    fence = _JSON_FENCE.search(text)
    candidate = fence.group(1) if fence else None
    if candidate is None:
        match = _JSON_OBJECT.search(text)
        candidate = match.group(0) if match else None
    if candidate is None:
        return None
    try:
        parsed = loads_safe(candidate)
    except (json.JSONDecodeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


@dataclass
class CliClassifier:
    """Thin wrapper that turns active-CLI text invocations into schema-validated dicts."""

    invoke: InvokeFn
    schema: dict

    async def classify(self, signal_text: str, *, recent_examples: list[str] | None = None) -> dict[str, Any] | None:
        prompt = build_prompt(signal_text=signal_text, schema=self.schema, recent_examples=recent_examples)
        try:
            raw = await self.invoke(prompt)
        except Exception as exc:
            logger.warning("lint_evolution.classifier: CLI invoke failed: %s", exc)
            return None
        parsed = extract_json(str(raw or ""))
        if parsed is None:
            logger.info("lint_evolution.classifier: no JSON object in CLI output")
            return None
        try:
            jsonschema.validate(parsed, self.schema)
        except jsonschema.ValidationError as exc:
            logger.info("lint_evolution.classifier: schema validation failed: %s", exc.message)
            return None
        return parsed
