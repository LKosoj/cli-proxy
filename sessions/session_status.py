from __future__ import annotations

import time
from typing import Any, Optional

from app.services.trace_contract import adapt_runtime_event
from sessions.session_state_access import get_active_mode, is_orchestrator_enabled, is_ssh_remote_enabled
from utils.ui import status_dot
from app.services.ssh_config_loader import load_ssh_config


def get_session_queue_len(session: Any) -> int:
    return len(getattr(session, "queue", []) or [])


def build_common_mode_stage(
    *,
    enabled: bool,
    running: bool,
    busy: bool,
    queue_len: int,
    disabled_stage: str = "выключен",
    running_stage: str = "обрабатывает задачу",
    queued_stage: str = "ждет задачи в очереди",
    idle_stage: str = "ожидает новый запрос",
    draining_stage: Optional[str] = None,
) -> str:
    if not enabled:
        return disabled_stage
    if running and busy:
        return running_stage
    if queue_len > 0:
        return queued_stage
    if running and draining_stage:
        return draining_stage
    if running:
        return running_stage
    return idle_stage


def build_manager_mode_stage(
    *,
    enabled: bool,
    running: bool,
    busy: bool,
    queue_len: int,
    plan_status: str,
) -> str:
    if not enabled:
        return "idle"
    ps = str(plan_status or "").strip()
    if ps:
        return ps
    return build_common_mode_stage(
        enabled=enabled,
        running=running,
        busy=busy,
        queue_len=queue_len,
        idle_stage="нет активного плана",
    )


def build_webmaster_mode_stage(
    *,
    enabled: bool,
    running: bool,
    busy: bool,
    queue_len: int,
    wm_stage: str,
) -> str:
    ws = str(wm_stage or "").strip()
    if ws and ws.lower() != "idle":
        return ws
    return build_common_mode_stage(
        enabled=enabled,
        running=running,
        busy=busy,
        queue_len=queue_len,
        idle_stage="idle",
    )


def registered_modes(mode_registry: Optional[Any]) -> list[tuple[str, str]]:
    if not mode_registry:
        return []
    if not hasattr(mode_registry, "list_modes"):
        return []
    try:
        modes = mode_registry.list_modes()
    except Exception:
        return []
    return [(str(mid), str(label)) for mid, label in list(modes or [])]


def visible_modes(
    mode_registry: Optional[Any],
    *,
    chat_id: Optional[int] = None,
    access_policy: Optional[Any] = None,
) -> list[tuple[str, str]]:
    modes = registered_modes(mode_registry)
    if chat_id is None:
        return modes
    checker = getattr(access_policy, "allowed_mode_ids_for_chat", None) if access_policy is not None else None
    if not callable(checker):
        return []
    try:
        allowed = {
            str(mode_id or "").strip()
            for mode_id in list(checker(int(chat_id)) or [])
            if str(mode_id or "").strip()
        }
    except Exception:
        return []
    return [(mode_id, label) for mode_id, label in modes if str(mode_id or "").strip() in allowed]


def build_modes_status_line(
    active_mode: str,
    mode_registry: Optional[Any],
    *,
    mode_items: Optional[list[tuple[str, str]]] = None,
) -> str:
    modes = list(mode_items) if mode_items is not None else registered_modes(mode_registry)
    if not modes:
        return "Режимы: нет"
    parts: list[str] = []
    for mode_id, label in modes:
        enabled = str(active_mode or "").strip() == mode_id
        parts.append(f"{label}: {status_dot(enabled)}")
    return " | ".join(parts)


def _runtime_progress_brief(session: Any) -> str:
    last = getattr(session, "runtime_progress_last_event", None)
    if not isinstance(last, dict):
        return ""
    trace = adapt_runtime_event(last)
    meta = trace.get("metadata") or {}
    source = str(meta.get("source") or "").strip()
    phase = str(meta.get("phase") or "").strip()
    status = str(trace.get("status") or "").strip()
    message = str(trace.get("message") or "").strip()
    parts = [x for x in [source, phase, status] if x]
    prefix = "/".join(parts) if parts else ""
    if prefix and message:
        return f"{prefix}: {message}"
    return prefix or message


