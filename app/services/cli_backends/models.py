from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Optional


BackendState = Literal["running", "active", "idle", "failed", "stopped", "unknown"]


@dataclass(frozen=True)
class ExecutionResult:
    text: str
    backend: str
    request_id: str
    started_at: float
    finished_at: float
    abnormal_stop: bool = False
    diagnostics: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class ExecutionBackendStatus:
    backend: str
    state: BackendState
    session_name: Optional[str] = None
    pane_target: Optional[str] = None
    last_activity_at: Optional[float] = None
    runtime_dir: Optional[str] = None
    detail: Optional[str] = None
