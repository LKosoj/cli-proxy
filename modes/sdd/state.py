"""Thin helpers for SddState access on a Session.

Importing SddState from session — no reverse dependency: session.py does NOT
import anything from modes.sdd.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from session import SddState

if TYPE_CHECKING:
    from session import Session


def get_sdd_state(session: "Session") -> SddState:
    """Return the SddState attached to *session*, guaranteed non-None."""
    if not isinstance(session.sdd, SddState):
        session.sdd = SddState()
    return session.sdd


def set_sdd_phase(session: "Session", phase: str) -> None:
    """Set the SDD phase on *session*."""
    get_sdd_state(session).phase = str(phase or "idle").strip() or "idle"


def clear_sdd_gate(session: "Session") -> None:
    """Clear the pending gate on *session*'s SDD state."""
    get_sdd_state(session).pending_gate = None
