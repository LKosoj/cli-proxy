from __future__ import annotations

from .mode import AgentMode

# ModeLoader expects a PLUGIN export in package __init__.py.
PLUGIN = AgentMode

__all__ = ["AgentMode", "PLUGIN"]
