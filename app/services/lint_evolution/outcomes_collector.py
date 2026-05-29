from __future__ import annotations

from typing import Final

from .fingerprints import insert_outcome

OUTCOME_COMMITTED: Final[str] = "committed"
OUTCOME_REVERTED: Final[str] = "reverted"
OUTCOME_IGNORED: Final[str] = "ignored"

_WEIGHTS: dict[str, float] = {
    OUTCOME_COMMITTED: 1.0,
    OUTCOME_REVERTED: 1.0,
    OUTCOME_IGNORED: 0.3,
}


def record_outcome(
    workdir: str,
    *,
    project_id: str,
    rule_id: str,
    outcome: str,
    schema_v: int = 1,
) -> None:
    if outcome not in _WEIGHTS:
        raise ValueError(f"unknown outcome {outcome!r}")
    insert_outcome(
        workdir,
        project_id=project_id,
        rule_id=rule_id,
        outcome=outcome,
        weight=_WEIGHTS[outcome],
        schema_v=schema_v,
    )
