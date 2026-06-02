from __future__ import annotations

import logging
import os
from typing import Any, Callable, Dict, List, Optional

from modes.sdk.planning import (
    MANAGER_CONTINUE_TOKEN,
    PlanObserver,
    load_plan,
    register_plan_observer,
    save_plan,
    unregister_plan_observer,
)
from modes.sdk.runtime.contracts import ProjectPlan
from session import session_scoped_key

from .artifacts import parse_tasks_md, render_tasks_md
from .phases import parse_spec_requirements, render_trace_md
from .state import get_sdd_state, set_sdd_phase

_log = logging.getLogger(__name__)


def handoff_scoped_key(session: Any) -> str:
    return str(session_scoped_key(session) or "").strip()


def _write_trace_md(tasks_md_path: str, plan: ProjectPlan) -> None:
    """Generate trace.md (REQ→task→status→files→tests) next to tasks.md. Best-effort."""
    try:
        spec_dir = os.path.dirname(tasks_md_path)
        spec_path = os.path.join(spec_dir, "spec.md")
        requirements: List[Dict[str, str]] = []
        try:
            with open(spec_path, encoding="utf-8") as fh:
                requirements = parse_spec_requirements(fh.read())
        except OSError:
            _log.warning("_write_trace_md: spec.md not found at %r, requirements=[]", spec_path)
        trace = render_trace_md(plan, requirements)
        trace_path = os.path.join(spec_dir, "trace.md")
        with open(trace_path, "w", encoding="utf-8") as fh:
            fh.write(trace)
        _log.info("_write_trace_md: written %r", trace_path)
    except Exception:
        _log.exception("_write_trace_md: failed, trace.md not written")


