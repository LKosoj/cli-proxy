from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

import jsonschema

from .cli_classifier import build_prompt as _classify_prompt_unused  # noqa: F401  (shared style)
from .cli_classifier import extract_json

logger = logging.getLogger(__name__)

InvokeFn = Callable[[str], Awaitable[str]]


META_SCHEMA: dict[str, Any] = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "title": "lint_evolution.meta_curator.v1",
    "type": "object",
    "additionalProperties": False,
    "required": ["emergent_fields"],
    "properties": {
        "emergent_fields": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "proposed_name",
                    "proposed_type",
                    "examples_count",
                    "distinct_cases",
                    "sample_notes",
                    "rationale_extracted",
                    "covered_by_existing_field",
                ],
                "properties": {
                    "proposed_name": {"type": "string", "pattern": "^[a-z][a-z0-9_]*$"},
                    "proposed_type": {"enum": ["enum", "bool", "number"]},
                    "proposed_values": {"type": "array", "items": {"type": "string"}},
                    "examples_count": {"type": "integer", "minimum": 0},
                    "distinct_cases": {"type": "integer", "minimum": 0},
                    "sample_notes": {"type": "array", "items": {"type": "string"}},
                    "rationale_extracted": {"type": "string", "maxLength": 500},
                    "covered_by_existing_field": {"type": ["string", "null"]},
                },
            },
        },
    },
}


_PROMPT_PREFIX = """\
You are a META-CLASSIFIER. Read the notes and review-fragments below and find
recurring THEMES that are NOT covered by the existing schema fields.

DO NOT recommend whether to add fields. Only EXTRACT candidate field structures.

CONSERVATIVE DEFAULTS:
- If a theme is already captured by an existing field — set covered_by_existing_field to that name.
- Use proposed_type "bool" when applicable; only use "enum" when you have ≥2 distinct values.
- Use proposed_type "number" only for clearly numeric quantities.
- proposed_name must be snake_case, ASCII identifier.
- distinct_cases counts how many DIFFERENT review situations the theme appeared in.
- Empty array is a valid output.

OUTPUT: a single JSON object that validates against the schema.
"""


def build_meta_prompt(notes: list[str], existing_field_names: list[str]) -> str:
    parts = [_PROMPT_PREFIX, "\nEXISTING FIELDS (do not propose duplicates):\n"]
    parts.append(", ".join(existing_field_names) or "(none)")
    parts.append("\n\nSCHEMA:\n")
    parts.append(json.dumps(META_SCHEMA, ensure_ascii=False, indent=2))
    parts.append("\n\nNOTES:\n")
    for i, note in enumerate(notes, 1):
        parts.append(f"\n[{i}] {note.strip()}\n")
    parts.append("\n\nReturn ONLY the JSON object.")
    return "".join(parts)


@dataclass
class MetaCurator:
    invoke: InvokeFn
    schema: dict[str, Any] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if not self.schema:
            self.schema = META_SCHEMA

    async def classify(self, notes: list[str], existing_field_names: list[str]) -> list[dict[str, Any]] | None:
        prompt = build_meta_prompt(notes, existing_field_names)
        try:
            raw = await self.invoke(prompt)
        except Exception as exc:
            logger.warning("lint_evolution.meta_curator: invoke failed: %s", exc)
            return None
        parsed = extract_json(str(raw or ""))
        if parsed is None:
            return None
        try:
            jsonschema.validate(parsed, self.schema)
        except jsonschema.ValidationError as exc:
            logger.info("lint_evolution.meta_curator: schema validation failed: %s", exc.message)
            return None
        return list(parsed.get("emergent_fields") or [])
