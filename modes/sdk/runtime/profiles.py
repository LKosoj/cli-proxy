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
    # Default steps can involve multi-provider web fetch + LLM summarization,
    # which regularly exceeds 90s. Give enough headroom for "long" step1 tasks.
    return ExecutorProfile(name="default", allowed_tools=tools, timeout_ms=600_000)


def _available(tool_registry: ToolRegistry, names: List[str]) -> List[str]:
    have = set(tool_registry.list_tool_names())
    return [n for n in names if n in have]


def build_reviewer_profile(config: AppConfig, tool_registry: ToolRegistry) -> ExecutorProfile:
    """
    Manager mode reviewer: read-only-ish tools + ability to run tests.
    Keep the list explicit to avoid surprising destructive operations.
    """
    allowed = _available(
        tool_registry,
        [
            "read_file",
            "list_directory",
            "search_files",
            "search_text",
            "run_command",      # для запуска тестов и линтера
        ],
    )
    timeout_ms = int(getattr(config.defaults, "manager_review_timeout_sec", 300)) * 1000
    return ExecutorProfile(name="reviewer", allowed_tools=allowed, timeout_ms=timeout_ms, max_retries=1)


def build_analyst_profile(config: AppConfig, tool_registry: ToolRegistry) -> ExecutorProfile:
    """
    Analyst mode: read-only tools only.

    Explicit allowlist to prevent destructive operations (write/edit/delete, git, shell).
    Allow use_cli for repo-grounded analyst work while still excluding generic shell access.
    """
    allowed = _available(
        tool_registry,
        [
            "read_file",
            "list_directory",
            "search_files",
            "search_text",
            "ask_user",
            "use_cli",
            # Web / research tools (optional; include those present in registry)
            "web_research",
            "fetch_page",
            "search_web",
            "website_content",
            # Thinking / ideation tools (optional)
            "brainstorm",
            # Some deployments expose sequential thinking via MCP name.
            "sequential_thinking",
            "mcp_sequentialthinking-tools_sequentialthinking_tools",
        ],
    )
    # Give some headroom for multi-step research + summarization.
    # Analyst steps (especially repo-grounding) need more time: 2x the base default.
    return ExecutorProfile(name="analyst", allowed_tools=allowed, timeout_ms=1_200_000, max_retries=1)


def build_developer_profile(config: AppConfig, tool_registry: ToolRegistry) -> ExecutorProfile:
    """
    Manager mode developer: development is executed via session.run_prompt (CLI), not Executor.
    This profile exists for UI/diagnostics symmetry and potential future extensions.
    """
    timeout_ms = int(getattr(config.defaults, "manager_dev_timeout_sec", 600)) * 1000
    # No tools: developer runs through CLI. Keep it empty intentionally.
    return ExecutorProfile(name="developer", allowed_tools=[], timeout_ms=timeout_ms, max_retries=0)
