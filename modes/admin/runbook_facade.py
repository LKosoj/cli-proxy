from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from modes.sdk import ToolResult
from modes.sdk.services.callback_data import build_mode_action_callback_data

from .ui import (
    build_admin_runbook_catalog_screen,
    build_admin_runbook_detail_screen,
    build_admin_runbook_promote_screen,
    build_admin_runbook_validation_screen,
)

if TYPE_CHECKING:
    from .mode import AdminMode

_log = logging.getLogger(__name__)


class RunbookFacade:
    """Handles runbook-related Telegram callback logic for AdminMode.

    Accepts ``mode`` to delegate self-state access (messaging, autonomy service,
    token helpers, error sender) without duplicating that logic.
    """

    def __init__(self, mode: "AdminMode") -> None:
        self._mode = mode

    # ------------------------------------------------------------------
    # Public entry-points — mirror AdminMode._cb_runbook_* signatures
    # ------------------------------------------------------------------

    async def cb_runbooks_list(
        self,
        *,
        bot_app: Any,
        session: Any,
        chat_id: int,
        context: Any,
        query: Any,
        server_token: str,
    ) -> ToolResult:
        mode = self._mode
        ms = mode._messaging(bot_app=bot_app, context=context)
        svc = mode._autonomy_service(session=session)
        sid = mode._resolve_server_id_from_token(session, token=server_token) if server_token else None
        if svc is None:
            return await mode._send_autonomy_error(
                ms=ms, query=query, chat_id=chat_id, message="Workdir не задан.",
            )
        try:
            summaries = svc.list_runbook_summary(server_id=sid, limit=15)
        except Exception:
            _log.exception("admin autonomy: list_runbook_summary failed sid=%s", sid)
            return await mode._send_autonomy_error(
                ms=ms, query=query, chat_id=chat_id,
                message="Не удалось получить список runbook-ов.",
            )
        text = build_admin_runbook_catalog_screen(server_id=sid, runbooks=summaries)
        kb_rows: list[list[InlineKeyboardButton]] = []
        for rb in summaries[:8]:
            rb_id = str(rb.get("id") or "").strip()
            if not rb_id:
                continue
            rb_token = mode._short_token(rb_id, max_len=16)
            mode._register_entity_token(session, "_admin_runbook_tokens", token=rb_token, value=rb_id)
            title = str(rb.get("title") or rb_id)[:28]
            kb_rows.append([
                InlineKeyboardButton(
                    f"📖 {title}",
                    callback_data=build_mode_action_callback_data(
                        mode.mode_id, "rb_view", session=session,
                        payload={"id": server_token, "rb": rb_token},
                    ),
                )
            ])
        if sid:
            kb_rows.append([mode._back_to_server_detail_button(session, server_token=server_token)])
        else:
            kb_rows.append([InlineKeyboardButton(
                "⬅️ Назад",
                callback_data=build_mode_action_callback_data(mode.mode_id, "menu", session=session),
            )])
        await ms.send_or_edit(
            query=query, chat_id=chat_id, text=text, md2=True,
            reply_markup=InlineKeyboardMarkup(kb_rows),
        )
        return ToolResult.ok()

    async def cb_runbook_view(
        self,
        *,
        bot_app: Any,
        session: Any,
        chat_id: int,
        context: Any,
        query: Any,
        server_token: str,
        runbook_token: str,
    ) -> ToolResult:
        from collections.abc import Mapping

        mode = self._mode
        ms = mode._messaging(bot_app=bot_app, context=context)
        svc = mode._autonomy_service(session=session)
        rb_id = mode._resolve_entity_by_token(session, "_admin_runbook_tokens", token=runbook_token)
        if svc is None or not rb_id:
            return await mode._send_autonomy_error(
                ms=ms, query=query, chat_id=chat_id, message="Runbook не найден.",
            )
        try:
            rb = svc.get_runbook(rb_id)
        except Exception:
            _log.exception("admin autonomy: get_runbook failed rb=%s", rb_id)
            return await mode._send_autonomy_error(
                ms=ms, query=query, chat_id=chat_id,
                message="Не удалось загрузить runbook.",
            )
        if rb is None:
            return await mode._send_autonomy_error(
                ms=ms, query=query, chat_id=chat_id, message="Runbook не найден.",
            )
        text = build_admin_runbook_detail_screen(runbook=rb.as_dict())
        is_script_rb = bool(isinstance(rb.metadata, Mapping) and rb.metadata.get("steps"))
        kb_rows: list[list[InlineKeyboardButton]] = []
        if is_script_rb:
            kb_rows.append([
                InlineKeyboardButton(
                    "✅ Validate",
                    callback_data=build_mode_action_callback_data(
                        mode.mode_id, "rb_validate", session=session,
                        payload={"id": server_token, "rb": runbook_token},
                    ),
                ),
                InlineKeyboardButton(
                    "🚀 Promote",
                    callback_data=build_mode_action_callback_data(
                        mode.mode_id, "rb_promote", session=session,
                        payload={"id": server_token, "rb": runbook_token},
                    ),
                ),
            ])
        kb_rows.append([
            InlineKeyboardButton(
                "⬅️ К списку",
                callback_data=build_mode_action_callback_data(
                    mode.mode_id, "rb_list", session=session, payload={"id": server_token},
                ),
            ),
        ])
        await ms.send_or_edit(
            query=query, chat_id=chat_id, text=text, md2=True,
            reply_markup=InlineKeyboardMarkup(kb_rows),
        )
        return ToolResult.ok()

    async def cb_runbook_validate(
        self,
        *,
        bot_app: Any,
        session: Any,
        chat_id: int,
        context: Any,
        query: Any,
        server_token: str,
        runbook_token: str,
    ) -> ToolResult:
        mode = self._mode
        ms = mode._messaging(bot_app=bot_app, context=context)
        svc = mode._autonomy_service(session=session)
        rb_id = mode._resolve_entity_by_token(session, "_admin_runbook_tokens", token=runbook_token)
        if svc is None or not rb_id:
            return await mode._send_autonomy_error(
                ms=ms, query=query, chat_id=chat_id, message="Runbook не найден.",
            )
        try:
            report = await svc.validate_runbook(rb_id)
        except Exception as exc:
            _log.exception("admin autonomy: validate_runbook failed rb=%s", rb_id)
            return await mode._send_autonomy_error(
                ms=ms, query=query, chat_id=chat_id,
                message=f"Validate failed: {exc}",
            )
        text = build_admin_runbook_validation_screen(rb_id=rb_id, report=report.to_dict())
        kb_rows = [[
            InlineKeyboardButton(
                "⬅️ Назад",
                callback_data=build_mode_action_callback_data(
                    mode.mode_id, "rb_view", session=session,
                    payload={"id": server_token, "rb": runbook_token},
                ),
            ),
        ]]
        await ms.send_or_edit(
            query=query, chat_id=chat_id, text=text, md2=True,
            reply_markup=InlineKeyboardMarkup(kb_rows),
        )
        return ToolResult.ok()

    async def cb_runbook_promote(
        self,
        *,
        bot_app: Any,
        session: Any,
        chat_id: int,
        context: Any,
        query: Any,
        server_token: str,
        runbook_token: str,
    ) -> ToolResult:
        mode = self._mode
        ms = mode._messaging(bot_app=bot_app, context=context)
        svc = mode._autonomy_service(session=session)
        rb_id = mode._resolve_entity_by_token(session, "_admin_runbook_tokens", token=runbook_token)
        if svc is None or not rb_id:
            return await mode._send_autonomy_error(
                ms=ms, query=query, chat_id=chat_id, message="Runbook не найден.",
            )
        # Продакшен-цели определяем по тегу "prod" в admin.servers.
        try:
            specs = svc.list_server_specs()
        except Exception:
            _log.exception("admin autonomy: list_server_specs failed")
            specs = []
        prod_targets = sorted({
            spec.server_id for spec in specs
            if any(str(t).lower() == "prod" for t in (spec.tags or []))
        })
        if not prod_targets:
            return await mode._send_autonomy_error(
                ms=ms, query=query, chat_id=chat_id,
                message="Не нашёл серверов с тегом `prod` в admin.servers. "
                        "Используйте MiniApp для ручного promote.",
            )
        try:
            result = await svc.promote_runbook(
                rb_id,
                add_servers=prod_targets,
                confidence=0.8,
                run_validation=True,
            )
        except Exception as exc:
            _log.exception("admin autonomy: promote_runbook failed rb=%s", rb_id)
            return await mode._send_autonomy_error(
                ms=ms, query=query, chat_id=chat_id,
                message=f"Promote failed: {exc}",
            )
        text = build_admin_runbook_promote_screen(rb_id=rb_id, result=result.to_dict())
        kb_rows = [[
            InlineKeyboardButton(
                "⬅️ Назад",
                callback_data=build_mode_action_callback_data(
                    mode.mode_id, "rb_view", session=session,
                    payload={"id": server_token, "rb": runbook_token},
                ),
            ),
        ]]
        await ms.send_or_edit(
            query=query, chat_id=chat_id, text=text, md2=True,
            reply_markup=InlineKeyboardMarkup(kb_rows),
        )
        return ToolResult.ok()
