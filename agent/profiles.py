from __future__ import annotations

from dataclasses import dataclass, field
from typing import List

from config import AppConfig


@dataclass
class ExecutorProfile:
    name: str
    allowed_tools: List[str] = field(default_factory=list)
    timeout_ms: int = 90_000
    max_retries: int = 2


def build_default_profile(config: AppConfig) -> ExecutorProfile:
    tools = list(sorted(config.tools.keys()))
    return ExecutorProfile(name="default", allowed_tools=tools)
