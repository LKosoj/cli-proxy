import asyncio

from sessions.conversation_scope import ConversationScope


class MessageBufferService:
    def __init__(self, bot_app):
        self.bot_app = bot_app

    def _state_map(self, name: str) -> dict:
        ui_state = getattr(self.bot_app, "ui_state", None)
        value = getattr(ui_state, str(name), None) if ui_state is not None else None
        if isinstance(value, dict):
            return value
        fallback = getattr(self.bot_app, str(name), None)
        if isinstance(fallback, dict):
            return fallback
        return {}

    @staticmethod
    def _scope_buffer_key(session, chat_id: int):
        scope = getattr(session, "conversation_scope", None)
        if (
            isinstance(scope, ConversationScope)
            and scope.message_thread_id is not None
            and int(scope.chat_id) == int(chat_id)
        ):
            return str(scope.session_uid)
        return int(chat_id)

    def _build_dest(self, session, chat_id: int, *, user_id=None, direct_messages_topic_id=None) -> dict:
        builder = getattr(self.bot_app, "build_telegram_reply_dest", None)
        if callable(builder):
            kwargs = {"user_id": user_id}
            if direct_messages_topic_id is not None:
                kwargs["direct_messages_topic_id"] = direct_messages_topic_id
            return builder(
                session,
                int(chat_id),
                **kwargs,
            )
        dest = {"kind": "telegram", "chat_id": int(chat_id)}
        if user_id is not None:
            dest["user_id"] = int(user_id)
        if direct_messages_topic_id is not None:
            dest["direct_messages_topic_id"] = int(direct_messages_topic_id)
        return dest

    async def buffer_or_send(
        self,
        session,
        text: str,
        chat_id: int,
        context,
        user_id=None,
        direct_messages_topic_id=None,
    ) -> None:
        buffer_key = self._scope_buffer_key(session, chat_id)
        message_buffer_user_id = self._state_map("message_buffer_user_id")
        direct_topic_map = self._state_map("message_buffer_direct_messages_topic_id")
        message_buffer = self._state_map("message_buffer")
        if user_id is not None:
            message_buffer_user_id[buffer_key] = int(user_id)
        if direct_messages_topic_id is not None:
            direct_topic_map[buffer_key] = int(direct_messages_topic_id)
        message_buffer.setdefault(buffer_key, []).append(text)
        await self.schedule_flush(chat_id, session, context)

    async def schedule_flush(self, chat_id: int, session, context) -> None:
        buffer_key = self._scope_buffer_key(session, chat_id)
        buffer_tasks = self._state_map("buffer_tasks")
        task = buffer_tasks.get(buffer_key)
        if task and not task.done():
            task.cancel()
        buffer_tasks[buffer_key] = asyncio.create_task(
            self.flush_after_delay(chat_id, session, context)
        )

    async def flush_after_delay(self, chat_id: int, session, context) -> None:
        try:
            await asyncio.sleep(2)
            await self.flush_buffer(chat_id, session, context)
        except asyncio.CancelledError:
            return

    async def flush_buffer(self, chat_id: int, session, context) -> None:
        buffer_key = self._scope_buffer_key(session, chat_id)
        message_buffer = self._state_map("message_buffer")
        buffer_tasks = self._state_map("buffer_tasks")
        message_buffer_user_id = self._state_map("message_buffer_user_id")
        direct_topic_map = self._state_map("message_buffer_direct_messages_topic_id")
        parts = message_buffer.get(buffer_key, [])
        if not parts:
            return
        message_buffer[buffer_key] = []
        task = buffer_tasks.pop(buffer_key, None)
        current_task = asyncio.current_task()
        if task and task is not current_task and not task.done():
            task.cancel()
        payload = "\n\n".join(parts)
        await self.bot_app._stage_user_input(
            session,
            payload,
            chat_id,
            context,
            dest=self._build_dest(
                session,
                chat_id,
                user_id=message_buffer_user_id.get(buffer_key),
                direct_messages_topic_id=direct_topic_map.get(buffer_key),
            ),
        )
        direct_topic_map.pop(buffer_key, None)

    def clear_buffer(self, session, chat_id: int) -> bool:
        buffer_key = self._scope_buffer_key(session, chat_id)
        message_buffer = self._state_map("message_buffer")
        buffer_tasks = self._state_map("buffer_tasks")
        message_buffer_user_id = self._state_map("message_buffer_user_id")
        direct_topic_map = self._state_map("message_buffer_direct_messages_topic_id")

        changed = False
        if buffer_key in message_buffer:
            changed = True
            message_buffer.pop(buffer_key, None)
        if buffer_key in message_buffer_user_id:
            changed = True
            message_buffer_user_id.pop(buffer_key, None)
        if buffer_key in direct_topic_map:
            changed = True
            direct_topic_map.pop(buffer_key, None)
        task = buffer_tasks.pop(buffer_key, None)
        try:
            current_task = asyncio.current_task()
        except RuntimeError:
            current_task = None
        if task is not None:
            changed = True
            if task is not current_task and not task.done():
                task.cancel()
        return changed
