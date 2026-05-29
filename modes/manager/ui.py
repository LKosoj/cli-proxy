from __future__ import annotations

from typing import Any, Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from app.services.menu_visibility_policy import ModeMenuVisibility
from modes.sdk.services.callback_data import build_mode_action_callback_data
from sessions.session_state_access import get_active_mode


def _allows(menu_visibility: Optional[ModeMenuVisibility], action: str) -> bool:
    return True if menu_visibility is None else menu_visibility.allows(action)


def _allows_run_operation(menu_visibility: Optional[ModeMenuVisibility], operation: str) -> bool:
    return _allows(menu_visibility, operation)


def build_manager_menu_with_back(
    session: Any,
    back_callback: str,
    back_text: str,
    *,
    plan_status: Optional[str] = None,
    mode_id: str = "manager",
    menu_visibility: Optional[ModeMenuVisibility] = None,
) -> tuple[str, InlineKeyboardMarkup]:
    enabled = str(get_active_mode(session, "") or "").strip() == str(mode_id or "").strip()
    quiet_mode = bool(
        getattr(
            getattr(session, "modes", None),
            "manager_quiet_mode",
            getattr(session, "manager_quiet_mode", False),
        )
    )
    quiet_status = "вкл" if quiet_mode else "выкл"
    quiet_icon = "🔇" if quiet_mode else "🔈"
    paused = str(plan_status or "").strip().lower() == "paused"

    if enabled:
        rows = []
        if _allows(menu_visibility, "disable"):
            rows.append(
                [
                    InlineKeyboardButton(
                        "🔴 Выключить менеджера",
                        callback_data=build_mode_action_callback_data(mode_id, "disable", session=session),
                    )
                ]
            )
        if _allows(menu_visibility, "quiet_toggle"):
            rows.append(
                [
                    InlineKeyboardButton(
                        f"{quiet_icon} Тихий режим: {quiet_status}",
                        callback_data=build_mode_action_callback_data(mode_id, "quiet_toggle", session=session),
                    )
                ]
            )
        if _allows(menu_visibility, "status"):
            rows.append(
                [
                    InlineKeyboardButton(
                        "📋 Статус плана",
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
        if paused:
            if _allows(menu_visibility, "resume_paused"):
                rows.append(
                    [
                        InlineKeyboardButton(
                            "▶️ Продолжить план",
                            callback_data=build_mode_action_callback_data(mode_id, "resume_paused", session=session),
                        )
                    ]
                )
        else:
            if _allows(menu_visibility, "pause"):
                rows.append(
                    [
                        InlineKeyboardButton(
                            "⏸ Приостановить",
                            callback_data=build_mode_action_callback_data(mode_id, "pause", session=session),
                        )
                    ]
                )
        if _allows(menu_visibility, "reset"):
            rows.append(
                [
                    InlineKeyboardButton(
                        "🗑 Сбросить план",
                        callback_data=build_mode_action_callback_data(mode_id, "reset", session=session),
                    )
                ]
            )
        rows.append([InlineKeyboardButton(back_text, callback_data=back_callback)])
        keyboard = InlineKeyboardMarkup(rows)
        status_line = " (пауза)" if paused else ""
        text = f"🏗 Менеджер проекта\n\nРежим: включен{status_line}\nТихий режим: {quiet_status}\n\nВыберите действие:"
    else:
        rows = []
        if _allows(menu_visibility, "enable"):
            rows.append(
                [
                    InlineKeyboardButton(
                        "🟢 Включить менеджера",
                        callback_data=build_mode_action_callback_data(mode_id, "enable", session=session),
                    )
                ]
            )
        rows.append([InlineKeyboardButton(back_text, callback_data=back_callback)])
        keyboard = InlineKeyboardMarkup(rows)
        text = f"🏗 Менеджер проекта\n\nРежим: выключен\nТихий режим: {quiet_status}\n\nВключить?"
    return text, keyboard
