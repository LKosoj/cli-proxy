from __future__ import annotations

import logging
from typing import Any, Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from app.services.input_dispatch_models import (
    PENDING_ACTION_CONFIRM,
    PENDING_ACTION_ORCHESTRATOR_TRANSITION,
    PENDING_ACTION_QUEUE_CHOICE,
    PENDING_ACTION_QUEUE_CONFIRM,
    PendingInputDecision,
)
from i18n import t
from utils.lang import resolve_user_lang

logger = logging.getLogger(__name__)


def _reply_kwargs(dest: Optional[dict], *, fallback_chat_id=None) -> dict:
    kwargs: dict = {}
    if isinstance(dest, dict):
        chat_id = dest.get("chat_id")
        if chat_id is not None:
            kwargs["chat_id"] = chat_id
        thread_id = dest.get("message_thread_id")
        if thread_id is not None:
            kwargs["message_thread_id"] = thread_id
        direct_messages_topic_id = dest.get("direct_messages_topic_id")
        if direct_messages_topic_id is not None:
            kwargs["direct_messages_topic_id"] = direct_messages_topic_id
    if "chat_id" not in kwargs and fallback_chat_id is not None:
        kwargs["chat_id"] = fallback_chat_id
    return kwargs


def build_pending_input_reply_markup(
    decision: PendingInputDecision, lang: str = "ru"
) -> InlineKeyboardMarkup | None:
    action = str(decision.action or "").strip()
    if action == PENDING_ACTION_CONFIRM:
        return InlineKeyboardMarkup(
            [
                [InlineKeyboardButton(t("btn.input.take", lang), callback_data="take_pending_input")],
                [InlineKeyboardButton(t("btn.input.discard", lang), callback_data="discard_input")],
            ]
        )
    if action == PENDING_ACTION_QUEUE_CHOICE:
        return InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(t("btn.input.queue_append", lang), callback_data="queue_append_pending"),
                    InlineKeyboardButton(t("btn.input.queue_new", lang), callback_data="queue_input"),
                ],
                [InlineKeyboardButton(t("btn.input.discard", lang), callback_data="discard_input")],
            ]
        )
    if action == PENDING_ACTION_QUEUE_CONFIRM:
        return InlineKeyboardMarkup(
            [
                [InlineKeyboardButton(t("btn.input.queue_confirm", lang), callback_data="queue_input")],
                [InlineKeyboardButton(t("btn.input.discard", lang), callback_data="discard_input")],
            ]
        )
    if action == PENDING_ACTION_ORCHESTRATOR_TRANSITION:
        payload = dict(decision.payload or {})
        session_uid = str(payload.get("session_uid") or "").strip()
        target_mode_id = str(payload.get("target_mode_id") or "").strip()
        if not session_uid or not target_mode_id:
            return None
        return InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        t("btn.input.orch_apply", lang),
                        callback_data=f"orch_transition:apply:{session_uid}:{target_mode_id}",
                    ),
                    InlineKeyboardButton(
                        t("btn.input.orch_cancel", lang),
                        callback_data=f"orch_transition:cancel:{session_uid}",
                    ),
                ]
            ]
        )
    return None


class TelegramPendingInputUiAdapter:
    def __init__(self, bot_app: Any) -> None:
        self.bot_app = bot_app

    async def send_decision(
        self,
        context,
        decision: PendingInputDecision,
        *,
        dest: Optional[dict],
        fallback_chat_id: Any,
    ) -> Any:
        reply_kwargs = _reply_kwargs(dest, fallback_chat_id=fallback_chat_id)
        chat_id = reply_kwargs.get("chat_id")
        lang = resolve_user_lang(self.bot_app.config, chat_id=chat_id)
        return await self.bot_app._send_message(
            context,
            text=str(decision.text or ""),
            reply_markup=build_pending_input_reply_markup(decision, lang),
            **reply_kwargs,
        )

    async def send_text(
        self,
        context,
        *,
        text: str,
        dest: Optional[dict],
        fallback_chat_id: Any,
        md2: bool = True,
    ) -> Any:
        kwargs = _reply_kwargs(dest, fallback_chat_id=fallback_chat_id)
        if not md2:
            kwargs["md2"] = False
        return await self.bot_app._send_message(
            context,
            text=str(text or ""),
            **kwargs,
        )

    async def send_document(
        self,
        context,
        *,
        document: Any,
        filename: str,
        caption: str,
        dest: Optional[dict],
        fallback_chat_id: Any,
    ) -> Any:
        return await self.bot_app._send_document(
            context,
            document=document,
            filename=str(filename or ""),
            caption=str(caption or ""),
            **_reply_kwargs(dest, fallback_chat_id=fallback_chat_id),
        )

    async def retire_prompt(
        self,
        context,
        *,
        dest: Optional[dict],
        message_id: int,
        stale_text: str,
        ui_key: Any,
    ) -> bool:
        reply_kwargs = _reply_kwargs(dest)
        chat_token = reply_kwargs.get("chat_id")
        if chat_token is None:
            return False
        try:
            lang = resolve_user_lang(self.bot_app.config, chat_id=chat_token)
        except Exception:
            lang = "ru"
        clearer = getattr(self.bot_app, "_clear_message_reply_markup", None)
        if callable(clearer):
            try:
                cleared = await clearer(
                    context,
                    chat_id=chat_token,
                    message_id=int(message_id),
                    dest=dest,
                )
                if cleared:
                    return True
            except Exception:
                logger.exception("failed to clear pending prompt reply markup ui_key=%s", ui_key)
        editor = getattr(self.bot_app, "_edit_message", None)
        if not callable(editor):
            return False
        try:
            await editor(
                context,
                chat_id=chat_token,
                message_id=int(message_id),
                text=str(stale_text or t("msg.input.prompt_retired", lang)),
                reply_markup=None,
            )
            return True
        except Exception:
            logger.exception("failed to retire pending prompt ui_key=%s", ui_key)
            return False
