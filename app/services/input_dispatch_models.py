from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


PENDING_ACTION_CONFIRM = "confirm"
PENDING_ACTION_QUEUE_CONFIRM = "queue_confirm"
PENDING_ACTION_QUEUE_CHOICE = "queue_choice"
PENDING_ACTION_TMUX_QUEUE_CONFIRM = "tmux_queue_confirm"
PENDING_ACTION_TMUX_QUEUE_CHOICE = "tmux_queue_choice"
PENDING_ACTION_ORCHESTRATOR_TRANSITION = "orchestrator_transition"


@dataclass
class PendingInput:
    session_id: str
    text: str
    dest: dict
    session_uid: Optional[str] = None
    image_path: Optional[str] = None
    image_paths: Optional[list[str]] = None
    action: str = PENDING_ACTION_CONFIRM


@dataclass(frozen=True)
class PendingInputDecision:
    action: str
    text: str
    payload: dict[str, Any] = field(default_factory=dict)


__all__ = [
    "PENDING_ACTION_CONFIRM",
    "PENDING_ACTION_ORCHESTRATOR_TRANSITION",
    "PENDING_ACTION_QUEUE_CHOICE",
    "PENDING_ACTION_QUEUE_CONFIRM",
    "PENDING_ACTION_TMUX_QUEUE_CHOICE",
    "PENDING_ACTION_TMUX_QUEUE_CONFIRM",
    "PendingInput",
    "PendingInputDecision",
]
