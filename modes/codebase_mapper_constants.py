from __future__ import annotations

from typing import Dict

CODEBASE_MAPPER_RESULT_STATUS: Dict[str, str] = {
    "DISABLED": "disabled",
    "FAILED": "failed",
    "SKIPPED": "skipped",
    "EMPTY": "empty",
    "READY": "ready",
    "FULL_UPDATED": "full_updated",
    "PARTIAL_UPDATED": "partial_updated",
    "GRAPH_VERIFIED": "graph_verified",
    "VALIDATION_DONE": "validation_done",
    "REPAIR_DONE": "repair_done",
}

CODEBASE_MAPPER_SUCCESS_ARTIFACT_STATUSES = frozenset(
    {
        CODEBASE_MAPPER_RESULT_STATUS["FULL_UPDATED"],
        CODEBASE_MAPPER_RESULT_STATUS["PARTIAL_UPDATED"],
        CODEBASE_MAPPER_RESULT_STATUS["GRAPH_VERIFIED"],
        CODEBASE_MAPPER_RESULT_STATUS["VALIDATION_DONE"],
        CODEBASE_MAPPER_RESULT_STATUS["REPAIR_DONE"],
    }
)

CODEBASE_MAPPER_GRAPH_STATE: Dict[str, str] = {
    "EMPTY": "empty",
    "READY": "ready",
    "NEEDS_REVIEW": "needs_review",
    "VALIDATED": "validated",
}
