"""Preset callback actions."""

from i18n import t, lang_from_query


class PresetActionsMixin:
    async def _cb_preset_run(self, *, data: str, chat_id: int, query, context) -> bool:
        lang = lang_from_query(query, self.bot_app.config)
        code = str(data).split(":", 1)[1]
        if code == "cancel":
            await self._edit_msg(context, query, t("msg.session.cancelled", lang))
            return True
        _reply_chat_id, message_thread_id, owner_chat_id, _session = self.bot_app.resolve_telegram_callback_scope(query)
        session = await self.bot_app.ensure_scope_session(
            owner_chat_id,
            context,
            reply_chat_id=_reply_chat_id,
            message_thread_id=message_thread_id,
        )
        if not session:
            await self._edit_msg(context, query, t("msg.error.session_no_scope", lang))
            return True
        presets = self.bot_app._preset_commands()
        prompt = presets.get(code)
        if not prompt:
            await self._edit_msg(context, query, t("msg.preset.not_found", lang))
            return True
        await self._edit_msg(context, query, t("msg.preset.sending_task", lang, code=code))
        await self.bot_app._handle_cli_input(session, prompt, chat_id, context)
        return True
