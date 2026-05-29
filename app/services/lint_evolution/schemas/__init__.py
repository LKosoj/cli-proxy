"""Bundled schemas for lint_evolution: classification template, weights template."""

from pathlib import Path

from modes.sdk.runtime.json_normalizer import loads_safe

_SCHEMAS_DIR = Path(__file__).parent


def classification_schema_v1() -> dict:
    return loads_safe((_SCHEMAS_DIR / "classification_v1.json").read_text(encoding="utf-8"))


def template_path(name: str) -> Path:
    return _SCHEMAS_DIR / name
