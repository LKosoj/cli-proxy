"""
Module containing message processing functionality for the Telegram bot.
"""

import asyncio
import logging
import os
import time

from telegram import Update, Message
from telegram.ext import ContextTypes

from app.services.telegram_ui_scope import TelegramUiKey
from i18n import t
from modes.sdk import decode_mode_dirs
from utils.lang import resolve_user_lang


TEXT_DOCUMENT_INLINE_LIMIT_BYTES = 5 * 1024


class MessageProcessor:
    """
    Class containing message processing functionality for the Telegram bot.
    """

    def __init__(self, bot_app):
        self.bot_app = bot_app

    def _ui_key(self, ui_chat_id: int, message_thread_id=None) -> TelegramUiKey:
        return self.bot_app.telegram_ui_key(int(ui_chat_id), message_thread_id)

    async def _handle_pending_session_message(self, ui_chat_id: int, text: str, context, *, message_thread_id=None) -> bool:
        handler = getattr(getattr(self.bot_app, "session_ui", None), "handle_pending_message", None)
        if not callable(handler):
            return False
        return bool(
            await handler(
                ui_chat_id,
                text,
                context,
                message_thread_id=message_thread_id,
            )
        )

    async def _handle_pending_git_commit_message(self, ui_chat_id: int, text: str, context, *, message_thread_id=None) -> bool:
        handler = getattr(getattr(self.bot_app, "git", None), "handle_pending_commit_message", None)
        if not callable(handler):
            return False
        return bool(
            await handler(
                ui_chat_id,
                text or "",
                context,
                message_thread_id=message_thread_id,
            )
        )

    async def _authorize_inbound(self, update: Update, context):
        ui_chat_id = int(update.effective_chat.id)
        if hasattr(self.bot_app, "ensure_telegram_inbound_authorized"):
            route = await self.bot_app.ensure_telegram_inbound_authorized(update, context)
            if route is None:
                return False, None, ui_chat_id, ui_chat_id, {"chat_id": ui_chat_id}
            return True, route, int(route.reply_chat_id), int(route.owner_chat_id), route.reply_kwargs()
        if not await self.bot_app.access_policy_service.ensure_allowed(ui_chat_id, context):
            return False, None, ui_chat_id, ui_chat_id, {"chat_id": ui_chat_id}
        return True, None, ui_chat_id, ui_chat_id, {"chat_id": ui_chat_id}

    def _resolve_session_by_uid(self, session_uid):
        manager = getattr(self.bot_app, "manager", None)
        getter = getattr(manager, "get_by_uid", None) if manager is not None else None
        if not callable(getter):
            return None
        uid = str(session_uid or "").strip()
        if not uid:
            return None
        return getter(uid)

    @staticmethod
    def _attachment_ref(session, path: str) -> str:
        workdir = str(getattr(session, "workdir", "") or "").strip()
        attachment_path = str(path or "").strip()
        if workdir and attachment_path:
            try:
                attachment_path = os.path.relpath(attachment_path, workdir)
            except Exception:
                pass
        return f"@{attachment_path}"

    async def _resolve_session(
        self,
        update: Update,
        context,
        *,
        owner_chat_id: int,
        reply_chat_id: int | None = None,
        message_thread_id=None,
        session_uid=None,
        auto_create: bool = False,
    ):
        if hasattr(self.bot_app, "ensure_telegram_inbound_session"):
            _route, session = await self.bot_app.ensure_telegram_inbound_session(
                update,
                context,
                auto_create=auto_create,
            )
            return session
        session = self._resolve_session_by_uid(session_uid)
        if session is not None:
            return session
        if auto_create:
            ensure_scope = getattr(self.bot_app, "ensure_scope_session", None)
            if callable(ensure_scope):
                return await ensure_scope(
                    owner_chat_id,
                    context,
                    reply_chat_id=reply_chat_id,
                    message_thread_id=message_thread_id,
                )
            return None
        resolver = getattr(self.bot_app, "resolve_telegram_scope_session", None)
        if callable(resolver):
            return resolver(
                reply_chat_id=int(reply_chat_id if reply_chat_id is not None else owner_chat_id),
                message_thread_id=message_thread_id,
                owner_chat_id=int(owner_chat_id),
            )
        return None

    def _reply_dest(self, session, ui_chat_id: int, *, user_id=None, direct_messages_topic_id=None) -> dict:
        builder = getattr(self.bot_app, "build_telegram_reply_dest", None)
        if callable(builder):
            kwargs = {"user_id": user_id}
            if direct_messages_topic_id is not None:
                kwargs["direct_messages_topic_id"] = direct_messages_topic_id
            return builder(
                session,
                int(ui_chat_id),
                **kwargs,
            )
        dest = {"kind": "telegram", "chat_id": int(ui_chat_id)}
        if user_id is not None:
            dest["user_id"] = int(user_id)
        if direct_messages_topic_id is not None:
            dest["direct_messages_topic_id"] = int(direct_messages_topic_id)
        return dest

    @staticmethod
    def _same_reply_scope(lhs: dict | None, rhs: dict | None) -> bool:
        left = dict(lhs or {})
        right = dict(rhs or {})
        try:
            left_chat = int(left.get("chat_id") or 0)
        except Exception:
            left_chat = 0
        try:
            right_chat = int(right.get("chat_id") or 0)
        except Exception:
            right_chat = 0
        try:
            left_thread = int(left.get("message_thread_id") or 0) or None
        except Exception:
            left_thread = None
        try:
            right_thread = int(right.get("message_thread_id") or 0) or None
        except Exception:
            right_thread = None
        return left_chat == right_chat and left_thread == right_thread

    async def _announce_created_session(
        self,
        *,
        context,
        origin_reply_kwargs: dict,
        owner_chat_id: int,
        ui_chat_id: int,
        session,
    ) -> None:
        lang = resolve_user_lang(self.bot_app.config, chat_id=owner_chat_id)
        target_reply_kwargs = {
            key: value
            for key, value in self._reply_dest(session, ui_chat_id).items()
            if key in {"chat_id", "message_thread_id", "direct_messages_topic_id"}
        }
        if self._same_reply_scope(origin_reply_kwargs, target_reply_kwargs):
            await self.bot_app._send_message(
                context,
                text=t("msg.session.created", lang, id=session.id),
                **target_reply_kwargs,
            )
            return
        await self.bot_app._send_message(
            context,
            text=t("msg.session.created_new_topic", lang, id=session.id),
            **origin_reply_kwargs,
        )
        handlers = getattr(self.bot_app, "handlers", None)
        overview_builder = getattr(handlers, "build_sessions_active_overview", None)
        if callable(overview_builder):
            overview_text, keyboard = overview_builder(owner_chat_id, session=session)
            await self.bot_app._send_message(
                context,
                text=overview_text,
                md2=True,
                reply_markup=keyboard,
                **target_reply_kwargs,
            )
            return
        await self.bot_app._send_message(
            context,
            text=t("msg.session.created", lang, id=session.id),
            **target_reply_kwargs,
        )

    def _has_attachments(self, message: Message) -> bool:
        return any(
            [
                message.document,
                message.photo,
                message.video,
                message.audio,
                message.voice,
                message.sticker,
                message.animation,
                message.video_note,
            ]
        )

    async def process_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        ok, route, ui_chat_id, owner_chat_id, reply_kwargs = await self._authorize_inbound(update, context)
        if not ok:
            return
        lang = resolve_user_lang(self.bot_app.config, chat_id=owner_chat_id)
        route_ui_chat_id = int(getattr(route, "reply_chat_id", ui_chat_id) or ui_chat_id)
        route_thread_id = getattr(route, "message_thread_id", None) if route is not None else None
        route_direct_topic_id = getattr(route, "direct_messages_topic_id", None) if route is not None else None
        route_session_uid = str(getattr(route, "session_uid", "") or "").strip() or None
        ui_key = self._ui_key(route_ui_chat_id, route_thread_id)
        text = update.message.text if update.message else None
        self.bot_app.ui_state.context_by_chat[route_ui_chat_id] = context
        self.bot_app.metrics.inc("messages")
        if self._has_attachments(update.message):
            return
        if await self._handle_pending_git_commit_message(
            route_ui_chat_id,
            text,
            context,
            message_thread_id=route_thread_id,
        ):
            return
        if await self._handle_pending_session_message(
            route_ui_chat_id,
            text,
            context,
            message_thread_id=route_thread_id,
        ):
            return
        if self.bot_app._resolve_pending_custom_answer(
            route_ui_chat_id,
            text or "",
            message_thread_id=route_thread_id,
        ):
            status = ""
            pop_status = getattr(self.bot_app, "_pop_pending_custom_input_status", None)
            if callable(pop_status):
                status = str(pop_status(route_ui_chat_id, message_thread_id=route_thread_id) or "")
            if status == "cancelled":
                await self.bot_app._send_message(context, text=t("msg.input.custom_cancelled", lang), **reply_kwargs)
            elif status == "stale":
                await self.bot_app._send_message(context, text=t("msg.input.question_stale", lang), **reply_kwargs)
            else:
                await self.bot_app._send_message(context, text=t("msg.input.custom_accepted", lang), **reply_kwargs)
            return
        if ui_key in self.bot_app.ui_state.files_pending_rename:
            pending = self.bot_app.ui_state.files_pending_rename.get(ui_key) or {}
            source_path = str(pending.get("path") or "")
            root_dir = str(pending.get("root") or "")
            expires_at = float(pending.get("expires_at", 0.0))
            if not source_path:
                self.bot_app._stop_files_rename_wait(route_ui_chat_id, message_thread_id=route_thread_id)
                await self.bot_app._send_message(context, text=t("msg.files.rename_cancelled", lang), **reply_kwargs)
                return
            name = (text or "").strip()
            if name in ("-", "отмена", "Отмена", "cancel", "Cancel"):
                self.bot_app._stop_files_rename_wait(route_ui_chat_id, message_thread_id=route_thread_id)
                await self.bot_app._send_message(context, text=t("msg.files.rename_cancelled", lang), **reply_kwargs)
                return
            if time.time() > expires_at:
                self.bot_app._stop_files_rename_wait(route_ui_chat_id, message_thread_id=route_thread_id)
                await self.bot_app._send_message(
                    context,
                    text=t("msg.files.rename_timeout", lang),
                    **reply_kwargs,
                )
                return
            if not os.path.exists(source_path):
                self.bot_app._stop_files_rename_wait(route_ui_chat_id, message_thread_id=route_thread_id)
                await self.bot_app._send_message(context, text=t("msg.files.rename_not_found", lang), **reply_kwargs)
                return
            if not self.bot_app.is_within_root(source_path, root_dir):
                self.bot_app._stop_files_rename_wait(route_ui_chat_id, message_thread_id=route_thread_id)
                await self.bot_app._send_message(context, text=t("msg.files.escapes_workdir", lang), **reply_kwargs)
                return
            if not name:
                await self.bot_app._send_message(context, text=t("msg.files.new_name_empty", lang), **reply_kwargs)
                return
            if os.path.basename(name) != name:
                await self.bot_app._send_message(context, text=t("msg.files.name_no_path", lang), **reply_kwargs)
                return
            target_path = os.path.join(os.path.dirname(source_path), name)
            if target_path == source_path:
                await self.bot_app._send_message(context, text=t("msg.files.name_unchanged", lang), **reply_kwargs)
                return
            if not self.bot_app.is_within_root(target_path, root_dir):
                await self.bot_app._send_message(context, text=t("msg.files.escapes_workdir", lang), **reply_kwargs)
                return
            if os.path.exists(target_path):
                await self.bot_app._send_message(context, text=t("msg.files.already_exists", lang), **reply_kwargs)
                return
            try:
                os.rename(source_path, target_path)
            except Exception as e:
                logging.exception(f"tool failed {str(e)}")
                await self.bot_app._send_message(context, text=t("msg.files.rename_failed", lang, e=e), **reply_kwargs)
                return
            self.bot_app._stop_files_rename_wait(route_ui_chat_id, message_thread_id=route_thread_id)
            await self.bot_app._send_message(
                context,
                text=t("msg.files.renamed", lang, name=os.path.basename(target_path)),
                **reply_kwargs,
            )
            session = route.session if route is not None else None
            if session is None:
                session = await self._resolve_session(
                    update,
                    context,
                    owner_chat_id=owner_chat_id,
                    reply_chat_id=route_ui_chat_id,
                    message_thread_id=route_thread_id,
                    session_uid=route_session_uid,
                    auto_create=False,
                )
            if session:
                await self.bot_app._send_files_menu(
                    route_ui_chat_id,
                    session,
                    context,
                    edit_message=None,
                    message_thread_id=route_thread_id,
                )
            return
        if ui_key in self.bot_app.ui_state.pending_dir_create:
            base = self.bot_app.ui_state.pending_dir_create.pop(ui_key)
            name = text.strip()
            if name in ("-", "отмена", "Отмена"):
                await self.bot_app._send_message(context, text=t("msg.files.dir_create_cancelled", lang), **reply_kwargs)
                return
            if not name:
                await self.bot_app._send_message(context, text=t("msg.files.dir_name_empty", lang), **reply_kwargs)
                return
            if not os.path.isdir(base):
                await self.bot_app._send_message(context, text=t("msg.files.base_dir_unavailable", lang), **reply_kwargs)
                return
            if os.path.isabs(name):
                target = os.path.normpath(name)
            else:
                target = os.path.normpath(os.path.join(base, name))
            root = self.bot_app.ui_state.dirs_root.get(ui_key, self.bot_app.config.defaults.workdir)
            if not self.bot_app.is_within_root(target, root):
                await self.bot_app._send_message(context, text=t("msg.dirs.cannot_leave_root", lang), **reply_kwargs)
                return
            if not self.bot_app.is_within_root(target, base):
                await self.bot_app._send_message(context, text=t("msg.files.path_must_be_inside", lang), **reply_kwargs)
                return
            if os.path.exists(target):
                await self.bot_app._send_message(context, text=t("msg.files.dir_exists", lang), **reply_kwargs)
                return
            try:
                os.makedirs(target, exist_ok=False)
            except Exception as e:
                logging.exception(f"tool failed {str(e)}")
                await self.bot_app._send_message(context, text=t("msg.files.dir_create_failed", lang, e=e), **reply_kwargs)
                return
            await self.bot_app._send_message(context, text=t("msg.files.dir_created", lang, path=target), **reply_kwargs)
            await self.bot_app._send_dirs_menu(
                route_ui_chat_id,
                context,
                base,
                message_thread_id=route_thread_id,
            )
            return
        if self.bot_app.ui_state.pending_dir_input.pop(ui_key, None):
            mode = self.bot_app.ui_state.dirs_mode.get(ui_key, "new_session")
            mode_id, flow = decode_mode_dirs(mode)
            if not self.bot_app.access_policy_service.can_input_project_path(owner_chat_id, mode_id=mode_id, flow=flow):
                # Users cannot input arbitrary paths; only admins can create sessions via directory picker.
                await self.bot_app._send_message(
                    context,
                    text=self.bot_app.access_policy_service.admin_denied_text("new_projects"),
                    **reply_kwargs,
                )
                # Clean up any pending mode/tool to avoid getting stuck.
                self.bot_app.ui_state.pending_new_tool.pop(ui_key, None)
                self.bot_app.ui_state.dirs_mode.pop(ui_key, None)
                return
            path = text.strip()
            if not os.path.isdir(path):
                await self.bot_app._send_message(context, text=t("msg.error.dir_not_found", lang), **reply_kwargs)
                return
            root = self.bot_app.ui_state.dirs_root.get(ui_key, self.bot_app.config.defaults.workdir)
            if not self.bot_app.is_within_root(path, root):
                await self.bot_app._send_message(context, text=t("msg.dirs.cannot_leave_root", lang), **reply_kwargs)
                return
            mode_id, flow = decode_mode_dirs(mode)
            if mode_id and flow:
                svc = getattr(self.bot_app, "mode_registry_service", None)
                plugin = svc.get(mode_id) if svc else None
                if plugin is not None and hasattr(plugin, "handle_dirs_selection"):
                    session = route.session if route is not None else None
                    if session is None:
                        session = await self._resolve_session(
                            update,
                            context,
                            owner_chat_id=owner_chat_id,
                            reply_chat_id=route_ui_chat_id,
                            message_thread_id=route_thread_id,
                            session_uid=route_session_uid,
                            auto_create=False,
                        )
                    user_id = getattr(getattr(update, "effective_user", None), "id", None)
                    dest = self._reply_dest(
                        session,
                        route_ui_chat_id,
                        user_id=user_id,
                        direct_messages_topic_id=route_direct_topic_id,
                    )
                    plugin_context = context
                    context_builder = getattr(self.bot_app, "build_telegram_transport_context", None)
                    if callable(context_builder):
                        context_kwargs = {
                            "session": session,
                            "chat_id": route_ui_chat_id,
                            "dest": dest,
                            "user_id": user_id,
                        }
                        if route_direct_topic_id is not None:
                            context_kwargs["direct_messages_topic_id"] = route_direct_topic_id
                        plugin_context = context_builder(context, **context_kwargs)
                    result = await plugin.handle_dirs_selection(
                        flow=flow,
                        event="path_input",
                        path=path,
                        ctx={
                            "bot_app": self.bot_app,
                            "session": session,
                            "chat_id": owner_chat_id,
                            "ui_chat_id": route_ui_chat_id,
                            "owner_chat_id": owner_chat_id,
                            "context": plugin_context,
                            "mode_id": mode_id,
                        },
                    )
                    if result is not None:
                        self.bot_app.ui_state.dirs_mode.pop(ui_key, None)
                        out = str(getattr(result, "output", "") or "").strip()
                        if out:
                            await self.bot_app._send_message(context, text=out, **reply_kwargs)
                        return
            session, err = await self.bot_app.session_creation_service.create_from_pending_tool(
                owner_chat_id=owner_chat_id,
                path=path,
                bot=getattr(context, "bot", None),
                message_thread_id=route_thread_id,
                ui_chat_id=route_ui_chat_id,
            )
            if err:
                await self.bot_app._send_message(context, text=err, **reply_kwargs)
                return
            await self._announce_created_session(
                context=context,
                origin_reply_kwargs=reply_kwargs,
                owner_chat_id=owner_chat_id,
                ui_chat_id=route_ui_chat_id,
                session=session,
            )
            return
        if ui_key in self.bot_app.ui_state.pending_git_clone:
            if not self.bot_app.access_policy_service.is_admin(owner_chat_id, scope="git"):
                self.bot_app.session_creation_service.pop_git_clone_pending(
                    owner_chat_id,
                    message_thread_id=route_thread_id,
                    ui_chat_id=route_ui_chat_id,
                )
                await self.bot_app._send_message(
                    context,
                    text=self.bot_app.access_policy_service.admin_denied_text("git"),
                    **reply_kwargs,
                )
                return
            base = self.bot_app.session_creation_service.pop_git_clone_pending(
                owner_chat_id,
                message_thread_id=route_thread_id,
                ui_chat_id=route_ui_chat_id,
            )
            url = text.strip()
            if not self.bot_app.is_within_root(
                base,
                self.bot_app.ui_state.dirs_root.get(ui_key, self.bot_app.config.defaults.workdir),
            ):
                await self.bot_app._send_message(context, text=t("msg.dirs.cannot_leave_root", lang), **reply_kwargs)
                return
            if not os.path.isdir(base):
                await self.bot_app._send_message(context, text=t("msg.error.dir_not_found", lang), **reply_kwargs)
                return
            await self.bot_app._send_message(context, text=t("msg.git.clone_starting", lang), **reply_kwargs)
            try:
                proc = await asyncio.create_subprocess_exec(
                    "git",
                    "clone",
                    url,
                    cwd=base,
                    env=self.bot_app.git.git_env(),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                )
                out, _ = await proc.communicate()
                output = (out or b"").decode(errors="ignore")
                if proc.returncode == 0:
                    await self.bot_app._send_message(context, text=t("msg.git.clone_done", lang), **reply_kwargs)
                    session, err = await self.bot_app.session_creation_service.complete_git_clone(
                        owner_chat_id=owner_chat_id,
                        base=base,
                        url=url,
                        output=output,
                        bot=getattr(context, "bot", None),
                        message_thread_id=route_thread_id,
                        ui_chat_id=route_ui_chat_id,
                    )
                    if err:
                        await self.bot_app._send_message(context, text=err, **reply_kwargs)
                        return
                    if session:
                        await self._announce_created_session(
                            context=context,
                            origin_reply_kwargs=reply_kwargs,
                            owner_chat_id=owner_chat_id,
                            ui_chat_id=route_ui_chat_id,
                            session=session,
                        )
                else:
                    await self.bot_app._send_message(
                        context, text=t("msg.git.clone_error", lang, output=output[:4000]), **reply_kwargs
                    )
            except Exception as e:
                logging.exception(f"tool failed {str(e)}")
                await self.bot_app._send_message(context, text=t("msg.git.clone_launch_error", lang, e=e), **reply_kwargs)
            return
        if self.bot_app._plugin_awaiting_input(route_ui_chat_id):
            # Safety net: if the agent was turned off while a dialog was active,
            # the plugin handler in group -1 won't fire (_AgentEnabledFilter blocks it).
            # Detect this and clean up so the user isn't stuck.
            session = route.session if route is not None else None
            if session is None:
                session = await self._resolve_session(
                    update,
                    context,
                    owner_chat_id=owner_chat_id,
                    reply_chat_id=route_ui_chat_id,
                    message_thread_id=route_thread_id,
                    session_uid=route_session_uid,
                    auto_create=False,
                )
            allows_plugin_ui = bool(getattr(self.bot_app, "_mode_allows_plugin_ui", lambda _s: False)(session))
            if not allows_plugin_ui:
                self.bot_app._cancel_plugin_dialogs(route_ui_chat_id)
            # Always fall through to normal on_message processing below.
        # Plugin handlers (group -1) stop propagation themselves when they
        # actually consume the message; otherwise we must not swallow input.
        session = route.session if route is not None else None
        if session is None:
            session = await self._resolve_session(
                update,
                context,
                owner_chat_id=owner_chat_id,
                reply_chat_id=route_ui_chat_id,
                message_thread_id=route_thread_id,
                session_uid=route_session_uid,
                auto_create=True,
            )
        if not session:
            return

        stripped = text.lstrip()
        if stripped.startswith(">"):
            forwarded = stripped[1:].lstrip()
            if not forwarded.startswith("/"):
                await self.bot_app._send_message(
                    context,
                    text=t("msg.input.forward_must_be_command", lang),
                    **reply_kwargs,
                )
                return
            await self.bot_app._handle_cli_input(
                session,
                forwarded,
                route_ui_chat_id,
                context,
                dest=self._reply_dest(
                    session,
                    route_ui_chat_id,
                    user_id=getattr(update.effective_user, "id", None),
                    direct_messages_topic_id=route_direct_topic_id,
                ),
            )
            return
        buffer_kwargs = {"user_id": getattr(update.effective_user, "id", None)}
        if route_direct_topic_id is not None:
            buffer_kwargs["direct_messages_topic_id"] = route_direct_topic_id
        await self.bot_app._buffer_or_send(
            session,
            text,
            route_ui_chat_id,
            context,
            **buffer_kwargs,
        )

    async def process_document(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        ok, route, ui_chat_id, owner_chat_id, reply_kwargs = await self._authorize_inbound(update, context)
        if not ok:
            return
        lang = resolve_user_lang(self.bot_app.config, chat_id=owner_chat_id)
        route_ui_chat_id = int(getattr(route, "reply_chat_id", ui_chat_id) or ui_chat_id)
        route_thread_id = getattr(route, "message_thread_id", None) if route is not None else None
        route_direct_topic_id = getattr(route, "direct_messages_topic_id", None) if route is not None else None
        route_session_uid = str(getattr(route, "session_uid", "") or "").strip() or None
        self.bot_app.metrics.inc("messages")
        doc = update.message.document
        if not doc:
            return
        filename = doc.file_name or ""
        lower = filename.lower()
        try:
            file_obj = await context.bot.get_file(doc.file_id)
            data = await file_obj.download_as_bytearray()
        except Exception as e:
            logging.exception(f"tool failed {str(e)}")
            await self.bot_app._send_message(context, text=t("msg.files.download_failed", lang, e=e), **reply_kwargs)
            return
        if await self.bot_app._maybe_save_pending_uploaded_file(
            route_ui_chat_id,
            doc,
            data,
            context,
            message_thread_id=route_thread_id,
        ):
            return
        session = route.session if route is not None else None
        if session is None:
            session = await self._resolve_session(
                update,
                context,
                owner_chat_id=owner_chat_id,
                reply_chat_id=route_ui_chat_id,
                message_thread_id=route_thread_id,
                session_uid=route_session_uid,
                auto_create=True,
            )
        if not session:
            return
        if lower.endswith(".png") or lower.endswith(".jpg") or lower.endswith(".jpeg") or (doc.mime_type or "").startswith("image/"):
            if doc.file_size and doc.file_size > self.bot_app.config.defaults.image_max_mb * 1024 * 1024:
                await self.bot_app._send_message(
                    context,
                    text=t("msg.files.image_too_large", lang, n=self.bot_app.config.defaults.image_max_mb),
                    **reply_kwargs,
                )
                return
            caption = (update.message.caption or "").strip()
            media_group_id = str(getattr(update.message, "media_group_id", "") or "").strip()
            if media_group_id:
                await self.bot_app._flush_media_groups_for_chat(route_ui_chat_id, exclude_media_group_id=media_group_id)
                image_path = await self.bot_app._store_image_bytes(session, data, filename or "image.jpg", route_ui_chat_id, context)
                if image_path:
                    await self.bot_app._add_media_group_image(
                        chat_id=route_ui_chat_id,
                        media_group_id=media_group_id,
                        session_id=session.id,
                        session_uid=getattr(getattr(session, "conversation_scope", None), "session_uid", None),
                        owner_chat_id=owner_chat_id,
                        context=context,
                        image_path=image_path,
                        caption=caption,
                    )
                return
            await self.bot_app._flush_media_groups_for_chat(route_ui_chat_id)
            await self.bot_app._flush_buffer(route_ui_chat_id, session, context)
            await self.bot_app._handle_image_bytes(session, data, filename or "image.jpg", caption, route_ui_chat_id, context)
            return
        if not (
            lower.endswith(".txt")
            or lower.endswith(".md")
            or lower.endswith(".rst")
            or lower.endswith(".log")
            or lower.endswith(".html")
            or lower.endswith(".htm")
        ):
            await self.bot_app._send_message(
                context,
                text=t("msg.files.unsupported_doc_type", lang),
                **reply_kwargs,
            )
            return
        actual_size = len(data)
        caption = (update.message.caption or "").strip()
        media_group_id = str(getattr(update.message, "media_group_id", "") or "").strip()
        display_name = filename or "attachment.txt"
        if actual_size > TEXT_DOCUMENT_INLINE_LIMIT_BYTES:
            attachment_path = await self.bot_app._store_attachment_bytes(
                session,
                data,
                display_name,
                route_ui_chat_id,
                context,
            )
            if not attachment_path:
                return
            block = f"===== Вложение: {display_name} =====\n\n{self._attachment_ref(session, attachment_path)}"
        else:
            content = data.decode("utf-8", errors="replace")
            block = f"===== Вложение: {display_name} =====\n\n{content}"
        if media_group_id:
            await self.bot_app._flush_media_groups_for_chat(route_ui_chat_id, exclude_media_group_id=media_group_id)
            await self.bot_app._add_media_group_document(
                chat_id=route_ui_chat_id,
                media_group_id=media_group_id,
                session_id=session.id,
                session_uid=getattr(getattr(session, "conversation_scope", None), "session_uid", None),
                owner_chat_id=owner_chat_id,
                context=context,
                block=block,
                caption=caption,
            )
            return
        await self.bot_app._flush_media_groups_for_chat(route_ui_chat_id)
        await self.bot_app._flush_buffer(route_ui_chat_id, session, context)
        payload = "\n\n".join([caption, block]) if caption else block
        await self.bot_app._stage_user_input(
            session,
            payload,
            route_ui_chat_id,
            context,
            dest=self._reply_dest(
                session,
                route_ui_chat_id,
                user_id=(getattr(self.bot_app, "message_buffer_user_id", {}) or {}).get(
                    self.bot_app.message_buffer_service._scope_buffer_key(session, route_ui_chat_id)
                ),
                direct_messages_topic_id=route_direct_topic_id,
            ),
        )

    async def process_photo(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        ok, route, ui_chat_id, owner_chat_id, reply_kwargs = await self._authorize_inbound(update, context)
        if not ok:
            return
        lang = resolve_user_lang(self.bot_app.config, chat_id=owner_chat_id)
        route_ui_chat_id = int(getattr(route, "reply_chat_id", ui_chat_id) or ui_chat_id)
        route_thread_id = getattr(route, "message_thread_id", None) if route is not None else None
        route_session_uid = str(getattr(route, "session_uid", "") or "").strip() or None
        self.bot_app.metrics.inc("messages")
        photos = update.message.photo or []
        if not photos:
            return
        session = route.session if route is not None else None
        if session is None:
            session = await self._resolve_session(
                update,
                context,
                owner_chat_id=owner_chat_id,
                reply_chat_id=route_ui_chat_id,
                message_thread_id=route_thread_id,
                session_uid=route_session_uid,
                auto_create=True,
            )
        if not session:
            return
        photo = photos[-1]
        if photo.file_size and photo.file_size > self.bot_app.config.defaults.image_max_mb * 1024 * 1024:
            await self.bot_app._send_message(
                context,
                text=t("msg.files.image_too_large", lang, n=self.bot_app.config.defaults.image_max_mb),
                **reply_kwargs,
            )
            return
        try:
            file_obj = await context.bot.get_file(photo.file_id)
            data = await file_obj.download_as_bytearray()
        except Exception as e:
            logging.exception(f"tool failed {str(e)}")
            await self.bot_app._send_message(context, text=t("msg.files.image_download_failed", lang, e=e), **reply_kwargs)
            return
        caption = (update.message.caption or "").strip()
        filename = f"{photo.file_unique_id}.jpg"
        media_group_id = str(getattr(update.message, "media_group_id", "") or "").strip()
        if media_group_id:
            await self.bot_app._flush_media_groups_for_chat(route_ui_chat_id, exclude_media_group_id=media_group_id)
            image_path = await self.bot_app._store_image_bytes(session, data, filename, route_ui_chat_id, context)
            if image_path:
                await self.bot_app._add_media_group_image(
                    chat_id=route_ui_chat_id,
                    media_group_id=media_group_id,
                    session_id=session.id,
                    session_uid=getattr(getattr(session, "conversation_scope", None), "session_uid", None),
                    owner_chat_id=owner_chat_id,
                    context=context,
                    image_path=image_path,
                    caption=caption,
                )
            return
        await self.bot_app._flush_media_groups_for_chat(route_ui_chat_id)
        await self.bot_app._flush_buffer(route_ui_chat_id, session, context)
        await self.bot_app._handle_image_bytes(session, data, filename, caption, route_ui_chat_id, context)