def build_session_status_text(
    session: Any,
    *,
    mode_registry: Optional[Any] = None,
    mode_items: Optional[list[tuple[str, str]]] = None,
    show_orchestrator: bool = True,
    title_prefix: str = "Активная сессия",
) -> str:
    now = time.time()
    busy_txt = "занята" if bool(getattr(session, "busy", False)) else "свободна"
    git_state = getattr(session, "git", None)
    git_busy = bool(getattr(git_state, "busy", getattr(session, "git_busy", False)))
    git_txt = "git: занято" if git_busy else "git: свободно"
    conflict_txt = ""
    git_conflict = bool(getattr(git_state, "conflict", getattr(session, "git_conflict", False)))
    if git_conflict:
        git_conflict_kind = getattr(git_state, "conflict_kind", getattr(session, "git_conflict_kind", None))
        conflict_txt = f" | конфликт: {git_conflict_kind or 'да'}"
    started_at = getattr(session, "started_at", None)
    last_output_ts = getattr(session, "last_output_ts", None)
    last_tick_ts = getattr(session, "last_tick_ts", None)
    run_for = f"{int(now - started_at)}с" if started_at else "нет"
    last_out = f"{int(now - last_output_ts)}с назад" if last_output_ts else "нет"
    tick_txt = f"{int(now - last_tick_ts)}с назад" if last_tick_ts else "нет"
    active_mode = str(get_active_mode(session, "") or "").strip()
    mode_status = build_modes_status_line(active_mode, mode_registry, mode_items=mode_items)
    project_root = getattr(session, "project_root", None)
    lines = [
        f"{title_prefix}: {session.id} ({session.name or session.tool.name}) @ {session.workdir}",
        (
            f"Статус: {busy_txt} | {git_txt}{conflict_txt} | В работе: {run_for} | "
            f"{mode_status}"
        ),
    ]
    if project_root:
        lines.append(f"Проект: {project_root}")
    lines.append(f"Последний вывод: {last_out} | Последний тик: {tick_txt} | Тиков: {getattr(session, 'tick_seen', 0)}")
    runtime_brief = _runtime_progress_brief(session)
    if runtime_brief:
        lines.append(f"Runtime: {runtime_brief}")
    queue_len = get_session_queue_len(session)
    queue_line = f"Очередь: {queue_len} | Resume: {'есть' if getattr(session, 'resume_token', None) else 'нет'}"
    if show_orchestrator:
        orchestrator = "вкл" if bool(is_orchestrator_enabled(session, False)) else "выкл"
        queue_line = f"{queue_line} | Оркестратор: {orchestrator}"
    lines.append(queue_line)
    ssh_hosts = load_ssh_config(session.workdir)
    if ssh_hosts:
        enabled = is_ssh_remote_enabled(session)
        if enabled:
            host_names = ", ".join(ssh_hosts.keys())
            lines.append(f"🔗 SSH: вкл ({host_names})")
        else:
            lines.append("🔗 SSH: выкл")
    return "\n".join(lines)


def build_mode_status_text(
    session: Any,
    *,
    title: str,
    stage: str,
    enabled: bool,
    queue_suffix: Optional[str] = None,
    task_suffix: Optional[str] = None,
    extra_sections: Optional[list[tuple[str, str]]] = None,
) -> str:
    now = time.time()
    started_at = getattr(session, "started_at", None)
    last_output_ts = getattr(session, "last_output_ts", None)
    last_tick_ts = getattr(session, "last_tick_ts", None)
    run_for = f"{int(now - started_at)}с" if started_at else "нет"
    last_out = f"{int(now - last_output_ts)}с назад" if last_output_ts else "нет"
    tick_txt = f"{int(now - last_tick_ts)}с назад" if last_tick_ts else "нет"
    queue_len = get_session_queue_len(session)

    state_line = f"Режим: {'включен' if enabled else 'выключен'}"
    if task_suffix:
        state_line += f" | {task_suffix}"
    state_line += f" | В работе: {run_for}"

    queue_line = f"Очередь: {queue_len}"
    if queue_suffix:
        queue_line += f" | {queue_suffix}"

    lines = [
        title,
        f"Сессия: {session.id} ({session.name or session.tool.name}) @ {session.workdir}",
        f"Стадия: {stage}",
        state_line,
        f"Последний вывод: {last_out} | Последний тик: {tick_txt} | Тиков: {getattr(session, 'tick_seen', 0)}",
        queue_line,
    ]
    runtime_brief = _runtime_progress_brief(session)
    if runtime_brief:
        lines.append(f"Runtime: {runtime_brief}")
    for key, value in (extra_sections or []):
        k = str(key or "").strip()
        if not k:
            continue
        lines.append(f"{k}: {value}")
    return "\n".join(lines)
