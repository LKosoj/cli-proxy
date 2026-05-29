"""Protocol callback actions (mode actions, approvals, ask-user)."""

from agent import (
    approve_pending_command,
    deny_pending_command,
    execute_shell_command,
    has_pending_command_waiter,
)


class ProtocolActionsMixin:
    async def _cb_mode_action(self, *, data: str, chat_id: int, query, context) -> bool:
        return bool(
            await self._handle_mode_action_callback(
                data=data,
                chat_id=chat_id,
                query=query,
                context=context,
            )
        )

    async def _cb_approve_cmd(self, *, data: str, chat_id: int, query, context) -> bool:
        cmd_id = str(data).split(":", 1)[1]
        waiter_active = has_pending_command_waiter(cmd_id)
        pending = approve_pending_command(cmd_id)
        if not pending:
            await self._edit_msg(context, query, "Запрос уже обработан.")
            return True
        if waiter_active:
            await self._edit_msg(context, query, "Одобрено. Продолжаю выполнение шага...")
            return True
        await self._edit_msg(context, query, "Одобрено. Выполняю команду...")
        result = await execute_shell_command(pending.command, pending.cwd)
        output = result.get("output") if result.get("success") else result.get("error")
        text = str(output or "(пустой вывод)")
        session = None
        manager = getattr(self.bot_app, "manager", None)
        if manager is not None:
            getter = getattr(manager, "get", None)
            if callable(getter):
                session = getter(int(chat_id), str(getattr(pending, "session_id", "") or ""))
        if session is None:
            resolver = getattr(self.bot_app, "resolve_telegram_callback_scope", None)
            if callable(resolver):
                _reply_chat_id, _thread_id, _owner_chat_id, session = resolver(query)
        if session is not None:
            await self.bot_app.send_output(
                session,
                self.bot_app.build_telegram_reply_dest(
                    session,
                    int(chat_id),
                    user_id=getattr(getattr(query, "from_user", None), "id", None),
                ),
                text,
                (
                    self.bot_app.build_telegram_transport_context(
                        context,
                        session=session,
                        chat_id=chat_id,
                        dest=self.bot_app.build_telegram_reply_dest(
                            session,
                            int(chat_id),
                            user_id=getattr(getattr(query, "from_user", None), "id", None),
                        ),
                        user_id=getattr(getattr(query, "from_user", None), "id", None),
                        message_thread_id=getattr(getattr(query, "message", None), "message_thread_id", None),
                    )
                    if hasattr(self.bot_app, "build_telegram_transport_context")
                    else context
                ),
                send_header=False,
            )
        else:
            send_kwargs = {"chat_id": int(chat_id), "text": text}
            thread_id = getattr(getattr(query, "message", None), "message_thread_id", None)
            if thread_id is not None:
                send_kwargs["message_thread_id"] = int(thread_id)
            await self.bot_app._send_message(context, **send_kwargs)
        return True

    async def _cb_deny_cmd(self, *, data: str, chat_id: int, query, context) -> bool:
        cmd_id = str(data).split(":", 1)[1]
        waiter_active = has_pending_command_waiter(cmd_id)
        pending = deny_pending_command(cmd_id)
        if not pending:
            await self._edit_msg(context, query, "Запрос уже обработан.")
            return True
        if waiter_active:
            await self._edit_msg(context, query, "Команда отклонена. Продолжаю без неё.")
            return True
        await self._edit_msg(context, query, "Команда отклонена.")
        return True

    async def _cb_ask(self, *, data: str, chat_id: int, query, context) -> bool:
        ui_key = self._ui_key(chat_id, query)
        try:
            _, question_id, idx_str = str(data).split(":", 2)
        except ValueError:
            await self._edit_msg(context, query, "Некорректный формат ответа.")
            return True
        pending = self.bot_app.ui_state.pending_questions.get(question_id)
        if not pending:
            await self._edit_msg(context, query, "Вопрос устарел.")
            return True
        if int(pending.get("chat_id") or 0) != int(chat_id):
            await self._edit_msg(context, query, "Вопрос относится к другому чату.")
            return True
        pending_thread_id = pending.get("message_thread_id")
        if pending_thread_id is not None and int(pending_thread_id) != int(ui_key.message_thread_id or 0):
            await self._edit_msg(context, query, "Вопрос относится к другому треду.")
            return True
        pending_session_id = str(pending.get("session_id") or "").strip()
        if pending_session_id:
            session_exists = True
            resolver = getattr(self.bot_app, "resolve_telegram_callback_scope", None)
            if callable(resolver):
                _reply_chat_id, _thread_id, owner_chat_id, _session = resolver(query)
                manager = getattr(self.bot_app, "manager", None)
                getter = getattr(manager, "get", None) if manager is not None else None
                if callable(getter):
                    session_exists = getter(int(owner_chat_id), pending_session_id) is not None
            else:
                checker = getattr(self.bot_app, "_session_exists_for_chat", None)
                if callable(checker):
                    session_exists = bool(checker(int(chat_id), pending_session_id))
                else:
                    manager = getattr(self.bot_app, "manager", None)
                    getter = getattr(manager, "get", None) if manager is not None else None
                    if callable(getter):
                        session_exists = getter(int(chat_id), pending_session_id) is not None
            if not session_exists:
                self.bot_app._clear_pending_question(question_id)
                await self._edit_msg(context, query, "Вопрос устарел.")
                return True
        if idx_str == "custom":
            pending["awaiting_custom"] = True
            pending["custom_prompt_msg_id"] = query.message.message_id if query.message else None
            self.bot_app.ui_state.active_ask_question_by_chat[ui_key] = str(question_id)
            await self._edit_msg(
                context,
                query,
                "Введите свой вариант одним сообщением.",
            )
            return True
        options = pending.get("options") or []
        try:
            idx = int(idx_str)
        except ValueError:
            await self._edit_msg(context, query, "Некорректный выбор.")
            return True
        if idx < 0 or idx >= len(options):
            await self._edit_msg(context, query, "Выбор недоступен.")
            return True
        answer = options[idx]
        runtime = None
        getter = getattr(self.bot_app, "get_runtime_by_capability", None)
        if callable(getter):
            runtime = getter("resolve_question")
        resolved = bool(runtime and runtime.resolve_question(question_id, answer))
        self.bot_app._clear_pending_question(question_id)
        if not resolved:
            await self._edit_msg(context, query, "Ответ уже получен.")
            return True
        await self._edit_msg(context, query, f"Вы выбрали: {answer}")
        return True
