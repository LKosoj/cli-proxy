from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)

_DEFAULT_CRITICAL_FIELDS: tuple[str, ...] = (
    "false_positive_risk",
    "category",
    "scope_subjectivity",
    "rule_kind",
)

ClassifyFn = Callable[[], Awaitable[dict[str, Any] | None]]


@dataclass(frozen=True)
class XvalResult:
    stable: bool
    result: dict[str, Any] | None
    diverged_fields: tuple[str, ...]


async def classify_stable(
    classify: ClassifyFn,
    *,
    critical_fields: tuple[str, ...] = _DEFAULT_CRITICAL_FIELDS,
) -> XvalResult:
    """Run *classify* twice; consider stable iff all *critical_fields* match.

    Returns the first response when stable. Otherwise returns ``stable=False`` with
    the list of fields that diverged. Caller is expected to treat unstable result as HOLD.
    """
    r1 = await classify()
    if r1 is None:
        return XvalResult(stable=False, result=None, diverged_fields=("__no_response__",))
    r2 = await classify()
    if r2 is None:
        return XvalResult(stable=False, result=None, diverged_fields=("__no_response_2__",))
    diverged = tuple(f for f in critical_fields if r1.get(f) != r2.get(f))
    if diverged:
        logger.info("lint_evolution: classifier unstable, fields=%s", diverged)
        return XvalResult(stable=False, result=None, diverged_fields=diverged)
    return XvalResult(stable=True, result=r1, diverged_fields=())
