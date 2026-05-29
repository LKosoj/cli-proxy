"""Directory-picker callback actions."""

import os

from app.services.dirs_ui import build_dirs_keyboard, prepare_dirs
from utils.paths import is_within_root


class DirsActionsMixin:
    def _callback_owner_chat_id(self, chat_id: int, query) -> int:
        resolver = getattr(self.bot_app, "resolve_telegram_callback_scope", None)
        if callable(resolver):
            try:
                _reply_chat_id, _thread_id, owner_chat_id, _session = resolver(query)
                return int(owner_chat_id or chat_id)
            except Exception:
                return int(chat_id)
        return int(chat_id)

    async def _cb_new_tool(self, *, data: str, chat_id: int, query, context) -> bool:
        tool = str(data).split(":", 1)[1]
        ui_key = self.bot_app.telegram_ui_key_from_query(query) or self.bot_app.telegram_ui_key(chat_id)
        owner_chat_id = self._callback_owner_chat_id(chat_id, query)
        if not self.bot_app.access_policy_service.is_admin(owner_chat_id):
            err = self.bot_app.session_creation_service.validate_tool(tool)
            if err:
                await self._edit_msg(context, query, err)
                return True
            text, keyboard = self.bot_app.handlers.build_user_project_picker(
                owner_chat_id,
                tool_name=tool,
                force_new=True,
                back_callback="sess_new",
            )
            await self._edit_msg(context, query, text, reply_markup=keyboard)
            return True
        err = self.bot_app.session_creation_service.begin_new_session_flow(
            owner_chat_id,
            tool,
            message_thread_id=ui_key.message_thread_id,
            ui_chat_id=ui_key.chat_id,
        )
        if err:
            await self._edit_msg(context, query, err)
            return True
        await self._edit_msg(context, query, f"Выбран инструмент {tool}. Выберите каталог.")
        await self.bot_app.dirs_service.start_flow(
            ui_key.chat_id,
            context,
            root=self.bot_app.config.defaults.workdir,
            mode_token="new_session",
            message_thread_id=ui_key.message_thread_id,
        )
        return True

    async def _cb_dir_pick(self, *, data: str, chat_id: int, query, context) -> bool:
        ui_key = self.bot_app.telegram_ui_key_from_query(query) or self.bot_app.telegram_ui_key(chat_id)
        owner_chat_id = self._callback_owner_chat_id(chat_id, query)
        idx = int(str(data).split(":", 1)[1])
        items = self.bot_app.ui_state.dirs_menu.get(ui_key, [])
        if idx < 0 or idx >= len(items):
            await self._edit_msg(context, query, "Выбор недоступен.")
            return True
        path = items[idx]
        mode = self.bot_app.ui_state.dirs_mode.get(ui_key, "new_session")
        if mode == "git_clone":
            self.bot_app.session_creation_service.mark_git_clone_pending(
                owner_chat_id,
                path,
                message_thread_id=ui_key.message_thread_id,
                ui_chat_id=ui_key.chat_id,
            )
            await self._edit_msg(context, query, "Отправьте ссылку для git clone.")
            return True
        mode_result = await self._dispatch_mode_dirs_event(
            chat_id=owner_chat_id,
            context=context,
            event="pick",
            path=path,
            message_thread_id=ui_key.message_thread_id,
        )
        if mode_result is not None:
            out = str(getattr(mode_result, "output", "") or "").strip()
            if out:
                await self._edit_msg(context, query, out)
            return True
        session, err = await self.bot_app.session_creation_service.create_from_pending_tool(
            owner_chat_id,
            path,
            bot=getattr(context, "bot", None),
            message_thread_id=ui_key.message_thread_id,
            ui_chat_id=ui_key.chat_id,
        )
        if err:
            await self._edit_msg(context, query, err)
            return True
        await self._present_selected_session_after_callback(
            context=context,
            query=query,
            owner_chat_id=owner_chat_id,
            session=session,
            created=True,
        )
        return True

    async def _cb_dir_page(self, *, data: str, chat_id: int, query, context) -> bool:
        ui_key = self.bot_app.telegram_ui_key_from_query(query) or self.bot_app.telegram_ui_key(chat_id)
        base = self.bot_app.ui_state.dirs_base.get(ui_key, self.bot_app.config.defaults.workdir)
        page = int(str(data).split(":", 1)[1])
        await self._edit_msg(
            context,
            query,
            "Выберите каталог:",
            reply_markup=build_dirs_keyboard(
                self.bot_app.ui_state.dirs_menu,
                self.bot_app.ui_state.dirs_base,
                self.bot_app.ui_state.dirs_page,
                self.bot_app._short_label,
                ui_key,
                base,
                page,
            ),
        )
        return True

    async def _cb_dir_up(self, *, data: str, chat_id: int, query, context) -> bool:
        ui_key = self.bot_app.telegram_ui_key_from_query(query) or self.bot_app.telegram_ui_key(chat_id)
        base = self.bot_app.ui_state.dirs_base.get(ui_key, self.bot_app.config.defaults.workdir)
        parent = os.path.dirname(base.rstrip(os.sep)) or base
        root = self.bot_app.ui_state.dirs_root.get(ui_key, self.bot_app.config.defaults.workdir)
        if not is_within_root(parent, root):
            await self._edit_msg(context, query, "Нельзя выйти за пределы корневого каталога.")
            return True
        err = prepare_dirs(
            self.bot_app.ui_state.dirs_menu,
            self.bot_app.ui_state.dirs_base,
            self.bot_app.ui_state.dirs_page,
            self.bot_app.ui_state.dirs_root,
            ui_key,
            parent,
        )
        if err:
            await self._edit_msg(context, query, err)
            return True
        await self._edit_msg(
            context,
            query,
            "Выберите каталог:",
            reply_markup=build_dirs_keyboard(
                self.bot_app.ui_state.dirs_menu,
                self.bot_app.ui_state.dirs_base,
                self.bot_app.ui_state.dirs_page,
                self.bot_app._short_label,
                ui_key,
                parent,
                0,
            ),
        )
        return True

    async def _cb_dir_enter(self, *, data: str, chat_id: int, query, context) -> bool:
        ui_key = self.bot_app.telegram_ui_key_from_query(query) or self.bot_app.telegram_ui_key(chat_id)
        self.bot_app.ui_state.pending_dir_input[ui_key] = True
        await self._edit_msg(context, query, "Отправьте путь к каталогу сообщением.")
        return True

    async def _cb_dir_create(self, *, data: str, chat_id: int, query, context) -> bool:
        ui_key = self.bot_app.telegram_ui_key_from_query(query) or self.bot_app.telegram_ui_key(chat_id)
        base = self.bot_app.ui_state.dirs_base.get(ui_key, self.bot_app.config.defaults.workdir)
        self.bot_app.ui_state.pending_dir_create[ui_key] = base
        await self._edit_msg(
            context,
            query,
            "Отправьте имя нового каталога или путь относительно текущего. Для отмены введите '-'.",
        )
        return True

    async def _cb_dir_git_clone(self, *, data: str, chat_id: int, query, context) -> bool:
        ui_key = self.bot_app.telegram_ui_key_from_query(query) or self.bot_app.telegram_ui_key(chat_id)
        base = self.bot_app.ui_state.dirs_base.get(ui_key, self.bot_app.config.defaults.workdir)
        owner_chat_id = self._callback_owner_chat_id(chat_id, query)
        self.bot_app.session_creation_service.mark_git_clone_pending(
            owner_chat_id,
            base,
            message_thread_id=ui_key.message_thread_id,
            ui_chat_id=ui_key.chat_id,
        )
        await self._edit_msg(context, query, "Отправьте ссылку для git clone.")
        return True

    async def _cb_dir_use_current(self, *, data: str, chat_id: int, query, context) -> bool:
        ui_key = self.bot_app.telegram_ui_key_from_query(query) or self.bot_app.telegram_ui_key(chat_id)
        owner_chat_id = self._callback_owner_chat_id(chat_id, query)
        base = self.bot_app.ui_state.dirs_base.get(ui_key, self.bot_app.config.defaults.workdir)
        root = self.bot_app.ui_state.dirs_root.get(ui_key, self.bot_app.config.defaults.workdir)
        if not is_within_root(base, root):
            await self._edit_msg(context, query, "Нельзя выйти за пределы корневого каталога.")
            return True
        mode = self.bot_app.ui_state.dirs_mode.get(ui_key, "new_session")
        if mode == "git_clone":
            self.bot_app.session_creation_service.mark_git_clone_pending(
                owner_chat_id,
                base,
                message_thread_id=ui_key.message_thread_id,
                ui_chat_id=ui_key.chat_id,
            )
            await self._edit_msg(context, query, "Отправьте ссылку для git clone.")
            return True
        mode_result = await self._dispatch_mode_dirs_event(
            chat_id=owner_chat_id,
            context=context,
            event="use_current",
            path=base,
            message_thread_id=ui_key.message_thread_id,
        )
        if mode_result is not None:
            out = str(getattr(mode_result, "output", "") or "").strip()
            if out:
                await self._edit_msg(context, query, out)
            return True
        session, err = await self.bot_app.session_creation_service.create_from_pending_tool(
            owner_chat_id,
            base,
            bot=getattr(context, "bot", None),
            message_thread_id=ui_key.message_thread_id,
            ui_chat_id=ui_key.chat_id,
        )
        if err:
            await self._edit_msg(context, query, err)
            return True
        await self._present_selected_session_after_callback(
            context=context,
            query=query,
            owner_chat_id=owner_chat_id,
            session=session,
            created=True,
        )
        return True
