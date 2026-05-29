from __future__ import annotations

from typing import Any, Tuple

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from app.services.menu_visibility_policy import ModeMenuVisibility
from modes.sdk.services.callback_data import build_mode_action_callback_data
from sessions.session_state_access import get_active_mode


def _allows(menu_visibility: ModeMenuVisibility | None, action: str) -> bool:
    return True if menu_visibility is None else menu_visibility.allows(action)


def _allows_run_operation(menu_visibility: ModeMenuVisibility | None, operation: str) -> bool:
    return _allows(menu_visibility, operation)


def build_webmaster_menu(
    session: Any,
    back_callback: str = "sess_active",
    back_text: str = "⬅️ Назад",
    mode_id: str = "webmaster",
    menu_visibility: ModeMenuVisibility | None = None,
) -> Tuple[str, InlineKeyboardMarkup]:
    enabled = str(get_active_mode(session, "") or "").strip() == mode_id
    if enabled:
        rows = []
        if _allows(menu_visibility, "disable"):
            rows.append(
                [
                    InlineKeyboardButton(
                        "🔴 Выключить вебмастер",
                        callback_data=build_mode_action_callback_data(mode_id, "disable", session=session),
                    )
                ]
            )
        if _allows(menu_visibility, "status"):
            rows.append(
                [
                    InlineKeyboardButton(
                        "📊 Статус",
                        callback_data=build_mode_action_callback_data(mode_id, "status", session=session),
                    )
                ]
            )
        run_ops = []
        if _allows_run_operation(menu_visibility, "doctor"):
            run_ops.append(
                InlineKeyboardButton(
                    "🏥 Doctor",
                    callback_data=build_mode_action_callback_data(mode_id, "doctor", session=session),
                )
            )
        if _allows_run_operation(menu_visibility, "recover"):
            run_ops.append(
                InlineKeyboardButton(
                    "🩹 Recover",
                    callback_data=build_mode_action_callback_data(mode_id, "recover", session=session),
                )
            )
        if _allows_run_operation(menu_visibility, "resume"):
            run_ops.append(
                InlineKeyboardButton(
                    "▶️ Resume",
                    callback_data=build_mode_action_callback_data(mode_id, "resume", session=session),
                )
            )
        if run_ops:
            rows.append(run_ops)
        if _allows_run_operation(menu_visibility, "promote_skills"):
            rows.append(
                [
                    InlineKeyboardButton(
                        "📌 Promote Skills",
                        callback_data=build_mode_action_callback_data(mode_id, "promote_skills", session=session),
                    )
                ]
            )
        if _allows(menu_visibility, "reset"):
            rows.append(
                [
                    InlineKeyboardButton(
                        "🧹 Сбросить контекст",
                        callback_data=build_mode_action_callback_data(mode_id, "reset", session=session),
                    )
                ]
            )
        rows.append([InlineKeyboardButton(back_text, callback_data=back_callback)])
        text = (
            "🌐 Вебмастер\n\nРежим: включен\n\n"
            "Работает через intent -> confirm -> dev_cli -> validation_cli -> feedback."
        )
    else:
        rows = []
        if _allows(menu_visibility, "enable"):
            rows.append(
                [
                    InlineKeyboardButton(
                        "🟢 Включить вебмастер",
                        callback_data=build_mode_action_callback_data(mode_id, "enable", session=session),
                    )
                ]
            )
        rows.append([InlineKeyboardButton(back_text, callback_data=back_callback)])
        text = "🌐 Вебмастер\n\nРежим: выключен\n\nВключить?"
    return text, InlineKeyboardMarkup(rows)
