from __future__ import annotations

import logging
from typing import Any, Dict, List

from app.events.bus import ManageTasksChangedEvent


_log = logging.getLogger(__name__)

CLOSED_STATUSES = {"completed", "cancelled"}
MAX_RENDERED_TASKS = 20
MAX_TASK_CONTENT = 160


class ManageTasksProgressBridge:
    """Render manage_tasks progress outside the tool implementation."""

    def __init__(self) -> None:
        self._telegram_message_ids: Dict[str, int] = {}

    async def sync(self, *, tool_name: str, result: Dict[str, Any], ctx: Dict[str, Any]) -> None:
        if str(tool_name or "") != "manage_tasks":
            return
        if not bool(result.get("success")):
            return
        payload = result.get("manage_tasks")
        if not isinstance(payload, dict) or not bool(payload.get("changed")):
            return

        bot = ctx.get("bot")
        bus = getattr(bot, "system_event_bus", None)
        if bus is None or not callable(getattr(bus, "publish", None)):
            return

        event = ManageTasksChangedEvent(
            session_uid=_session_uid(ctx),
            chat_id=ctx.get("chat_id") or "",
            scope_key=_message_key(ctx),
            run_id=str(ctx.get("run_id") or ""),
            correlation_id=str(ctx.get("corr_id") or ""),
            action=str(payload.get("action") or ""),
            tasks=payload.get("tasks") if isinstance(payload.get("tasks"), list) else [],
            progress=payload.get("progress") if isinstance(payload.get("progress"), dict) else {},
        )
        unsubscribe = None
        subscribe = getattr(bus, "subscribe", None)
        if callable(subscribe):
            unsubscribe = subscribe(ManageTasksChangedEvent, lambda event_: self._render_event(event_, ctx))
        try:
            await bus.publish(event)
        except Exception:
            _log.exception("manage_tasks event publish failed scope=%s", event.scope_key)
        finally:
            if callable(unsubscribe):
                unsubscribe()

    async def _render_event(self, event: ManageTasksChangedEvent, ctx: Dict[str, Any]) -> None:
        bot = ctx.get("bot")
        notify = getattr(bot, "notify", None)
        if callable(notify):
            if _has_open_tasks(event.tasks):
                self._show_desktop(notify, ctx, event)
            else:
                self._clear_desktop(notify, ctx)
            return

        text = render_manage_tasks_progress(event.tasks)
        if text:
            await self._show_telegram(bot, ctx, text)
        else:
            await self._clear_telegram(bot, ctx)

    @staticmethod
    def _show_desktop(notify: Any, ctx: Dict[str, Any], event: ManageTasksChangedEvent) -> None:
        session_uid = _session_uid(ctx)
        if not session_uid:
            return
        try:
            notify(
                "ui:manage_tasks_progress",
                session_id=session_uid,
                scope_key=event.scope_key,
                run_id=event.run_id,
                action=event.action,
                tasks=list(event.tasks),
                progress=dict(event.progress),
            )
        except Exception:
            _log.exception("manage_tasks desktop progress notify failed session=%s", session_uid)

    @staticmethod
    def _clear_desktop(notify: Any, ctx: Dict[str, Any]) -> None:
        session_uid = _session_uid(ctx)
        if not session_uid:
            return
        try:
            notify("ui:manage_tasks_progress_clear", session_id=session_uid)
        except Exception:
            _log.exception("manage_tasks desktop progress clear failed session=%s", session_uid)

    async def _show_telegram(self, bot: Any, ctx: Dict[str, Any], text: str) -> None:
        context = ctx.get("context")
        chat_id = ctx.get("chat_id")
        if context is None or chat_id is None:
            return
        key = _message_key(ctx)
        message_id = self._telegram_message_ids.get(key)
        edit = getattr(bot, "_edit_message", None)
        if message_id and callable(edit):
            try:
                edited = await edit(context, chat_id=chat_id, message_id=message_id, text=text, md2=True)
                if edited:
                    return
            except Exception:
                _log.exception("manage_tasks telegram progress edit failed key=%s", key)

        send = getattr(bot, "_send_message", None)
        if not callable(send):
            return
        try:
            message = await send(context, chat_id=chat_id, text=text, md2=True)
            new_message_id = getattr(message, "message_id", None)
            if new_message_id:
                self._telegram_message_ids[key] = int(new_message_id)
        except Exception:
            _log.exception("manage_tasks telegram progress send failed key=%s", key)

    async def _clear_telegram(self, bot: Any, ctx: Dict[str, Any]) -> None:
        context = ctx.get("context")
        chat_id = ctx.get("chat_id")
        if context is None or chat_id is None:
            return
        key = _message_key(ctx)
        message_id = self._telegram_message_ids.pop(key, None)
        if not message_id:
            return
        delete = getattr(bot, "_delete_message", None)
        if not callable(delete):
            return
        try:
            await delete(context, chat_id=chat_id, message_id=message_id)
        except Exception:
            _log.exception("manage_tasks telegram progress delete failed key=%s", key)


def render_manage_tasks_progress(tasks: List[Dict[str, Any]]) -> str:
    normalized = [task for task in tasks if isinstance(task, dict)]
    if not normalized:
        return ""

    open_count = sum(1 for task in normalized if str(task.get("status") or "pending") not in CLOSED_STATUSES)
    if open_count <= 0:
        return ""

    closed_count = sum(1 for task in normalized if str(task.get("status") or "") in CLOSED_STATUSES)
    total = len(normalized)
    lines = [
        "План выполнения",
        f"Закрыто: {closed_count}/{total}",
        "",
    ]
    rendered = normalized[:MAX_RENDERED_TASKS]
    for task in rendered:
        status = str(task.get("status") or "pending")
        task_id = str(task.get("id") or "").strip()
        content = _compact_task_content(str(task.get("content") or ""))
        prefix = _status_prefix(status)
        label = f"{task_id}: {content}" if task_id else content
        lines.append(f"{prefix} {label}".rstrip())
    remaining = total - len(rendered)
    if remaining > 0:
        lines.append(f"... еще {remaining}")
    return "\n".join(lines).strip()


def _has_open_tasks(tasks: List[Dict[str, Any]]) -> bool:
    return any(
        isinstance(task, dict) and str(task.get("status") or "pending") not in CLOSED_STATUSES
        for task in tasks
    )


def _status_prefix(status: str) -> str:
    if status == "completed":
        return "[x]"
    if status == "in_progress":
        return "[~]"
    if status == "cancelled":
        return "[-]"
    return "[ ]"


def _compact_task_content(text: str) -> str:
    compact = " ".join(str(text or "").split())
    if len(compact) <= MAX_TASK_CONTENT:
        return compact
    return compact[: MAX_TASK_CONTENT - 3].rstrip() + "..."


def _message_key(ctx: Dict[str, Any]) -> str:
    return str(
        ctx.get("manage_tasks_scope_key")
        or ctx.get("run_id")
        or ctx.get("task_id")
        or ctx.get("session_scoped_key")
        or ctx.get("session_id")
        or ctx.get("chat_id")
        or "default"
    )


def _session_uid(ctx: Dict[str, Any]) -> str:
    return str(ctx.get("chat_id") or ctx.get("session_scoped_key") or ctx.get("session_id") or "").strip()
