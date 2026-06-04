from __future__ import annotations

import time
from typing import Any, Optional

from app.services.trace_contract import adapt_runtime_event
from i18n import t
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
    disabled_stage: Optional[str] = None,
    running_stage: Optional[str] = None,
    queued_stage: Optional[str] = None,
    idle_stage: Optional[str] = None,
    draining_stage: Optional[str] = None,
    lang: str = "ru",
) -> str:
    _disabled = disabled_stage if disabled_stage is not None else t("session_status.disabled", lang)
    _running = running_stage if running_stage is not None else t("session_status.stage_running", lang)
    _queued = queued_stage if queued_stage is not None else t("session_status.stage_queued", lang)
    _idle = idle_stage if idle_stage is not None else t("session_status.stage_idle", lang)
    if not enabled:
        return _disabled
    if running and busy:
        return _running
    if queue_len > 0:
        return _queued
    if running and draining_stage:
        return draining_stage
    if running:
        return _running
    return _idle


def build_manager_mode_stage(
    *,
    enabled: bool,
    running: bool,
    busy: bool,
    queue_len: int,
    plan_status: str,
    lang: str = "ru",
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
        idle_stage=t("session_status.stage_manager_idle", lang),
        lang=lang,
    )


def build_webmaster_mode_stage(
    *,
    enabled: bool,
    running: bool,
    busy: bool,
    queue_len: int,
    wm_stage: str,
    lang: str = "ru",
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
        lang=lang,
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
    lang: str = "ru",
) -> str:
    modes = list(mode_items) if mode_items is not None else registered_modes(mode_registry)
    if not modes:
        return t("session_status.modes_none", lang)
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
    title_prefix: Optional[str] = None,
    lang: str = "ru",
) -> str:
    now = time.time()

    def _sec(seconds: float) -> str:
        return f"{int(seconds)}{t('session_status.sec', lang)}"

    def _ago(ts: Optional[float]) -> str:
        if not ts:
            return t("session_status.none", lang)
        return t("session_status.ago", lang, v=_sec(now - ts))

    title = title_prefix if title_prefix is not None else t("session_status.active_session", lang)
    busy_txt = t("session_status.busy", lang) if bool(getattr(session, "busy", False)) else t("session_status.free", lang)
    git_state = getattr(session, "git", None)
    git_busy = bool(getattr(git_state, "busy", getattr(session, "git_busy", False)))
    git_txt = t("session_status.git_busy", lang) if git_busy else t("session_status.git_free", lang)
    conflict_txt = ""
    git_conflict = bool(getattr(git_state, "conflict", getattr(session, "git_conflict", False)))
    if git_conflict:
        git_conflict_kind = getattr(git_state, "conflict_kind", getattr(session, "git_conflict_kind", None))
        conflict_txt = f" | {t('session_status.conflict', lang)}: {git_conflict_kind or t('session_status.conflict_yes', lang)}"
    started_at = getattr(session, "started_at", None)
    last_output_ts = getattr(session, "last_output_ts", None)
    last_tick_ts = getattr(session, "last_tick_ts", None)
    run_for = _sec(now - started_at) if started_at else t("session_status.none", lang)
    last_out = _ago(last_output_ts)
    tick_txt = _ago(last_tick_ts)
    active_mode = str(get_active_mode(session, "") or "").strip()
    mode_status = build_modes_status_line(active_mode, mode_registry, mode_items=mode_items, lang=lang)
    project_root = getattr(session, "project_root", None)
    lines = [
        f"{title}: {session.id} ({session.name or session.tool.name}) @ {session.workdir}",
        (
            f"{t('session_status.status', lang)}: {busy_txt} | {git_txt}{conflict_txt} | "
            f"{t('session_status.in_progress', lang)}: {run_for} | {mode_status}"
        ),
    ]
    if project_root:
        lines.append(f"{t('session_status.project', lang)}: {project_root}")
    lines.append(
        f"{t('session_status.last_output', lang)}: {last_out} | "
        f"{t('session_status.last_tick', lang)}: {tick_txt} | "
        f"{t('session_status.ticks', lang)}: {getattr(session, 'tick_seen', 0)}"
    )
    runtime_brief = _runtime_progress_brief(session)
    if runtime_brief:
        lines.append(f"Runtime: {runtime_brief}")
    queue_len = get_session_queue_len(session)
    resume_txt = t("session_status.yes", lang) if getattr(session, "resume_token", None) else t("session_status.no", lang)
    queue_line = f"{t('session_status.queue', lang)}: {queue_len} | {t('session_status.resume', lang)}: {resume_txt}"
    if show_orchestrator:
        orchestrator = (
            t("session_status.on", lang) if bool(is_orchestrator_enabled(session, False)) else t("session_status.off", lang)
        )
        queue_line = f"{queue_line} | {t('session_status.orchestrator', lang)}: {orchestrator}"
    lines.append(queue_line)
    ssh_hosts = load_ssh_config(session.workdir)
    if ssh_hosts:
        enabled = is_ssh_remote_enabled(session)
        if enabled:
            host_names = ", ".join(ssh_hosts.keys())
            lines.append(f"🔗 {t('session_status.ssh_on', lang)} ({host_names})")
        else:
            lines.append(f"🔗 {t('session_status.ssh_off', lang)}")
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
    lang: str = "ru",
) -> str:
    now = time.time()
    started_at = getattr(session, "started_at", None)
    last_output_ts = getattr(session, "last_output_ts", None)
    last_tick_ts = getattr(session, "last_tick_ts", None)
    _sec = t("session_status.sec", lang)
    _none = t("session_status.none", lang)
    run_for = f"{int(now - started_at)}{_sec}" if started_at else _none
    last_out = t("session_status.ago", lang, v=f"{int(now - last_output_ts)}{_sec}") if last_output_ts else _none
    tick_txt = t("session_status.ago", lang, v=f"{int(now - last_tick_ts)}{_sec}") if last_tick_ts else _none
    queue_len = get_session_queue_len(session)

    _enabled_lbl = t("session_status.enabled", lang) if enabled else t("session_status.disabled", lang)
    state_line = f"{t('session_status.mode_label', lang)}: {_enabled_lbl}"
    if task_suffix:
        state_line += f" | {task_suffix}"
    state_line += f" | {t('session_status.in_progress', lang)}: {run_for}"

    queue_line = f"{t('session_status.queue', lang)}: {queue_len}"
    if queue_suffix:
        queue_line += f" | {queue_suffix}"

    _last_out_line = (
        f"{t('session_status.last_output', lang)}: {last_out} | "
        f"{t('session_status.last_tick', lang)}: {tick_txt} | "
        f"{t('session_status.ticks', lang)}: {getattr(session, 'tick_seen', 0)}"
    )
    lines = [
        title,
        f"{t('session_status.session', lang)}: {session.id} ({session.name or session.tool.name}) @ {session.workdir}",
        f"{t('session_status.stage', lang)}: {stage}",
        state_line,
        _last_out_line,
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
