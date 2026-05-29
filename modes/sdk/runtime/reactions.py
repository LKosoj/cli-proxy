from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional

from .events import EventSeverity, EventType, OrchestratorEvent


ReactionHandler = Callable[[OrchestratorEvent, "ReactionAction", Dict[str, Any]], Awaitable[Dict[str, Any]]]

_SEVERITY_RANK: Dict[EventSeverity, int] = {
    EventSeverity.INFO: 10,
    EventSeverity.WARNING: 20,
    EventSeverity.ERROR: 30,
}


@dataclass(frozen=True)
class ReactionAction:
    action_type: str
    params: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        data = {
            "action_type": str(self.action_type or ""),
            "params": dict(self.params or {}),
        }
        json.dumps(data, ensure_ascii=False)
        return data

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "ReactionAction":
        if not isinstance(raw, dict):
            raise ValueError("ReactionAction payload must be dict")
        params = raw.get("params") or {}
        if not isinstance(params, dict):
            raise ValueError("ReactionAction.params must be dict")
        return cls(
            action_type=str(raw.get("action_type") or ""),
            params=params,
        )


@dataclass(frozen=True)
class ReactionRule:
    rule_id: str
    event_types: List[EventType] = field(default_factory=list)
    min_severity: Optional[EventSeverity] = None
    payload_equals: Dict[str, Any] = field(default_factory=dict)
    actions: List[ReactionAction] = field(default_factory=list)
    enabled: bool = True

    def matches(self, event: OrchestratorEvent) -> bool:
        if not self.enabled:
            return False
        if self.event_types and event.event_type not in self.event_types:
            return False
        if self.min_severity is not None:
            if _SEVERITY_RANK[event.severity] < _SEVERITY_RANK[self.min_severity]:
                return False
        for key, expected in (self.payload_equals or {}).items():
            if (event.payload or {}).get(key) != expected:
                return False
        return True

    def to_dict(self) -> Dict[str, Any]:
        data = {
            "rule_id": str(self.rule_id or ""),
            "event_types": [x.value for x in (self.event_types or [])],
            "min_severity": self.min_severity.value if self.min_severity is not None else None,
            "payload_equals": dict(self.payload_equals or {}),
            "actions": [x.to_dict() for x in (self.actions or [])],
            "enabled": bool(self.enabled),
        }
        json.dumps(data, ensure_ascii=False)
        return data

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "ReactionRule":
        if not isinstance(raw, dict):
            raise ValueError("ReactionRule payload must be dict")
        event_types: List[EventType] = []
        for token in (raw.get("event_types") or []):
            event_types.append(EventType(str(token)))
        min_severity_raw = raw.get("min_severity")
        min_severity = EventSeverity(str(min_severity_raw)) if min_severity_raw else None
        actions_raw = raw.get("actions") or []
        actions = [ReactionAction.from_dict(x) for x in actions_raw if isinstance(x, dict)]
        payload_equals = raw.get("payload_equals") or {}
        if not isinstance(payload_equals, dict):
            raise ValueError("ReactionRule.payload_equals must be dict")
        return cls(
            rule_id=str(raw.get("rule_id") or ""),
            event_types=event_types,
            min_severity=min_severity,
            payload_equals=payload_equals,
            actions=actions,
            enabled=bool(raw.get("enabled", True)),
        )


class ReactionEngine:
    def __init__(
        self,
        *,
        logger: Optional[logging.Logger] = None,
        ask_user_fn: Optional[Callable[[str, List[str], Dict[str, Any]], Awaitable[str]]] = None,
        notify_failure_fn: Optional[Callable[[str, Dict[str, Any]], Awaitable[None]]] = None,
    ) -> None:
        self._log = logger or logging.getLogger(__name__)
        self._ask_user_fn = ask_user_fn
        self._notify_failure_fn = notify_failure_fn
        self._handlers: Dict[str, ReactionHandler] = {}
        self.register_action("retry_step", self._handle_retry_step)
        self.register_action("ask_user", self._handle_ask_user)
        self.register_action("notify_failure", self._handle_notify_failure)

    def register_action(self, action_type: str, handler: ReactionHandler) -> None:
        token = str(action_type or "").strip()
        if not token:
            raise ValueError("action_type is required")
        self._handlers[token] = handler

    def evaluate(self, event: OrchestratorEvent, rules: List[ReactionRule]) -> List[ReactionAction]:
        actions: List[ReactionAction] = []
        for rule in rules or []:
            if rule.matches(event):
                actions.extend(list(rule.actions or []))
        return actions

    async def execute(
        self,
        event: OrchestratorEvent,
        rules: List[ReactionRule],
        *,
        ctx: Optional[Dict[str, Any]] = None,
    ) -> List[Dict[str, Any]]:
        actions = self.evaluate(event, rules)
        results: List[Dict[str, Any]] = []
        action_ctx = dict(ctx or {})
        for action in actions:
            handler = self._handlers.get(str(action.action_type or "").strip())
            if handler is None:
                self._log.warning("reaction handler is not registered action=%s", action.action_type)
                results.append(
                    {
                        "action": str(action.action_type or ""),
                        "status": "skipped",
                        "reason": "handler_not_registered",
                    }
                )
                continue
            try:
                res = await handler(event, action, action_ctx)
                results.append(dict(res or {}))
            except Exception:
                self._log.exception("reaction action execution failed action=%s", action.action_type)
                results.append(
                    {
                        "action": str(action.action_type or ""),
                        "status": "error",
                        "reason": "exception",
                    }
                )
        return results

    async def _handle_retry_step(
        self,
        event: OrchestratorEvent,
        action: ReactionAction,
        ctx: Dict[str, Any],
    ) -> Dict[str, Any]:
        retry_count = int((event.payload or {}).get("retry_count") or 0)
        max_retries = int(action.params.get("max_retries", 1) or 1)
        if retry_count >= max_retries:
            return {
                "action": "retry_step",
                "status": "skipped",
                "reason": "max_retries_reached",
                "step_id": event.step_id,
                "retry_count": retry_count,
                "max_retries": max_retries,
            }
        return {
            "action": "retry_step",
            "status": "queued",
            "step_id": event.step_id,
            "next_retry_count": retry_count + 1,
            "max_retries": max_retries,
        }

    async def _handle_ask_user(
        self,
        event: OrchestratorEvent,
        action: ReactionAction,
        ctx: Dict[str, Any],
    ) -> Dict[str, Any]:
        question = str(action.params.get("question") or event.message or "Нужно уточнение.").strip()
        options_raw = action.params.get("options") or []
        options = [str(x).strip() for x in options_raw if str(x).strip()]
        if self._ask_user_fn is None:
            return {
                "action": "ask_user",
                "status": "queued",
                "question": question,
                "options": options,
            }
        answer = await self._ask_user_fn(question, options, dict(ctx or {}))
        return {
            "action": "ask_user",
            "status": "answered",
            "question": question,
            "selected": str(answer or ""),
        }

    async def _handle_notify_failure(
        self,
        event: OrchestratorEvent,
        action: ReactionAction,
        ctx: Dict[str, Any],
    ) -> Dict[str, Any]:
        message = str(action.params.get("message") or event.message or "Step failed").strip()
        if self._notify_failure_fn is not None:
            await self._notify_failure_fn(message, dict(ctx or {}))
        return {
            "action": "notify_failure",
            "status": "sent" if self._notify_failure_fn is not None else "queued",
            "message": message,
            "step_id": event.step_id,
        }
