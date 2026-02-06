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

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    filters,
)

from agent.plugins.base import ToolPlugin
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


class TaskManagementTool(ToolPlugin):
    """
    User-level task manager with Telegram UI and a periodic deadline checker (implemented in bot.py).
    Storage: AGENT_SANDBOX_ROOT/_shared/tasks.json
    """

    _policy = _NotifyPolicy()
    _ST_ADD_TEXT = 1

    def _dialog_state(self) -> Dict[int, Dict[str, Any]]:
        # chat_id -> dialog data
        return self.services.setdefault("task_dialog_state", {})

    def _dialog_active_filter(self):
        # Custom filter that matches only when dialog is active for this chat.
        tool = self

        class _Active(filters.BaseFilter):
            def filter(self, message) -> bool:
                try:
                    chat_id = getattr(message, "chat_id", None)
                    if not chat_id:
                        return False
                    return bool(tool._dialog_state().get(int(chat_id)))
                except Exception:
                    return False

        return _Active()

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

    def get_commands(self) -> List[Dict[str, Any]]:
        return [
            {
                "command": "tasks",
                "description": "Задачи: список и меню",
                "handler": self.cmd_tasks,
                "handler_kwargs": {},
                "add_to_menu": True,
            },
            {
                "command": "task_add",
                "description": "Добавить задачу. Формат: /task_add <priority> [YYYY-MM-DD HH:MM] <title>",
                "args": "<priority> [YYYY-MM-DD HH:MM] <title>",
                "handler": self.cmd_task_add,
                "handler_kwargs": {},
                "add_to_menu": False,
            },
            {
                "command": "task_add_dialog",
                "description": "Добавить задачу через диалог (кнопка в /tasks).",
                "handler": self._start_add_from_command,
                "handler_kwargs": {},
                "add_to_menu": False,
            },
            {
                "command": "task_update",
                "description": "Обновить задачу. Формат: /task_update <id> status=<pending|in_progress|completed|cancelled> priority=<high|medium|low> deadline='YYYY-MM-DD HH:MM'",
                "args": "<id> key=value ...",
                "handler": self.cmd_task_update,
                "handler_kwargs": {},
                "add_to_menu": False,
            },
            {
                "command": "task_delete",
                "description": "Удалить задачу. Формат: /task_delete <id>",
                "args": "<id>",
                "handler": self.cmd_task_delete,
                "handler_kwargs": {},
                "add_to_menu": False,
            },
            {
                "callback_query_handler": self.handle_task_callback,
                # Keep this handler narrow so conversation entry-points (e.g. task:add) can work.
                "callback_pattern": r"^task:(refresh|view|del|next)(:|$)",
                "handler_kwargs": {},
                "add_to_menu": False,
            },
            {
                "callback_query_handler": self._start_add_from_button,
                "callback_pattern": r"^task:add$",
                "handler_kwargs": {},
                "add_to_menu": False,
            },
            {
                "callback_query_handler": self._cancel_add_dialog_from_button,
                "callback_pattern": r"^task:add_cancel$",
                "handler_kwargs": {},
                "add_to_menu": False,
            },
        ]

    def get_message_handlers(self) -> List[Dict[str, Any]]:
        # Avoid ConversationHandler here: it produces PTB warnings for callback entry points with per_message defaults.
        # We keep a lightweight dialog state inside ToolRegistry services and match only when active.
        active = self._dialog_active_filter()
        return [{"filters": active & filters.TEXT & ~filters.COMMAND, "handler": self._on_add_text}]

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

    async def cmd_tasks(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        msg = update.effective_message
        if not msg:
            return
        user_id = update.effective_user.id if update.effective_user else None
        if not user_id:
            return
        text, markup = self._build_tasks_menu(user_id)
        await msg.reply_text(text, reply_markup=markup)

    async def cmd_task_add(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        msg = update.effective_message
        if not msg:
            return
        user_id = update.effective_user.id if update.effective_user else None
        chat_id = update.effective_chat.id if update.effective_chat else None
        if not user_id or not chat_id:
            return
        raw = (msg.text or "").strip()
        parts = raw.split(maxsplit=3)
        if len(parts) < 3:
            await msg.reply_text("Использование: /task_add <priority> [YYYY-MM-DD HH:MM] <title>")
            return
        # /task_add <priority> <...>
        _cmd = parts[0]
        priority = parts[1].strip().lower()
        if priority not in ("high", "medium", "low"):
            await msg.reply_text("priority должен быть high|medium|low")
            return
        title = ""
        deadline = None
        if len(parts) == 3:
            # no deadline, title is rest
            title = parts[2]
        else:
            # maybe has deadline
            tail = parts[2:]
            # deadline may be two tokens: YYYY-MM-DD HH:MM
            if len(tail) >= 2 and len(tail[0]) == 10 and ":" in tail[1]:
                deadline = f"{tail[0]} {tail[1]}"
                title = tail[2] if len(tail) >= 3 else ""
                if len(tail) > 3:
                    title = " ".join(tail[2:])
            else:
                title = " ".join(tail)
        title = title.strip()
        if not title:
            await msg.reply_text("title обязателен")
            return
        dl_ts, dl_err = _parse_deadline(deadline)
        if dl_err:
            await msg.reply_text(dl_err)
            return
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
        await msg.reply_text(f"✅ Добавлено: {_format_task_line(task)}")

    async def cmd_task_update(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        msg = update.effective_message
        if not msg:
            return
        user_id = update.effective_user.id if update.effective_user else None
        if not user_id:
            return
        text = (msg.text or "").strip()
        parts = text.split(maxsplit=2)
        if len(parts) < 2:
            await msg.reply_text("Использование: /task_update <id> key=value ...")
            return
        task_id = parts[1].strip()
        kv = parts[2] if len(parts) >= 3 else ""
        updates: Dict[str, Any] = {"action": "update", "task_id": task_id}
        if kv:
            for token in kv.split():
                if "=" not in token:
                    continue
                k, v = token.split("=", 1)
                k = k.strip().lower()
                v = v.strip().strip("\"'")
                if k in ("status", "priority", "deadline", "description"):
                    updates[k] = v
        res = await self.execute(updates, {"chat_id": int(user_id)})
        if res.get("success"):
            await msg.reply_text(str(res.get("output") or ""))
        else:
            await msg.reply_text(str(res.get("error") or "Ошибка"))

    async def cmd_task_delete(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        msg = update.effective_message
        if not msg:
            return
        user_id = update.effective_user.id if update.effective_user else None
        if not user_id:
            return
        args = context.args or []
        if not args:
            await msg.reply_text("Использование: /task_delete <id>")
            return
        task_id = args[0].strip()
        res = await self.execute({"action": "delete", "task_id": task_id}, {"chat_id": int(user_id)})
        if res.get("success"):
            await msg.reply_text(str(res.get("output") or ""))
        else:
            await msg.reply_text(str(res.get("error") or "Ошибка"))

    def _build_tasks_menu(self, user_id: int) -> Tuple[str, Optional[InlineKeyboardMarkup]]:
        all_tasks = _load_all_tasks()
        bucket = all_tasks.get(str(user_id), {})
        items = list(bucket.values()) if isinstance(bucket, dict) else []
        if not items:
            rows = [
                [InlineKeyboardButton("Добавить", callback_data="task:add")],
                [InlineKeyboardButton("Как добавить (команда)", callback_data="task:view_help")],
            ]
            text = "Задач нет.\n\nДобавить:\n- кнопкой ниже (диалог)\n- или командой: /task_add <priority> [YYYY-MM-DD HH:MM] <title>\n- или: /task_add_dialog"
            return text, InlineKeyboardMarkup(rows)
        # show up to 12 tasks in menu
        items = items[:12]
        rows = []
        rows.append([InlineKeyboardButton("Добавить", callback_data="task:add")])
        for t in items:
            tid = t.get("id")
            title = (t.get("title") or "").strip()[:30]
            st = t.get("status") or "pending"
            pr = (t.get("priority") or "low").upper()
            label = f"[{pr}] {title} ({_human_status(st)})"
            rows.append(
                [
                    InlineKeyboardButton("Статус", callback_data=f"task:next:{tid}"),
                    InlineKeyboardButton("Удалить", callback_data=f"task:del:{tid}"),
                ]
            )
            rows.append([InlineKeyboardButton(label, callback_data=f"task:view:{tid}")])
        rows.append([InlineKeyboardButton("Обновить", callback_data="task:refresh:")])
        return "Задачи:", InlineKeyboardMarkup(rows)

    def _ensure_agent_enabled(self, context: ContextTypes.DEFAULT_TYPE) -> bool:
        try:
            bot_app = context.application.bot_data.get("bot_app")
            session = bot_app.manager.active() if bot_app else None
            return bool(session and getattr(session, "agent_enabled", False))
        except Exception:
            return False

    async def _start_add_from_button(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        if query:
            try:
                await query.answer()
            except Exception:
                pass
        if not self._ensure_agent_enabled(context):
            if query and query.message:
                await query.message.reply_text("Агент не активен.")
            return
        user_id = query.from_user.id if query and query.from_user else None
        chat_id = query.message.chat_id if query and query.message else None
        if not user_id or not chat_id:
            return
        self._dialog_state()[int(chat_id)] = {"mode": "add", "user_id": int(user_id), "ts": int(_now_ts())}
        if query and query.message:
            await query.message.reply_text(
                "Добавление задачи.\n"
                "Отправьте строку в одном из форматов:\n"
                "1) high Сделать важное\n"
                "2) medium 2026-02-06 10:00 Созвон\n"
                "3) low 2026-02-06 10:00 Купить молоко\n\n"
                "Кнопка Отмена ниже, чтобы выйти из диалога.",
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("Отмена", callback_data="task:add_cancel")]]
                ),
            )
        return

    async def _start_add_from_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        msg = update.effective_message
        if not self._ensure_agent_enabled(context):
            if msg:
                await msg.reply_text("Агент не активен.")
            return
        user_id = update.effective_user.id if update.effective_user else None
        chat_id = update.effective_chat.id if update.effective_chat else None
        if not user_id or not chat_id:
            return
        self._dialog_state()[int(chat_id)] = {"mode": "add", "user_id": int(user_id), "ts": int(_now_ts())}
        if msg:
            await msg.reply_text(
                "Добавление задачи.\n"
                "Отправьте строку: <priority> [YYYY-MM-DD HH:MM] <title>\n"
                "Пример: high 2026-02-06 10:00 Созвон\n\n"
                "Напишите текст задачи следующим сообщением. Для отмены нажмите кнопку ниже.",
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("Отмена", callback_data="task:add_cancel")]]
                ),
            )
        return

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
        msg = update.effective_message
        if not msg:
            return
        user_id = update.effective_user.id if update.effective_user else None
        chat_id = update.effective_chat.id if update.effective_chat else None
        if not user_id or not chat_id:
            return
        st = self._dialog_state().get(int(chat_id)) or {}
        if st.get("mode") != "add":
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
        self._dialog_state().pop(int(chat_id), None)
        await msg.reply_text(f"✅ Добавлено: {_format_task_line(task)}\nОткройте /tasks чтобы увидеть меню.")
        return

    async def _cancel_add_dialog_from_button(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        if not query:
            return
        try:
            await query.answer()
        except Exception:
            pass
        chat_id = query.message.chat_id if query.message else None
        if chat_id:
            self._dialog_state().pop(int(chat_id), None)
        if query.message:
            await query.message.reply_text("Отменено.")

    async def handle_task_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        if not query or not query.data:
            return
        try:
            await query.answer()
        except Exception:
            pass
        user_id = query.from_user.id if query.from_user else None
        if not user_id:
            return
        data = query.data
        parts = data.split(":", 2)
        if len(parts) < 2:
            return
        cmd = parts[1]
        arg = parts[2] if len(parts) >= 3 else ""
        if cmd == "refresh":
            text, markup = self._build_tasks_menu(int(user_id))
            await query.edit_message_text(text, reply_markup=markup)
            return
        if cmd == "view_help":
            await query.answer("Используйте /task_add или кнопку Добавить.", show_alert=True)
            return
        if cmd == "view":
            tid = arg
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
            return
        if cmd == "del":
            tid = arg
            all_tasks = _load_all_tasks()
            bucket = all_tasks.get(str(user_id), {})
            if isinstance(bucket, dict) and tid in bucket:
                bucket.pop(tid, None)
                all_tasks[str(user_id)] = bucket
                _save_all_tasks(all_tasks)
            text, markup = self._build_tasks_menu(int(user_id))
            await query.edit_message_text(text, reply_markup=markup)
            return
        if cmd == "next":
            tid = arg
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
            return


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
                                text = f"⏳ Скоро дедлайн: {task.get('title','(без названия)')}\nДедлайн: {task.get('deadline')}\nID: {tid}"
                                await application.bot.send_message(chat_id=chat_id, text=text)
                                notify["due_soon_sent_at"] = now
                                dirty = True
                        if dl_ts <= now:
                            last = notify.get("overdue_sent_at") or 0
                            if now - int(last) >= policy.overdue_repeat_sec:
                                text = f"⚠️ Просрочено: {task.get('title','(без названия)')}\nДедлайн: {task.get('deadline')}\nID: {tid}"
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
