from __future__ import annotations

import logging
import os
import sqlite3
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Optional

from app.services.path_normalization import normalize_optional_path, normalize_optional_state_path
from app.services.state_repository import get_state_repository

from .errors import DenyReasonCode
from .interfaces import RateLimitDecision


class RateLimitStoreError(RuntimeError):
    """Raised when rate limit storage cannot be initialized."""


@dataclass(frozen=True)
class RateLimitPolicy:
    limit: int
    window_sec: float
    burst_limit: int | None = None
    burst_window_sec: float | None = None


class InMemoryRateLimitService:
    def __init__(
        self,
        *,
        clock: Callable[[], float] | None = None,
        default_policy: RateLimitPolicy | None = None,
        policies: Mapping[str, RateLimitPolicy] | None = None,
    ) -> None:
        self._clock = clock or time.monotonic
        self._policies = dict(policies or {})
        self._default_policy = default_policy
        self._lock = threading.RLock()
        self._events: dict[tuple[str, str], list[tuple[float, int]]] = {}

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
        token_scope = str(scope or "").strip() or "default"
        token_subject = str(subject or "").strip() or "anonymous"
        weight = int(cost)
        if weight < 1:
            raise ValueError("cost must be >= 1")

        policy = _resolve_policy(
            token_scope,
            limit=limit,
            window_sec=window_sec,
            burst_limit=burst_limit,
            burst_window_sec=burst_window_sec,
            policies=self._policies,
            default_policy=self._default_policy,
        )
        now = float(self._clock())
        retention_sec = max(policy.window_sec, float(policy.burst_window_sec or 0.0))

        with self._lock:
            key = (token_scope, token_subject)
            events = self._events.setdefault(key, [])
            cutoff = now - retention_sec
            events[:] = [(ts, item_cost) for ts, item_cost in events if ts > cutoff]
            decision = _consume_events(
                events,
                now=now,
                weight=weight,
                policy=policy,
                scope=token_scope,
                subject=token_subject,
            )
            if decision.allowed:
                events.append((now, weight))
            return decision


