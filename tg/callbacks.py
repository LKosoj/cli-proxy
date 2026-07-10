"""
Module containing callback handling functionality for the Telegram bot.
"""

import asyncio
import logging

from telegram import Update
from telegram.error import NetworkError, TimedOut
from telegram.ext import ContextTypes

from app.services.input_dispatch_service import InputDispatchService
from modes.analyst.draft_service import build_draft_text as build_analyst_draft_text
from modes.sdk.runtime.openai_client import chat_completion
from session import session_runtime_uid
from sessions.queue_item import normalize_queue_item
from sessions.session_state_access import (
    get_active_mode,
    get_orchestrator_pending_input,
    set_orchestrator_enabled,
    set_orchestrator_pending_input,
)
from i18n import t, lang_from_query
from utils.lang import resolve_user_lang
from tg.callback_actions import CallbackActionsMixin


class CallbackHandler(CallbackActionsMixin):
    """
    Class containing callback handling functionality for the Telegram bot.
    """

    def __init__(self, bot_app):
        self.bot_app = bot_app
        self._callback_prefix_handlers = [
            ("orch_transition:", self._cb_orch_transition),
            ("ma:", self._cb_mode_action),
            ("mode_action:", self._cb_mode_action),
            ("approve_cmd:", self._cb_approve_cmd),
            ("deny_cmd:", self._cb_deny_cmd),
            ("ask:", self._cb_ask),
        ]
        self._session_ui_handlers = [
            (("eq", "sess_active"), self._cb_sess_active),
            (("prefix", "sess_active_pick:"), self._cb_sess_active_pick),
            (("eq", "user_project_menu"), self._cb_user_project_menu),
            (("prefix", "user_project_menu:"), self._cb_user_project_menu),
            (("prefix", "user_project_pick:"), self._cb_user_project_pick),
            (("prefix", "user_project_pick_new:"), self._cb_user_project_pick_new),
            (("eq", "sess_list"), self._cb_sess_list),
            (("eq", "sess_new"), self._cb_sess_new),
            (("prefix", "sess_cli:"), self._cb_sess_cli),
            (("prefix", "sess_backend:"), self._cb_sess_backend),
            (("prefix", "sess_transfer_yes:"), self._cb_sess_transfer_yes),
            (("prefix", "sess_transfer_no:"), self._cb_sess_transfer_no),
            (("prefix", "sess_mode_pick:"), self._cb_sess_mode_pick),
            (("prefix", "sess_mode:"), self._cb_sess_mode),
            (("prefix", "sess_ssh_toggle:"), self._cb_sess_ssh_toggle),
            (("prefix", "sess_snapshot:"), self._cb_sess_snapshot),
            (("eq", "agent_cancel"), self._cb_agent_cancel),
            (("prefix", "state_pick:"), self._cb_state_pick),
            (("prefix", "state_page:"), self._cb_state_page),
            (("eq", "lang_menu"), self._cb_lang_menu),
            (("prefix", "lang_set:"), self._cb_lang_set),
        ]
        self._post_callback_handlers = [
            (("prefix", "close_pick:"), self._cb_close_pick),
            (("prefix", "new_tool:"), self._cb_new_tool),
            (("prefix", "dir_pick:"), self._cb_dir_pick),
            (("prefix", "dir_page:"), self._cb_dir_page),
            (("eq", "dir_up"), self._cb_dir_up),
            (("eq", "dir_enter"), self._cb_dir_enter),
            (("eq", "dir_create"), self._cb_dir_create),
            (("eq", "dir_git_clone"), self._cb_dir_git_clone),
            (("eq", "dir_use_current"), self._cb_dir_use_current),
            (("eq", "file_save_here"), self._cb_file_save_here),
            (("prefix", "file_pick:"), self._cb_file_pick),
            (("prefix", "file_nav:"), self._cb_file_nav),
            (("prefix", "file_del:"), self._cb_file_del),
            (("prefix", "file_rename:"), self._cb_file_rename),
            (("eq", "file_del_current"), self._cb_file_del_current),
            (("eq", "file_del_confirm"), self._cb_file_del_confirm),
            (("eq", "file_del_cancel"), self._cb_file_del_cancel),
            (("eq", "file_rename_cancel"), self._cb_file_rename_cancel),
            (("prefix", "preset_run:"), self._cb_preset_run),
        ]

    def _persist_sessions(self) -> None:
        try:
            self.bot_app.mode_session_control.persist()
        except Exception as e:
            logging.exception("persist sessions failed: %s", e)

    def _persist_session(self, chat_id: int, session_id: str) -> None:
        manager = getattr(self.bot_app, "manager", None)
        if manager is not None and hasattr(manager, "persist_session"):
            try:
                if bool(manager.persist_session(int(chat_id), str(session_id))):
                    return
            except Exception as e:
                logging.exception("persist single session failed: %s", e)
        self._persist_sessions()

    async def _persist_session_async(self, chat_id: int, session_id: str) -> None:
        # H1: снапшот session.queue/dict сериализуем СИНХРОННО на event-loop
        # (мутация очереди из корутин ломает обход), в worker-поток уходит только запись.
        manager = getattr(self.bot_app, "manager", None)
        if manager is None or not hasattr(manager, "serialize_chat_entry_for_persist"):
            # Легаси/тесты без нового API менеджера — синхронный путь.
            self._persist_session(int(chat_id), str(session_id))
            return
        try:
            entry = manager.serialize_chat_entry_for_persist(int(chat_id), str(session_id))
            if entry is None:
                return  # сессия исчезла — персистить нечего
            ok = await asyncio.to_thread(manager.write_chat_entry, int(chat_id), entry)
            if bool(ok):
                return
            self._persist_sessions()
        except Exception as e:
            logging.exception("persist single session async failed: %s", e)
            self._persist_sessions()

    async def _cancel_mode_tasks_session(self, session_id: str) -> None:
        try:
            await self.bot_app.mode_session_control.cancel_session(session_id=session_id, timeout_s=0.2)
        except Exception as e:
            logging.exception("cancel mode tasks session=%s failed: %s", session_id, e)

    def _mode_callback_router(self):
        router = getattr(self.bot_app, "mode_callback_router", None)
        if router is None:
            return None
        try:
            router.send_message = getattr(self.bot_app, "_send_message", None)
            router.dialogs = getattr(self.bot_app, "mode_dialogs", None)
            router.mode_registry = getattr(self.bot_app, "mode_registry_service")
        except Exception:
            logging.getLogger(__name__).exception("failed to sync mode callback router runtime dependencies")
        return router

    def _ui_key(self, chat_id: int, query):
        return self.bot_app.telegram_ui_key_from_query(query) or self.bot_app.telegram_ui_key(
            int(chat_id),
            getattr(getattr(query, "message", None), "message_thread_id", None),
        )

    def _clear_stale_orchestrator_pending_input(self, *, session_token: str, chat_id: int, query) -> bool:
        token = str(session_token or "").strip()
        if not token:
            return False
        manager = getattr(self.bot_app, "manager", None)
        if manager is None:
            return False

        candidate = None
        target_thread_id = getattr(getattr(query, "message", None), "message_thread_id", None)
        get_by_scope = getattr(manager, "get_by_scope", None)
        if callable(get_by_scope):
            try:
                scoped = get_by_scope(
                    int(chat_id),
                    target_thread_id,
                )
            except Exception:
                logging.exception("failed to resolve scoped session for stale orchestrator cleanup")
                scoped = None
            if scoped is not None:
                scoped_tokens = {
                    str(getattr(scoped, "id", "") or "").strip(),
                    session_runtime_uid(scoped),
                }
                scoped_tokens.discard("")
                if token not in scoped_tokens:
                    return False
                if isinstance(get_orchestrator_pending_input(scoped, None), dict):
                    candidate = scoped
                else:
                    return False
        if candidate is None and ":" in token:
            get_by_uid = getattr(manager, "get_by_uid", None)
            if callable(get_by_uid):
                try:
                    resolved = get_by_uid(token)
                except Exception:
                    logging.exception("failed to resolve session_uid for stale orchestrator cleanup")
                    resolved = None
                if resolved is not None and session_runtime_uid(resolved) == token:
                    if isinstance(get_orchestrator_pending_input(resolved, None), dict):
                        candidate = resolved

        if candidate is None:
            return False

        set_orchestrator_pending_input(candidate, None)
        owner_chat_id = getattr(candidate, "chat_id", None)
        if owner_chat_id is None:
            owner_chat_id = chat_id
        self._persist_session(int(owner_chat_id), str(getattr(candidate, "id", "") or ""))
        return True

    async def _handle_mode_action_callback(self, *, data: str, chat_id: int, query, context) -> bool:
        router = self._mode_callback_router()
        if router is None:
            return False
        return bool(
            await router.handle_mode_action_callback(
                data=data,
                chat_id=chat_id,
                query=query,
                context=context,
                bot_app=self.bot_app,
            )
        )

    async def _cb_orch_transition(self, *, data: str, chat_id: int, query, context) -> bool:
        # Format:
        # - orch_transition:apply:<session_uid>:<target_mode_id>
        # - orch_transition:cancel:<session_uid>
        try:
            lang = lang_from_query(query, self.bot_app.config)
        except Exception:
            lang = "ru"
        parts = str(data or "").split(":")
        if len(parts) < 3:
            await self._edit_msg(context, query, t("msg.error.orch_invalid", lang))
            return True
        action = str(parts[1] or "").strip().lower()
        if action == "apply":
            if len(parts) < 4:
                await self._edit_msg(context, query, t("msg.error.orch_invalid", lang))
                return True
            target_mode_id = str(parts[-1] or "").strip()
            session_uid = ":".join(parts[2:-1]).strip()
        else:
            target_mode_id = ""
            session_uid = ":".join(parts[2:]).strip()
        if not session_uid:
            await self._edit_msg(context, query, t("msg.error.session_not_found", lang))
            return True

        session = self.bot_app.manager.get_by_uid(session_uid)
        if not session:
            self._clear_stale_orchestrator_pending_input(session_token=session_uid, chat_id=chat_id, query=query)
            await self._edit_msg(context, query, t("msg.error.session_not_found", lang))
            return True
        pending = get_orchestrator_pending_input(session, None)
        if not isinstance(pending, dict):
            await self._edit_msg(context, query, t("msg.error.orch_no_pending", lang))
            return True

        pending_target = str(pending.get("target_mode_id") or "").strip()
        disable_on_cancel = bool(pending.get("disable_orchestrator_on_cancel"))

        if action == "apply":
            if not target_mode_id or target_mode_id != pending_target:
                await self._edit_msg(context, query, t("msg.error.orch_stale", lang))
                return True
            orch = getattr(self.bot_app, "advanced_orchestrator_service", None)
            if orch is None:
                await self._edit_msg(context, query, t("msg.error.orch_unavailable", lang))
                return True
        else:
            if action != "cancel":
                await self._edit_msg(context, query, t("msg.error.orch_unknown_action", lang))
                return True

        text = str(pending.get("text") or "")
        if isinstance(pending.get("dest"), dict):
            dest = pending.get("dest")
        else:
            builder = getattr(self.bot_app, "build_telegram_reply_dest", None)
            if callable(builder):
                dest = builder(session, int(chat_id))
            else:
                dest = {"kind": "telegram", "chat_id": int(chat_id)}
        set_orchestrator_pending_input(session, None)

        if action == "apply":
            orch.apply_mode(session=session, target_mode_id=target_mode_id)
            await self._persist_session_async(int(chat_id), session.id)
            await self._edit_msg(context, query, t("msg.orch.applied", lang))
        elif disable_on_cancel:
            set_orchestrator_enabled(session, False)
            await self._persist_session_async(int(chat_id), session.id)
            await self._edit_msg(
                context,
                query,
                t("msg.orch.disabled", lang),
            )
            return True
        else:
            await self._edit_msg(context, query, t("msg.orch.cancelled", lang))

        await self.bot_app.input_dispatch_service.handle_user_input_no_orchestration(
            session,
            text,
            int(chat_id),
            context,
            dest=dest,
        )
        return True

    async def _edit_msg(self, context, query, text, *, reply_markup=None, md2: bool = True) -> bool:
        """Shortcut: edit the callback query message with given text."""
        if query.message:
            return await self.bot_app._edit_message(
                context,
                chat_id=query.message.chat_id,
                message_id=query.message.message_id,
                text=text,
                reply_markup=reply_markup,
                md2=md2,
            )
        return False

    async def _respond_callback(self, *, context, query, chat_id: int, text: str, reply_markup=None, md2: bool = True) -> None:
        try:
            edited = await self._edit_msg(
                context,
                query,
                text,
                reply_markup=reply_markup,
                md2=md2,
            )
        except Exception:
            logging.exception("callback edit failed, fallback to send_message")
            edited = False
        if edited:
            return
        try:
            send_kwargs = {
                "chat_id": int(chat_id),
                "text": text,
                "reply_markup": reply_markup,
                "md2": md2,
            }
            thread_id = getattr(getattr(query, "message", None), "message_thread_id", None)
            if thread_id is not None:
                send_kwargs["message_thread_id"] = int(thread_id)
            await self.bot_app._send_message(
                context,
                **send_kwargs,
            )
        except Exception:
            logging.exception("callback fallback send_message failed chat_id=%s", int(chat_id))

    def _resolve_pending_session(self, pending):
        pending_session_uid = str(getattr(pending, "session_uid", "") or "").strip()
        if not pending_session_uid:
            return None, pending_session_uid
        return self.bot_app.manager.get_by_uid(pending_session_uid), pending_session_uid

    def _next_live_pending_input(self, *, ui_key):
        pending_map = getattr(getattr(self.bot_app, "ui_state", None), "pending", None)
        if not isinstance(pending_map, dict):
            return None, None, 0

        purged = 0
        stale_session_uids: list[str] = []
        while True:
            pending = InputDispatchService.pending_head(pending_map, ui_key)
            if not pending:
                if purged:
                    logging.getLogger(__name__).warning(
                        "purged stale pending inputs ui_key=%s session_uids=%s",
                        ui_key,
                        stale_session_uids,
                    )
                return None, None, purged
            session, pending_session_uid = self._resolve_pending_session(pending)
            if session is not None:
                if purged:
                    logging.getLogger(__name__).info(
                        "recovered pending input flow after purging stale entries ui_key=%s session_uids=%s",
                        ui_key,
                        stale_session_uids,
                    )
                return pending, session, purged
            InputDispatchService.pop_pending(pending_map, ui_key)
            purged += 1
            stale_session_uids.append(pending_session_uid or str(getattr(pending, "session_id", "") or ""))

    async def _show_next_pending_input(self, *, chat_id: int, context, message_thread_id: int | None = None) -> None:
        ui_key = self.bot_app.telegram_ui_key(int(chat_id), message_thread_id)
        pending, _session, _purged = self._next_live_pending_input(ui_key=ui_key)
        if not pending:
            return
        dispatch = getattr(self.bot_app, "input_dispatch_service", None)
        action = str(getattr(pending, "action", "") or InputDispatchService.PENDING_ACTION_QUEUE_CHOICE)
        if dispatch is None or not hasattr(dispatch, "send_pending_input_decision"):
            return
        try:
            lang = resolve_user_lang(self.bot_app.config, chat_id=int(chat_id))
            await dispatch.send_pending_input_decision(
                context=context,
                decision=dispatch.pending_input_decision_for_action(action, pending_input=pending, lang=lang),
                dest=getattr(pending, "dest", None),
                chat_id=int(chat_id),
                ui_key=ui_key,
            )
        except Exception:
            logging.exception("failed to show next pending input chat_id=%s", int(chat_id))

    async def _kick_session_queue_if_idle(self, *, session, chat_id: int, context) -> None:
        if InputDispatchService._is_session_running(session, self.bot_app):
            return
        queue = getattr(session, "queue", None)
        if not queue:
            return
        if hasattr(queue, "__getitem__"):
            raw_next_item = queue[0]
        else:
            return
        builder = getattr(self.bot_app, "build_telegram_reply_dest", None)
        if callable(builder):
            fallback_dest = builder(session, int(chat_id))
        else:
            fallback_dest = {"kind": "telegram", "chat_id": int(chat_id)}
        next_item = normalize_queue_item(raw_next_item, fallback_dest=fallback_dest)
        if hasattr(queue, "popleft"):
            queue.popleft()
        else:
            queue.pop(0)
        await self._persist_session_async(int(chat_id), session.id)
        next_prompt = str(next_item.text or "")
        next_dest = dict(next_item.dest)
        if isinstance(raw_next_item, dict):
            image_paths = (raw_next_item or {}).get("image_paths")
            if image_paths:
                next_dest["image_paths"] = list(image_paths)
                next_dest["cleanup_images"] = True
            image_path = (raw_next_item or {}).get("image_path")
            if image_path:
                next_dest["image_path"] = image_path
                next_dest["cleanup_image"] = True
        if next_dest.get("kind") == "telegram" and next_dest.get("chat_id") is None:
            next_dest["chat_id"] = int(chat_id)
        try:
            active_mode = str(get_active_mode(session, "") or "").strip()
            if active_mode:
                await self.bot_app._handle_user_input(
                    session,
                    next_prompt,
                    int(chat_id),
                    context,
                    dest=next_dest,
                )
                return
            session_management = getattr(self.bot_app, "session_management", None)
            start_prompt_task = getattr(session_management, "start_prompt_task", None)
            if callable(start_prompt_task):
                start_prompt_task(
                    session,
                    next_prompt,
                    next_dest,
                    context,
                    task_name="callbacks.queue_kick.run_prompt",
                )
                return
            dispatcher = getattr(self.bot_app, "input_dispatch_service", None)
            run_coro = self.bot_app.run_prompt(session, next_prompt, next_dest, context)
            if dispatcher is not None and hasattr(dispatcher, "_safe_create_task"):
                task = dispatcher._safe_create_task(
                    run_coro,
                    label="callbacks.queue_kick.run_prompt",
                )
                if task is None:
                    raise RuntimeError("failed to schedule queue kick run_prompt task")
                return
            asyncio.create_task(run_coro)
        except Exception:
            logging.exception("failed to kick queued input for idle session session_id=%s", getattr(session, "id", "?"))
            try:
                queue_ref = getattr(session, "queue", None)
                if hasattr(queue_ref, "appendleft"):
                    queue_ref.appendleft(raw_next_item)
                elif isinstance(queue_ref, list):
                    queue_ref.insert(0, raw_next_item)
                await self._persist_session_async(int(chat_id), session.id)
            except Exception:
                logging.exception(
                    "failed to restore queued input after dispatch failure session_id=%s",
                    getattr(session, "id", "?"),
                )

    def _resolve_dirs_mode_plugin(self, chat_id: int, message_thread_id: int | None = None):
        router = self._mode_callback_router()
        if router is None:
            return "", None, None, None
        return router.resolve_dirs_mode_plugin(int(chat_id), message_thread_id)

    async def _dispatch_mode_dirs_event(
        self,
        *,
        chat_id: int,
        context,
        event: str,
        path: str,
        message_thread_id: int | None = None,
    ):
        router = self._mode_callback_router()
        if router is None:
            return None
        return await router.dispatch_dirs_event(
            chat_id=int(chat_id),
            message_thread_id=message_thread_id,
            context=context,
            event=str(event),
            path=str(path),
            bot_app=self.bot_app,
        )

    async def _build_analyst_draft_text(self, session) -> str:
        chat_id = int(getattr(session, "chat_id", 0) or 0)
        template = self._resolve_analyst_template_from_mode(session)
        return await build_analyst_draft_text(
            self.bot_app,
            session,
            chat_id=chat_id,
            template_override=template,
            chat_completion_fn=chat_completion,
        )

    def _resolve_analyst_template_from_mode(self, session) -> dict:
        getter = getattr(self.bot_app, "get_runtime_by_capability", None)
        runtime = getter("template_provider") if callable(getter) else None
        if runtime is None or not hasattr(runtime, "get_template_for_session"):
            return {}
        return dict(runtime.get_template_for_session(session) or {})

    async def _dispatch_callback_protocol(self, *, data: str, chat_id: int, query, context) -> bool:
        for prefix, handler in self._callback_prefix_handlers:
            if str(data or "").startswith(prefix):
                return bool(await handler(data=data, chat_id=chat_id, query=query, context=context))
        return False

    async def _dispatch_handlers(self, *, handlers, data: str, chat_id: int, query, context) -> bool:
        sdata = str(data or "")
        for matcher, handler in handlers:
            kind, value = matcher
            if kind == "eq" and sdata != value:
                continue
            if kind == "prefix" and not sdata.startswith(value):
                continue
            return bool(await handler(data=sdata, chat_id=chat_id, query=query, context=context))
        return False

    def _callback_admin_scope(
        self,
        chat_id: int,
        data: str,
        *,
        message_thread_id: int | None = None,
        policy_chat_id: int | None = None,
    ) -> str:
        policy = getattr(self.bot_app, "access_policy_service", None)
        if policy is None or not hasattr(policy, "callback_admin_scope"):
            return ""
        _raw, mode_id, flow, _plugin = self._resolve_dirs_mode_plugin(chat_id, message_thread_id)
        return str(
            policy.callback_admin_scope(
                int(policy_chat_id if policy_chat_id is not None else chat_id),
                str(data or ""),
                mode_id=mode_id,
                flow=flow,
            )
            or ""
        )

    async def handle_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        try:
            await query.answer()
        except (TimedOut, NetworkError) as e:
            # Network glitches shouldn't spam full tracebacks.
            logging.warning("Ошибка сети при ответе на callback: %s", e)
        except Exception as e:
            logging.exception("Ошибка ответа на callback: %s", e)
        chat_id = query.message.chat_id if query.message else None
        if not chat_id:
            return
        try:
            lang = lang_from_query(query, self.bot_app.config)
        except Exception:
            lang = "ru"
        policy_chat_id = int(chat_id)
        resolver = getattr(self.bot_app, "resolve_telegram_callback_scope", None)
        if callable(resolver):
            try:
                resolved_reply_chat_id, _resolved_thread_id, owner_chat_id, _session = resolver(query)
                chat_id = int(resolved_reply_chat_id or chat_id)
                policy_chat_id = int(owner_chat_id or chat_id)
            except Exception:
                logging.getLogger(__name__).exception("failed to resolve callback owner scope")
        ui_key = self._ui_key(int(chat_id), query)
        try:
            if not await self.bot_app.access_policy_service.ensure_allowed(policy_chat_id, context):
                return
            ui_state = getattr(self.bot_app, "ui_state", None)
            context_by_chat = getattr(ui_state, "context_by_chat", None) if ui_state is not None else None
            if not isinstance(context_by_chat, dict):
                context_by_chat = getattr(self.bot_app, "context_by_chat", None)
            if isinstance(context_by_chat, dict):
                context_by_chat[chat_id] = context
            # Role-gated callbacks: block restricted actions for non-admin users even if buttons exist.
            admin_scope = self._callback_admin_scope(
                chat_id,
                str(query.data or ""),
                message_thread_id=ui_key.message_thread_id,
                policy_chat_id=policy_chat_id,
            )
            if admin_scope:
                await self._edit_msg(context, query, self.bot_app.access_policy_service.admin_denied_text(admin_scope))
                return

            # ------------------------------------------------------------------
            # Unified routing:
            # 1) DialogService intercepts mode_action callbacks when a dialog is active.
            # 2) mode_action:* routes to active mode plugin via ModeRegistry.
            # 3) fallback to common callback handlers below.
            # ------------------------------------------------------------------

            data = str(query.data or "")
            if await self._dispatch_callback_protocol(data=data, chat_id=chat_id, query=query, context=context):
                return
            if await self._dispatch_handlers(
                handlers=self._session_ui_handlers,
                data=data,
                chat_id=chat_id,
                query=query,
                context=context,
            ):
                return
        except Exception as e:
            logging.exception(f"Ошибка обработки кнопки: {e}")
            send_kwargs = {"chat_id": int(chat_id), "text": t("msg.error.callback_handler", lang, e=e)}
            thread_id = getattr(getattr(query, "message", None), "message_thread_id", None)
            if thread_id is not None:
                send_kwargs["message_thread_id"] = int(thread_id)
            await self.bot_app._send_message(context, **send_kwargs)
            return
        if await self._dispatch_handlers(
            handlers=self._post_callback_handlers,
            data=str(query.data or ""),
            chat_id=chat_id,
            query=query,
            context=context,
        ):
            return
        if await self.bot_app.git.handle_callback(query, chat_id, context):
            return
        if await self.bot_app.session_ui.handle_callback(query, chat_id, context):
            return
        if query.data == "cancel_current":
            pending_map = self.bot_app.ui_state.pending
            pending, session, purged = self._next_live_pending_input(ui_key=ui_key)
            if not pending:
                await self._respond_callback(
                    context=context,
                    query=query,
                    chat_id=int(chat_id),
                    text=t("msg.error.session_closed_stale", lang) if purged else t("msg.input.no_pending", lang),
                )
                return
            dispatch = getattr(self.bot_app, "input_dispatch_service", None)
            if dispatch is not None and hasattr(dispatch, "clear_pending_prompt_record"):
                dispatch.clear_pending_prompt_record(ui_key)
            InputDispatchService.pop_pending(pending_map, ui_key)
            interrupt_runtime = getattr(getattr(self.bot_app, "session_management", None), "interrupt_session_runtime", None)
            message_text = t("msg.input.interrupted", lang)
            if callable(interrupt_runtime):
                try:
                    report = await interrupt_runtime(
                        session,
                        owner_chat_id=int(policy_chat_id),
                        reply_chat_id=int(chat_id),
                        message_thread_id=ui_key.message_thread_id,
                        reason="cancel_current",
                    )
                except Exception:
                    logging.getLogger(__name__).exception(
                        "failed to interrupt session runtime session=%s",
                        session.id,
                    )
                else:
                    if str(getattr(report, "status", "") or "") == "completed":
                        message_text = t("msg.input.interrupted_freed", lang)
                    elif str(getattr(report, "status", "") or "") == "partial_timeout":
                        message_text = t("msg.input.interrupted_partial", lang)
                    else:
                        message_text = t("msg.input.interrupt_failed", lang)
            else:
                session.interrupt()
                try:
                    await self._cancel_mode_tasks_session(session_runtime_uid(session))
                except Exception:
                    logging.getLogger(__name__).exception(
                        "failed to cancel mode tasks for interrupted session=%s",
                        session.id,
                    )
            await self._respond_callback(
                context=context,
                query=query,
                chat_id=int(chat_id),
                text=message_text,
            )
            await self._show_next_pending_input(
                chat_id=int(chat_id),
                context=context,
                message_thread_id=ui_key.message_thread_id,
            )
            return
        if query.data == "take_pending_input":
            pending_map = self.bot_app.ui_state.pending
            pending, session, purged = self._next_live_pending_input(ui_key=ui_key)
            if not pending:
                await self._respond_callback(
                    context=context,
                    query=query,
                    chat_id=int(chat_id),
                    text=t("msg.error.session_closed_stale", lang) if purged else t("msg.input.no_pending", lang),
                )
                return
            dispatch = getattr(self.bot_app, "input_dispatch_service", None)
            if dispatch is not None and hasattr(dispatch, "clear_pending_prompt_record"):
                dispatch.clear_pending_prompt_record(ui_key)
            InputDispatchService.pop_pending(pending_map, ui_key)
            if InputDispatchService._is_session_busy(session, self.bot_app):
                await self._respond_callback(
                    context=context,
                    query=query,
                    chat_id=int(chat_id),
                    text=t("msg.input.busy_queued", lang),
                )
                if dispatch is not None and hasattr(dispatch, "_handle_busy_pending_input"):
                    await dispatch._handle_busy_pending_input(
                        session=session,
                        pending_input=pending,
                        chat_id=int(chat_id),
                        context=context,
                        lang=lang,
                    )
                return
            await self._respond_callback(
                context=context,
                query=query,
                chat_id=int(chat_id),
                text=t("msg.input.taken", lang),
            )
            await self.bot_app._handle_user_input(
                session,
                str(getattr(pending, "text", "") or ""),
                int(chat_id),
                context,
                dest=dict(getattr(pending, "dest", {}) or {}),
            )
            return
        if query.data == "send_current_tmux":
            pending_map = self.bot_app.ui_state.pending
            pending, session, purged = self._next_live_pending_input(ui_key=ui_key)
            if not pending:
                await self._respond_callback(
                    context=context,
                    query=query,
                    chat_id=int(chat_id),
                    text=t("msg.error.session_closed_stale", lang) if purged else t("msg.input.no_pending", lang),
                )
                return
            dispatch = getattr(self.bot_app, "input_dispatch_service", None)
            sender = getattr(dispatch, "send_pending_to_active_tmux", None) if dispatch is not None else None
            if not callable(sender):
                await self._respond_callback(
                    context=context,
                    query=query,
                    chat_id=int(chat_id),
                    text=t("msg.error.tmux_current_send_failed", lang),
                )
                return
            try:
                await sender(session, pending)
            except Exception:
                logging.exception(
                    "failed to send pending input to active tmux session_id=%s",
                    getattr(session, "id", "?"),
                )
                await dispatch.refresh_busy_pending_action(session, pending)
                await self._respond_callback(
                    context=context,
                    query=query,
                    chat_id=int(chat_id),
                    text=t("msg.error.tmux_current_send_failed", lang),
                )
                await dispatch._show_pending_prompt(
                    ui_key=ui_key,
                    pending_input=pending,
                    chat_id=int(chat_id),
                    context=context,
                    lang=lang,
                )
                return
            if hasattr(dispatch, "clear_pending_prompt_record"):
                dispatch.clear_pending_prompt_record(ui_key)
            InputDispatchService.pop_pending(pending_map, ui_key)
            await self._respond_callback(
                context=context,
                query=query,
                chat_id=int(chat_id),
                text=t("msg.input.tmux_current_sent", lang),
            )
            await self._show_next_pending_input(
                chat_id=int(chat_id),
                context=context,
                message_thread_id=ui_key.message_thread_id,
            )
            return
        if query.data == "queue_append_pending":
            pending_map = self.bot_app.ui_state.pending
            pending, session, purged = self._next_live_pending_input(ui_key=ui_key)
            if not pending:
                await self._respond_callback(
                    context=context,
                    query=query,
                    chat_id=int(chat_id),
                    text=t("msg.error.session_closed_stale", lang) if purged else t("msg.input.no_pending", lang),
                )
                return
            dispatch = getattr(self.bot_app, "input_dispatch_service", None)
            if dispatch is not None and hasattr(dispatch, "clear_pending_prompt_record"):
                dispatch.clear_pending_prompt_record(ui_key)
            InputDispatchService.pop_pending(pending_map, ui_key)
            try:
                ok = InputDispatchService.append_pending_to_queue_tail(session, pending)
                if not ok:
                    raise RuntimeError("queue append rejected")
                await self._persist_session_async(int(chat_id), session.id)
            except Exception:
                logging.exception(
                    "failed to append pending input to queued tail session_id=%s",
                    getattr(session, "id", "?"),
                )
                await self._respond_callback(
                    context=context,
                    query=query,
                    chat_id=int(chat_id),
                    text=t("msg.error.queue_append_failed", lang),
                )
                return
            await self._respond_callback(
                context=context,
                query=query,
                chat_id=int(chat_id),
                text=t("msg.input.appended", lang),
            )
            await self._show_next_pending_input(
                chat_id=int(chat_id),
                context=context,
                message_thread_id=ui_key.message_thread_id,
            )
            await self._kick_session_queue_if_idle(session=session, chat_id=int(chat_id), context=context)
            return
        if query.data == "queue_input":
            pending_map = self.bot_app.ui_state.pending
            pending, session, purged = self._next_live_pending_input(ui_key=ui_key)
            if not pending:
                await self._respond_callback(
                    context=context,
                    query=query,
                    chat_id=int(chat_id),
                    text=t("msg.error.session_closed_stale", lang) if purged else t("msg.input.no_pending", lang),
                )
                return
            dispatch = getattr(self.bot_app, "input_dispatch_service", None)
            if dispatch is not None and hasattr(dispatch, "clear_pending_prompt_record"):
                dispatch.clear_pending_prompt_record(ui_key)
            item = InputDispatchService.queue_item_from_pending(pending)
            try:
                if not InputDispatchService.append_queue_item(session, item):
                    raise RuntimeError("queue append rejected")
                InputDispatchService.pop_pending(pending_map, ui_key)
                await self._persist_session_async(int(chat_id), session.id)
            except Exception:
                logging.exception("failed to enqueue pending input into session queue session_id=%s", getattr(session, "id", "?"))
                await self._respond_callback(
                    context=context,
                    query=query,
                    chat_id=int(chat_id),
                    text=t("msg.error.queue_failed", lang),
                )
                return
            await self._respond_callback(context=context, query=query, chat_id=int(chat_id), text=t("msg.input.queued", lang))
            await self._show_next_pending_input(
                chat_id=int(chat_id),
                context=context,
                message_thread_id=ui_key.message_thread_id,
            )
            await self._kick_session_queue_if_idle(session=session, chat_id=int(chat_id), context=context)
            return
        if query.data == "discard_input":
            pending_map = self.bot_app.ui_state.pending
            dispatch = getattr(self.bot_app, "input_dispatch_service", None)
            if dispatch is not None and hasattr(dispatch, "clear_pending_prompt_record"):
                dispatch.clear_pending_prompt_record(ui_key)
            pending = InputDispatchService.pop_pending(pending_map, ui_key)
            if not pending:
                await self._respond_callback(context=context, query=query, chat_id=int(chat_id), text=t("msg.input.no_pending", lang))
                return
            await self._respond_callback(context=context, query=query, chat_id=int(chat_id), text=t("msg.input.discarded", lang))
            await self._show_next_pending_input(
                chat_id=int(chat_id),
                context=context,
                message_thread_id=ui_key.message_thread_id,
            )
            return
