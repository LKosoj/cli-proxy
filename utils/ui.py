from __future__ import annotations

import inspect
import os


def _direct_attr(obj, name: str):
    if obj is None:
        return None
    data = getattr(obj, "__dict__", None)
    if isinstance(data, dict) and name in data:
        return data.get(name)
    return getattr(obj, name, None)


def _has_attr(obj, name: str) -> bool:
    if obj is None:
        return False
    data = getattr(obj, "__dict__", None)
    if isinstance(data, dict) and name in data:
        return True
    return hasattr(obj, name)


def _clean_text(value) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _resolve_session_title_prefix(session, explicit_prefix: str | None) -> str:
    if explicit_prefix is not None:
        return _clean_text(explicit_prefix)
    config = _direct_attr(session, "config")
    if not _has_attr(config, "thread_mode"):
        return "cli"
    thread_mode = _direct_attr(config, "thread_mode")
    if not _has_attr(thread_mode, "topic_title_prefix"):
        return "cli"
    prefix = _direct_attr(thread_mode, "topic_title_prefix")
    return _clean_text(prefix)


def _resolve_session_title_label(session) -> str:
    label = _clean_text(_direct_attr(session, "name"))
    if label:
        return label
    workdir = _clean_text(_direct_attr(session, "workdir"))
    if workdir:
        workdir_label = os.path.basename(os.path.normpath(workdir))
        if workdir_label:
            return workdir_label
        return workdir
    tool = _direct_attr(session, "tool")
    tool_name = _clean_text(_direct_attr(tool, "name"))
    return tool_name or "session"


def format_session_title(
    session,
    *,
    topic_title_prefix: str | None = None,
    max_length: int | None = None,
) -> str:
    prefix = _resolve_session_title_prefix(session, topic_title_prefix)
    session_id = _clean_text(_direct_attr(session, "id"))
    label = _resolve_session_title_label(session)
    parts = [part for part in (prefix, session_id, label) if part]
    title = " | ".join(parts).strip() or session_id or label or "session"
    if max_length is not None and max_length > 0 and len(title) > max_length:
        return title[: max_length - 1].rstrip() + "…"
    return title


def format_session_label(session) -> str:
    return format_session_title(session)


def format_session_selector_label(session, *, telegram_user_id: int | None = None) -> str:
    label = format_session_title(session)
    if telegram_user_id is None:
        return label
    return f"{label} | tg:{int(telegram_user_id)}"


def status_dot(enabled: bool) -> str:
    return "🟢" if enabled else "🔴"


def ensure_async(coro, parent=None):
    import asyncio
    import logging

    logger = logging.getLogger(__name__)

    def _close_unscheduled_coroutine() -> None:
        if inspect.iscoroutine(coro):
            try:
                coro.close()
            except Exception:
                logger.exception("ensure_async: failed to close unscheduled coroutine")

    try:
        loop = None
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            try:
                loop = asyncio.get_event_loop()
            except RuntimeError:
                loop = None

        if not loop:
            logger.error("ensure_async: no event loop available; coroutine will not be scheduled")
            _close_unscheduled_coroutine()
            return None
        if hasattr(loop, "is_running") and not loop.is_running():
            logger.error("ensure_async: event loop is not running; coroutine will not be scheduled")
            _close_unscheduled_coroutine()
            return None

        task = loop.create_task(coro)
        if parent and hasattr(parent, "_background_tasks"):
            if not isinstance(parent._background_tasks, set):
                parent._background_tasks = set()
            parent._background_tasks.add(task)

        def _on_done(done_task):
            if parent and hasattr(parent, "_background_tasks"):
                parent._background_tasks.discard(done_task)
            try:
                done_task.result()
            except asyncio.CancelledError:
                return
            except Exception:
                logger.exception("ensure_async: background task failed")

        task.add_done_callback(_on_done)
        return task
    except Exception as exc:
        _close_unscheduled_coroutine()
        logger.error(f"Critical error in ensure_async: {exc}")
        return None
