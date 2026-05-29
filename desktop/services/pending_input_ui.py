from __future__ import annotations

from typing import Any, Optional

from app.services.input_dispatch_models import (
    PENDING_ACTION_CONFIRM,
    PENDING_ACTION_ORCHESTRATOR_TRANSITION,
    PENDING_ACTION_QUEUE_CHOICE,
    PENDING_ACTION_QUEUE_CONFIRM,
    PendingInputDecision,
)


def desktop_rows_for_pending_input_decision(decision: PendingInputDecision) -> list[list[dict[str, str]]]:
    action = str(decision.action or "").strip()
    if action == PENDING_ACTION_CONFIRM:
        return [
            [{"text": "✅ Взять в работу", "data": "take_pending_input"}],
            [{"text": "❌ Отмена ввода", "data": "discard_input"}],
        ]
    if action == PENDING_ACTION_QUEUE_CHOICE:
        return [
            [
                {"text": "➕ Добавить к текущему", "data": "queue_append_pending"},
                {"text": "🆕 Новая очередь", "data": "queue_input"},
            ],
            [{"text": "❌ Отмена ввода", "data": "discard_input"}],
        ]
    if action == PENDING_ACTION_QUEUE_CONFIRM:
        return [
            [{"text": "🆕 Поставить в очередь", "data": "queue_input"}],
            [{"text": "❌ Отмена ввода", "data": "discard_input"}],
        ]
    if action == PENDING_ACTION_ORCHESTRATOR_TRANSITION:
        payload = dict(decision.payload or {})
        session_uid = str(payload.get("session_uid") or "").strip()
        target_mode_id = str(payload.get("target_mode_id") or "").strip()
        if not session_uid or not target_mode_id:
            return []
        return [
            [
                {"text": "✅ Перейти", "data": f"orch_transition:apply:{session_uid}:{target_mode_id}"},
                {"text": "⛔ Отменить", "data": f"orch_transition:cancel:{session_uid}"},
            ]
        ]
    return []


def _session_token(dest: Optional[dict], fallback_chat_id: Any) -> str:
    payload = dict(dest or {})
    for key in ("session_uid", "chat_id", "session_id"):
        token = str(payload.get(key) or "").strip()
        if token:
            return token
    return str(fallback_chat_id or "").strip()


class DesktopPendingInputUiAdapter:
    def __init__(self, facade: Any, bot_app: Any) -> None:
        self.facade = facade
        self.bot_app = bot_app

    async def send_decision(
        self,
        context,
        decision: PendingInputDecision,
        *,
        dest: Optional[dict],
        fallback_chat_id: Any,
    ) -> Any:
        _ = context
        session_uid = _session_token(dest, fallback_chat_id)
        rows = desktop_rows_for_pending_input_decision(decision)
        if rows:
            self.facade.notify(
                "ui:mode_menu",
                session_id=session_uid,
                text=str(decision.text or ""),
                rows=rows,
            )
        else:
            self.facade.notify(
                "ui:message",
                session_id=session_uid,
                role="agent",
                text=str(decision.text or ""),
                md2=True,
            )
        self.facade._desktop_message_id += 1
        return type("DesktopMessageRef", (), {"message_id": self.facade._desktop_message_id})()

    async def send_text(
        self,
        context,
        *,
        text: str,
        dest: Optional[dict],
        fallback_chat_id: Any,
        md2: bool = True,
    ) -> Any:
        _ = context
        session_uid = _session_token(dest, fallback_chat_id)
        self.facade.notify(
            "ui:message",
            session_id=session_uid,
            role="agent",
            text=str(text or ""),
            md2=bool(md2),
        )
        self.facade._desktop_message_id += 1
        return type("DesktopMessageRef", (), {"message_id": self.facade._desktop_message_id})()

    async def retire_prompt(
        self,
        context,
        *,
        dest: Optional[dict],
        message_id: int,
        stale_text: str,
        ui_key: Any,
    ) -> bool:
        _ = stale_text, ui_key
        clearer = getattr(self.bot_app, "_clear_message_reply_markup", None)
        if not callable(clearer):
            return False
        session_uid = _session_token(dest, None)
        if not session_uid:
            return False
        return bool(
            await clearer(
                context,
                chat_id=session_uid,
                message_id=int(message_id),
                dest=dest,
            )
        )