class SqliteSlidingWindowRateLimitService:
    TABLE_NAME = "security_rate_limit_events"

    def __init__(
        self,
        *,
        state_path: str | None = None,
        sqlite_path: str | None = None,
        clock: Callable[[], float] | None = None,
        default_policy: RateLimitPolicy | None = None,
        policies: Mapping[str, RateLimitPolicy] | None = None,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self._clock = clock or time.time
        self._default_policy = default_policy
        self._policies = dict(policies or {})
        self._logger = logger or logging.getLogger(__name__)
        self.db_path = self._resolve_db_path(state_path=state_path, sqlite_path=sqlite_path)
        self._lock = threading.RLock()
        self._max_retention_sec = _max_retention(self._default_policy, self._policies)
        self.ensure_schema()

    @staticmethod
    def _resolve_db_path(*, state_path: str | None, sqlite_path: str | None) -> str:
        try:
            explicit_path = normalize_optional_path(sqlite_path, field_name="sqlite_path")
        except TypeError as exc:
            raise RateLimitStoreError("rate limit sqlite_path is invalid") from exc
        if explicit_path:
            normalized = os.path.abspath(explicit_path)
            parent = os.path.dirname(normalized)
            if parent:
                os.makedirs(parent, exist_ok=True)
            return normalized

        try:
            state_token = normalize_optional_state_path(state_path)
        except TypeError as exc:
            raise RateLimitStoreError("rate limit storage path is invalid") from exc
        if not state_token:
            raise RateLimitStoreError("rate limit storage path is not configured")
        return str(get_state_repository(state_token).db_path)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def ensure_schema(self) -> None:
        with self._lock:
            with self._connect() as conn:
                conn.execute(
                    f"""
                    CREATE TABLE IF NOT EXISTS {self.TABLE_NAME} (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        scope TEXT NOT NULL,
                        subject TEXT NOT NULL,
                        created_at REAL NOT NULL,
                        cost INTEGER NOT NULL
                    )
                    """
                )
                conn.execute(
                    f"""
                    CREATE INDEX IF NOT EXISTS idx_{self.TABLE_NAME}_scope_subject_created
                    ON {self.TABLE_NAME}(scope, subject, created_at)
                    """
                )

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
        token_scope = str(scope or "").strip() or "default"
        token_subject = str(subject or "").strip() or "anonymous"
        weight = int(cost)
        if weight < 1:
            raise ValueError("cost must be >= 1")

        policy = _resolve_policy(
            token_scope,
            limit=limit,
            window_sec=window_sec,
            burst_limit=burst_limit,
            burst_window_sec=burst_window_sec,
            policies=self._policies,
            default_policy=self._default_policy,
        )
        now = float(self._clock())
        retention_sec = max(
            self._max_retention_sec,
            policy.window_sec,
            float(policy.burst_window_sec or 0.0),
        )
        cutoff = now - retention_sec

        with self._lock:
            with self._connect() as conn:
                conn.execute(
                    f"DELETE FROM {self.TABLE_NAME} WHERE created_at <= ?",
                    (cutoff,),
                )
                rows = conn.execute(
                    f"""
                    SELECT created_at, cost
                    FROM {self.TABLE_NAME}
                    WHERE scope = ? AND subject = ? AND created_at > ?
                    ORDER BY created_at ASC, id ASC
                    """,
                    (token_scope, token_subject, cutoff),
                ).fetchall()
                events = [
                    (float(row["created_at"] or 0.0), int(row["cost"] or 0))
                    for row in rows
                ]
                decision = _consume_events(
                    events,
                    now=now,
                    weight=weight,
                    policy=policy,
                    scope=token_scope,
                    subject=token_subject,
                )
                if decision.allowed:
                    conn.execute(
                        f"""
                        INSERT INTO {self.TABLE_NAME}(scope, subject, created_at, cost)
                        VALUES (?, ?, ?, ?)
                        """,
                        (token_scope, token_subject, now, weight),
                    )
                return decision


def _effective_cost(
    events: list[tuple[float, int]],
    *,
    cutoff: float,
) -> tuple[int, float | None]:
    total = 0
    oldest: float | None = None
    for ts, item_cost in events:
        if ts <= cutoff:
            continue
        if oldest is None:
            oldest = ts
        total += int(item_cost)
    return total, oldest


def _consume_events(
    events: list[tuple[float, int]],
    *,
    now: float,
    weight: int,
    policy: RateLimitPolicy,
    scope: str,
    subject: str,
) -> RateLimitDecision:
    current_total, oldest_total = _effective_cost(events, cutoff=now - policy.window_sec)
    projected_total = current_total + weight
    configured_burst_limit = int(policy.burst_limit or 0)
    configured_burst_window = float(policy.burst_window_sec or 0.0)
    current_burst = current_total
    oldest_burst = oldest_total
    if configured_burst_limit > 0 and configured_burst_window > 0:
        current_burst, oldest_burst = _effective_cost(events, cutoff=now - configured_burst_window)
        projected_burst = current_burst + weight
        if projected_burst > configured_burst_limit:
            retry_after = max(
                0.0,
                ((oldest_burst or now) + configured_burst_window - now),
            )
            return RateLimitDecision(
                scope=scope,
                subject=subject,
                allowed=False,
                limit=policy.limit,
                remaining=max(0, policy.limit - current_total),
                window_sec=policy.window_sec,
                retry_after_sec=retry_after,
                reason=DenyReasonCode.BURST_LIMIT_EXCEEDED,
                burst_limit=configured_burst_limit,
                burst_remaining=max(0, configured_burst_limit - current_burst),
                burst_window_sec=configured_burst_window,
            )

    if projected_total > policy.limit:
        retry_after = max(0.0, ((oldest_total or now) + policy.window_sec - now))
        return RateLimitDecision(
            scope=scope,
            subject=subject,
            allowed=False,
            limit=policy.limit,
            remaining=max(0, policy.limit - current_total),
            window_sec=policy.window_sec,
            retry_after_sec=retry_after,
            reason=DenyReasonCode.WINDOW_LIMIT_EXCEEDED,
            burst_limit=configured_burst_limit,
            burst_remaining=max(
                0,
                configured_burst_limit - current_burst if configured_burst_limit > 0 else 0,
            ),
            burst_window_sec=configured_burst_window,
        )

    return RateLimitDecision(
        scope=scope,
        subject=subject,
        allowed=True,
        limit=policy.limit,
        remaining=max(0, policy.limit - projected_total),
        window_sec=policy.window_sec,
        retry_after_sec=0.0,
        reason=DenyReasonCode.OK,
        burst_limit=configured_burst_limit,
        burst_remaining=max(
            0,
            configured_burst_limit - (current_burst + weight) if configured_burst_limit > 0 else 0,
        ),
        burst_window_sec=configured_burst_window,
    )


def _max_retention(
    default_policy: RateLimitPolicy | None,
    policies: Mapping[str, RateLimitPolicy],
) -> float:
    candidates: list[float] = []
    if default_policy is not None:
        candidates.extend([default_policy.window_sec, float(default_policy.burst_window_sec or 0.0)])
    for policy in policies.values():
        candidates.extend([policy.window_sec, float(policy.burst_window_sec or 0.0)])
    return max(candidates or [0.0])


def _policy_from_mapping(value: Mapping[str, Any]) -> RateLimitPolicy:
    limit = int(value["limit"])
    window_sec = float(value["window_sec"])
    burst_limit_raw = value.get("burst_limit")
    burst_window_raw = value.get("burst_window_sec")
    burst_limit = int(burst_limit_raw) if burst_limit_raw is not None else None
    burst_window_sec = float(burst_window_raw) if burst_window_raw is not None else None
    if limit < 1:
        raise ValueError("limit must be >= 1")
    if window_sec <= 0:
        raise ValueError("window_sec must be > 0")
    if (burst_limit is None) != (burst_window_sec is None):
        raise ValueError("burst_limit and burst_window_sec must be set together")
    if burst_limit is not None:
        if burst_limit < 1:
            raise ValueError("burst_limit must be >= 1")
        if burst_limit > limit:
            raise ValueError("burst_limit must be <= limit")
        if burst_window_sec is None or burst_window_sec <= 0:
            raise ValueError("burst_window_sec must be > 0")
        if burst_window_sec > window_sec:
            raise ValueError("burst_window_sec must be <= window_sec")
    return RateLimitPolicy(
        limit=limit,
        window_sec=window_sec,
        burst_limit=burst_limit,
        burst_window_sec=burst_window_sec,
    )


def _resolve_policy(
    scope: str,
    *,
    limit: int | None,
    window_sec: float | None,
    burst_limit: int | None,
    burst_window_sec: float | None,
    policies: Mapping[str, RateLimitPolicy],
    default_policy: RateLimitPolicy | None,
) -> RateLimitPolicy:
    if limit is not None or window_sec is not None or burst_limit is not None or burst_window_sec is not None:
        if limit is None or window_sec is None:
            raise ValueError("limit and window_sec must be set together")
        return _policy_from_mapping(
            {
                "limit": limit,
                "window_sec": window_sec,
                "burst_limit": burst_limit,
                "burst_window_sec": burst_window_sec,
            }
        )

    policy = policies.get(scope) or default_policy
    if policy is None:
        raise ValueError(f"rate limit policy is not configured for scope={scope}")
    return policy


def build_rate_limit_service(
    rate_limit_config: Mapping[str, Any] | None,
    *,
    default_state_path: str | None = None,
    clock: Callable[[], float] | None = None,
    logger: Optional[logging.Logger] = None,
) -> InMemoryRateLimitService | SqliteSlidingWindowRateLimitService:
    config = dict(rate_limit_config or {})
    if not config or not bool(config.get("enabled", False)):
        return InMemoryRateLimitService(clock=clock)

    default_policy = None
    if isinstance(config.get("default"), Mapping):
        default_policy = _policy_from_mapping(dict(config["default"]))

    policies: dict[str, RateLimitPolicy] = {}
    policies_raw = config.get("policies") or {}
    if isinstance(policies_raw, Mapping):
        for scope, value in policies_raw.items():
            if not isinstance(value, Mapping):
                continue
            policies[str(scope)] = _policy_from_mapping(dict(value))
    if default_policy is None and not policies:
        raise ValueError("default or policies is required when rate_limits.enabled=true")

    backend = str(config.get("backend", "sqlite") or "sqlite").strip().lower()
    if backend != "sqlite":
        raise ValueError(f"unsupported rate limit backend: {backend}")

    try:
        sqlite_path = normalize_optional_path(config.get("sqlite_path"), field_name="sqlite_path")
        state_path = normalize_optional_state_path(default_state_path)
    except TypeError as exc:
        raise RateLimitStoreError("rate limit storage path is invalid") from exc
    return SqliteSlidingWindowRateLimitService(
        state_path=state_path,
        sqlite_path=sqlite_path,
        clock=clock,
        default_policy=default_policy,
        policies=policies,
        logger=logger,
    )


__all__ = [
    "InMemoryRateLimitService",
    "RateLimitPolicy",
    "RateLimitStoreError",
    "SqliteSlidingWindowRateLimitService",
    "build_rate_limit_service",
]
