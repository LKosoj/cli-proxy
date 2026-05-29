from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Dict, Mapping, Optional, Protocol

from tg.markdown import escape_markdown_v2_all


class AdminNotifierError(RuntimeError):
    """Raised when notifier input is invalid."""


@dataclass(frozen=True)
class AdminNotificationResult:
    sent: bool
    muted: bool
    text: str
    muted_until_ts: Optional[float] = None


class _AdminNotifierStateStore(Protocol):
    def get_session_state(self, session_id: str, *, chat_id: Optional[int] = None) -> Optional[Dict[str, Any]]:
        ...


class _AdminNotifierMessaging(Protocol):
    async def send_text(self, chat_id: int, text: str, *, md2: bool = True, **kwargs: Any) -> Any:
        ...


def _esc(value: Any) -> str:
    return escape_markdown_v2_all(str(value if value is not None else ""))


def _as_payload(row: Mapping[str, Any]) -> Dict[str, Any]:
    payload = row.get("payload")
    if isinstance(payload, Mapping):
        return dict(payload)
    return {}


class AdminNotifier:
    def __init__(
        self,
        *,
        state_store: _AdminNotifierStateStore,
        clock: Optional[Any] = None,
    ) -> None:
        self._state_store = state_store
        self._clock = clock if callable(clock) else time.time

    async def notify_incident(
        self,
        *,
        session_id: str,
        chat_id: int,
        incident_row: Mapping[str, Any],
        messaging: _AdminNotifierMessaging,
        now_ts: Optional[float] = None,
    ) -> AdminNotificationResult:
        return await self._notify(
            session_id=session_id,
            chat_id=chat_id,
            messaging=messaging,
            text=self._format_incident_text(session_id=session_id, incident_row=incident_row),
            now_ts=now_ts,
        )

    async def notify_action(
        self,
        *,
        session_id: str,
        chat_id: int,
        action_row: Mapping[str, Any],
        messaging: _AdminNotifierMessaging,
        now_ts: Optional[float] = None,
    ) -> AdminNotificationResult:
        return await self._notify(
            session_id=session_id,
            chat_id=chat_id,
            messaging=messaging,
            text=self._format_action_text(session_id=session_id, action_row=action_row),
            now_ts=now_ts,
        )

    async def _notify(
        self,
        *,
        session_id: str,
        chat_id: int,
        messaging: _AdminNotifierMessaging,
        text: str,
        now_ts: Optional[float],
    ) -> AdminNotificationResult:
        sid = str(session_id or "").strip()
        if not sid:
            raise AdminNotifierError("session_id is empty")
        now_value = float(now_ts if now_ts is not None else self._clock())
        muted_until_ts = self._resolve_muted_until_ts(session_id=sid, chat_id=int(chat_id))
        if muted_until_ts is not None and muted_until_ts > now_value:
            return AdminNotificationResult(
                sent=False,
                muted=True,
                text=str(text or ""),
                muted_until_ts=muted_until_ts,
            )

        await messaging.send_text(int(chat_id), str(text or ""), md2=True)
        return AdminNotificationResult(
            sent=True,
            muted=False,
            text=str(text or ""),
            muted_until_ts=muted_until_ts,
        )

    def _resolve_muted_until_ts(self, *, session_id: str, chat_id: Optional[int] = None) -> Optional[float]:
        state = self._state_store.get_session_state(str(session_id or "").strip(), chat_id=chat_id) or {}
        raw_muted_until = state.get("muted_until_ts")
        if raw_muted_until is None:
            return None
        try:
            muted_until_ts = float(raw_muted_until)
        except Exception:
            return None
        if muted_until_ts <= 0:
            return None
        return muted_until_ts

    @staticmethod
    def _format_incident_text(*, session_id: str, incident_row: Mapping[str, Any]) -> str:
        payload = _as_payload(incident_row)
        decision = payload.get("decision")
        decision_map = dict(decision) if isinstance(decision, Mapping) else {}
        incident_id = str(incident_row.get("incident_id") or "")
        action = str(decision_map.get("action") or "-")
        urgency = str(decision_map.get("urgency") or "-")
        reason = str(decision_map.get("reason") or "-")
        return "\n".join(
            [
                "*🛡 Admin Incident*",
                f"*Session:* {_esc(session_id)}",
                f"*Incident:* {_esc(incident_id)}",
                f"*Action:* {_esc(action)}",
                f"*Urgency:* {_esc(urgency)}",
                f"*Reason:* {_esc(reason)}",
            ]
        )

    @staticmethod
    def _format_action_text(*, session_id: str, action_row: Mapping[str, Any]) -> str:
        payload = _as_payload(action_row)
        action_id = str(action_row.get("action_id") or "")
        event = str(payload.get("event") or "-")
        result = payload.get("result")
        result_map = dict(result) if isinstance(result, Mapping) else {}
        success = "yes" if bool(result_map.get("success")) else "no"
        returncode = str(result_map.get("returncode") if result_map.get("returncode") is not None else "-")
        return "\n".join(
            [
                "*🛡 Admin Action*",
                f"*Session:* {_esc(session_id)}",
                f"*Action ID:* {_esc(action_id)}",
                f"*Event:* {_esc(event)}",
                f"*Success:* {_esc(success)}",
                f"*Return code:* {_esc(returncode)}",
            ]
        )


__all__ = [
    "AdminNotifier",
    "AdminNotifierError",
    "AdminNotificationResult",
]
