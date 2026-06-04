from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from i18n import t


CLOSED_STATUSES = {"completed", "cancelled"}
MAX_RENDERED_TASKS = 20
MAX_TASK_CONTENT = 160


def format_manage_tasks_progress(tasks: Sequence[Mapping[str, Any]], lang: str = "ru") -> str:
    normalized = [task for task in tasks if isinstance(task, Mapping)]
    if not normalized:
        return ""
    if not any(str(task.get("status") or "pending") not in CLOSED_STATUSES for task in normalized):
        return ""

    closed_count = sum(1 for task in normalized if str(task.get("status") or "") in CLOSED_STATUSES)
    total = len(normalized)
    lines = [
        t("desktop.managetasks.header", lang),
        t("desktop.managetasks.closed_count", lang, closed=closed_count, total=total),
        "",
    ]
    for task in normalized[:MAX_RENDERED_TASKS]:
        status = str(task.get("status") or "pending")
        task_id = str(task.get("id") or "").strip()
        content = _compact_task_content(str(task.get("content") or ""))
        label = f"{task_id}: {content}" if task_id else content
        lines.append(f"{_status_prefix(status)} {label}".rstrip())
    remaining = total - min(total, MAX_RENDERED_TASKS)
    if remaining > 0:
        lines.append(t("desktop.managetasks.remaining", lang, count=remaining))
    return "\n".join(lines).strip()


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
