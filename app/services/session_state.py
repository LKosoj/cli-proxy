from dataclasses import dataclass
from typing import Optional


@dataclass
class SessionState:
    session_id: Optional[str]
    tool: str
    workdir: str
    resume_token: Optional[str]
    summary: Optional[str]
    updated_at: float
    name: Optional[str] = None
