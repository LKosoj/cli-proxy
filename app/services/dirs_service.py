"""Directory picker service for bot runtime."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Optional

from app.services.dirs_ui import build_dirs_keyboard, prepare_dirs
from app.services.telegram_ui_scope import TelegramUiKey
from i18n import t
from modes.sdk import decode_mode_dirs
from utils.lang import resolve_user_lang


@dataclass
class DirsService:
    bot_app: Any

    def _ui_key(
        self,
        chat_id: int,
        *,
        context: Any = None,
        message_thread_id: Optional[int] = None,
    ) -> TelegramUiKey:
        thread_id = message_thread_id if message_thread_id is not None else getattr(context, "message_thread_id", None)
        return self.bot_app.telegram_ui_key(int(chat_id), thread_id)

    async def start_flow(
        self,
        chat_id: int,
        context: Any,
        *,
        root: str,
        mode_token: str,
        message_thread_id: Optional[int] = None,
    ) -> None:
        ui_key = self._ui_key(chat_id, context=context, message_thread_id=message_thread_id)
        self.bot_app.ui_state.dirs_root[ui_key] = str(root)
        self.bot_app.ui_state.dirs_mode[ui_key] = str(mode_token)
        await self.send_menu(int(chat_id), context, str(root), message_thread_id=ui_key.message_thread_id)

    def clear_flow(
        self,
        chat_id: int,
        *,
        mode_id: str = "",
        flow: str = "",
        message_thread_id: Optional[int] = None,
    ) -> None:
        ui_key = self._ui_key(chat_id, message_thread_id=message_thread_id)
        mode_raw = str(self.bot_app.ui_state.dirs_mode.get(ui_key, "") or "")
        token_mode_id, token_flow = decode_mode_dirs(mode_raw)

        expected_mode = str(mode_id or "").strip()
        expected_flow = str(flow or "").strip()
        if expected_mode and token_mode_id != expected_mode:
            return
        if expected_flow and token_flow != expected_flow:
            return

        self.bot_app.ui_state.dirs_mode.pop(ui_key, None)
        self.bot_app.ui_state.dirs_root.pop(ui_key, None)
        self.bot_app.ui_state.dirs_menu.pop(ui_key, None)
        self.bot_app.ui_state.dirs_base.pop(ui_key, None)
        self.bot_app.ui_state.dirs_page.pop(ui_key, None)

    async def send_menu(
        self,
        chat_id: int,
        context: Any,
        base: str,
        *,
        message_thread_id: Optional[int] = None,
    ) -> None:
        ui_key = self._ui_key(chat_id, context=context, message_thread_id=message_thread_id)
        mode = self.bot_app.ui_state.dirs_mode.get(ui_key)
        include_files = False
        mode_id, flow = decode_mode_dirs(mode)
        if mode_id and flow:
            registry = getattr(self.bot_app, "mode_registry", None)
            plugin = registry.get(mode_id) if registry else None
            if plugin is not None and hasattr(plugin, "include_files_in_dirs"):
                try:
                    include_files = bool(plugin.include_files_in_dirs(flow))
                except Exception:
                    include_files = False

        err = prepare_dirs(
            self.bot_app.ui_state.dirs_menu,
            self.bot_app.ui_state.dirs_base,
            self.bot_app.ui_state.dirs_page,
            self.bot_app.ui_state.dirs_root,
            ui_key,
            base,
            include_files=include_files,
        )
        if err:
            if mode == "new_session":
                self.bot_app.ui_state.pending_new_tool.pop(ui_key, None)
            if mode == "git_clone":
                self.bot_app.ui_state.pending_git_clone.pop(ui_key, None)
            self.bot_app.ui_state.dirs_mode.pop(ui_key, None)
            self.bot_app.ui_state.dirs_menu.pop(ui_key, None)
            await self.bot_app._send_message(context, text=err, **ui_key.reply_kwargs())
            return

        lang = resolve_user_lang(self.bot_app.config, chat_id=chat_id)
        keyboard = build_dirs_keyboard(
            self.bot_app.ui_state.dirs_menu,
            self.bot_app.ui_state.dirs_base,
            self.bot_app.ui_state.dirs_page,
            self.bot_app._short_label,
            ui_key,
            base,
            0,
            lang=lang,
        )
        await self.bot_app._send_message(
            context,
            text=t("msg.dirs.choose_file_or_dir", lang) if include_files else t("msg.dirs.choose_dir", lang),
            reply_markup=keyboard,
            **ui_key.reply_kwargs(),
        )

    async def refresh_current(
        self,
        chat_id: int,
        context: Any,
        *,
        message_thread_id: Optional[int] = None,
    ) -> None:
        ui_key = self._ui_key(chat_id, context=context, message_thread_id=message_thread_id)
        try:
            base = self.bot_app.ui_state.dirs_base.get(ui_key, self.bot_app.config.defaults.workdir)
            await self.send_menu(int(chat_id), context, base, message_thread_id=ui_key.message_thread_id)
        except Exception as e:
            logging.getLogger(__name__).exception("dirs refresh failed chat_id=%s ui_key=%s: %s", chat_id, ui_key, e)
