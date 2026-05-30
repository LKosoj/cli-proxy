from __future__ import annotations

import logging
from typing import Any, Dict, List


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

        tasks: List[Dict[str, Any]] = payload.get("tasks") if isinstance(payload.get("tasks"), list) else []
        progress: Dict[str, Any] = payload.get("progress") if isinstance(payload.get("progress"), dict) else {}
        action: str = str(payload.get("action") or "")
        scope_key: str = _message_key(ctx)
        run_id: str = str(ctx.get("run_id") or "")

        await self._render(
            tasks=tasks,
            progress=progress,
            action=action,
            scope_key=scope_key,
            run_id=run_id,
            ctx=ctx,
        )

    async def _render(
        self,
        *,
        tasks: List[Dict[str, Any]],
        progress: Dict[str, Any],
        action: str,
        scope_key: str,
        run_id: str,
        ctx: Dict[str, Any],
    ) -> None:
        bot = ctx.get("bot")
        notify = getattr(bot, "notify", None)
        if callable(notify):
            if _has_open_tasks(tasks):
                self._show_desktop(notify, ctx, tasks=tasks, progress=progress, action=action, scope_key=scope_key, run_id=run_id)
            else:
                self._clear_desktop(notify, ctx)
            return

        text = render_manage_tasks_progress(tasks)
        if text:
            await self._show_telegram(bot, ctx, text)
        else:
            await self._clear_telegram(bot, ctx)

    @staticmethod
    def _show_desktop(
        notify: Any,
        ctx: Dict[str, Any],
        *,
        tasks: List[Dict[str, Any]],
        progress: Dict[str, Any],
        action: str,
        scope_key: str,
        run_id: str,
    ) -> None:
        session_uid = _session_uid(ctx)
        if not session_uid:
            return
        try:
            notify(
                "ui:manage_tasks_progress",
                session_id=session_uid,
                scope_key=scope_key,
                run_id=run_id,
                action=action,
                tasks=list(tasks),
                progress=dict(progress),
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
