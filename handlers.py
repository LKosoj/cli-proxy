"""
Module containing command handlers for the Telegram bot.
"""

import logging
import os
import time
from dataclasses import dataclass
from typing import Dict, Optional

from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    ContextTypes,
)

from session import Session
from command_registry import build_command_registry
from state import get_state
from dirs_ui import build_dirs_keyboard, prepare_dirs
from utils import (
    format_session_label,
    is_within_root,
)


@dataclass
class PendingInput:
    session_id: str
    text: str
    dest: dict
    image_path: Optional[str] = None


def build_manager_menu(session: Session) -> tuple[str, InlineKeyboardMarkup]:
    """Build text and keyboard for /manager menu based on current session state."""
    enabled = bool(getattr(session, "manager_enabled", False))
    quiet_mode = bool(getattr(session, "manager_quiet_mode", False))
    quiet_status = "вкл" if quiet_mode else "выкл"
    quiet_icon = "🔇" if quiet_mode else "🔈"

    if enabled:
        keyboard = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("🔴 Выключить менеджера", callback_data="manager_set:off")],
                [InlineKeyboardButton(f"{quiet_icon} Тихий режим: {quiet_status}", callback_data="manager_quiet:toggle")],
                [InlineKeyboardButton("📋 Статус плана", callback_data="manager_status")],
                [InlineKeyboardButton("⏸ Приостановить", callback_data="manager_pause")],
                [InlineKeyboardButton("🗑 Сбросить план", callback_data="manager_reset")],
                [InlineKeyboardButton("❌ Отмена", callback_data="agent_cancel")],
            ]
        )
        text = f"🏗 Менеджер проекта\n\nРежим: включен\nТихий режим: {quiet_status}\n\nВыберите действие:"
    else:
        keyboard = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("🟢 Включить менеджера", callback_data="manager_set:on")],
                [InlineKeyboardButton("❌ Отмена", callback_data="agent_cancel")],
            ]
        )
        text = f"🏗 Менеджер проекта\n\nРежим: выключен\nТихий режим: {quiet_status}\n\nВключить?"
    return text, keyboard


