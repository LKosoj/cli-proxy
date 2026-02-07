from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from telegram import InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from agent.plugins.base import DialogMixin, ToolPlugin
from agent.tooling.spec import ToolSpec


def _now_ts() -> float:
    return time.time()


def _shared_root() -> str:
    sandbox_root = os.getenv("AGENT_SANDBOX_ROOT")
    if sandbox_root:
        return os.path.join(sandbox_root, "_shared")
    # Fallback: still keep data under cwd to avoid writing to global FS.
    return os.path.join(os.getcwd(), "_sandbox", "_shared")


def _tasks_path() -> str:
    return os.path.join(_shared_root(), "tasks.json")


def _ensure_storage() -> None:
    os.makedirs(_shared_root(), exist_ok=True)


def _load_all_tasks() -> Dict[str, Dict[str, Any]]:
    _ensure_storage()
    path = _tasks_path()
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception as e:
        logging.exception(f"tool failed {str(e)}")
        return {}


def _save_all_tasks(data: Dict[str, Dict[str, Any]]) -> None:
    _ensure_storage()
    path = _tasks_path()
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def _parse_deadline(deadline: Optional[str]) -> Tuple[Optional[int], Optional[str]]:
    if not deadline:
        return None, None
    s = str(deadline).strip()
    if not s:
        return None, None
    try:
        dt = datetime.strptime(s, "%Y-%m-%d %H:%M")
        return int(dt.timestamp()), None
    except Exception:
        return None, "Неверный формат дедлайна. Нужно YYYY-MM-DD HH:MM"


def _format_task_line(task: Dict[str, Any]) -> str:
    tid = task.get("id", "")
    title = (task.get("title") or "").strip()
    status = (task.get("status") or "pending").replace("_", " ")
    priority = (task.get("priority") or "low").upper()
    deadline = task.get("deadline") or ""
    bits = [f"[{priority}] {title}", f"({status})"]
    if deadline:
        bits.append(f"до {deadline}")
    bits.append(f"id={tid}")
    return " ".join([b for b in bits if b])


def _human_status(status: str) -> str:
    m = {
        "pending": "ожидает",
        "in_progress": "в работе",
        "completed": "готово",
        "cancelled": "отменено",
    }
    return m.get(status, status)


def _next_status(current: str) -> str:
    order = ["pending", "in_progress", "completed"]
    try:
        i = order.index(current)
        return order[(i + 1) % len(order)]
    except Exception:
        return "pending"


@dataclass
class _NotifyPolicy:
    check_interval_sec: int = 60
    due_soon_window_sec: int = 10 * 60
    overdue_repeat_sec: int = 60 * 60


