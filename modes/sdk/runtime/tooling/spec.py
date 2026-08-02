from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class ToolSpec:
    name: str
    description: str
    parameters: Dict[str, Any]
    timeout_ms: int = 120_000
    risk_level: str = "low"
    requires_approval: bool = False
    tags: List[str] = field(default_factory=list)
    parallelizable: bool = True
    category: str = "general"
    one_liner: str = ""
    returns_external_content: bool = False

    def to_openai_tool(self) -> Dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters or {"type": "object", "properties": {}},
            },
        }

    def to_openai_summary(self) -> Dict[str, Any]:
        """Compact schema for progressive disclosure: name + one_liner, no parameters."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.one_liner or self.description[:120],
                "parameters": {"type": "object", "properties": {}},
            },
        }

    def to_google_tool(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters or {"type": "object", "properties": {}},
        }


@dataclass
class ToolResponse:
    success: bool
    output: Optional[str] = None
    error: Optional[str] = None
    meta: Optional[Dict[str, Any]] = None
