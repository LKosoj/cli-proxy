from __future__ import annotations

from dataclasses import dataclass, field
import time
from typing import Any, Callable, Mapping, Protocol, Sequence


@dataclass(frozen=True)
class AuthDecision:
    chat_id: int
    allowed: bool
    scope: str = "generic"
    is_admin: bool = False
    is_user: bool = False
    reason: str = ""


@dataclass(frozen=True)
class AuthenticationResult:
    strategy: str
    authenticated: bool
    subject: str = ""
    reason: str = ""
    claims: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class PathValidationResult:
    root: str
    input_path: str
    resolved_path: str
    relative_path: str


@dataclass(frozen=True)
class RateLimitDecision:
    scope: str
    subject: str
    allowed: bool
    limit: int
    remaining: int
    window_sec: float
    retry_after_sec: float = 0.0
    reason: str = ""
    burst_limit: int = 0
    burst_remaining: int = 0
    burst_window_sec: float = 0.0


@dataclass(frozen=True)
class AuditRecord:
    category: str
    action: str
    status: str = "ok"
    user_id: str = ""
    subject: str = ""
    scope: str = ""
    reason: str = ""
    timestamp: float = field(default_factory=time.time)
    context: Mapping[str, Any] = field(default_factory=dict)
    details: Mapping[str, Any] = field(default_factory=dict)

    def to_payload(self) -> dict[str, Any]:
        return {
            "category": str(self.category or "").strip(),
            "action": str(self.action or "").strip(),
            "status": str(self.status or "").strip(),
            "user_id": str(self.user_id or "").strip(),
            "subject": str(self.subject or "").strip(),
            "scope": str(self.scope or "").strip(),
            "reason": str(self.reason or "").strip(),
            "timestamp": float(self.timestamp or 0.0),
            "context": dict(self.context or {}),
            "details": dict(self.details or {}),
        }


class AuthService(Protocol):
    def authorize(self, chat_id: int, *, scope: str = "generic", require_admin: bool = False) -> AuthDecision:
        ...

    def authenticate(
        self,
        credentials: Mapping[str, Any],
        *,
        strategy: str | None = None,
    ) -> AuthenticationResult:
        ...


class AuthenticationStrategy(Protocol):
    strategy_name: str

    def authenticate(self, credentials: Mapping[str, Any]) -> AuthenticationResult:
        ...


class ValidatorService(Protocol):
    def require_text(
        self,
        value: str,
        *,
        field_name: str = "input",
        context: Mapping[str, Any] | None = None,
    ) -> str:
        ...

    def resolve_path(
        self,
        root: str,
        rel_path: str,
        *,
        deny_names: Sequence[str] = (),
        deny_extensions: Sequence[str] = (),
        context: Mapping[str, Any] | None = None,
    ) -> PathValidationResult:
        ...


TextValidationRule = Callable[..., Any]
PathValidationRule = Callable[..., Any]


class AuditService(Protocol):
    async def emit(self, record: AuditRecord) -> None:
        ...

    def list_records(
        self,
        *,
        limit: int = 100,
        action: str = "",
        user_id: str | int | None = None,
        status: str = "",
        subject: str = "",
    ) -> list[AuditRecord]:
        ...


class RateLimitService(Protocol):
    def consume(
        self,
        scope: str,
        subject: str | int,
        *,
        limit: int | None = None,
        window_sec: float | None = None,
        cost: int = 1,
        burst_limit: int | None = None,
        burst_window_sec: float | None = None,
    ) -> RateLimitDecision:
        ...


__all__ = [
    "AuditRecord",
    "AuditService",
    "AuthenticationResult",
    "AuthenticationStrategy",
    "AuthDecision",
    "AuthService",
    "PathValidationRule",
    "PathValidationResult",
    "RateLimitDecision",
    "RateLimitService",
    "TextValidationRule",
    "ValidatorService",
]