class TaskManagementTool(DialogMixin, ToolPlugin):
    """
    User-level task manager with Telegram UI and a periodic deadline checker (implemented in bot.py).
    Storage: AGENT_SANDBOX_ROOT/_shared/tasks.json
    """

    _policy = _NotifyPolicy()

    def dialog_steps(self):
        return {"wait_text": self._on_add_text}

    def get_source_name(self) -> str:
        return "TaskManagement"

    def get_spec(self) -> ToolSpec:
        return ToolSpec(
            name="task_management",
            description="Задачи: создать/список/обновить/удалить. Поддерживает приоритет, статус, дедлайн, теги.",
            parameters={
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["create", "list", "update", "delete"]},
                    "task_id": {"type": "string", "description": "ID задачи (update/delete)"},
                    "title": {"type": "string", "description": "Заголовок (create)"},
                    "description": {"type": "string", "description": "Описание (create/update)"},
                    "priority": {"type": "string", "enum": ["high", "medium", "low"]},
                    "status": {"type": "string", "enum": ["pending", "in_progress", "completed", "cancelled"]},
                    "deadline": {"type": "string", "description": "YYYY-MM-DD HH:MM"},
                    "tags": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["action"],
            },
            parallelizable=False,
            timeout_ms=30_000,
        )

    def get_menu_label(self):
        return "Задачи"

    def get_menu_actions(self):
        return [
            {"label": "Список", "action": "refresh"},
            {"label": "Добавить", "action": "add"},
        ]

    def get_commands(self) -> List[Dict[str, Any]]:
        return self._dialog_callback_commands()

    def callback_handlers(self) -> Dict[str, Any]:
        return {
            "add": self._cb_start_add,
            "refresh": self._cb_refresh,
            "view": self._cb_view,
            "view_help": self._cb_view_help,
            "del": self._cb_del,
            "next": self._cb_next,
        }

    # get_message_handlers is provided by DialogMixin.

    async def execute(self, args: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
        action = args.get("action")
        chat_id = int(ctx.get("chat_id") or 0)
        user_id = int(ctx.get("chat_id") or 0)
        # In cli-proxy, chat_id is a stable user chat in most cases; keep user scoping by chat_id.
        # If needed later, we can separate effective_user.id.
        if action == "create":
            title = (args.get("title") or "").strip()
            priority = (args.get("priority") or "").strip() or "low"
            description = (args.get("description") or "").strip()
            deadline = (args.get("deadline") or "").strip() or None
            tags = args.get("tags") or []
            if not title:
                return {"success": False, "error": "title обязателен"}
            if priority not in ("high", "medium", "low"):
                return {"success": False, "error": "priority должен быть high|medium|low"}
            dl_ts, dl_err = _parse_deadline(deadline)
            if dl_err:
                return {"success": False, "error": dl_err}
            task_id = f"tsk_{int(_now_ts())}_{uuid.uuid4().hex[:4]}"
            task = {
                "id": task_id,
                "user_id": str(user_id),
                "chat_id": chat_id,
                "title": title,
                "description": description,
                "priority": priority,
                "status": "pending",
                "created_at": datetime.now().isoformat(),
                "deadline": deadline,
                "deadline_ts": dl_ts,
                "tags": [str(t) for t in tags if str(t).strip()],
                "last_updated": datetime.now().isoformat(),
                "notify": {},
            }
            all_tasks = _load_all_tasks()
            bucket = all_tasks.setdefault(str(user_id), {})
            bucket[task_id] = task
            _save_all_tasks(all_tasks)
            return {"success": True, "output": f"✅ Создано: {_format_task_line(task)}"}

        if action == "list":
            all_tasks = _load_all_tasks()
            bucket = all_tasks.get(str(user_id), {})
            items = list(bucket.values()) if isinstance(bucket, dict) else []
            if not items:
                return {"success": True, "output": "Задач нет."}
            # Sort: deadline first, then priority.
            prio_rank = {"high": 0, "medium": 1, "low": 2}

            def _key(t: Dict[str, Any]):
                dl = t.get("deadline_ts")
                dl = dl if isinstance(dl, int) else 2**31
                pr = prio_rank.get(t.get("priority"), 9)
                st = t.get("status") or "pending"
                st_rank = {"pending": 0, "in_progress": 1, "completed": 9, "cancelled": 10}.get(st, 5)
                return (st_rank, dl, pr)
            items.sort(key=_key)
            lines = ["Задачи:"]
            for t in items[:50]:
                lines.append(f"• {_format_task_line(t)}")
            return {"success": True, "output": "\n".join(lines)}

        if action == "update":
            task_id = (args.get("task_id") or "").strip()
            if not task_id:
                return {"success": False, "error": "task_id обязателен"}
            all_tasks = _load_all_tasks()
            bucket = all_tasks.get(str(user_id), {})
            if not isinstance(bucket, dict) or task_id not in bucket:
                return {"success": False, "error": "Задача не найдена"}
            task = bucket[task_id]
            if "status" in args and args["status"]:
                task["status"] = args["status"]
            if "priority" in args and args["priority"]:
                task["priority"] = args["priority"]
            if "description" in args and args["description"] is not None:
                task["description"] = str(args["description"])
            if "deadline" in args:
                deadline = (args.get("deadline") or "").strip() or None
                dl_ts, dl_err = _parse_deadline(deadline)
                if dl_err:
                    return {"success": False, "error": dl_err}
                task["deadline"] = deadline
                task["deadline_ts"] = dl_ts
                # Reset notify state when deadline changes.
                task["notify"] = {}
            task["last_updated"] = datetime.now().isoformat()
            bucket[task_id] = task
            all_tasks[str(user_id)] = bucket
            _save_all_tasks(all_tasks)
            return {"success": True, "output": f"✅ Обновлено: {_format_task_line(task)}"}

        if action == "delete":
            task_id = (args.get("task_id") or "").strip()
            if not task_id:
                return {"success": False, "error": "task_id обязателен"}
            all_tasks = _load_all_tasks()
            bucket = all_tasks.get(str(user_id), {})
            if not isinstance(bucket, dict) or task_id not in bucket:
                return {"success": False, "error": "Задача не найдена"}
            task = bucket.pop(task_id)
            all_tasks[str(user_id)] = bucket
            _save_all_tasks(all_tasks)
            title = (task.get("title") or "").strip() or task_id
            return {"success": True, "output": f"🗑️ Удалено: {title} ({task_id})"}

        return {"success": False, "error": f"Unknown action: {action}"}

    def _build_tasks_menu(self, user_id: int) -> Tuple[str, Optional[InlineKeyboardMarkup]]:
        all_tasks = _load_all_tasks()
        bucket = all_tasks.get(str(user_id), {})
        items = list(bucket.values()) if isinstance(bucket, dict) else []
        if not items:
            rows = [
                [self.action_button("Добавить", "add")],
            ]
            text = "Задач нет.\n\nНажмите «Добавить», чтобы создать задачу."
            return text, InlineKeyboardMarkup(rows)
        # show up to 12 tasks in menu
        items = items[:12]
        rows = []
        rows.append([self.action_button("Добавить", "add")])
        for t in items:
            tid = t.get("id")
            title = (t.get("title") or "").strip()[:30]
            st = t.get("status") or "pending"
            pr = (t.get("priority") or "low").upper()
            label = f"[{pr}] {title} ({_human_status(st)})"
            rows.append(
                [
                    self.action_button("Статус", "next", tid),
                    self.action_button("Удалить", "del", tid),
                ]
            )
            rows.append([self.action_button(label, "view", tid)])
        rows.append([self.action_button("Обновить", "refresh")])
        return "Задачи:", InlineKeyboardMarkup(rows)

    async def _cb_start_add(self, update: Update, context: ContextTypes.DEFAULT_TYPE, payload: str) -> None:
        """Autonomous callback: start the add-task dialog from a button."""
        query = update.callback_query
        user_id = query.from_user.id if query and query.from_user else None
        chat_id = query.message.chat_id if query and query.message else None
        if not user_id or not chat_id:
            return
        self.start_dialog(chat_id, "wait_text", data={"mode": "add"}, user_id=user_id)
        if query and query.message:
            await query.message.reply_text(
                "Добавление задачи.\n"
                "Отправьте строку в одном из форматов:\n"
                "1) high Сделать важное\n"
                "2) medium 2026-02-06 10:00 Созвон\n"
                "3) low 2026-02-06 10:00 Купить молоко\n\n"
                "Кнопка Отмена или текст: отмена, cancel, выход, -",
                reply_markup=self.cancel_markup(),
            )

    def _parse_add_input(self, text: str) -> Tuple[Optional[str], Optional[str], Optional[str], Optional[str]]:
        raw = (text or "").strip()
        if not raw:
            return None, None, None, "Пустой ввод."
        parts = raw.split()
        if len(parts) < 2:
            return None, None, None, "Нужно минимум: <priority> <title>."
        priority = parts[0].lower()
        if priority not in ("high", "medium", "low"):
            return None, None, None, "priority должен быть high|medium|low."
        deadline = None
        title_parts = parts[1:]
        # Optional deadline: YYYY-MM-DD HH:MM
        if len(title_parts) >= 2 and len(title_parts[0]) == 10 and ":" in title_parts[1]:
            deadline = f"{title_parts[0]} {title_parts[1]}"
            title_parts = title_parts[2:]
        title = " ".join(title_parts).strip()
        if not title:
            return None, None, None, "title обязателен."
        dl_ts, dl_err = _parse_deadline(deadline)
        if dl_err:
            return None, None, None, dl_err
        return priority, deadline, title, None

    async def _on_add_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Step handler for wait_text: parse task input and save.

        Cancel words are already handled by DialogMixin.handle_message.
        """
        msg = update.effective_message
        if not msg:
            return
        user_id = update.effective_user.id if update.effective_user else None
        chat_id = update.effective_chat.id if update.effective_chat else None
        if not user_id or not chat_id:
            return
        priority, deadline, title, err = self._parse_add_input(msg.text or "")
        if err:
            await msg.reply_text(err)
            return

        dl_ts, _ = _parse_deadline(deadline)
        task_id = f"tsk_{int(_now_ts())}_{uuid.uuid4().hex[:4]}"
        task = {
            "id": task_id,
            "user_id": str(user_id),
            "chat_id": chat_id,
            "title": title,
            "description": "",
            "priority": priority,
            "status": "pending",
            "created_at": datetime.now().isoformat(),
            "deadline": deadline,
            "deadline_ts": dl_ts,
            "tags": [],
            "last_updated": datetime.now().isoformat(),
            "notify": {},
        }
        all_tasks = _load_all_tasks()
        bucket = all_tasks.setdefault(str(user_id), {})
        bucket[task_id] = task
        _save_all_tasks(all_tasks)
        self.end_dialog(chat_id)
        await msg.reply_text(f"✅ Добавлено: {_format_task_line(task)}\nОткройте /tasks чтобы увидеть меню.")
        return

    # -- autonomous callback handlers (routed by DialogMixin._dispatch_callback) --

    async def _cb_refresh(self, update: Update, context: ContextTypes.DEFAULT_TYPE, payload: str) -> None:
        query = update.callback_query
        user_id = query.from_user.id if query and query.from_user else None
        if not user_id or not query:
            return
        text, markup = self._build_tasks_menu(int(user_id))
        await query.edit_message_text(text, reply_markup=markup)

    async def _cb_view_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE, payload: str) -> None:
        query = update.callback_query
        if query:
            await query.answer("Нажмите кнопку «Добавить» для создания задачи.", show_alert=True)

    async def _cb_view(self, update: Update, context: ContextTypes.DEFAULT_TYPE, payload: str) -> None:
        query = update.callback_query
        user_id = query.from_user.id if query and query.from_user else None
        if not user_id or not query:
            return
        tid = payload
        all_tasks = _load_all_tasks()
        bucket = all_tasks.get(str(user_id), {})
        task = bucket.get(tid) if isinstance(bucket, dict) else None
        if not task:
            await query.answer("Не найдено", show_alert=True)
            return
        lines = [
            f"ID: {task.get('id')}",
            f"Заголовок: {task.get('title')}",
            f"Статус: {_human_status(task.get('status') or 'pending')}",
            f"Приоритет: {(task.get('priority') or 'low').upper()}",
        ]
        if task.get("deadline"):
            lines.append(f"Дедлайн: {task.get('deadline')}")
        if task.get("description"):
            lines.append(f"Описание: {task.get('description')}")
        await query.answer("\n".join(lines)[:200], show_alert=True, cache_time=0)

    async def _cb_del(self, update: Update, context: ContextTypes.DEFAULT_TYPE, payload: str) -> None:
        query = update.callback_query
        user_id = query.from_user.id if query and query.from_user else None
        if not user_id or not query:
            return
        tid = payload
        all_tasks = _load_all_tasks()
        bucket = all_tasks.get(str(user_id), {})
        if isinstance(bucket, dict) and tid in bucket:
            bucket.pop(tid, None)
            all_tasks[str(user_id)] = bucket
            _save_all_tasks(all_tasks)
        text, markup = self._build_tasks_menu(int(user_id))
        await query.edit_message_text(text, reply_markup=markup)

    async def _cb_next(self, update: Update, context: ContextTypes.DEFAULT_TYPE, payload: str) -> None:
        query = update.callback_query
        user_id = query.from_user.id if query and query.from_user else None
        if not user_id or not query:
            return
        tid = payload
        all_tasks = _load_all_tasks()
        bucket = all_tasks.get(str(user_id), {})
        task = bucket.get(tid) if isinstance(bucket, dict) else None
        if not task:
            await query.answer("Не найдено", show_alert=True)
            return
        cur = task.get("status") or "pending"
        task["status"] = _next_status(cur)
        task["last_updated"] = datetime.now().isoformat()
        # Reset overdue notify when completing/uncompleting.
        if task["status"] in ("completed", "cancelled"):
            task.setdefault("notify", {})["overdue_sent_at"] = None
        bucket[tid] = task
        all_tasks[str(user_id)] = bucket
        _save_all_tasks(all_tasks)
        text, markup = self._build_tasks_menu(int(user_id))
        await query.edit_message_text(text, reply_markup=markup)


async def run_task_deadline_checker(application: Any, is_allowed_cb) -> None:
    """
    Periodic checker for task deadlines.
    - Sends "due soon" once when deadline enters the window.
    - Sends "overdue" at most once per hour while task is overdue.
    """
    policy = TaskManagementTool._policy
    while True:
        try:
            all_tasks = _load_all_tasks()
            now = int(_now_ts())
            dirty = False
            for _user_id, bucket in list(all_tasks.items()):
                if not isinstance(bucket, dict):
                    continue
                for tid, task in list(bucket.items()):
                    try:
                        status = task.get("status") or "pending"
                        if status in ("completed", "cancelled"):
                            continue
                        chat_id = int(task.get("chat_id") or 0)
                        if not chat_id or (is_allowed_cb and not is_allowed_cb(chat_id)):
                            continue
                        dl_ts = task.get("deadline_ts")
                        if not isinstance(dl_ts, int):
                            continue
                        notify = task.setdefault("notify", {})
                        if dl_ts > now and dl_ts - now <= policy.due_soon_window_sec:
                            if not notify.get("due_soon_sent_at"):
                                text = f"⏳ Скоро дедлайн: {task.get('title', '(без названия)')}\nДедлайн: {task.get('deadline')}\nID: {tid}"
                                await application.bot.send_message(chat_id=chat_id, text=text)
                                notify["due_soon_sent_at"] = now
                                dirty = True
                        if dl_ts <= now:
                            last = notify.get("overdue_sent_at") or 0
                            if now - int(last) >= policy.overdue_repeat_sec:
                                text = f"⚠️ Просрочено: {task.get('title', '(без названия)')}\nДедлайн: {task.get('deadline')}\nID: {tid}"
                                await application.bot.send_message(chat_id=chat_id, text=text)
                                notify["overdue_sent_at"] = now
                                dirty = True
                    except Exception as e:
                        logging.exception(f"tool failed {str(e)}")
                        continue
            if dirty:
                _save_all_tasks(all_tasks)
        except Exception as e:
            logging.exception(f"tool failed {str(e)}")
        await asyncio.sleep(policy.check_interval_sec)
