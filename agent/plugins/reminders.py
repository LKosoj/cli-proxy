from __future__ import annotations

import asyncio
import datetime as _dt
import logging
import re
import time
import uuid
from typing import Any, Callable, Dict, List, Optional, Tuple

from telegram import InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from agent.plugins.base import DialogMixin, ToolPlugin
from modes.sdk.runtime.tooling.spec import ToolSpec

logger = logging.getLogger(__name__)


class RemindersTool(DialogMixin, ToolPlugin):
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

    # -- menu & commands ----------------------------------------------------

    def get_menu_label(self):
        return "Напоминания"

    def get_menu_actions(self):
        return [
            {"label": "Список", "action": "list"},
            {"label": "Создать", "action": "set"},
        ]

    # -- DialogMixin contract -----------------------------------------------

    def dialog_steps(self):
        return {"wait_reminder_input": self._on_reminder_text}

    def callback_handlers(self) -> Dict[str, Callable]:
        return {
            "list": self._cb_list,
            "set": self._cb_set,
            "delete": self._cb_delete,
            "view": self._cb_view,
            "close_menu": self._cb_close_menu,
        }

    # -- helpers ------------------------------------------------------------

    def _get_user_chat(self, update: Update) -> Tuple[Optional[int], Optional[int]]:
        chat_id = update.effective_chat.id if update.effective_chat else None
        user_id = update.effective_user.id if update.effective_user else None
        return user_id, chat_id

    def _user_task_ids(self, user_id: int) -> set:
        user_tasks = self.services.setdefault("user_tasks", {})
        return user_tasks.setdefault(user_id, set())

    def _scheduler_tasks(self) -> Dict[str, Dict[str, Any]]:
        return self.services.setdefault("scheduler_tasks", {})

    def _build_reminder_keyboard(self, user_id: int) -> List[list]:
        scheduler_tasks = self._scheduler_tasks()
        user_set = self._user_task_ids(user_id)
        keyboard = []
        for rid in sorted(user_set):
            t = scheduler_tasks.get(rid)
            if not t:
                continue
            when = t.get("when", "")
            content = (t.get("content") or "")[:60]
            keyboard.append([
                self.action_button(f"{when} | {content}", "view", rid),
                self.action_button("Удалить", "delete", rid),
            ])
        keyboard.append([self.action_button("Закрыть", "close_menu")])
        return keyboard

    # -- callback handlers --------------------------------------------------

    async def _cb_list(self, update: Update, context: ContextTypes.DEFAULT_TYPE, payload: str) -> None:
        query = update.callback_query
        user_id = query.from_user.id if query and query.from_user else None
        if not user_id or not query:
            return
        user_set = self._user_task_ids(user_id)
        if not user_set:
            if query.message:
                await query.message.reply_text("Нет активных напоминаний.")
            return
        keyboard = self._build_reminder_keyboard(user_id)
        if query.message:
            await query.message.reply_text(
                "Активные напоминания:",
                reply_markup=InlineKeyboardMarkup(keyboard),
            )

    async def _cb_set(self, update: Update, context: ContextTypes.DEFAULT_TYPE, payload: str) -> None:
        query = update.callback_query
        user_id = query.from_user.id if query and query.from_user else None
        chat_id = query.message.chat_id if query and query.message else None
        if not user_id or not chat_id:
            return
        self.start_dialog(chat_id, "wait_reminder_input", data={}, user_id=user_id)
        if query and query.message:
            await query.message.reply_text(
                "Создание напоминания.\n"
                "Отправьте строку в формате:\n"
                "YYYY-MM-DD HH:MM текст напоминания\n\n"
                "Пример: 2026-02-06 15:00 Позвонить маме\n\n"
                "Для отмены — кнопка ниже или текст: отмена, cancel, выход, -",
                reply_markup=self.cancel_markup(),
            )

    async def _cb_delete(self, update: Update, context: ContextTypes.DEFAULT_TYPE, payload: str) -> None:
        query = update.callback_query
        user_id = query.from_user.id if query and query.from_user else None
        if not user_id or not query:
            return
        rid = payload
        scheduler_tasks = self._scheduler_tasks()
        user_set = self._user_task_ids(user_id)
        t = scheduler_tasks.get(rid)
        if not t or rid not in user_set:
            await query.answer("Напоминание не найдено", show_alert=True)
            return
        scheduler_tasks.pop(rid, None)
        user_set.discard(rid)
        await query.answer("Удалено")
        if not user_set:
            await query.edit_message_text("Нет активных напоминаний.")
            return
        keyboard = self._build_reminder_keyboard(user_id)
        await query.edit_message_text(
            "Активные напоминания:",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )

    async def _cb_view(self, update: Update, context: ContextTypes.DEFAULT_TYPE, payload: str) -> None:
        query = update.callback_query
        user_id = query.from_user.id if query and query.from_user else None
        if not user_id or not query:
            return
        rid = payload
        scheduler_tasks = self._scheduler_tasks()
        user_set = self._user_task_ids(user_id)
        t = scheduler_tasks.get(rid)
        if not t or rid not in user_set:
            await query.answer("Напоминание не найдено", show_alert=True)
            return
        when = t.get("when", "")
        content = t.get("content", "")
        await query.answer(f"{when}\n{content}", show_alert=True, cache_time=0)

    async def _cb_close_menu(self, update: Update, context: ContextTypes.DEFAULT_TYPE, payload: str) -> None:
        query = update.callback_query
        if not query:
            return
        try:
            await query.message.delete()
        except Exception:
            pass

    # -- dialog step handler ------------------------------------------------

    async def _on_reminder_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Parse 'YYYY-MM-DD HH:MM message' and create a reminder."""
        msg = update.effective_message
        if not msg:
            return
        user_id = update.effective_user.id if update.effective_user else None
        chat_id = update.effective_chat.id if update.effective_chat else None
        if not user_id or not chat_id:
            return

        text = (msg.text or "").strip()
        parts = text.split(maxsplit=2)
        if len(parts) < 3:
            await msg.reply_text(
                "Нужно: YYYY-MM-DD HH:MM текст\n"
                "Пример: 2026-02-06 15:00 Позвонить маме"
            )
            return

        date_s, time_s, reminder_msg = parts
        when = f"{date_s} {time_s}"
        reminder_msg = reminder_msg.strip()
        try:
            dt = _dt.datetime.strptime(when, "%Y-%m-%d %H:%M")
        except Exception:
            await msg.reply_text("Неверный формат времени. Нужно YYYY-MM-DD HH:MM")
            return

        delay_sec = int((dt - _dt.datetime.now()).total_seconds())
        if delay_sec <= 0:
            await msg.reply_text("Время напоминания должно быть в будущем.")
            return
        if delay_sec > 24 * 60 * 60:
            await msg.reply_text("Максимальная задержка: 24 часа.")
            return

        user_set = self._user_task_ids(user_id)
        if len(user_set) >= 5:
            await msg.reply_text("Максимум 5 активных напоминаний на пользователя.")
            return

        reminder_id = f"rem_{int(time.time())}_{uuid.uuid4().hex[:4]}"
        task = {
            "id": reminder_id,
            "user_id": user_id,
            "chat_id": chat_id,
            "type": "message",
            "content": reminder_msg,
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
                await msg.reply_text(f"⏰ Напоминание: {reminder_msg}")
            except Exception:
                logger.exception("reminders: failed to send reminder message id=%r", reminder_id)

        asyncio.create_task(_job())
        self.end_dialog(chat_id)
        await msg.reply_text(f"✅ Напоминание создано\nID: {reminder_id}\nВремя: {when}")

    # -- execute (agent API) ------------------------------------------------

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
                    except Exception:
                        logger.exception("reminders: failed to deliver reminder id=%r chat_id=%s", reminder_id, chat_id)

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
                lines.append(f"• {rid}: через {left_min} мин ({t.get('when', '')}) - {t.get('content', '')[:40]}")
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
