import time
from typing import Callable, Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from state import get_state
from utils import format_session_label


class SessionUI:
    def __init__(
        self,
        config,
        manager,
        send_message,
        format_ts,
        short_label,
        on_close: Optional[Callable[[str], None]] = None,
        on_before_close: Optional[Callable[[str, int, ContextTypes.DEFAULT_TYPE], None]] = None,
    ) -> None:
        self.config = config
        self.manager = manager
        self._send_message = send_message
        self._format_ts = format_ts
        self._short_label = short_label
        self._on_close = on_close
        self._on_before_close = on_before_close
        self.pending_session_rename: dict[int, str] = {}
        self.pending_session_resume: dict[int, str] = {}

    def build_sessions_menu(self) -> InlineKeyboardMarkup:
        rows = []
        for sid, s in self.manager.sessions.items():
            active = "★" if sid == self.manager.active_session_id else " "
            label = s.name or f"{s.tool.name} @ {s.workdir}"
            text = self._short_label(f"{active} {sid}: {label}", max_len=60)
            rows.append([InlineKeyboardButton(text, callback_data=f"sess_pick:{sid}")])
        rows.append([InlineKeyboardButton("❌ Закрыть меню", callback_data="sess_close_menu")])
        return InlineKeyboardMarkup(rows)

    async def handle_pending_message(self, chat_id: int, text: str, context: ContextTypes.DEFAULT_TYPE) -> bool:
        if chat_id in self.pending_session_rename:
            session_id = self.pending_session_rename.pop(chat_id)
            session = self.manager.get(session_id)
            name = text.strip()
            if name in ("-", "отмена", "Отмена"):
                await self._send_message(context, chat_id=chat_id, text="Переименование отменено.")
                return True
            if not name:
                await self._send_message(context, chat_id=chat_id, text="Имя сессии пустое.")
                return True
            if not session:
                await self._send_message(context, chat_id=chat_id, text="Сессия не найдена.")
                return True
            session.name = name
            self.manager._persist_sessions()
            await self._send_message(context, chat_id=chat_id, text="Имя сессии обновлено.")
            return True
        if chat_id in self.pending_session_resume:
            session_id = self.pending_session_resume.pop(chat_id)
            session = self.manager.get(session_id)
            token = text.strip()
            if token in ("-", "отмена", "Отмена"):
                await self._send_message(context, chat_id=chat_id, text="Изменение resume отменено.")
                return True
            if not session:
                await self._send_message(context, chat_id=chat_id, text="Сессия не найдена.")
                return True
            session.resume_token = token
            self.manager._persist_sessions()
            await self._send_message(context, chat_id=chat_id, text="Resume обновлен.")
            return True
        return False

    async def handle_callback(self, query, chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> bool:
        data = query.data or ""
        if data.startswith("sess_pick:"):
            session_id = data.split(":", 1)[1]
            session = self.manager.get(session_id)
            if not session:
                await query.edit_message_text("Сессия не найдена.")
                return True
            label = session.name or f"{session.tool.name} @ {session.workdir}"
            keyboard = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton("✅ Use", callback_data=f"sess_use:{session_id}"),
                        InlineKeyboardButton("📋 Status", callback_data=f"sess_status:{session_id}"),
                    ],
                    [
                        InlineKeyboardButton("✏️ Rename", callback_data=f"sess_rename:{session_id}"),
                        InlineKeyboardButton("🔄 Resume", callback_data=f"sess_resume:{session_id}"),
                    ],
                    [
                        InlineKeyboardButton("📥 Queue", callback_data=f"sess_queue:{session_id}"),
                        InlineKeyboardButton("🗑 Clear queue", callback_data=f"sess_clearqueue:{session_id}"),
                    ],
                    [
                        InlineKeyboardButton("💾 State", callback_data=f"sess_state:{session_id}"),
                        InlineKeyboardButton("🚫 Close", callback_data=f"sess_close:{session_id}"),
                    ],
                    [
                        InlineKeyboardButton("❌ Закрыть меню", callback_data="sess_close_menu"),
                    ],
                ]
            )
            await query.edit_message_text(
                f"Сессия {session.id}: {label}",
                reply_markup=keyboard,
            )
            return True
        if data.startswith("sess_use:"):
            session_id = data.split(":", 1)[1]
            ok = self.manager.set_active(session_id)
            if ok:
                session = self.manager.get(session_id)
                await query.edit_message_text(format_session_label(session))
            else:
                await query.edit_message_text("Сессия не найдена.")
            return True
        if data.startswith("sess_status:"):
            session_id = data.split(":", 1)[1]
            session = self.manager.get(session_id)
            if not session:
                await query.edit_message_text("Сессия не найдена.")
                return True
            now = time.time()
            busy_txt = "занята" if session.busy else "свободна"
            git_txt = "git: занято" if getattr(session, "git_busy", False) else "git: свободно"
            conflict_txt = ""
            if getattr(session, "git_conflict", False):
                conflict_txt = f" | конфликт: {session.git_conflict_kind or 'да'}"
            run_for = f"{int(now - session.started_at)}с" if session.started_at else "нет"
            last_out = f"{int(now - session.last_output_ts)}с назад" if session.last_output_ts else "нет"
            tick_txt = f"{int(now - session.last_tick_ts)}с назад" if session.last_tick_ts else "нет"
            agent_txt = "включен" if getattr(session, "agent_enabled", False) else "выключен"
            manager_txt = "включен" if getattr(session, "manager_enabled", False) else "выключен"
            project_root = getattr(session, "project_root", None)
            lines = [
                f"Сессия: {session.id} ({session.name or session.tool.name}) @ {session.workdir}",
                f"Статус: {busy_txt} | {git_txt}{conflict_txt} | В работе: {run_for} | Агент: {agent_txt} | Manager: {manager_txt}",
            ]
            if project_root:
                lines.append(f"Проект: {project_root}")
            lines.append(f"Последний вывод: {last_out} | Последний тик: {tick_txt} | Тиков: {session.tick_seen}")
            lines.append(f"Очередь: {len(session.queue)} | Resume: {'есть' if session.resume_token else 'нет'}")
            text = "\n".join(lines)
            await query.edit_message_text(text)
            return True
        if data.startswith("sess_rename:"):
            session_id = data.split(":", 1)[1]
            session = self.manager.get(session_id)
            if not session:
                await query.edit_message_text("Сессия не найдена.")
                return True
            self.pending_session_rename[chat_id] = session_id
            await query.edit_message_text(
                f"Введите новое имя для {session.id} (или '-' для отмены)."
            )
            return True
        if data.startswith("sess_resume:"):
            session_id = data.split(":", 1)[1]
            session = self.manager.get(session_id)
            if not session:
                await query.edit_message_text("Сессия не найдена.")
                return True
            current = session.resume_token or "нет"
            self.pending_session_resume[chat_id] = session_id
            await query.edit_message_text(
                f"Текущий resume: {current}\nВведите новый resume (или '-' для отмены)."
            )
            return True
        if data.startswith("sess_state:"):
            session_id = data.split(":", 1)[1]
            session = self.manager.get(session_id)
            if not session:
                await query.edit_message_text("Сессия не найдена.")
                return True
            st = get_state(self.config.defaults.state_path, session.tool.name, session.workdir, session_id=session.id)
            if not st:
                await query.edit_message_text("Состояние не найдено.")
                return True
            summary = st.summary or "нет"
            header = (
                f"Session: {st.session_id or 'нет'}\n"
                f"Инструмент: {st.tool}\n"
                f"Каталог: {st.workdir}\n"
                f"Resume: {st.resume_token or 'нет'}\n"
                f"Summary: "
            )
            footer = f"\nUpdated: {self._format_ts(st.updated_at)}"
            max_summary = 4096 - len(header) - len(footer) - 4
            if len(summary) > max_summary:
                summary = summary[:max_summary] + " ..."
            text = header + summary + footer
            await query.edit_message_text(text)
            return True
        if data.startswith("sess_queue:"):
            session_id = data.split(":", 1)[1]
            session = self.manager.get(session_id)
            if not session:
                await query.edit_message_text("Сессия не найдена.")
                return True
            if not session.queue:
                await query.edit_message_text("Очередь пуста.")
                return True
            await query.edit_message_text(f"В очереди {len(session.queue)} сообщений.")
            return True
        if data.startswith("sess_clearqueue:"):
            session_id = data.split(":", 1)[1]
            session = self.manager.get(session_id)
            if not session:
                await query.edit_message_text("Сессия не найдена.")
                return True
            if not session.queue:
                await query.edit_message_text("Очередь пуста.")
                return True
            session.queue.clear()
            self.manager._persist_sessions()
            await query.edit_message_text("Очередь очищена.")
            return True
        if data.startswith("sess_close:"):
            session_id = data.split(":", 1)[1]
            if self._on_before_close:
                self._on_before_close(session_id, chat_id, context)
            ok = self.manager.close(session_id)
            if ok:
                if self._on_close:
                    self._on_close(session_id)
                await query.edit_message_text("Сессия закрыта и удалена из состояния.")
            else:
                await query.edit_message_text("Сессия не найдена.")
            return True
        if data == "sess_close_menu":
            await query.edit_message_text("Меню закрыто.")
            return True
        return False
