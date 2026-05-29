from __future__ import annotations

from typing import Any, Dict, Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from app.services.menu_visibility_policy import ModeMenuVisibility
from modes.analyst.draft_service import resolve_draft_document_profile
from modes.sdk.services import ModeStatusService
from modes.sdk.services.callback_data import build_mode_action_callback_data
from modes.sdk.services.status_details import (
    collect_pending_questions_for_session,
    extract_queue_origin,
    format_queue_origin,
)
from sessions.session_state_access import get_active_mode


_PHASE_LABELS: Dict[str, str] = {
    "classify": "классифицирует запрос",
    "gate1": "уточняет постановку",
    "gather": "собирает контекст",
    "gate2": "уточняет по контексту",
    "analyze": "анализирует и пишет документ",
    "deliver": "оформляет результат",
    "completed": "анализ завершён",
    "failed": "ошибка анализа",
}


def _allows(menu_visibility: Optional[ModeMenuVisibility], action: str) -> bool:
    return True if menu_visibility is None else menu_visibility.allows(action)


def _allows_run_operation(menu_visibility: Optional[ModeMenuVisibility], operation: str) -> bool:
    return _allows(menu_visibility, operation)


def build_analyst_menu(
    session: Any,
    back_callback: str,
    back_text: str,
    *,
    analyst_context: Optional[Any] = None,
    mode_id: str = "analyst",
    menu_visibility: Optional[ModeMenuVisibility] = None,
) -> tuple[str, InlineKeyboardMarkup]:
    enabled = str(get_active_mode(session, "") or "").strip() == str(mode_id or "").strip()
    draft_profile = resolve_draft_document_profile(session=session, analyst_context=analyst_context)
    current_template_id = str(
        getattr(
            getattr(session, "modes", None),
            "analyst_template_id",
            getattr(session, "analyst_template_id", "default"),
        )
        or "default"
    )
    if enabled:
        tpl_default_prefix = "✅" if current_template_id == "default" else "🧩"
        tpl_audit_prefix = "✅" if current_template_id == "audit" else "🧩"
        rows = []
        if _allows(menu_visibility, "disable"):
            rows.append(
                [
                    InlineKeyboardButton(
                        "🔴 Выключить аналитика",
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
        if _allows(menu_visibility, "download"):
            rows.append(
                [
                    InlineKeyboardButton(
                        draft_profile["menu_label"],
                        callback_data=build_mode_action_callback_data(mode_id, "download", session=session),
                    )
                ]
            )
        if _allows(menu_visibility, "audit"):
            rows.append(
                [
                    InlineKeyboardButton(
                        "🛡 Провести аудит",
                        callback_data=build_mode_action_callback_data(mode_id, "audit", session=session),
                    )
                ]
            )
        if _allows(menu_visibility, "template"):
            rows.extend(
                [
                    [
                        InlineKeyboardButton(
                            f"{tpl_default_prefix} Шаблон: default",
                            callback_data=build_mode_action_callback_data(
                                mode_id,
                                "template",
                                session=session,
                                payload="default",
                            ),
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            f"{tpl_audit_prefix} Шаблон: audit",
                            callback_data=build_mode_action_callback_data(
                                mode_id,
                                "template",
                                session=session,
                                payload="audit",
                            ),
                        )
                    ],
                ]
            )
        rows.append([InlineKeyboardButton(back_text, callback_data=back_callback)])
        keyboard = InlineKeyboardMarkup(rows)
        text = (
            "🧠 Аналитик\n\n"
            f"Режим: включен\nШаблон: {current_template_id}\n\n"
            "Аналитик формирует полноценное ТЗ из вашего запроса."
        )
    else:
        rows = []
        if _allows(menu_visibility, "enable"):
            rows.append(
                [
                    InlineKeyboardButton(
                        "🟢 Включить аналитика",
                        callback_data=build_mode_action_callback_data(mode_id, "enable", session=session),
                    )
                ]
            )
        rows.append([InlineKeyboardButton(back_text, callback_data=back_callback)])
        keyboard = InlineKeyboardMarkup(rows)
        text = "🧠 Аналитик\n\nРежим: выключен\n\nВключить?"
    return text, keyboard


def build_analyst_status_text(
    session: Any,
    *,
    analyst_context: Optional[Any] = None,
    analyst_task: Optional[Any] = None,
    analyst_running: Optional[bool] = None,
    pending_questions: Optional[Dict[str, Dict[str, object]]] = None,
    mode_id: str = "analyst",
) -> str:
    payload = build_analyst_status_payload(
        session,
        analyst_context=analyst_context,
        analyst_task=analyst_task,
        analyst_running=analyst_running,
        pending_questions=pending_questions,
        mode_id=mode_id,
    )
    pending = payload["pending_questions"]
    template = payload["template"]
    pending_line = (
        f"{pending['count']}; active={pending['active_question_id'] or '-'}; "
        f"custom={'да' if pending['awaiting_custom'] else 'нет'}"
    )
    template_line = (
        f"selected={template['selected']} | "
        f"runtime={template['runtime_override'] or '-'} | "
        f"effective={template['effective'] or '-'}"
    )
    return ModeStatusService.build_mode_status_text(
        session,
        title="🧠 Статус Аналитика",
        stage=str(payload.get("stage") or ""),
        enabled=bool(payload.get("enabled")),
        task_suffix=f"Задача: {'активна' if bool(payload.get('running')) else 'нет'}",
        queue_suffix=f"Ожидание ответа: {pending['count']}",
        extra_sections=[
            ("Pending questions", pending_line),
            ("Active plugin flow", str(payload.get("active_plugin_flow") or "нет")),
            ("Template/override", template_line),
            ("Queue origin", format_queue_origin(payload["queue_origin"])),
        ],
    )


def build_analyst_status_payload(
    session: Any,
    *,
    analyst_context: Optional[Any] = None,
    analyst_task: Optional[Any] = None,
    analyst_running: Optional[bool] = None,
    pending_questions: Optional[Dict[str, Dict[str, object]]] = None,
    mode_id: str = "analyst",
) -> Dict[str, Any]:
    enabled = str(get_active_mode(session, "") or "").strip() == str(mode_id or "").strip()
    if analyst_running is None:
        task_running = bool(analyst_task and not analyst_task.done())
    else:
        task_running = bool(analyst_running)

    pending = collect_pending_questions_for_session(
        pending_questions,
        session_id=str(getattr(session, "id", "") or ""),
    )
    pending_count = int(pending.get("count") or 0)
    queue_len = ModeStatusService.get_session_queue_len(session)
    awaiting_custom = bool(pending.get("awaiting_custom"))
    analyst_mode = str(getattr(analyst_context, "mode", "spec") or "spec").strip()
    active_flow = str(getattr(analyst_context, "active_flow", "") or "").strip()

    # Pipeline phase (set on session by run_pipeline via run_directory)
    pipeline_phase = str(getattr(session, "analyst_pipeline_phase", "") or "").strip()

    if pending_count:
        stage = "ожидает ваш ответ (свободный ввод)" if awaiting_custom else "ожидает ваш ответ"
    elif pipeline_phase and pipeline_phase in _PHASE_LABELS:
        stage = _PHASE_LABELS[pipeline_phase]
    elif analyst_mode == "awaiting_input":
        stage = "ожидает выбор пути для аудита"
    elif task_running and analyst_mode == "audit":
        stage = "проводит аудит"
    elif task_running:
        stage = "анализирует запрос"
    else:
        stage = ModeStatusService.build_common_mode_stage(
            enabled=enabled,
            running=task_running,
            busy=bool(getattr(session, "busy", False)),
            queue_len=queue_len,
            running_stage="анализирует запрос",
            draining_stage="завершает обработку",
        )
    if not active_flow and pending_count > 0:
        active_flow = "ask_user"
    elif not active_flow and analyst_mode == "awaiting_input":
        active_flow = "audit_picker"
    return {
        "stage": stage,
        "enabled": enabled,
        "running": task_running,
        "pending_questions": pending,
        "active_plugin_flow": active_flow,
        "template": {
            "selected": str(
                getattr(
                    getattr(session, "modes", None),
                    "analyst_template_id",
                    getattr(session, "analyst_template_id", "default"),
                )
                or "default"
            ),
            "runtime_override": str(getattr(analyst_context, "runtime_template_id", "") or ""),
            "effective": str(getattr(analyst_context, "effective_template_id", "") or ""),
        },
        "queue_origin": extract_queue_origin(session),
    }
