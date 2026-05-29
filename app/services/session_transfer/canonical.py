"""Canonical intermediate representation for cross-CLI session transfer."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class CanonicalMessage:
    """A single message in canonical form."""

    role: str  # "user" | "assistant" | "tool" | "system"
    content: str
    timestamp: Optional[float] = None


@dataclass
class CanonicalSession:
    """Canonical session representation sufficient for context injection."""

    source_cli: str
    session_id: str
    workspace: str
    messages: List[CanonicalMessage] = field(default_factory=list)
    summary: Optional[str] = None
    extracted_at: float = field(default_factory=time.time)