def _write_tasks_md(tasks_md_path: str, plan: ProjectPlan) -> None:
    parent = os.path.dirname(tasks_md_path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(tasks_md_path, "w", encoding="utf-8") as fh:
        fh.write(render_tasks_md(plan))


def seed_plan_from_tasks_md(
    workdir: str,
    tasks_md_path: str,
    scoped_key: str,
) -> ProjectPlan:
    """Read tasks.md, parse into ProjectPlan, force status='active', persist via save_plan."""
    if not os.path.isfile(tasks_md_path):
        raise FileNotFoundError(
            f"seed_plan_from_tasks_md: tasks.md not found at {tasks_md_path!r}"
        )
    try:
        with open(tasks_md_path, encoding="utf-8") as fh:
            text = fh.read()
    except Exception:
        _log.exception("seed_plan_from_tasks_md: failed to read tasks.md path=%r", tasks_md_path)
        raise

    try:
        plan = parse_tasks_md(text)
    except Exception:
        _log.exception(
            "seed_plan_from_tasks_md: parse_tasks_md failed path=%r", tasks_md_path
        )
        raise
    if not str(getattr(plan, "project_goal", "") or "").strip():
        raise ValueError(f"seed_plan_from_tasks_md: project goal is missing in {tasks_md_path!r}")
    tasks = list(getattr(plan, "tasks", []) or [])
    if not tasks:
        raise ValueError(f"seed_plan_from_tasks_md: no tasks found in {tasks_md_path!r}")
    invalid_tasks = [
        str(getattr(task, "id", "") or "").strip() or "?"
        for task in tasks
        if not str(getattr(task, "id", "") or "").strip()
        or not str(getattr(task, "title", "") or "").strip()
    ]
    if invalid_tasks:
        raise ValueError(
            f"seed_plan_from_tasks_md: invalid task headers in {tasks_md_path!r}: {', '.join(invalid_tasks)}"
        )

    plan.status = "active"
    save_plan(workdir, plan, scoped_key or None)
    _log.info(
        "seed_plan_from_tasks_md: seeded plan workdir=%r scoped_key=%r tasks=%d",
        workdir,
        scoped_key,
        len(plan.tasks),
    )
    return plan


async def _notify_handoff_error(
    mode: Any,
    bot_app: Any,
    context: Any,
    dest: Optional[Dict[str, Any]],
    text: str,
    *,
    reply_markup: Any = None,
) -> None:
    """Best-effort user-facing notification when the handoff cannot proceed."""
    try:
        ms = mode._messaging(bot_app=bot_app, context=context)
        chat_id = int((dest or {}).get("chat_id") or 0)
        await ms.send_text(chat_id, text, md2=False, reply_markup=reply_markup)
    except Exception:
        _log.exception("run_handoff_to_manager: failed to notify user of handoff error")


async def _notify_handoff_done(
    mode: Any,
    bot_app: Any,
    context: Any,
    dest: Optional[Dict[str, Any]],
    tasks_md_path: str,
    plan: Optional[ProjectPlan],
) -> None:
    """Notify user that Manager finished, surfacing any unresolved failed/blocked tasks."""
    try:
        ms = mode._messaging(bot_app=bot_app, context=context)
        chat_id = int((dest or {}).get("chat_id") or 0)
        lines: List[str] = ["✅ Менеджер завершил работу по tasks.md.", "", f"Файл: {tasks_md_path}"]
        if plan is not None:
            tasks = list(getattr(plan, "tasks", []) or [])
            unresolved = [t for t in tasks if str(getattr(t, "status", "")) in ("failed", "blocked")]
            if unresolved:
                # Не маскируем нерешённые задачи — показываем их явно.
                lines.append("")
                lines.append("⚠️ Остались нерешённые задачи:")
                for t in unresolved:
                    tid = str(getattr(t, "id", "?"))
                    title = str(getattr(t, "title", ""))
                    st = str(getattr(t, "status", ""))
                    lines.append(f"• {tid} [{st}]: {title}")
        await ms.send_text(chat_id, "\n".join(lines), md2=False)
    except Exception:
        _log.exception("run_handoff_to_manager: failed to notify user of completion")


def make_writeback_observer(
    tasks_md_path: str,
    *,
    on_plan: Optional[Callable[[ProjectPlan], None]] = None,
) -> PlanObserver:
    def _observer(plan: ProjectPlan) -> None:
        _write_tasks_md(tasks_md_path, plan)
        if on_plan is not None:
            on_plan(plan)

    return _observer


async def run_handoff_to_manager(
    *,
    mode: Any,
    session: Any,
    bot_app: Any,
    context: Any,
    dest: Optional[Dict[str, Any]],
    tasks_md_path: str,
    restore_gate_on_failure: Optional[Callable[[], None]] = None,
    restore_gate_reply_markup: Any = None,
) -> None:
    workdir = str(getattr(session, "workdir", "") or "")
    scoped_key = handoff_scoped_key(session)

    _log.info(
        "run_handoff_to_manager: start workdir=%r scoped_key=%r tasks_md=%r",
        workdir,
        scoped_key,
        tasks_md_path,
    )

    try:
        seeded = seed_plan_from_tasks_md(workdir, tasks_md_path, scoped_key)
    except Exception:
        _log.exception("run_handoff_to_manager: seeding failed tasks_md=%r", tasks_md_path)
        if restore_gate_on_failure is not None:
            try:
                restore_gate_on_failure()
            except Exception:
                _log.exception("run_handoff_to_manager: failed to restore SDD gate")
        await _notify_handoff_error(
            mode, bot_app, context, dest,
            "❌ Не удалось подготовить план для Менеджера (tasks.md). Проверьте логи.\n\n"
            "Гейт tasks восстановлен.",
            reply_markup=restore_gate_reply_markup,
        )
        return
    last: Dict[str, Any] = {"plan": seeded}

    observer = make_writeback_observer(
        tasks_md_path,
        on_plan=lambda p: last.__setitem__("plan", p),
    )
    register_plan_observer(workdir, scoped_key or None, observer)
    _log.info(
        "run_handoff_to_manager: observer registered scoped_key=%r", scoped_key
    )

    manager_ok = False
    try:
        await mode._pipeline().run_mode_pipeline(
            session,
            MANAGER_CONTINUE_TOKEN,
            dict(dest or {}),
            context,
            mode_id="manager",
        )
        manager_ok = True
        _log.info("run_handoff_to_manager: manager pipeline returned scoped_key=%r", scoped_key)
    except Exception:
        if restore_gate_on_failure is not None:
            try:
                restore_gate_on_failure()
            except Exception:
                _log.exception("run_handoff_to_manager: failed to restore SDD gate")
        await _notify_handoff_error(
            mode, bot_app, context, dest,
            "❌ Не удалось передать задачи Менеджеру. Проверьте логи.\n\n"
            "Гейт tasks восстановлен.",
            reply_markup=restore_gate_reply_markup,
        )
        raise
    finally:
        final_plan = load_plan(workdir, scoped_key or None) or last["plan"]
        if final_plan is not None:
            _write_tasks_md(tasks_md_path, final_plan)
            _write_trace_md(tasks_md_path, final_plan)
            _log.info(
                "run_handoff_to_manager: final tasks.md written scoped_key=%r status=%r",
                scoped_key,
                final_plan.status,
            )
        unregister_plan_observer(workdir, scoped_key or None)
        _log.info("run_handoff_to_manager: observer unregistered scoped_key=%r", scoped_key)

    # Менеджер отработал — закрываем SDD-поток: фаза done, сброс гейта, уведомление.
    # На исключении из пайплайна сюда не доходим (оно пробрасывается после finally),
    # поэтому фаза остаётся "handoff" и ошибка не маскируется.
    if manager_ok:
        set_sdd_phase(session, "done")
        sdd = get_sdd_state(session)
        sdd.pending_gate = None
        sdd.last_action = ""
        mode._persist_sessions(bot_app)
        await _notify_handoff_done(mode, bot_app, context, dest, tasks_md_path, final_plan)
