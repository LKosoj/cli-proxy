from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class PlanStep:
    id: str
    title: str
    instruction: str
    step_type: str = "task"
    parallel_group: Optional[str] = None
    allowed_tools: Optional[List[str]] = None
    ask_question: Optional[str] = None
    ask_options: Optional[List[str]] = None


@dataclass
class ExecutorRequest:
    task_id: str
    goal: str
    context: str
    inputs: Dict[str, Any] = field(default_factory=dict)
    constraints: Optional[str] = None
    allowed_tools: Optional[List[str]] = None
    expected_outputs: Optional[List[str]] = None
    deadline_ms: Optional[int] = None
    profile: str = "default"


@dataclass
class ExecutorResponse:
    task_id: str
    status: str
    summary: str
    outputs: List[Dict[str, Any]] = field(default_factory=list)
    tool_calls: List[Dict[str, Any]] = field(default_factory=list)
    next_questions: List[str] = field(default_factory=list)


def validate_request(req: ExecutorRequest) -> None:
    if not req.task_id:
        raise ValueError("ExecutorRequest.task_id is required")
    if not req.goal:
        raise ValueError("ExecutorRequest.goal is required")
    if req.allowed_tools is not None and not isinstance(req.allowed_tools, list):
        raise ValueError("ExecutorRequest.allowed_tools must be list or None")


def validate_response(resp: ExecutorResponse) -> None:
    if not resp.task_id:
        raise ValueError("ExecutorResponse.task_id is required")
    if resp.status not in ("ok", "needs_input", "error"):
        raise ValueError("ExecutorResponse.status invalid")
