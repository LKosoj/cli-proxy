from __future__ import annotations

from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from modes.sdk.services.callback_data import build_mode_action_callback_data
from sessions.session_state_access import get_active_mode


def build_codebase_mapper_menu(
    session: Any,
    back_callback: str,
    back_text: str,
    *,
    mode_id: str = "codebase_mapper",
    init_label: str = "🧭 Инициализировать граф",
) -> tuple[str, InlineKeyboardMarkup]:
    enabled = str(get_active_mode(session, "") or "").strip() == str(mode_id or "").strip()
    if enabled:
        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "🔴 Выключить маппер",
                        callback_data=build_mode_action_callback_data(mode_id, "disable", session=session),
                    )
                ],
                [InlineKeyboardButton(init_label, callback_data=build_mode_action_callback_data(mode_id, "init", session=session))],
                [InlineKeyboardButton("🛠 Repair", callback_data=build_mode_action_callback_data(mode_id, "repair", session=session))],
                [InlineKeyboardButton("🧾 Ревью", callback_data=build_mode_action_callback_data(mode_id, "review", session=session))],
                [
                    InlineKeyboardButton(
                        "📊 Статус",
                        callback_data=build_mode_action_callback_data(mode_id, "status", session=session),
                    )
                ],
                [InlineKeyboardButton(back_text, callback_data=back_callback)],
            ]
        )
        text = (
            "🗺 Codebase Mapper\n\nРежим: включен\n\n"
            "Собирает и актуализирует карту проекта в `.cli-proxy/.codebase_map/`."
        )
    else:
        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "🟢 Включить маппер",
                        callback_data=build_mode_action_callback_data(mode_id, "enable", session=session),
                    )
                ],
                [InlineKeyboardButton(back_text, callback_data=back_callback)],
            ]
        )
        text = "🗺 Codebase Mapper\n\nРежим: выключен\n\nВключить?"
    return text, keyboard
