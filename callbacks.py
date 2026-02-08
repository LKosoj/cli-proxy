"""
Module containing callback handling functionality for the Telegram bot.
"""

import asyncio
import logging
import os
import shutil

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from session import run_tool_help
from handlers import build_manager_menu
from dirs_ui import build_dirs_keyboard, prepare_dirs
from state import load_active_state, clear_active_state
from toolhelp import get_toolhelp, update_toolhelp
from utils import (
    format_session_label,
    is_within_root,
)
from agent import execute_shell_command, pop_pending_command
from agent.manager import MANAGER_CONTINUE_TOKEN, format_manager_status


class CallbackHandler:
    """
    Class containing callback handling functionality for the Telegram bot.
    """

    def __init__(self, bot_app):
        self.bot_app = bot_app

    async def _edit_msg(self, context, query, text):
        """Shortcut: edit the callback query message with given text."""
        if query.message:
            await self.bot_app._edit_message(
                context,
                chat_id=query.message.chat_id,
                message_id=query.message.message_id,
                text=text,
            )

    async def handle_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        try:
            await query.answer()
        except Exception as e:
            logging.exception(f"Ошибка ответа на callback: {e}")
        chat_id = query.message.chat_id if query.message else None
        if not chat_id:
            return
        try:
            if not self.bot_app.is_allowed(chat_id):
                return
            self.bot_app.context_by_chat[chat_id] = context
            if query.data.startswith("approve_cmd:"):
                cmd_id = query.data.split(":", 1)[1]
                pending = pop_pending_command(cmd_id)
                if not pending:
                    await query.edit_message_text("Запрос уже обработан.")
                    return
                await query.edit_message_text("Одобрено. Выполняю команду...")
                result = await execute_shell_command(pending.command, pending.cwd)
                output = result.get("output") if result.get("success") else result.get("error")
                await self.bot_app._send_message(context, chat_id=chat_id, text=output or "(пустой вывод)")
                return
            if query.data.startswith("deny_cmd:"):
                cmd_id = query.data.split(":", 1)[1]
                pop_pending_command(cmd_id)
                await query.edit_message_text("Команда отклонена.")
                return
            if query.data.startswith("ask:"):
                _, question_id, idx_str = query.data.split(":", 2)
                pending = self.bot_app.pending_questions.get(question_id)
                if not pending:
                    await query.edit_message_text("Вопрос устарел.")
                    return
                options = pending.get("options") or []
                try:
                    idx = int(idx_str)
                except ValueError:
                    await query.edit_message_text("Некорректный выбор.")
                    return
                if idx < 0 or idx >= len(options):
                    await query.edit_message_text("Выбор недоступен.")
                    return
                answer = options[idx]
                resolved = self.bot_app.agent.resolve_question(question_id, answer)
                self.bot_app.pending_questions.pop(question_id, None)
                if not resolved:
                    await query.edit_message_text("Ответ уже получен.")
                    return
                await query.edit_message_text(f"Вы выбрали: {answer}")
                return
            if query.data.startswith("agent_set:"):
                session = self.bot_app.manager.active()
                if not session:
                    await query.edit_message_text("Активной сессии нет.")
                    return
                mode = query.data.split(":", 1)[1]
                session.agent_enabled = mode == "on"
                if session.agent_enabled:
                    # manager and agent are mutually exclusive
                    session.manager_enabled = False
                try:
                    self.bot_app.manager._persist_sessions()
                except Exception:
                    pass
                # When the agent is turned off, cancel any pending plugin
                # dialogs so that on_message doesn't silently swallow text.
                if not session.agent_enabled:
                    cb_chat_id = query.message.chat_id if query.message else None
                    if cb_chat_id:
                        self.bot_app._cancel_plugin_dialogs(cb_chat_id)
                status = "включен" if session.agent_enabled else "выключен"
                await query.edit_message_text(f"Агент {status}.")
                return
            if query.data.startswith("manager_set:"):
                session = self.bot_app.manager.active()
                if not session:
                    await query.edit_message_text("Активной сессии нет.")
                    return
                mode = query.data.split(":", 1)[1]
                if mode == "on":
                    # Preconditions check (TZ section 16)
                    if not self.bot_app.config.defaults.openai_api_key or not self.bot_app.config.defaults.openai_model:
                        if query.message:
                            await self.bot_app._edit_message(
                                context,
                                chat_id=query.message.chat_id,
                                message_id=query.message.message_id,
                                text="Для работы Manager нужен OpenAI API. Настройте openai_api_key и openai_model в config.yaml.",
                            )
                        return
                    if not session or not os.path.isdir(session.workdir):
                        if query.message:
                            await self.bot_app._edit_message(
                                context,
                                chat_id=query.message.chat_id,
                                message_id=query.message.message_id,
                                text="Сначала создайте сессию через /new.",
                            )
                        return
                session.manager_enabled = mode == "on"
                if session.manager_enabled:
                    session.agent_enabled = False
                try:
                    self.bot_app.manager._persist_sessions()
                except Exception:
                    pass
                # When manager is turned off, cancel running manager tasks.
                if not session.manager_enabled:
                    task = self.bot_app.manager_tasks.get(session.id)
                    if task and not task.done():
                        task.cancel()
                text, keyboard = build_manager_menu(session)
                await query.edit_message_text(text=text, reply_markup=keyboard)
                return
            if query.data.startswith("manager_quiet:"):
                session = self.bot_app.manager.active()
                if not session:
                    await query.edit_message_text("Активной сессии нет.")
                    return
                mode = query.data.split(":", 1)[1]
                current = bool(getattr(session, "manager_quiet_mode", False))
                if mode == "on":
                    session.manager_quiet_mode = True
                elif mode == "off":
                    session.manager_quiet_mode = False
                elif mode == "toggle":
                    session.manager_quiet_mode = not current
                else:
                    await query.edit_message_text("Некорректный режим тихого режима.")
                    return
                try:
                    self.bot_app.manager._persist_sessions()
                except Exception:
                    logging.exception("Не удалось сохранить manager_quiet_mode.")
                text, keyboard = build_manager_menu(session)
                await query.edit_message_text(text=text, reply_markup=keyboard)
                return
            if query.data == "manager_resume:continue":
                session = self.bot_app.manager.active()
                if not session:
                    await self._edit_msg(context, query, "Активной сессии нет.")
                    return
                pending = self.bot_app.manager_resume_pending.pop(session.id, None)
                if not pending:
                    await self._edit_msg(context, query, "Выбор устарел.")
                    return
                await self._edit_msg(context, query, "Продолжаю текущий план...")
                self.bot_app._start_manager_task(
                    session, MANAGER_CONTINUE_TOKEN,
                    pending.get("dest") or {"kind": "telegram"}, context,
                )
                return
            if query.data == "manager_resume:new":
                session = self.bot_app.manager.active()
                if not session:
                    await self._edit_msg(context, query, "Активной сессии нет.")
                    return
                pending = self.bot_app.manager_resume_pending.pop(session.id, None)
                if not pending:
                    await self._edit_msg(context, query, "Выбор устарел.")
                    return
                try:
                    self.bot_app.manager_orchestrator.reset(session)
                except Exception as e:
                    logging.exception(f"tool failed {str(e)}")
                await self._edit_msg(context, query, "Начинаю новый план...")
                self.bot_app._start_manager_task(
                    session, str(pending.get("prompt") or ""),
                    pending.get("dest") or {"kind": "telegram"}, context,
                )
                return
            if query.data == "manager_failed:retry":
                session = self.bot_app.manager.active()
                if not session:
                    await self._edit_msg(context, query, "Активной сессии нет.")
                    return
                await self._edit_msg(context, query, "🔄 Повторяю выполнение плана...")
                from agent.manager import MANAGER_CONTINUE_TOKEN
                dest = {"kind": "telegram", "chat_id": query.message.chat_id if query.message else chat_id}
                self.bot_app._start_manager_task(session, MANAGER_CONTINUE_TOKEN, dest, context)
                return
            if query.data == "manager_failed:archive":
                session = self.bot_app.manager.active()
                if not session:
                    await self._edit_msg(context, query, "Активной сессии нет.")
                    return
                from agent.manager_store import archive_plan
                archive_plan(session.workdir, "failed")
                await self._edit_msg(context, query, "📦 План перенесён в архив.")
                return
            if query.data == "manager_pause":
                session = self.bot_app.manager.active()
                if not session:
                    await self._edit_msg(context, query, "Активной сессии нет.")
                    return
                try:
                    self.bot_app.manager_orchestrator.pause(session)
                except Exception as e:
                    logging.exception(f"tool failed {str(e)}")
                await self._edit_msg(context, query, "План приостановлен.")
                return
            if query.data == "manager_reset":
                session = self.bot_app.manager.active()
                if not session:
                    await self._edit_msg(context, query, "Активной сессии нет.")
                    return
                try:
                    self.bot_app.manager_orchestrator.reset(session)
                except Exception as e:
                    logging.exception(f"tool failed {str(e)}")
                await self._edit_msg(context, query, "План сброшен.")
                return
            if query.data == "manager_status":
                session = self.bot_app.manager.active()
                if not session:
                    await self._edit_msg(context, query, "Активной сессии нет.")
                    return
                try:
                    from agent.manager_store import load_plan

                    plan = load_plan(session.workdir)
                except Exception:
                    plan = None
                if not plan:
                    await self._edit_msg(context, query, "План не найден.")
                    return
                text = format_manager_status(plan)
                await self._edit_msg(context, query, text)
                return
            if query.data in ("agent_project_connect", "agent_project_change"):
                session = self.bot_app.manager.active()
                if not session:
                    await query.edit_message_text("Активной сессии нет.")
                    return
                self.bot_app.pending_agent_project[chat_id] = session.id
                self.bot_app.dirs_root[chat_id] = self.bot_app.config.defaults.workdir
                self.bot_app.dirs_mode[chat_id] = "agent_project"
                await query.edit_message_text("Выберите каталог проекта.")
                await self.bot_app._send_dirs_menu(chat_id, context, self.bot_app.config.defaults.workdir)
                return
            if query.data == "agent_project_disconnect":
                session = self.bot_app.manager.active()
                if not session:
                    await query.edit_message_text("Активной сессии нет.")
                    return
                ok, msg = self.bot_app._set_agent_project_root(session, chat_id, context, None)
                await query.edit_message_text(msg if ok else "Не удалось отключить проект.")
                return
            if query.data == "agent_cancel":
                await query.edit_message_text("Отменено.")
                return
            if query.data == "agent_clean_all":
                session = self.bot_app.manager.active()
                if session:
                    self.bot_app._interrupt_before_close(session.id, chat_id, context)
                    self.bot_app._clear_agent_session_cache(session.id)
                removed, errors = self.bot_app._clear_agent_sandbox()
                msg = f"Песочница очищена. Удалено: {removed}."
                if errors:
                    msg += f" Ошибок: {errors}."
                await query.edit_message_text(msg)
                return
            if query.data == "agent_clean_session":
                session = self.bot_app.manager.active()
                if not session:
                    await query.edit_message_text("Активной сессии нет.")
                    return
                self.bot_app._interrupt_before_close(session.id, chat_id, context)
                self.bot_app._clear_agent_session_cache(session.id)
                ok = self.bot_app._clear_agent_session_files(session.id)
                msg = "Файлы текущей сессии удалены." if ok else "Не удалось очистить файлы сессии."
                await query.edit_message_text(msg)
                return
            if query.data == "agent_plugin_commands":
                session = self.bot_app.manager.active()
                if not session or not getattr(session, "agent_enabled", False):
                    await query.edit_message_text("Агент не активен.")
                    return
                try:
                    from agent.profiles import build_default_profile
                    tool_registry = getattr(self.bot_app, "_tool_registry", None)
                    if tool_registry is None:
                        await query.edit_message_text("Реестр инструментов недоступен.")
                        return
                    profile = build_default_profile(self.bot_app.config, tool_registry)
                    commands = self.bot_app.agent.get_plugin_commands(profile)
                    plugin_menu = commands.get("plugin_menu") or []
                    if not plugin_menu:
                        await query.edit_message_text("Нет доступных плагинов.")
                        return
                    rows = [
                        [InlineKeyboardButton(entry["label"], callback_data=f"agent_plugin:{entry['plugin_id']}")]
                        for entry in plugin_menu
                    ]
                    rows.append([InlineKeyboardButton("⬅️ Назад", callback_data="agent_cancel")])
                    await query.edit_message_text("Плагины:", reply_markup=InlineKeyboardMarkup(rows))
                except Exception as e:
                    logging.exception(f"tool failed {str(e)}")
                    await query.edit_message_text("Не удалось получить список плагинов.")
                return
            if query.data.startswith("agent_plugin:"):
                pid = query.data.split(":", 1)[1]
                session = self.bot_app.manager.active()
                if not session or not getattr(session, "agent_enabled", False):
                    await query.edit_message_text("Агент не активен.")
                    return
                try:
                    from agent.profiles import build_default_profile
                    tool_registry = getattr(self.bot_app, "_tool_registry", None)
                    if tool_registry is None:
                        await query.edit_message_text("Реестр инструментов недоступен.")
                        return
                    profile = build_default_profile(self.bot_app.config, tool_registry)
                    commands = self.bot_app.agent.get_plugin_commands(profile)
                    plugin_menu = commands.get("plugin_menu") or []
                    entry = next((e for e in plugin_menu if e["plugin_id"] == pid), None)
                    if not entry:
                        await query.edit_message_text("Плагин недоступен.")
                        return
                    plugin = entry.get("plugin")
                    actions = entry.get("actions") or []
                    rows = []
                    for act in actions:
                        if plugin and hasattr(plugin, "action_button"):
                            btn = plugin.action_button(act["label"], act["action"])
                        else:
                            btn = InlineKeyboardButton(act["label"], callback_data=f"cb:{pid}:{act['action']}")
                        rows.append([btn])
                    rows.append([InlineKeyboardButton("⬅️ Назад к плагинам", callback_data="agent_plugin_commands")])
                    label = entry.get("label", pid)
                    await query.edit_message_text(f"{label}:", reply_markup=InlineKeyboardMarkup(rows))
                except Exception as e:
                    logging.exception(f"tool failed {str(e)}")
                    await query.edit_message_text("Ошибка при загрузке плагина.")
                return
            if query.data.startswith("state_pick:"):
                idx = int(query.data.split(":", 1)[1])
                keys = self.bot_app.state_menu.get(chat_id, [])
                if idx < 0 or idx >= len(keys):
                    await query.edit_message_text("Выбор недоступен.")
                    return
                from state import load_state

                data = load_state(self.bot_app.config.defaults.state_path)
                key = keys[idx]
                st = data.get(key)
                if not st:
                    await query.edit_message_text("Состояние не найдено.")
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
                await query.edit_message_text(text)
                return
            if query.data.startswith("state_page:"):
                page = int(query.data.split(":", 1)[1])
                keys = self.bot_app.state_menu.get(chat_id, [])
                if not keys:
                    await query.edit_message_text("Состояние не найдено.")
                    return
                self.bot_app.state_menu_page[chat_id] = page
                await query.edit_message_text(
                    "Выберите запись состояния:",
                    reply_markup=self.bot_app._build_state_keyboard(chat_id),
                )
                return
        except Exception as e:
            logging.exception(f"Ошибка обработки кнопки: {e}")
            await self.bot_app._send_message(context, chat_id=chat_id, text=f"Ошибка обработки кнопки: {e}")
            return
        if query.data.startswith("use_pick:"):
            idx = int(query.data.split(":", 1)[1])
            items = self.bot_app.use_menu.get(chat_id, [])
            if idx < 0 or idx >= len(items):
                await query.edit_message_text("Выбор недоступен.")
                return
            sid = items[idx]
            ok = self.bot_app.manager.set_active(sid)
            if ok:
                s = self.bot_app.manager.get(sid)
                await query.edit_message_text(format_session_label(s))
            else:
                await query.edit_message_text("Сессия не найдена.")
            return
        if query.data.startswith("close_pick:"):
            idx = int(query.data.split(":", 1)[1])
            items = self.bot_app.close_menu.get(chat_id, [])
            if idx < 0 or idx >= len(items):
                await query.edit_message_text("Выбор недоступен.")
                return
            sid = items[idx]
            self.bot_app._interrupt_before_close(sid, chat_id, context)
            ok = self.bot_app.manager.close(sid)
            if ok:
                self.bot_app._clear_agent_session_cache(sid)
                await query.edit_message_text("Сессия закрыта.")
            else:
                await query.edit_message_text("Сессия не найдена.")
            return
        if query.data.startswith("new_tool:"):
            tool = query.data.split(":", 1)[1]
            if tool not in self.bot_app.config.tools:
                await query.edit_message_text("Инструмент не найден.")
                return
            if not self.bot_app._is_tool_available(tool):
                await query.edit_message_text(
                    "Инструмент не установлен. Сначала установите его. "
                    f"Ожидаемые: {self.bot_app._expected_tools()}"
                )
                return
            self.bot_app.pending_new_tool[chat_id] = tool
            await query.edit_message_text(f"Выбран инструмент {tool}. Выберите каталог.")
            self.bot_app.dirs_root[chat_id] = self.bot_app.config.defaults.workdir
            self.bot_app.dirs_mode[chat_id] = "new_session"
            await self.bot_app._send_dirs_menu(chat_id, context, self.bot_app.config.defaults.workdir)
            return
        if query.data.startswith("dir_pick:"):
            idx = int(query.data.split(":", 1)[1])
            items = self.bot_app.dirs_menu.get(chat_id, [])
            if idx < 0 or idx >= len(items):
                await query.edit_message_text("Выбор недоступен.")
                return
            path = items[idx]
            mode = self.bot_app.dirs_mode.get(chat_id, "new_session")
            if mode == "git_clone":
                self.bot_app.pending_git_clone[chat_id] = path
                await query.edit_message_text("Отправьте ссылку для git clone.")
                return
            if mode == "agent_project":
                session_id = self.bot_app.pending_agent_project.pop(chat_id, None)
                session = self.bot_app.manager.get(session_id) if session_id else None
                if not session:
                    await query.edit_message_text("Активная сессия не найдена.")
                    return
                ok, msg = self.bot_app._set_agent_project_root(session, chat_id, context, path)
                self.bot_app.dirs_mode.pop(chat_id, None)
                await query.edit_message_text(msg if ok else "Не удалось подключить проект.")
                return
            tool = self.bot_app.pending_new_tool.pop(chat_id, None)
            if not tool:
                await query.edit_message_text("Инструмент не выбран.")
                return
            session = self.bot_app.manager.create(tool, path)
            await query.edit_message_text(f"Сессия {session.id} создана и выбрана.")
            return
        if query.data.startswith("dir_page:"):
            base = self.bot_app.dirs_base.get(chat_id, self.bot_app.config.defaults.workdir)
            page = int(query.data.split(":", 1)[1])
            await query.edit_message_text(
                "Выберите каталог:",
                reply_markup=build_dirs_keyboard(
                    self.bot_app.dirs_menu,
                    self.bot_app.dirs_base,
                    self.bot_app.dirs_page,
                    self.bot_app._short_label,
                    chat_id,
                    base,
                    page,
                ),
            )
            return
        if query.data == "dir_up":
            base = self.bot_app.dirs_base.get(chat_id, self.bot_app.config.defaults.workdir)
            parent = os.path.dirname(base.rstrip(os.sep)) or base
            root = self.bot_app.dirs_root.get(chat_id, self.bot_app.config.defaults.workdir)
            if not is_within_root(parent, root):
                await query.edit_message_text("Нельзя выйти за пределы корневого каталога.")
                return
            err = prepare_dirs(
                self.bot_app.dirs_menu,
                self.bot_app.dirs_base,
                self.bot_app.dirs_page,
                self.bot_app.dirs_root,
                chat_id,
                parent,
            )
            if err:
                await query.edit_message_text(err)
                return
            await query.edit_message_text(
                "Выберите каталог:",
                reply_markup=build_dirs_keyboard(
                    self.bot_app.dirs_menu,
                    self.bot_app.dirs_base,
                    self.bot_app.dirs_page,
                    self.bot_app._short_label,
                    chat_id,
                    parent,
                    0,
                ),
            )
            return
        if query.data == "dir_enter":
            self.bot_app.pending_dir_input[chat_id] = True
            await query.edit_message_text("Отправьте путь к каталогу сообщением.")
            return
        if query.data == "dir_create":
            base = self.bot_app.dirs_base.get(chat_id, self.bot_app.config.defaults.workdir)
            self.bot_app.pending_dir_create[chat_id] = base
            await query.edit_message_text(
                "Отправьте имя нового каталога или путь относительно текущего. Для отмены введите '-'."
            )
            return
        if query.data == "dir_git_clone":
            base = self.bot_app.dirs_base.get(chat_id, self.bot_app.config.defaults.workdir)
            self.bot_app.pending_git_clone[chat_id] = base
            await query.edit_message_text("Отправьте ссылку для git clone.")
            return
        if query.data == "dir_use_current":
            base = self.bot_app.dirs_base.get(chat_id, self.bot_app.config.defaults.workdir)
            root = self.bot_app.dirs_root.get(chat_id, self.bot_app.config.defaults.workdir)
            if not is_within_root(base, root):
                await query.edit_message_text("Нельзя выйти за пределы корневого каталога.")
                return
            mode = self.bot_app.dirs_mode.get(chat_id, "new_session")
            if mode == "git_clone":
                self.bot_app.pending_git_clone[chat_id] = base
                await query.edit_message_text("Отправьте ссылку для git clone.")
                return
            if mode == "agent_project":
                session_id = self.bot_app.pending_agent_project.pop(chat_id, None)
                session = self.bot_app.manager.get(session_id) if session_id else None
                if not session:
                    await query.edit_message_text("Активная сессия не найдена.")
                    return
                ok, msg = self.bot_app._set_agent_project_root(session, chat_id, context, base)
                self.bot_app.dirs_mode.pop(chat_id, None)
                await query.edit_message_text(msg if ok else "Не удалось подключить проект.")
                return
            tool = self.bot_app.pending_new_tool.pop(chat_id, None)
            if not tool:
                await query.edit_message_text("Инструмент не выбран.")
                return
            session = self.bot_app.manager.create(tool, base)
            await query.edit_message_text(f"Сессия {session.id} создана и выбрана.")
            return
        if query.data == "restore_yes":
            active = load_active_state(self.bot_app.config.defaults.state_path)
            if not active:
                await query.edit_message_text("Сохраненная активная сессия не найдена.")
                return
            if active.tool not in self.bot_app.config.tools or not os.path.isdir(active.workdir):
                await query.edit_message_text("Сохраненная сессия недоступна.")
                return
            session = self.bot_app.manager.create(active.tool, active.workdir)
            await query.edit_message_text(f"Сессия {session.id} восстановлена.")
            return
        if query.data == "restore_no":
            try:
                clear_active_state(self.bot_app.config.defaults.state_path)
            except Exception:
                pass
            await query.edit_message_text("Восстановление отменено.")
            return
        if query.data.startswith("toolhelp_pick:"):
            tool = query.data.split(":", 1)[1]
            entry = get_toolhelp(self.bot_app.config.defaults.toolhelp_path, tool)
            if entry:
                await query.edit_message_text("Отправляю help…")
                await self.bot_app._send_toolhelp_content(chat_id, context, entry.content)
                return
            await query.edit_message_text("Загружаю help…")
            try:
                workdir = self.bot_app.config.defaults.workdir
                active = self.bot_app.manager.active()
                if active and active.tool.name == tool:
                    workdir = active.workdir
                content = await asyncio.to_thread(
                    run_tool_help,
                    self.bot_app.config.tools[tool],
                    workdir,
                    self.bot_app.config.defaults.idle_timeout_sec,
                )
                update_toolhelp(self.bot_app.config.defaults.toolhelp_path, tool, content)
                await query.edit_message_text("Help получен, отправляю…")
                await self.bot_app._send_toolhelp_content(chat_id, context, content)
            except Exception as e:
                logging.exception(f"tool failed {str(e)}")
                await query.edit_message_text(f"Ошибка получения help: {e}")
            return
        if query.data.startswith("file_pick:"):
            idx = int(query.data.split(":", 1)[1])
            items = self.bot_app.files_entries.get(chat_id, [])
            if idx < 0 or idx >= len(items):
                await query.edit_message_text("Файл не найден.")
                return
            item = items[idx]
            path = item.get("path") if isinstance(item, dict) else item
            session = self.bot_app.manager.active()
            if not session:
                await query.edit_message_text("Активной сессии нет.")
                return
            if not is_within_root(path, session.workdir):
                await query.edit_message_text("Нельзя выйти за пределы рабочей директории.")
                return
            if not os.path.isfile(path):
                await query.edit_message_text("Файл не найден.")
                return
            size = os.path.getsize(path)
            if size > 45 * 1024 * 1024:
                await query.edit_message_text("Файл слишком большой для отправки.")
                return
            await query.edit_message_text(f"Отправляю файл: {os.path.basename(path)}")
            try:
                with open(path, "rb") as f:
                    ok = await self.bot_app._send_document(context, chat_id=chat_id, document=f)
                if not ok:
                    await query.edit_message_text("Ошибка отправки файла. Проверьте логи бота.")
            except Exception as e:
                logging.exception(f"Ошибка отправки файла из меню: {e}")
                await query.edit_message_text("Ошибка отправки файла. Проверьте логи бота.")
            return
        if query.data.startswith("file_nav:"):
            action = query.data.split(":", 1)[1]
            session = self.bot_app.manager.active()
            if not session:
                await query.edit_message_text("Активной сессии нет.")
                return
            if action == "cancel":
                await query.edit_message_text("Операция отменена.")
                return
            if action.startswith("open:"):
                idx = int(action.split(":", 1)[1])
                entries = self.bot_app.files_entries.get(chat_id, [])
                if idx < 0 or idx >= len(entries):
                    await query.edit_message_text("Папка не найдена.")
                    return
                entry = entries[idx]
                path = entry.get("path") if isinstance(entry, dict) else None
                if not path or not os.path.isdir(path):
                    await query.edit_message_text("Папка не найдена.")
                    return
                if not is_within_root(path, session.workdir):
                    await query.edit_message_text("Нельзя выйти за пределы рабочей директории.")
                    return
                self.bot_app.files_dir[chat_id] = path
                self.bot_app.files_page[chat_id] = 0
                await self.bot_app._send_files_menu(chat_id, session, context, edit_message=query)
                return
            if action == "up":
                current = self.bot_app.files_dir.get(chat_id, session.workdir)
                root = session.workdir
                if os.path.abspath(current) == os.path.abspath(root):
                    await query.edit_message_text("Уже в корне рабочей директории.")
                    return
                parent = os.path.dirname(current)
                if not is_within_root(parent, root):
                    await query.edit_message_text("Нельзя выйти за пределы рабочей директории.")
                    return
                self.bot_app.files_dir[chat_id] = parent
                self.bot_app.files_page[chat_id] = 0
                await self.bot_app._send_files_menu(chat_id, session, context, edit_message=query)
                return
            if action == "prev":
                page = max(0, self.bot_app.files_page.get(chat_id, 0) - 1)
                self.bot_app.files_page[chat_id] = page
                await self.bot_app._send_files_menu(chat_id, session, context, edit_message=query)
                return
            if action == "next":
                page = self.bot_app.files_page.get(chat_id, 0) + 1
                self.bot_app.files_page[chat_id] = page
                await self.bot_app._send_files_menu(chat_id, session, context, edit_message=query)
                return
        if query.data.startswith("file_del:"):
            idx = int(query.data.split(":", 1)[1])
            entries = self.bot_app.files_entries.get(chat_id, [])
            if idx < 0 or idx >= len(entries):
                await query.edit_message_text("Элемент не найден.")
                return
            entry = entries[idx]
            path = entry.get("path") if isinstance(entry, dict) else None
            session = self.bot_app.manager.active()
            if not session:
                await query.edit_message_text("Активной сессии нет.")
                return
            if not path or not is_within_root(path, session.workdir):
                await query.edit_message_text("Нельзя выйти за пределы рабочей директории.")
                return
            name = os.path.basename(path)
            self.bot_app.files_pending_delete[chat_id] = path
            keyboard = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton("✅ Да", callback_data="file_del_confirm"),
                        InlineKeyboardButton("❌ Отмена", callback_data="file_del_cancel"),
                    ]
                ]
            )
            await query.edit_message_text(f"Удалить {name}? Подтвердите:", reply_markup=keyboard)
            return
        if query.data == "file_del_current":
            session = self.bot_app.manager.active()
            if not session:
                await query.edit_message_text("Активной сессии нет.")
                return
            current = self.bot_app.files_dir.get(chat_id, session.workdir)
            root = session.workdir
            if os.path.abspath(current) == os.path.abspath(root):
                await query.edit_message_text("Нельзя удалить корневую рабочую директорию.")
                return
            if not is_within_root(current, root):
                await query.edit_message_text("Нельзя выйти за пределы рабочей директории.")
                return
            self.bot_app.files_pending_delete[chat_id] = current
            name = os.path.basename(current)
            keyboard = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton("✅ Да", callback_data="file_del_confirm"),
                        InlineKeyboardButton("❌ Отмена", callback_data="file_del_cancel"),
                    ]
                ]
            )
            await query.edit_message_text(f"Удалить папку {name} рекурсивно? Подтвердите:", reply_markup=keyboard)
            return
        if query.data == "file_del_confirm":
            session = self.bot_app.manager.active()
            if not session:
                await query.edit_message_text("Активной сессии нет.")
                return
            path = self.bot_app.files_pending_delete.pop(chat_id, None)
            if not path:
                await query.edit_message_text("Нет операции удаления.")
            if not is_within_root(path, session.workdir):
                await query.edit_message_text("Нельзя выйти за пределы рабочей директории.")
                return
            try:
                if os.path.isdir(path):
                    shutil.rmtree(path)
                else:
                    os.remove(path)
                await query.edit_message_text("Удалено.")
            except Exception as e:
                logging.exception(f"tool failed {str(e)}")
                await query.edit_message_text(f"Ошибка удаления: {e}")
            current = self.bot_app.files_dir.get(chat_id, session.workdir)
            if not os.path.isdir(current) or not is_within_root(current, session.workdir):
                current = session.workdir
                self.bot_app.files_dir[chat_id] = current
                self.bot_app.files_page[chat_id] = 0
            await self.bot_app._send_files_menu(chat_id, session, context, edit_message=None)
            return
        if query.data == "file_del_cancel":
            self.bot_app.files_pending_delete.pop(chat_id, None)
            session = self.bot_app.manager.active()
            if not session:
                await query.edit_message_text("Активной сессии нет.")
                return
            await query.edit_message_text("Удаление отменено.")
            await self.bot_app._send_files_menu(chat_id, session, context, edit_message=None)
            return
        if query.data.startswith("preset_run:"):
            code = query.data.split(":", 1)[1]
            if code == "cancel":
                await query.edit_message_text("Отменено.")
                return
            session = await self.bot_app.ensure_active_session(chat_id, context)
            if not session:
                await query.edit_message_text("Активной сессии нет.")
                return
            presets = self.bot_app._preset_commands()
            prompt = presets.get(code)
            if not prompt:
                await query.edit_message_text("Шаблон не найден.")
                return
            await query.edit_message_text(f"Отправляю задачу: {code}")
            await self.bot_app._handle_cli_input(session, prompt, chat_id, context)
            return
        if await self.bot_app.git.handle_callback(query, chat_id, context):
            return
        if await self.bot_app.session_ui.handle_callback(query, chat_id, context):
            return
        pending = self.bot_app.pending.pop(chat_id, None)
        if not pending:
            await query.edit_message_text("Нет ожидающего ввода.")
            return
        session = self.bot_app.manager.get(pending.session_id)
        if not session:
            await query.edit_message_text("Сессия уже закрыта.")
            return

        if query.data == "cancel_current":
            session.interrupt()
            if pending.image_path:
                try:
                    os.remove(pending.image_path)
                except Exception:
                    pass
            await query.edit_message_text("Текущая генерация прервана. Ввод отброшен.")
            return
        if query.data == "queue_input":
            item = {"text": pending.text, "dest": pending.dest}
            if pending.image_path:
                item["image_path"] = pending.image_path
            session.queue.append(item)
            self.bot_app.manager._persist_sessions()
            await query.edit_message_text("Ввод поставлен в очередь.")
            return
        if query.data == "discard_input":
            if pending.image_path:
                try:
                    os.remove(pending.image_path)
                except Exception:
                    pass
            await query.edit_message_text("Ввод отменен.")
            return
