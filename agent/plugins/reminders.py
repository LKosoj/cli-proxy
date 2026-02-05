from __future__ import annotations

import asyncio
import datetime as _dt
import logging
import re
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from agent.plugins.base import ToolPlugin
from agent.tooling.spec import ToolSpec


class RemindersTool(ToolPlugin):
    def get_source_name(self) -> str:
        return "Reminders"

    def get_spec(self) -> ToolSpec:
        now_str = _dt.datetime.now().strftime("%Y-%m-%d %H:%M")
        return ToolSpec(
            name="reminders",
            description=f"Напоминания: создать, показать список, удалить. Текущее время: {now_str}",
            parameters={
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["set", "list", "delete"]},
                    "time": {"type": "string", "description": "Для set: YYYY-MM-DD HH:MM"},
                    "message": {"type": "string", "description": "Для set: текст напоминания"},
                    "reminder_id": {"type": "string", "description": "Для delete: ID напоминания"},
                },
                "required": ["action"],
            },
            parallelizable=False,
            timeout_ms=30_000,
        )

    def get_commands(self) -> List[Dict[str, Any]]:
        return [
            {
                "command": "list_reminders",
                "description": "Показать активные напоминания (инлайн-меню)",
                "handler": self.cmd_list_reminders,
                "handler_kwargs": {},
                "add_to_menu": True,
            },
            {
                "command": "set_reminder",
                "description": "Создать напоминание. Формат: /set_reminder YYYY-MM-DD HH:MM текст",
                "args": "YYYY-MM-DD HH:MM <текст>",
                "handler": self.cmd_set_reminder,
                "handler_kwargs": {},
                "add_to_menu": False,
            },
            {
                "command": "delete_reminder",
                "description": "Удалить напоминание. Формат: /delete_reminder <id>",
                "args": "<id>",
                "handler": self.cmd_delete_reminder,
                "handler_kwargs": {},
                "add_to_menu": False,
            },
            {
                "callback_query_handler": self.handle_reminder_callback,
                "callback_pattern": r"^reminder:",
                "handler_kwargs": {},
                "add_to_menu": False,
            },
        ]

    def _get_user_chat(self, update: Update) -> Tuple[Optional[int], Optional[int]]:
        chat_id = update.effective_chat.id if update.effective_chat else None
        user_id = update.effective_user.id if update.effective_user else None
        return user_id, chat_id

    def _user_task_ids(self, user_id: int) -> set:
        user_tasks = self.services.setdefault("user_tasks", {})
        return user_tasks.setdefault(user_id, set())

    def _scheduler_tasks(self) -> Dict[str, Dict[str, Any]]:
        return self.services.setdefault("scheduler_tasks", {})

    async def cmd_set_reminder(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user_id, chat_id = self._get_user_chat(update)
        if not user_id or not chat_id:
            return
        message = update.effective_message
        text = (message.text or "").strip() if message else ""
        parts = text.split(maxsplit=3)
        if len(parts) < 4:
            await message.reply_text("Использование: /set_reminder YYYY-MM-DD HH:MM текст")
            return
        _, date_s, time_s, msg = parts
        when = f"{date_s} {time_s}"
        msg = msg.strip()
        try:
            dt = _dt.datetime.strptime(when, "%Y-%m-%d %H:%M")
        except Exception:
            await message.reply_text("Неверный формат времени. Нужно YYYY-MM-DD HH:MM")
            return

        delay_sec = int((dt - _dt.datetime.now()).total_seconds())
        if delay_sec <= 0:
            await message.reply_text("Время напоминания должно быть в будущем.")
            return
        if delay_sec > 24 * 60 * 60:
            await message.reply_text("Максимальная задержка: 24 часа.")
            return

        user_set = self._user_task_ids(user_id)
        if len(user_set) >= 5:
            await message.reply_text("Максимум 5 активных напоминаний на пользователя.")
            return

        reminder_id = f"rem_{int(time.time())}_{uuid.uuid4().hex[:4]}"
        task = {
            "id": reminder_id,
            "user_id": user_id,
            "chat_id": chat_id,
            "type": "message",
            "content": msg,
            "execute_at": time.time() + delay_sec,
            "when": when,
        }
        scheduler_tasks = self._scheduler_tasks()
        scheduler_tasks[reminder_id] = task
        user_set.add(reminder_id)

        async def _job() -> None:
            await asyncio.sleep(delay_sec)
            if reminder_id not in scheduler_tasks:
                return
            scheduler_tasks.pop(reminder_id, None)
            self._user_task_ids(user_id).discard(reminder_id)
            try:
                await message.reply_text(f"⏰ Напоминание: {msg}")
            except Exception as e:
                logging.exception(f"tool failed {str(e)}")

        asyncio.create_task(_job())
        await message.reply_text(f"✅ Напоминание создано\nID: {reminder_id}\nВремя: {when}")

    async def cmd_list_reminders(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user_id, _chat_id = self._get_user_chat(update)
        message = update.effective_message
        if not user_id or not message:
            return

        scheduler_tasks = self._scheduler_tasks()
        user_set = self._user_task_ids(user_id)
        if not user_set:
            await message.reply_text("Нет активных напоминаний.")
            return

        keyboard = []
        for rid in sorted(user_set):
            t = scheduler_tasks.get(rid)
            if not t:
                continue
            when = t.get("when", "")
            content = (t.get("content") or "")[:60]
            keyboard.append(
                [
                    InlineKeyboardButton(
                        text=f"{when} | {content}",
                        callback_data=f"reminder:view:{rid}",
                    ),
                    InlineKeyboardButton(
                        text="Удалить",
                        callback_data=f"reminder:delete:{rid}",
                    ),
                ]
            )
        keyboard.append([InlineKeyboardButton("Закрыть", callback_data="reminder:close_menu:")])
        await message.reply_text("Активные напоминания:", reply_markup=InlineKeyboardMarkup(keyboard))

    async def cmd_delete_reminder(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user_id, _chat_id = self._get_user_chat(update)
        message = update.effective_message
        if not user_id or not message:
            return
        args = context.args or []
        if not args:
            await message.reply_text("Использование: /delete_reminder <id>")
            return
        rid = args[0].strip()
        scheduler_tasks = self._scheduler_tasks()
        t = scheduler_tasks.get(rid)
        if not t:
            await message.reply_text("Напоминание не найдено.")
            return
        if int(t.get("user_id") or 0) != int(user_id):
            await message.reply_text("Нельзя удалить чужое напоминание.")
            return
        scheduler_tasks.pop(rid, None)
        self._user_task_ids(user_id).discard(rid)
        await message.reply_text(f"🗑️ Удалено: {rid}")

    async def handle_reminder_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        if not query or not query.data:
            return
        try:
            _action, command, rid = query.data.split(":", 2)
        except Exception:
            return
        user_id = query.from_user.id if query.from_user else None
        if not user_id:
            return
        scheduler_tasks = self._scheduler_tasks()
        user_set = self._user_task_ids(user_id)

        if command == "close_menu":
            try:
                await query.answer("Ок")
            except Exception:
                pass
            try:
                await query.message.delete()
            except Exception:
                pass
            return

        if command == "view":
            t = scheduler_tasks.get(rid)
            if not t or rid not in user_set:
                await query.answer("Напоминание не найдено", show_alert=True)
                return
            when = t.get("when", "")
            content = t.get("content", "")
            await query.answer(f"{when}\n{content}", show_alert=True, cache_time=0)
            return

        if command == "delete":
            t = scheduler_tasks.get(rid)
            if not t or rid not in user_set:
                await query.answer("Напоминание не найдено", show_alert=True)
                return
            scheduler_tasks.pop(rid, None)
            user_set.discard(rid)
            await query.answer("Удалено")
            # Update keyboard.
            if not user_set:
                await query.edit_message_text("Нет активных напоминаний.")
                return
            keyboard = []
            for r2 in sorted(user_set):
                t2 = scheduler_tasks.get(r2)
                if not t2:
                    continue
                when = t2.get("when", "")
                content = (t2.get("content") or "")[:60]
                keyboard.append(
                    [
                        InlineKeyboardButton(text=f"{when} | {content}", callback_data=f"reminder:view:{r2}"),
                        InlineKeyboardButton(text="Удалить", callback_data=f"reminder:delete:{r2}"),
                    ]
                )
            keyboard.append([InlineKeyboardButton("Закрыть", callback_data="reminder:close_menu:")])
            await query.edit_message_text("Активные напоминания:", reply_markup=InlineKeyboardMarkup(keyboard))
            return

    async def execute(self, args: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
        action = args.get("action")
        session_id = ctx.get("session_id") or "0"
        user_id = int(re.sub(r"\\D", "", session_id) or 0)
        chat_id = ctx.get("chat_id") or 0
        bot = ctx.get("bot")
        context = ctx.get("context")

        scheduler_tasks = self.services.setdefault("scheduler_tasks", {})
        user_tasks = self.services.setdefault("user_tasks", {})

        if action == "set":
            when = (args.get("time") or "").strip()
            msg = (args.get("message") or "").strip()
            if not when or not msg:
                return {"success": False, "error": "Для set нужны time и message"}
            try:
                dt = _dt.datetime.strptime(when, "%Y-%m-%d %H:%M")
            except Exception:
                return {"success": False, "error": "Неверный формат time. Нужно YYYY-MM-DD HH:MM"}

            delay_sec = int((dt - _dt.datetime.now()).total_seconds())
            if delay_sec <= 0:
                return {"success": False, "error": "Время напоминания должно быть в будущем"}
            if delay_sec > 24 * 60 * 60:
                return {"success": False, "error": "Максимальная задержка 24 часа"}

            user_set = user_tasks.get(user_id, set())
            if len(user_set) >= 5:
                return {"success": False, "error": "Максимум 5 напоминаний на пользователя"}

            reminder_id = f"rem_{int(time.time())}_{uuid.uuid4().hex[:4]}"
            task = {
                "id": reminder_id,
                "user_id": user_id,
                "chat_id": chat_id,
                "type": "message",
                "content": msg,
                "execute_at": time.time() + delay_sec,
                "when": when,
            }
            scheduler_tasks[reminder_id] = task
            user_set.add(reminder_id)
            user_tasks[user_id] = user_set

            async def _job():
                await asyncio.sleep(delay_sec)
                if reminder_id not in scheduler_tasks:
                    return
                scheduler_tasks.pop(reminder_id, None)
                user_tasks.get(user_id, set()).discard(reminder_id)
                if bot and context:
                    try:
                        await bot._send_message(context, chat_id=chat_id, text=f"⏰ Напоминание: {msg}")
                    except Exception as e:
                        logging.exception(f"tool failed {str(e)}")

            asyncio.create_task(_job())
            return {"success": True, "output": f"✅ Напоминание создано\nID: {reminder_id}\nВремя: {when}\nТекст: {msg[:80]}"}

        if action == "list":
            user_set = user_tasks.get(user_id, set())
            if not user_set:
                return {"success": True, "output": "Нет активных напоминаний"}
            lines = []
            for rid in sorted(user_set):
                t = scheduler_tasks.get(rid)
                if not t:
                    continue
                left_min = int(max(0, (t["execute_at"] - time.time()) / 60))
                lines.append(f"• {rid}: через {left_min} мин ({t.get('when','')}) - {t.get('content','')[:40]}")
            return {"success": True, "output": "Активные напоминания:\n" + "\n".join(lines)}

        if action == "delete":
            rid = (args.get("reminder_id") or "").strip()
            if not rid:
                return {"success": False, "error": "Для delete нужен reminder_id"}
            t = scheduler_tasks.get(rid)
            if not t:
                return {"success": False, "error": "Напоминание не найдено"}
            if t.get("user_id") != user_id:
                return {"success": False, "error": "Нельзя удалить чужое напоминание"}
            scheduler_tasks.pop(rid, None)
            user_tasks.get(user_id, set()).discard(rid)
            return {"success": True, "output": f"🗑️ Напоминание {rid} удалено"}

        return {"success": False, "error": f"Unknown action: {action}"}
