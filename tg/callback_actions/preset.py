"""Preset callback actions."""


class PresetActionsMixin:
    async def _cb_preset_run(self, *, data: str, chat_id: int, query, context) -> bool:
        code = str(data).split(":", 1)[1]
        if code == "cancel":
            await self._edit_msg(context, query, "Отменено.")
            return True
        _reply_chat_id, message_thread_id, owner_chat_id, _session = self.bot_app.resolve_telegram_callback_scope(query)
        session = await self.bot_app.ensure_scope_session(
            owner_chat_id,
            context,
            reply_chat_id=_reply_chat_id,
            message_thread_id=message_thread_id,
        )
        if not session:
            await self._edit_msg(context, query, "Сессия не определена для текущего scope.")
            return True
        presets = self.bot_app._preset_commands()
        prompt = presets.get(code)
        if not prompt:
            await self._edit_msg(context, query, "Шаблон не найден.")
            return True
        await self._edit_msg(context, query, f"Отправляю задачу: {code}")
        await self.bot_app._handle_cli_input(session, prompt, chat_id, context)
        return True
