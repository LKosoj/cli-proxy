from __future__ import annotations

from typing import Any, Dict, Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from app.services.menu_visibility_policy import ModeMenuVisibility
from modes.sdk.services import ModeStatusService
from modes.sdk.services.callback_data import build_mode_action_callback_data
from session import session_runtime_uid, session_scoped_key
from modes.sdk.services.status_details import (
    collect_pending_questions_for_session,
    extract_queue_origin,
    format_queue_origin,
)
from sessions.session_state_access import get_active_mode


def _allows(menu_visibility: Optional[ModeMenuVisibility], action: str) -> bool:
    return True if menu_visibility is None else menu_visibility.allows(action)


def _allows_run_operation(menu_visibility: Optional[ModeMenuVisibility], operation: str) -> bool:
    return _allows(menu_visibility, operation)


def build_agent_menu(
    session: Any,
    back_callback: str,
    back_text: str,
    *,
    mode_id: str = "agent",
    menu_visibility: Optional[ModeMenuVisibility] = None,
) -> tuple[str, InlineKeyboardMarkup]:
    enabled = str(get_active_mode(session, "") or "").strip() == str(mode_id or "").strip()
    project_session_key = str(session_scoped_key(session) or getattr(session, "id", "") or "").strip()
    project_root = getattr(session, "project_root", None)
    project_line = f"Проект: {project_root}" if project_root else "Проект: не подключен"
    if enabled:
        rows = []
        if _allows(menu_visibility, "disable"):
            rows.append(
                [
                    InlineKeyboardButton(
                        "🔴 Выключить агента",
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
        if project_root:
            if _allows(menu_visibility, "project_change"):
                rows.append(
                    [
                        InlineKeyboardButton(
                            "📂 Сменить проект",
                            callback_data=build_mode_action_callback_data(
                                mode_id,
                                "project_change",
                                session=session,
                                payload=f"sk={project_session_key}",
                            ),
                        )
                    ]
                )
            if _allows(menu_visibility, "project_disconnect"):
                rows.append(
                    [
                        InlineKeyboardButton(
                            "🔌 Отключить проект",
                            callback_data=build_mode_action_callback_data(mode_id, "project_disconnect", session=session),
                        )
                    ]
                )
        else:
            if _allows(menu_visibility, "project_connect"):
                rows.append(
                    [
                        InlineKeyboardButton(
                            "📂 Подключить проект",
                            callback_data=build_mode_action_callback_data(
                                mode_id,
                                "project_connect",
                                session=session,
                                payload=f"sk={project_session_key}",
                            ),
                        )
                    ]
                )
        plugins_callback = build_mode_action_callback_data(
            mode_id,
            "plugins",
            payload={"s": session_runtime_uid(session)},
        )
        if _allows(menu_visibility, "plugins"):
            rows.append([InlineKeyboardButton("🧩 Плагины", callback_data=plugins_callback)])
        if _allows(menu_visibility, "clean_all"):
            rows.append(
                [
                    InlineKeyboardButton(
                        "🧹 Очистить песочницу",
                        callback_data=build_mode_action_callback_data(mode_id, "clean_all", session=session),
                    )
                ]
            )
        if _allows(menu_visibility, "clean_session"):
            rows.append(
                [
                    InlineKeyboardButton(
                        "🧹 Очистить сессию",
                        callback_data=build_mode_action_callback_data(mode_id, "clean_session", session=session),
                    )
                ]
            )
        rows.append([InlineKeyboardButton(back_text, callback_data=back_callback)])
        keyboard = InlineKeyboardMarkup(rows)
        text = f"Агент сейчас включен.\n{project_line}\nВыберите действие:"
    else:
        rows = []
        if _allows(menu_visibility, "enable"):
            rows.append(
                [
                    InlineKeyboardButton(
                        "🟢 Включить агента",
                        callback_data=build_mode_action_callback_data(mode_id, "enable", session=session),
                    )
                ]
            )
        rows.append([InlineKeyboardButton(back_text, callback_data=back_callback)])
        keyboard = InlineKeyboardMarkup(rows)
        text = f"Агент сейчас выключен.\n{project_line}\nВключить?"
    return text, keyboard


def build_agent_status_payload(
    session: Any,
    *,
    mode_id: str = "agent",
    agent_running: Optional[bool] = None,
    pending_questions: Optional[Dict[str, Dict[str, object]]] = None,
    active_plugin_flow: str = "",
    queue_len: Optional[int] = None,
    runtime_progress: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    enabled = str(get_active_mode(session, "") or "").strip() == str(mode_id or "").strip()
    running = bool(agent_running)
    pending = collect_pending_questions_for_session(
        pending_questions,
        session_id=str(getattr(session, "id", "") or ""),
    )
    queue_len_value = ModeStatusService.get_session_queue_len(session) if queue_len is None else int(queue_len)

    flow = str(active_plugin_flow or "").strip()
    if pending["count"] > 0 and "ask_user" not in flow:
        flow = "ask_user" if not flow else f"{flow},ask_user"

    if pending["count"] > 0:
        stage = "ожидает ваш ответ (свободный ввод)" if pending["awaiting_custom"] else "ожидает ваш ответ"
    elif "project" in flow:
        stage = "ожидает выбор проекта"
    else:
        stage = ModeStatusService.build_common_mode_stage(
            enabled=enabled,
            running=running,
            busy=bool(getattr(session, "busy", False)),
            queue_len=queue_len_value,
            running_stage="выполняет задачу",
            draining_stage="завершает обработку",
        )

    return {
        "stage": stage,
        "enabled": enabled,
        "running": running,
        "pending_questions": pending,
        "active_plugin_flow": flow,
        "runtime_progress": dict(runtime_progress or {}),
        "template": {
            "selected": "n/a",
            "runtime_override": "n/a",
            "effective": "n/a",
        },
        "queue_origin": extract_queue_origin(session),
    }


def build_agent_status_text(
    session: Any,
    *,
    mode_id: str = "agent",
    agent_running: Optional[bool] = None,
    pending_questions: Optional[Dict[str, Dict[str, object]]] = None,
    active_plugin_flow: str = "",
    runtime_progress: Optional[Dict[str, Any]] = None,
    lang: str = "ru",
) -> str:
    payload = build_agent_status_payload(
        session,
        mode_id=mode_id,
        agent_running=agent_running,
        pending_questions=pending_questions,
        active_plugin_flow=active_plugin_flow,
        runtime_progress=runtime_progress,
    )
    pending = payload["pending_questions"]
    template = payload["template"]
    pending_line = (
        f"{pending['count']}; active={pending['active_question_id'] or '-'}; "
        f"custom={'да' if pending['awaiting_custom'] else 'нет'}"
    )
    template_line = (
        f"selected={template['selected']} | "
        f"runtime={template['runtime_override']} | "
        f"effective={template['effective']}"
    )
    runtime = payload.get("runtime_progress") or {}
    runtime_line = "-"
    if isinstance(runtime, dict):
        source = str(runtime.get("last_source") or "").strip()
        phase = str(runtime.get("last_phase") or "").strip()
        status = str(runtime.get("last_status") or "").strip()
        message = str(runtime.get("last_message") or "").strip()
        parts = [x for x in [source, phase, status] if x]
        prefix = "/".join(parts) if parts else "-"
        runtime_line = f"{prefix}: {message}" if message else prefix

    # Token usage from runtime_progress (if available).
    token_line = "-"
    if isinstance(runtime, dict):
        ctx_tokens = runtime.get("context_tokens")
        ctx_window = runtime.get("context_window")
        if ctx_tokens is not None and ctx_window:
            pct = int(ctx_tokens / ctx_window * 100) if ctx_window else 0
            token_line = f"{ctx_tokens:,} / {ctx_window:,} ({pct}%)"

    return ModeStatusService.build_mode_status_text(
        session,
        title="🤖 Статус Агента",
        stage=str(payload.get("stage") or ""),
        enabled=bool(payload.get("enabled")),
        task_suffix=f"Задача: {'активна' if bool(payload.get('running')) else 'нет'}",
        queue_suffix=f"Ожидание ответа: {pending['count']}",
        extra_sections=[
            ("Проект", str(getattr(session, "project_root", None) or "не подключен")),
            ("Контекст", token_line),
            ("Pending questions", pending_line),
            ("Active plugin flow", str(payload.get("active_plugin_flow") or "нет")),
            ("Runtime", runtime_line),
            ("Template/override", template_line),
            ("Queue origin", format_queue_origin(payload["queue_origin"])),
        ],
        lang=lang,
    )