class BotHandlers:
    """
    Class containing command handlers for the Telegram bot.
    """

    def __init__(self, bot_app):
        self.bot_app = bot_app

    def _preset_commands(self) -> Dict[str, str]:
        if self.bot_app.config.presets:
            return {p.name: p.prompt for p in self.bot_app.config.presets}
        return {
            "tests": "Запусти тесты и дай краткий отчёт.",
            "lint": "Запусти линтер/форматтер и дай краткий отчёт.",
            "build": "Запусти сборку и дай краткий отчёт.",
            "refactor": "Сделай небольшой рефакторинг по месту и объясни изменения.",
        }

    def _guess_clone_path(self, url: str, base: str) -> Optional[str]:
        u = url.strip()
        if not u:
            return None
        path = u
        if u.startswith("git@") and ":" in u:
            path = u.split(":", 1)[1]
        elif "://" in u:
            path = u.split("://", 1)[1]
            if "/" in path:
                path = path.split("/", 1)[1]
        name = path.rstrip("/").split("/")[-1]
        if name.endswith(".git"):
            name = name[:-4]
        if not name:
            return None
        return os.path.join(base, name)

    async def cmd_tools(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        chat_id = update.effective_chat.id
        if not self.bot_app.is_allowed(chat_id):
            return
        tools = sorted(self.bot_app._available_tools())
        if not tools:
            await self.bot_app._send_message(
                context,
                chat_id=chat_id,
                text=(
                    "CLI не найдены. Сначала установите нужные инструменты. "
                    f"Ожидаемые: {self.bot_app._expected_tools()}"
                ),
            )
            return
        await self.bot_app._send_message(context, chat_id=chat_id, text=f"Доступные инструменты: {', '.join(tools)}")

    async def cmd_new(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        chat_id = update.effective_chat.id
        if not self.bot_app.is_allowed(chat_id):
            return
        args = context.args
        if len(args) < 2:
            tools = list(sorted(self.bot_app._available_tools()))
            if not tools:
                await self.bot_app._send_message(
                    context,
                    chat_id=chat_id,
                    text=(
                        "CLI не найдены. Сначала установите нужные инструменты. "
                        f"Ожидаемые: {self.bot_app._expected_tools()}"
                    ),
                )
                return
            rows = [
                [InlineKeyboardButton(t, callback_data=f"new_tool:{t}")]
                for t in tools
            ]
            rows.append([InlineKeyboardButton("❌ Отмена", callback_data="agent_cancel")])
            keyboard = InlineKeyboardMarkup(rows)
            await self.bot_app._send_message(context,
                                             chat_id=chat_id,
                                             text="Выберите инструмент для новой сессии:",
                                             reply_markup=keyboard,
                                             )
            return
        tool, path = args[0], " ".join(args[1:])
        if tool not in self.bot_app.config.tools:
            await self.bot_app._send_message(context, chat_id=chat_id, text="Неизвестный инструмент.")
            return
        if not self.bot_app._is_tool_available(tool):
            await self.bot_app._send_message(
                context,
                chat_id=chat_id,
                text=(
                    "Инструмент не установлен. Сначала установите его. "
                    f"Ожидаемые: {self.bot_app._expected_tools()}"
                ),
            )
            return
        if not os.path.isdir(path):
            await self.bot_app._send_message(context, chat_id=chat_id, text="Каталог не существует.")
            return
        session = self.bot_app.manager.create(tool, path)
        await self.bot_app._send_message(context, chat_id=chat_id, text=f"Сессия {session.id} создана и выбрана.")

    async def cmd_newpath(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        chat_id = update.effective_chat.id
        if not self.bot_app.is_allowed(chat_id):
            return
        tool = self.bot_app.pending_new_tool.pop(chat_id, None)
        if not tool:
            await self.bot_app._send_message(context, chat_id=chat_id, text="Сначала выберите инструмент через /new.")
            return
        if not context.args:
            await self.bot_app._send_message(context, chat_id=chat_id, text="Использование: /newpath <path>")
            return
        path = " ".join(context.args)
        if not os.path.isdir(path):
            await self.bot_app._send_message(context, chat_id=chat_id, text="Каталог не существует.")
            return
        root = self.bot_app.dirs_root.get(chat_id, self.bot_app.config.defaults.workdir)
        if not is_within_root(path, root):
            await self.bot_app._send_message(context, chat_id=chat_id, text="Нельзя выйти за пределы корневого каталога.")
            return
        session = self.bot_app.manager.create(tool, path)
        await self.bot_app._send_message(context, chat_id=chat_id, text=f"Сессия {session.id} создана и выбрана.")

    async def cmd_sessions(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        chat_id = update.effective_chat.id
        if not self.bot_app.is_allowed(chat_id):
            return
        if not self.bot_app.manager.sessions:
            await self.bot_app._send_message(context, chat_id=chat_id, text="Активных сессий нет.")
            return
        keyboard = self.bot_app.session_ui.build_sessions_menu()
        await self.bot_app._send_message(
            context,
            chat_id=chat_id,
            text="Выберите сессию:",
            reply_markup=keyboard,
        )

    async def cmd_use(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        chat_id = update.effective_chat.id
        if not self.bot_app.is_allowed(chat_id):
            return
        if not context.args:
            items = list(self.bot_app.manager.sessions.keys())
            if not items:
                await self.bot_app._send_message(context, chat_id=chat_id, text="Сессий нет.")
                return
            self.bot_app.use_menu[chat_id] = items
            rows = []
            for i, sid in enumerate(items):
                m = self.bot_app.manager.get(sid)
                label = f"{sid}: {(m.name or (m.tool.name + ' @ ' + m.workdir))}"
                rows.append([InlineKeyboardButton(label, callback_data=f"use_pick:{i}")])
            rows.append([InlineKeyboardButton("❌ Отмена", callback_data="agent_cancel")])
            keyboard = InlineKeyboardMarkup(rows)
            await self.bot_app._send_message(context,
                                             chat_id=chat_id, text="Выберите сессию:", reply_markup=keyboard
                                             )
            return
        ok = self.bot_app.manager.set_active(context.args[0])
        if ok:
            s = self.bot_app.manager.get(context.args[0])
            await self.bot_app._send_message(context, chat_id=chat_id, text=format_session_label(s))
        else:
            await self.bot_app._send_message(context, chat_id=chat_id, text="Сессия не найдена.")

    async def cmd_close(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        chat_id = update.effective_chat.id
        if not self.bot_app.is_allowed(chat_id):
            return
        if not context.args:
            items = list(self.bot_app.manager.sessions.keys())
            if not items:
                await self.bot_app._send_message(context, chat_id=chat_id, text="Сессий нет.")
                return
            self.bot_app.close_menu[chat_id] = items
            rows = [
                [InlineKeyboardButton(sid, callback_data=f"close_pick:{i}")]
                for i, sid in enumerate(items)
            ]
            rows.append([InlineKeyboardButton("❌ Отмена", callback_data="agent_cancel")])
            keyboard = InlineKeyboardMarkup(rows)
            await self.bot_app._send_message(context,
                                             chat_id=chat_id, text="Выберите сессию для закрытия:", reply_markup=keyboard
                                             )
            return
        self.bot_app._interrupt_before_close(context.args[0], chat_id, context)
        ok = self.bot_app.manager.close(context.args[0])
        if ok:
            self.bot_app._clear_agent_session_cache(context.args[0])
            await self.bot_app._send_message(context, chat_id=chat_id, text="Сессия закрыта.")
        else:
            await self.bot_app._send_message(context, chat_id=chat_id, text="Сессия не найдена.")

    async def cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        chat_id = update.effective_chat.id
        if not self.bot_app.is_allowed(chat_id):
            return
        s = self.bot_app.manager.active()
        if not s:
            await self.bot_app._send_message(context, chat_id=chat_id, text="Активной сессии нет.")
            return
        now = time.time()
        busy_txt = "занята" if s.busy else "свободна"
        git_txt = "git: занято" if getattr(s, "git_busy", False) else "git: свободно"
        conflict_txt = ""
        if getattr(s, "git_conflict", False):
            conflict_txt = f" | конфликт: {s.git_conflict_kind or 'да'}"
        run_for = f"{int(now - s.started_at)}с" if s.started_at else "нет"
        last_out = f"{int(now - s.last_output_ts)}с назад" if s.last_output_ts else "нет"
        tick_txt = f"{int(now - s.last_tick_ts)}с назад" if s.last_tick_ts else "нет"
        agent_txt = "включен" if getattr(s, "agent_enabled", False) else "выключен"
        manager_txt = "включен" if getattr(s, "manager_enabled", False) else "выключен"
        project_root = getattr(s, "project_root", None)
        lines = [
            f"Активная сессия: {s.id} ({s.name or s.tool.name}) @ {s.workdir}",
            f"Статус: {busy_txt} | {git_txt}{conflict_txt} | В работе: {run_for} | Агент: {agent_txt} | Manager: {manager_txt}",
        ]
        if project_root:
            lines.append(f"Проект: {project_root}")
        lines.append(f"Последний вывод: {last_out} | Последний тик: {tick_txt} | Тиков: {s.tick_seen}")
        lines.append(f"Очередь: {len(s.queue)} | Resume: {'есть' if s.resume_token else 'нет'}")
        await self.bot_app._send_message(context, chat_id=chat_id, text="\\n".join(lines))

    async def cmd_agent(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        chat_id = update.effective_chat.id
        if not self.bot_app.is_allowed(chat_id):
            return
        s = self.bot_app.manager.active()
        if not s:
            await self.bot_app._send_message(context, chat_id=chat_id, text="Активной сессии нет.")
            return
        enabled = bool(getattr(s, "agent_enabled", False))
        project_root = getattr(s, "project_root", None)
        project_line = f"Проект: {project_root}" if project_root else "Проект: не подключен"
        if enabled:
            rows = [[InlineKeyboardButton("🔴 Выключить агента", callback_data="agent_set:off")]]
            if project_root:
                rows.append([InlineKeyboardButton("📂 Сменить проект", callback_data="agent_project_change")])
                rows.append([InlineKeyboardButton("🔌 Отключить проект", callback_data="agent_project_disconnect")])
            else:
                rows.append([InlineKeyboardButton("📂 Подключить проект", callback_data="agent_project_connect")])
            rows.append([InlineKeyboardButton("🧩 Плагины", callback_data="agent_plugin_commands")])
            rows.append([InlineKeyboardButton("🧹 Очистить песочницу", callback_data="agent_clean_all")])
            rows.append([InlineKeyboardButton("🧹 Очистить сессию", callback_data="agent_clean_session")])
            rows.append([InlineKeyboardButton("❌ Отмена", callback_data="agent_cancel")])
            keyboard = InlineKeyboardMarkup(rows)
            text = f"Агент сейчас включен.\n{project_line}\nВыберите действие:"
        else:
            keyboard = InlineKeyboardMarkup(
                [
                    [InlineKeyboardButton("🟢 Включить агента", callback_data="agent_set:on")],
                    [InlineKeyboardButton("❌ Отмена", callback_data="agent_cancel")],
                ]
            )
            text = f"Агент сейчас выключен.\n{project_line}\nВключить?"
        await self.bot_app._send_message(context, chat_id=chat_id, text=text, reply_markup=keyboard)

    async def cmd_manager(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        chat_id = update.effective_chat.id
        if not self.bot_app.is_allowed(chat_id):
            return
        s = self.bot_app.manager.active()
        if not s:
            await self.bot_app._send_message(context, chat_id=chat_id, text="Активной сессии нет.")
            return
        text, keyboard = build_manager_menu(s)
        await self.bot_app._send_message(context, chat_id=chat_id, text=text, reply_markup=keyboard)

    async def cmd_interrupt(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        chat_id = update.effective_chat.id
        if not self.bot_app.is_allowed(chat_id):
            return
        s = self.bot_app.manager.active()
        if not s:
            await self.bot_app._send_message(context, chat_id=chat_id, text="Активной сессии нет.")
            return
        s.interrupt()
        mtask = self.bot_app.manager_tasks.get(s.id)
        if mtask and not mtask.done():
            mtask.cancel()
        task = self.bot_app.agent_tasks.get(s.id)
        if task and not task.done():
            task.cancel()
        await self.bot_app._send_message(context, chat_id=chat_id, text="Прерывание отправлено.")

    async def cmd_queue(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        chat_id = update.effective_chat.id
        if not self.bot_app.is_allowed(chat_id):
            return
        s = self.bot_app.manager.active()
        if not s:
            await self.bot_app._send_message(context, chat_id=chat_id, text="Активной сессии нет.")
            return
        if not s.queue:
            await self.bot_app._send_message(context, chat_id=chat_id, text="Очередь пуста.")
            return
        await self.bot_app._send_message(context, chat_id=chat_id, text=f"В очереди {len(s.queue)} сообщений.")

    async def cmd_clearqueue(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        chat_id = update.effective_chat.id
        if not self.bot_app.is_allowed(chat_id):
            return
        s = self.bot_app.manager.active()
        if not s:
            await self.bot_app._send_message(context, chat_id=chat_id, text="Активной сессии нет.")
            return
        s.queue.clear()
        self.bot_app.manager._persist_sessions()
        await self.bot_app._send_message(context, chat_id=chat_id, text="Очередь очищена.")

    async def cmd_rename(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        chat_id = update.effective_chat.id
        if not self.bot_app.is_allowed(chat_id):
            return
        if not context.args:
            await self.bot_app._send_message(context, chat_id=chat_id, text="Использование: /rename <name> или /rename <id> <name>")
            return
        session = None
        if len(context.args) >= 2 and context.args[0] in self.bot_app.manager.sessions:
            session = self.bot_app.manager.get(context.args[0])
            name = " ".join(context.args[1:])
        else:
            session = self.bot_app.manager.active()
            name = " ".join(context.args)
        if not session:
            await self.bot_app._send_message(context, chat_id=chat_id, text="Активной сессии нет.")
            return
        session.name = name.strip()
        self.bot_app.manager._persist_sessions()
        await self.bot_app._send_message(context, chat_id=chat_id, text="Имя сессии обновлено.")

    async def cmd_dirs(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        chat_id = update.effective_chat.id
        if not self.bot_app.is_allowed(chat_id):
            return
        path = " ".join(context.args) if context.args else self.bot_app.config.defaults.workdir
        if not os.path.isdir(path):
            await self.bot_app._send_message(context, chat_id=chat_id, text="Каталог не существует.")
            return
        self.bot_app.dirs_root[chat_id] = path
        self.bot_app.dirs_mode[chat_id] = "browse"
        await self.bot_app._send_dirs_menu(chat_id, context, path)

    async def cmd_cwd(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        chat_id = update.effective_chat.id
        if not self.bot_app.is_allowed(chat_id):
            return
        if not context.args:
            await self.bot_app._send_message(context, chat_id=chat_id, text="Использование: /cwd <path>")
            return
        path = " ".join(context.args)
        if not os.path.isdir(path):
            await self.bot_app._send_message(context, chat_id=chat_id, text="Каталог не существует.")
            return
        s = self.bot_app.manager.active()
        if not s:
            await self.bot_app._send_message(context, chat_id=chat_id, text="Активной сессии нет.")
            return
        session = self.bot_app.manager.create(s.tool.name, path)
        await self.bot_app._send_message(context, chat_id=chat_id, text=f"Новая сессия {session.id} создана и выбрана.")

    async def cmd_git(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        chat_id = update.effective_chat.id
        if not self.bot_app.is_allowed(chat_id):
            return
        session = await self.bot_app.git.ensure_git_session(chat_id, context)
        if not session:
            return
        if not await self.bot_app.git.ensure_git_repo(session, chat_id, context):
            return
        await self.bot_app._send_message(
            context,
            chat_id=chat_id,
            text="Git-операции:",
            reply_markup=self.bot_app.git.build_git_keyboard(),
        )

    async def cmd_setprompt(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        chat_id = update.effective_chat.id
        if not self.bot_app.is_allowed(chat_id):
            return
        args = context.args
        if len(args) < 2:
            await self.bot_app._send_message(context, chat_id=chat_id, text="Использование: /setprompt <tool> <regex>")
            return
        tool_name = args[0]
        regex = " ".join(args[1:])
        tool = self.bot_app.config.tools.get(tool_name)
        if not tool:
            await self.bot_app._send_message(context, chat_id=chat_id, text="Инструмент не найден.")
            return
        tool.prompt_regex = regex
        from config import save_config

        save_config(self.bot_app.config)
        await self.bot_app._send_message(context, chat_id=chat_id, text="prompt_regex сохранен.")

    async def cmd_resume(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        chat_id = update.effective_chat.id
        if not self.bot_app.is_allowed(chat_id):
            return
        s = self.bot_app.manager.active()
        if not s:
            await self.bot_app._send_message(context, chat_id=chat_id, text="Активной сессии нет.")
            return
        if not context.args:
            token = s.resume_token or "нет"
            await self.bot_app._send_message(context, chat_id=chat_id, text=f"Текущий resume: {token}")
            return
        token = " ".join(context.args).strip()
        s.resume_token = token
        self.bot_app.manager._persist_sessions()
        await self.bot_app._send_message(context, chat_id=chat_id, text="Resume сохранен.")

    async def cmd_state(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        chat_id = update.effective_chat.id
        if not self.bot_app.is_allowed(chat_id):
            return
        s = self.bot_app.manager.active()
        if context.args:
            # Prefer session_id to avoid ambiguity when multiple sessions share tool/workdir.
            st = None
            sid = context.args[0]
            if sid in self.bot_app.manager.sessions:
                s0 = self.bot_app.manager.get(sid)
                if s0:
                    st = get_state(self.bot_app.config.defaults.state_path, s0.tool.name, s0.workdir, session_id=s0.id)
            if not st and len(context.args) >= 2:
                tool = context.args[0]
                workdir = " ".join(context.args[1:])
                st = get_state(self.bot_app.config.defaults.state_path, tool, workdir)
            if not st:
                await self.bot_app._send_message(
                    context, chat_id=chat_id,
                    text="Состояние не найдено (используйте /state <session_id> или /state <tool> <workdir>)",
                )
                return
            text = (
                f"Session: {st.session_id or 'нет'}\\n"
                f"Tool: {st.tool}\\n"
                f"Workdir: {st.workdir}\\n"
                f"Resume: {st.resume_token or 'нет'}\\n"
                f"Name: {st.name or 'нет'}\\n"
                f"Summary: {st.summary or 'нет'}\\n"
                f"Updated: {self.bot_app._format_ts(st.updated_at)}"
            )
            await self.bot_app._send_message(context, chat_id=chat_id, text=text)
            return
        if not s:
            await self.bot_app._send_message(context, chat_id=chat_id, text="Активной сессии нет.")
            return
        try:
            from state import load_state

            data = load_state(self.bot_app.config.defaults.state_path)
        except Exception as e:
            logging.exception(f"tool failed {str(e)}")
            await self.bot_app._send_message(context, chat_id=chat_id, text=f"Ошибка чтения состояния: {e}")
            return
        if not data:
            await self.bot_app._send_message(context, chat_id=chat_id, text="Состояние не найдено.")
            return
        keys = list(data.keys())
        self.bot_app.state_menu[chat_id] = keys
        self.bot_app.state_menu_page[chat_id] = 0
        keyboard = self.bot_app._build_state_keyboard(chat_id)
        await self.bot_app._send_message(context,
                                         chat_id=chat_id,
                                         text="Выберите запись состояния:",
                                         reply_markup=keyboard,
                                         )

    async def cmd_send(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        chat_id = update.effective_chat.id
        if not self.bot_app.is_allowed(chat_id):
            return
        if not context.args:
            await self.bot_app._send_message(context, chat_id=chat_id, text="Использование: /send <текст>")
            return
        session = await self.bot_app.ensure_active_session(chat_id, context)
        if not session:
            return
        text = " ".join(context.args)
        await self.bot_app._handle_cli_input(session, text, chat_id, context)

    def _bot_commands(self) -> list[BotCommand]:
        commands = []
        for entry in build_command_registry(self.bot_app):
            if not entry["menu"]:
                continue
            commands.append(BotCommand(command=entry["name"], description=str(entry["desc"])))
        return commands

    async def set_bot_commands(self, app: Application) -> None:
        await app.bot.set_my_commands(self._bot_commands())

    async def cmd_toolhelp(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        chat_id = update.effective_chat.id
        if not self.bot_app.is_allowed(chat_id):
            return
        tools = list(sorted(self.bot_app._available_tools()))
        if not tools:
            await self.bot_app._send_message(
                context,
                chat_id=chat_id,
                text=(
                    "CLI не найдены. Сначала установите нужные инструменты. "
                    f"Ожидаемые: {self.bot_app._expected_tools()}"
                ),
            )
            return
        self.bot_app.toolhelp_menu[chat_id] = tools
        rows = [
            [InlineKeyboardButton(t, callback_data=f"toolhelp_pick:{t}")]
            for t in tools
        ]
        rows.append([InlineKeyboardButton("❌ Отмена", callback_data="agent_cancel")])
        keyboard = InlineKeyboardMarkup(rows)
        await self.bot_app._send_message(
            context,
            chat_id=chat_id,
            text="Выберите инструмент для просмотра /команд:",
            reply_markup=keyboard,
        )

    async def _send_toolhelp_content(self, chat_id: int, context: ContextTypes.DEFAULT_TYPE, content: str) -> None:
        await self.bot_app._send_message(context, chat_id=chat_id, text=content)

    async def cmd_files(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        chat_id = update.effective_chat.id
        if not self.bot_app.is_allowed(chat_id):
            return
        session = await self.bot_app.ensure_active_session(chat_id, context)
        if not session:
            return
        base = session.workdir
        if not os.path.isdir(base):
            await self.bot_app._send_message(context, chat_id=chat_id, text="Рабочий каталог недоступен.")
            return
        self.bot_app.files_dir[chat_id] = base
        self.bot_app.files_page[chat_id] = 0
        await self.bot_app._send_files_menu(chat_id, session, context, edit_message=None)

    def _list_dir_entries(self, base: str) -> list[dict]:
        entries: list[dict] = []
        try:
            for name in os.listdir(base):
                path = os.path.join(base, name)
                try:
                    is_dir = os.path.isdir(path)
                except Exception:
                    continue
                entries.append({"name": name, "path": path, "is_dir": is_dir})
        except Exception:
            return []
        entries.sort(key=lambda e: (not e["is_dir"], e["name"].lower()))
        return entries

    async def _send_dirs_menu(self, chat_id: int, context: ContextTypes.DEFAULT_TYPE, base: str) -> None:
        err = prepare_dirs(
            self.bot_app.dirs_menu,
            self.bot_app.dirs_base,
            self.bot_app.dirs_page,
            self.bot_app.dirs_root,
            chat_id,
            base,
        )
        if err:
            mode = self.bot_app.dirs_mode.get(chat_id)
            if mode == "new_session":
                self.bot_app.pending_new_tool.pop(chat_id, None)
            if mode == "git_clone":
                self.bot_app.pending_git_clone.pop(chat_id, None)
            self.bot_app.dirs_mode.pop(chat_id, None)
            self.bot_app.dirs_menu.pop(chat_id, None)
            await self.bot_app._send_message(context, chat_id=chat_id, text=err)
            return
        keyboard = build_dirs_keyboard(
            self.bot_app.dirs_menu,
            self.bot_app.dirs_base,
            self.bot_app.dirs_page,
            self.bot_app._short_label,
            chat_id,
            base,
            0,
        )
        await self.bot_app._send_message(
            context,
            chat_id=chat_id,
            text="Выберите каталог:",
            reply_markup=keyboard,
        )

    async def _send_files_menu(
        self,
        chat_id: int,
        session: Session,
        context: ContextTypes.DEFAULT_TYPE,
        edit_message: Optional[object],
    ) -> None:
        base = self.bot_app.files_dir.get(chat_id, session.workdir)
        if not os.path.isdir(base):
            base = session.workdir
            self.bot_app.files_dir[chat_id] = base
            self.bot_app.files_page[chat_id] = 0
        entries = self._list_dir_entries(base)
        self.bot_app.files_entries[chat_id] = entries
        page = max(0, self.bot_app.files_page.get(chat_id, 0))
        page_size = 20
        start = page * page_size
        end = start + page_size
        page_entries = entries[start:end]
        total_pages = max(1, (len(entries) + page_size - 1) // page_size)
        if page >= total_pages:
            page = max(0, total_pages - 1)
            self.bot_app.files_page[chat_id] = page
            start = page * page_size
            end = start + page_size
            page_entries = entries[start:end]
        rows = []
        for idx, entry in enumerate(page_entries, start=start):
            if entry["is_dir"]:
                open_cb = f"file_nav:open:{idx}"
                label = f"📁 {entry['name']}"
            else:
                open_cb = f"file_pick:{idx}"
                label = f"📄 {entry['name']}"
            rows.append(
                [
                    InlineKeyboardButton(self.bot_app._short_label(label, 60), callback_data=open_cb),
                    InlineKeyboardButton("🗑", callback_data=f"file_del:{idx}"),
                ]
            )
        nav_row = []
        nav_row.append(InlineKeyboardButton("⬅️ вверх", callback_data="file_nav:up"))
        if page > 0:
            nav_row.append(InlineKeyboardButton("◀️", callback_data="file_nav:prev"))
        if page < total_pages - 1:
            nav_row.append(InlineKeyboardButton("▶️", callback_data="file_nav:next"))
        if nav_row:
            rows.append(nav_row)
        if os.path.abspath(base) != os.path.abspath(session.workdir):
            rows.append([InlineKeyboardButton("🗑 Удалить эту папку", callback_data="file_del_current")])
        rows.append([InlineKeyboardButton("❌ Отмена", callback_data="file_nav:cancel")])
        text = f"Каталог: {base}\nСтраница {page + 1}/{total_pages}"
        keyboard = InlineKeyboardMarkup(rows)
        if edit_message:
            await edit_message.edit_message_text(text, reply_markup=keyboard)
        else:
            await self.bot_app._send_message(context, chat_id=chat_id, text=text, reply_markup=keyboard)

    async def cmd_preset(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        chat_id = update.effective_chat.id
        if not self.bot_app.is_allowed(chat_id):
            return
        presets = self._preset_commands()
        keyboard = InlineKeyboardMarkup(
            [[InlineKeyboardButton(k, callback_data=f"preset_run:{k}")] for k in presets.keys()]
            + [[InlineKeyboardButton("❌ Отмена", callback_data="preset_run:cancel")]]
        )
        await self.bot_app._send_message(context, chat_id=chat_id, text="Выберите шаблон:", reply_markup=keyboard)

    async def cmd_metrics(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        chat_id = update.effective_chat.id
        if not self.bot_app.is_allowed(chat_id):
            return
        await self.bot_app._send_message(context, chat_id=chat_id, text=self.bot_app.metrics.snapshot())
