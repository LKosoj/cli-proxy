from __future__ import annotations

from dataclasses import dataclass, field
from typing import List

from config import AppConfig
from .tooling.registry import ToolRegistry


@dataclass
class ExecutorProfile:
    name: str
    allowed_tools: List[str] = field(default_factory=list)
    timeout_ms: int = 90_000
    max_retries: int = 2


def build_default_profile(config: AppConfig, tool_registry: ToolRegistry) -> ExecutorProfile:
    tools = list(sorted(tool_registry.list_tool_names()))
    return ExecutorProfile(name="default", allowed_tools=tools)
