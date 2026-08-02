"""Session/UI callback actions."""

import asyncio
import logging
import os

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from app.services.menu_visibility_policy import build_mode_menu_visibility, call_mode_build_menu
from app.services.path_normalization import normalize_optional_state_path
from app.services.state_repository import get_state_repository
from modes.sdk.services.callback_data import (
    build_session_overview_callback_data,
)
from sessions.session_state_access import get_active_mode, is_ssh_remote_enabled, set_ssh_remote_enabled
from app.services.ssh_config_loader import ssh_remote_available
from session import set_session_execution_backend
from tg.handlers import build_lang_menu, format_session_state
from i18n import t, SUPPORTED_LANGS, lang_from_query
from i18n.resolver import resolve_language


class SessionActionsMixin:
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

    def _session_reply_kwargs(self, session, *, ui_chat_id: int) -> dict[str, int]:
        builder = getattr(self.bot_app, "build_telegram_reply_dest", None)
        if callable(builder):
            dest = builder(session, int(ui_chat_id))
            filtered = {
                key: value
                for key, value in dict(dest or {}).items()
                if key in {"chat_id", "message_thread_id", "direct_messages_topic_id"}
            }
            if filtered.get("chat_id") is not None:
                return filtered
        return {"chat_id": int(ui_chat_id)}

    async def _present_selected_session_after_callback(
        self,
        *,
        context,
        query,
        owner_chat_id: int,
        session,
        created: bool,
    ) -> None:
        lang = lang_from_query(query, self.bot_app.config)
        ui_key = self.bot_app.telegram_ui_key_from_query(query) or self.bot_app.telegram_ui_key(owner_chat_id)
        current_kwargs = ui_key.reply_kwargs()
        target_kwargs = self._session_reply_kwargs(session, ui_chat_id=ui_key.chat_id)
        text, keyboard = self.bot_app.handlers.build_sessions_active_overview(owner_chat_id, session=session)
        if self._same_reply_scope(current_kwargs, target_kwargs):
            await self._edit_msg(context, query, text=text, reply_markup=keyboard)
            return
        await self.bot_app._send_message(
            context,
            text=text,
            md2=True,
            reply_markup=keyboard,
            **target_kwargs,
        )
        notice = (
            t("msg.session.created_bound_topic", lang, id=session.id)
            if created
            else t("msg.session.selected_topic", lang, id=session.id)
        )
        await self._edit_msg(context, query, notice)

    def _state_repository(self):
        cfg = getattr(self.bot_app, "config", None)
        defaults = getattr(cfg, "defaults", None) if cfg is not None else None
        raw_state_path = getattr(defaults, "state_path", None)
        try:
            state_path = normalize_optional_state_path(raw_state_path)
        except TypeError:
            logging.getLogger(__name__).warning(
                "telegram callback state repository disabled: invalid state_path type=%s",
                type(raw_state_path).__name__,
            )
            return None
        if not state_path:
            return None
        return get_state_repository(state_path)

    def _callback_scope(self, chat_id: int, query) -> tuple[int, int, object | None]:
        resolver = getattr(self.bot_app, "resolve_telegram_callback_scope", None)
        if callable(resolver):
            reply_chat_id, _thread_id, owner_chat_id, session = resolver(query)
            return int(reply_chat_id or chat_id), int(owner_chat_id or chat_id), session
        return int(chat_id), int(chat_id), None

    async def _cb_sess_active(self, *, data: str, chat_id: int, query, context) -> bool:
        _reply_chat_id, owner_chat_id, session = self._callback_scope(chat_id, query)
        text, keyboard = self.bot_app.handlers.build_sessions_active_overview(owner_chat_id, session=session)
        await self._edit_msg(context, query, text=text, reply_markup=keyboard)
        return True

    async def _cb_sess_active_pick(self, *, data: str, chat_id: int, query, context) -> bool:
        lang = lang_from_query(query, self.bot_app.config)
        session_uid = str(data or "").split(":", 1)[1].strip() if ":" in str(data or "") else ""
        if not session_uid:
            await self._edit_msg(context, query, t("msg.error.session_not_found", lang))
            return True
        session = self.bot_app.manager.get_by_uid(session_uid)
        if not session:
            await self._edit_msg(context, query, t("msg.error.session_not_found", lang))
            return True
        owner_chat_id = int(getattr(session, "chat_id", 0) or chat_id)
        await self._present_selected_session_after_callback(
            context=context,
            query=query,
            owner_chat_id=owner_chat_id,
            session=session,
            created=False,
        )
        return True

    async def _cb_user_project_menu(self, *, data: str, chat_id: int, query, context) -> bool:
        lang = lang_from_query(query, self.bot_app.config)
        payload = str(data or "").split(":", 1)[1].strip() if ":" in str(data or "") else ""
        _reply_chat_id, owner_chat_id, scope_session = self._callback_scope(chat_id, query)
        session = self.bot_app.manager.get_by_uid(payload) if payload else scope_session
        if session is not None:
            owner_chat_id = int(getattr(session, "chat_id", 0) or owner_chat_id)
        back_callback = build_session_overview_callback_data(session) if session is not None else "sess_active"
        text, keyboard = self.bot_app.handlers.build_user_project_picker(
            owner_chat_id,
            session_uid=str(getattr(getattr(session, "conversation_scope", None), "session_uid", "") or payload or ""),
            force_new=False,
            back_callback=back_callback,
            lang=lang,
        )
        await self._edit_msg(context, query, text, reply_markup=keyboard)
        return True

    async def _handle_user_project_pick(
        self,
        *,
        data: str,
        chat_id: int,
        query,
        context,
        force_new: bool,
    ) -> bool:
        lang = lang_from_query(query, self.bot_app.config)
        payload = str(data or "").split(":", 1)[1].strip() if ":" in str(data or "") else ""
        current = None
        idx_token = payload
        tool_name = ""
        candidate_prefix = ""
        if ":" in payload:
            candidate_prefix, candidate_idx = payload.rsplit(":", 1)
            candidate_prefix = candidate_prefix.strip()
            idx_token = candidate_idx.strip()
            if candidate_prefix.startswith("tool="):
                tool_name = candidate_prefix[len("tool="):].strip()
                candidate_prefix = ""
            elif ":tool=" in candidate_prefix:
                candidate_prefix, tool_name = candidate_prefix.rsplit(":tool=", 1)
                candidate_prefix = candidate_prefix.strip()
                tool_name = tool_name.strip()
        if candidate_prefix:
            candidate_session = self.bot_app.manager.get_by_uid(candidate_prefix) if candidate_prefix else None
            if candidate_session is not None:
                current = candidate_session
        try:
            idx = int(idx_token)
        except Exception:
            await self._edit_msg(context, query, t("msg.error.choice_unavailable", lang))
            return True
        _reply_chat_id, owner_chat_id, scope_session = self._callback_scope(chat_id, query)
        if current is None:
            current = scope_session
        if current is not None:
            owner_chat_id = int(getattr(current, "chat_id", 0) or owner_chat_id)
        projects = self.bot_app.user_projects(owner_chat_id)
        if idx < 0 or idx >= len(projects):
            await self._edit_msg(context, query, t("msg.error.choice_unavailable", lang))
            return True
        if current and not force_new:
            busy = bool(getattr(current, "busy", False))
            locked = bool(getattr(current, "run_lock", None) and current.run_lock.locked())
            ticking = bool(getattr(current, "is_active_by_tick", None) and current.is_active_by_tick())
            if busy or locked or ticking:
                await self._edit_msg(context, query, t("msg.error.session_busy_project", lang))
                return True
        target = projects[idx]
        found_id = None
        if not force_new:
            for sid, s in self.bot_app.manager.sessions_for_chat(owner_chat_id).items():
                try:
                    if os.path.realpath(s.workdir) == os.path.realpath(target):
                        found_id = sid
                        break
                except Exception:
                    continue
        if found_id:
            session = self.bot_app.manager.get(owner_chat_id, found_id)
        else:
            preferred = tool_name or None
            if current and not preferred:
                preferred = str(getattr(current, "active_cli", "") or current.tool.name)
            session, err = await self.bot_app.session_creation_service.create_session(
                owner_chat_id,
                preferred,
                target,
                bot=getattr(context, "bot", None),
            )
            if err:
                await self._edit_msg(context, query, err)
                return True
        await self._present_selected_session_after_callback(
            context=context,
            query=query,
            owner_chat_id=owner_chat_id,
            session=session,
            created=found_id is None,
        )
        return True

    async def _cb_user_project_pick(self, *, data: str, chat_id: int, query, context) -> bool:
        return await self._handle_user_project_pick(
            data=data,
            chat_id=chat_id,
            query=query,
            context=context,
            force_new=False,
        )

    async def _cb_user_project_pick_new(self, *, data: str, chat_id: int, query, context) -> bool:
        return await self._handle_user_project_pick(
            data=data,
            chat_id=chat_id,
            query=query,
            context=context,
            force_new=True,
        )

    async def _cb_sess_list(self, *, data: str, chat_id: int, query, context) -> bool:
        lang = lang_from_query(query, self.bot_app.config)
        _reply_chat_id, owner_chat_id, _session = self._callback_scope(chat_id, query)
        keyboard = self.bot_app.session_ui.build_sessions_menu(
            owner_chat_id,
            include_back=True, back_callback="sess_active", back_text=t("common.back", lang)
        )
        await self._edit_msg(context, query, t("msg.session.choose", lang), reply_markup=keyboard)
        return True

    async def _cb_sess_new(self, *, data: str, chat_id: int, query, context) -> bool:
        await self.bot_app.handlers.show_new_session_menu(chat_id, context, edit_message=query)
        return True

    async def _cb_sess_cli(self, *, data: str, chat_id: int, query, context) -> bool:
        lang = lang_from_query(query, self.bot_app.config)
        payload = str(data).split(":", 1)[1].strip() if ":" in str(data) else ""
        session = None
        owner_chat_id = int(chat_id)
        cli = payload
        if ":" in payload:
            candidate_uid, candidate_cli = payload.rsplit(":", 1)
            candidate_uid = candidate_uid.strip()
            candidate_session = self.bot_app.manager.get_by_uid(candidate_uid) if candidate_uid else None
            if candidate_session is not None:
                session = candidate_session
                owner_chat_id = int(getattr(session, "chat_id", 0) or owner_chat_id)
                cli = candidate_cli.strip()
        if session is None:
            _reply_chat_id, owner_chat_id, session = self._callback_scope(chat_id, query)
        if not session:
            await self._edit_msg(context, query, t("msg.error.session_no_scope", lang))
            return True
        available = list(sorted(self.bot_app._available_tools()))
        if cli not in available:
            await self._edit_msg(context, query, t("msg.error.cli_unavailable", lang))
            return True
        busy = bool(getattr(session, "busy", False))
        locked = bool(getattr(session, "run_lock", None) and session.run_lock.locked())
        ticking = bool(getattr(session, "is_active_by_tick", None) and session.is_active_by_tick())
        if busy or locked or ticking:
            await self._edit_msg(context, query, t("msg.error.session_busy", lang))
            return True
        try:
            if not hasattr(session, "set_active_cli_persistent_when_idle"):
                await self._edit_msg(context, query, t("msg.error.cli_switch_unsupported", lang))
                return True
            # Capture previous CLI info for transfer offer.
            previous_cli = str(getattr(getattr(session, "cli", None), "active_cli", "") or "").strip()
            previous_token = (getattr(getattr(session, "cli", None), "resume_tokens", None) or {}).get(previous_cli)
            await session.set_active_cli_persistent_when_idle(cli)
            await self._persist_session_async(owner_chat_id, session.id)
        except Exception:
            logging.getLogger(__name__).exception(
                "session CLI switch failed session_id=%s previous_cli=%s target_cli=%s",
                getattr(session, "id", ""),
                previous_cli,
                cli,
            )
            await self._edit_msg(context, query, t("msg.error.cli_switch_failed", lang))
            return True
        # Offer session transfer if source CLI had a session.
        transfer_available = bool(
            previous_cli
            and previous_cli != cli
            and previous_token
            and str(previous_token).strip()
        )
        if transfer_available:
            from session import session_runtime_uid

            session_uid = session_runtime_uid(session)
            transfer_keyboard = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        t("btn.transfer.yes", lang),
                        callback_data=f"sess_transfer_yes:{session_uid}:{previous_cli}",
                    ),
                    InlineKeyboardButton(
                        t("btn.transfer.no", lang),
                        callback_data=f"sess_transfer_no:{session_uid}",
                    ),
                ]
            ])
            transfer_text = t("msg.transfer.offer", lang, src=previous_cli, dst=cli)
            await self._edit_msg(context, query, text=transfer_text, reply_markup=transfer_keyboard)
        else:
            text, keyboard = self.bot_app.handlers.build_sessions_active_overview(owner_chat_id, session=session)
            await self._edit_msg(context, query, text=text, reply_markup=keyboard)
        return True

    async def _cb_sess_backend(self, *, data: str, chat_id: int, query, context) -> bool:
        lang = lang_from_query(query, self.bot_app.config)
        payload = str(data).split(":", 1)[1].strip() if ":" in str(data) else ""
        if ":" not in payload:
            await self._edit_msg(context, query, t("msg.error.callback_format", lang))
            return True
        session_uid, backend = payload.rsplit(":", 1)
        session_uid = session_uid.strip()
        backend = backend.strip()
        session = self.bot_app.manager.get_by_uid(session_uid) if session_uid else None
        if session is None:
            session = self.bot_app.manager.get(int(chat_id), session_uid) if session_uid else None
        if not session:
            await self._edit_msg(context, query, t("msg.error.session_not_found", lang))
            return True
        _reply_chat_id, callback_owner_chat_id, _scope_session = self._callback_scope(chat_id, query)
        owner_chat_id = int(getattr(session, "chat_id", 0) or chat_id)
        access_chat_id = int(callback_owner_chat_id or chat_id)
        access_checker = getattr(self.bot_app, "is_session_allowed_for_chat", None)
        if callable(access_checker) and not bool(access_checker(access_chat_id, session)):
            await self._edit_msg(context, query, t("msg.error.session_unavailable", lang))
            return True
        result = set_session_execution_backend(session, backend)
        if result.reason:
            if result.reason == "unsupported backend" or result.reason == "backend not available for cli":
                await self._edit_msg(context, query, t("msg.error.execution_backend_unavailable", lang))
                return True
            await self._edit_msg(
                context,
                query,
                t("msg.error.execution_backend_switch_failed", lang, reason=result.reason),
            )
            return True
        await self._persist_session_async(owner_chat_id, session.id)
        text, keyboard = self.bot_app.handlers.build_sessions_active_overview(owner_chat_id, session=session)
        await self._edit_msg(context, query, text=text, reply_markup=keyboard)
        return True

    async def _cb_sess_transfer_yes(self, *, data: str, chat_id: int, query, context) -> bool:
        """User confirmed session transfer: read source session and write it into the target CLI."""
        lang = lang_from_query(query, self.bot_app.config)
        # data format: "sess_transfer_yes:<session_uid>:<source_cli>"
        payload = str(data).split(":", 1)[1].strip() if ":" in str(data) else ""
        if ":" not in payload:
            await self._edit_msg(context, query, t("msg.error.callback_format", lang))
            return True
        session_uid, source_cli = payload.rsplit(":", 1)
        session_uid = session_uid.strip()
        source_cli = source_cli.strip()
        if not session_uid or not source_cli:
            await self._edit_msg(context, query, t("msg.error.callback_format", lang))
            return True
        session = self.bot_app.manager.get_by_uid(session_uid) if session_uid else None
        if not session:
            await self._edit_msg(context, query, t("msg.error.session_not_found", lang))
            return True
        try:
            from app.services.session_transfer.service import extract_session, write_target_session

            target_cli = str(getattr(getattr(session, "cli", None), "active_cli", "") or "").strip()
            source_token = (getattr(getattr(session, "cli", None), "resume_tokens", None) or {}).get(source_cli)
            workspace = getattr(session, "workdir", "") or ""
            if not (source_token and workspace and target_cli):
                await self._edit_msg(context, query, t("msg.error.transfer_extract_failed", lang))
                return True

            canonical = extract_session(source_cli, str(source_token), workspace)
            if not canonical or not canonical.messages:
                await self._edit_msg(context, query, t("msg.error.transfer_extract_failed", lang))
                return True

            new_token = write_target_session(canonical, target_cli, workspace)
            if not new_token:
                await self._edit_msg(context, query, t("msg.error.transfer_write_failed", lang, cli=target_cli))
                return True

            session.resume_token = new_token
            owner_chat_id = int(getattr(session, "chat_id", 0) or chat_id)
            await self._persist_session_async(owner_chat_id, session.id)
            await self._edit_msg(
                context, query,
                t("msg.transfer.done", lang, n=len(canonical.messages), src=source_cli, dst=target_cli),
            )
        except Exception:
            logging.getLogger(__name__).exception("session transfer failed")
            await self._edit_msg(context, query, t("msg.error.transfer_failed", lang))
        return True

    async def _cb_sess_transfer_no(self, *, data: str, chat_id: int, query, context) -> bool:
        """User declined session transfer."""
        lang = lang_from_query(query, self.bot_app.config)
        # data format: "sess_transfer_no:<session_uid>"
        parts = str(data).split(":", 1)
        session_uid = parts[1].strip() if len(parts) > 1 else ""
        session = self.bot_app.manager.get_by_uid(session_uid) if session_uid else None
        if session:
            owner_chat_id = int(getattr(session, "chat_id", 0) or chat_id)
            text, keyboard = self.bot_app.handlers.build_sessions_active_overview(owner_chat_id, session=session)
            await self._edit_msg(context, query, text=text, reply_markup=keyboard)
        else:
            await self._edit_msg(context, query, t("msg.transfer.declined", lang))
        return True

    async def _open_mode_menu(self, *, mode_id: str, chat_id: int, query, context, session, owner_chat_id: int) -> bool:
        lang = lang_from_query(query, self.bot_app.config)
        policy = getattr(self.bot_app, "access_policy_service", None)
        is_mode_allowed = policy.is_mode_allowed_for_chat(owner_chat_id, mode_id) if policy else True
        if not is_mode_allowed:
            await self._edit_msg(context, query, t("msg.error.mode_unavailable_user", lang))
            return True
        svc = getattr(self.bot_app, "mode_registry_service", None)
        plugin = svc.get(mode_id) if svc else None
        if plugin is None:
            await self._edit_msg(context, query, t("msg.error.mode_unavailable", lang))
            return True
        if hasattr(plugin, "build_menu"):
            try:
                menu_visibility = build_mode_menu_visibility(
                    session=session,
                    mode_id=mode_id,
                    access_policy=getattr(self.bot_app, "access_policy_service", None),
                    user_id=getattr(getattr(query, "from_user", None), "id", None),
                )
                text, keyboard = call_mode_build_menu(
                    plugin,
                    session,
                    back_callback=build_session_overview_callback_data(session),
                    back_text=t("common.back", lang),
                    menu_visibility=menu_visibility,
                )
                await self._edit_msg(context, query, text=text, reply_markup=keyboard, md2=True)
                return True
            except Exception:
                logging.getLogger(__name__).exception("mode build_menu failed mode=%s", mode_id)
        try:
            from modes.sdk import CallbackModel

            result = await plugin.handle_callback(
                CallbackModel(
                    action="menu",
                    chat_id=int(chat_id),
                    payload={},
                    user_id=getattr(getattr(query, "from_user", None), "id", None),
                    message_id=getattr(getattr(query, "message", None), "message_id", None),
                    raw={"query": query, "data": f"sess_mode:{mode_id}"},
                ),
                {
                    "bot_app": self.bot_app,
                    "session": session,
                    "chat_id": chat_id,
                    "context": (
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
                    "query": query,
                    "mode_id": mode_id,
                },
            )
            if result and getattr(result, "output", None):
                await self.bot_app.send_output(
                    session,
                    self.bot_app.build_telegram_reply_dest(
                        session,
                        int(chat_id),
                        user_id=getattr(getattr(query, "from_user", None), "id", None),
                    ),
                    str(result.output),
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
            return True
        except Exception:
            logging.getLogger(__name__).exception("mode open menu callback failed mode=%s", mode_id)
            await self._edit_msg(context, query, t("msg.error.mode_menu_failed", lang))
            return True

    async def _cb_sess_mode(self, *, data: str, chat_id: int, query, context) -> bool:
        lang = lang_from_query(query, self.bot_app.config)
        _reply_chat_id, owner_chat_id, session = self._callback_scope(chat_id, query)
        if not session:
            await self._edit_msg(context, query, t("msg.error.session_no_scope", lang))
            return True
        mode_id = str(data.split(":", 1)[1] or "").strip()
        return await self._open_mode_menu(
            mode_id=mode_id,
            chat_id=chat_id,
            query=query,
            context=context,
            session=session,
            owner_chat_id=owner_chat_id,
        )

    async def _cb_sess_mode_pick(self, *, data: str, chat_id: int, query, context) -> bool:
        lang = lang_from_query(query, self.bot_app.config)
        payload = str(data or "").split(":", 1)[1].strip() if ":" in str(data or "") else ""
        session_uid = payload
        explicit_mode_id = ""
        if not session_uid:
            await self._edit_msg(context, query, t("msg.error.session_not_found", lang))
            return True
        _reply_chat_id, owner_chat_id, _session = self._callback_scope(chat_id, query)
        session = self.bot_app.manager.get_by_uid(session_uid)
        if session is None and ":" in payload:
            candidate_uid, candidate_mode_id = payload.rsplit(":", 1)
            candidate_uid = candidate_uid.strip()
            candidate_mode_id = candidate_mode_id.strip()
            candidate_session = self.bot_app.manager.get_by_uid(candidate_uid) if candidate_uid else None
            if candidate_session is not None:
                session_uid = candidate_uid
                explicit_mode_id = candidate_mode_id
                session = candidate_session
        if not session:
            await self._edit_msg(context, query, t("msg.error.session_not_found", lang))
            return True
        mode_id = explicit_mode_id or str(get_active_mode(session, "") or "").strip()
        if not mode_id:
            text, keyboard = self.bot_app.handlers.build_sessions_active_overview(owner_chat_id, session=session)
            await self._edit_msg(context, query, text=text, reply_markup=keyboard)
            return True
        return await self._open_mode_menu(
            mode_id=mode_id,
            chat_id=chat_id,
            query=query,
            context=context,
            session=session,
            owner_chat_id=owner_chat_id,
        )

    async def _cb_agent_cancel(self, *, data: str, chat_id: int, query, context) -> bool:
        lang = lang_from_query(query, self.bot_app.config)
        _reply_chat_id, _owner_chat_id, session = self._callback_scope(chat_id, query)
        if session is not None:
            try:
                pending = getattr(self.bot_app, "manager_resume_pending", None)
                if isinstance(pending, dict):
                    pending.pop(getattr(session, "id", None), None)
            except Exception:
                logging.getLogger(__name__).exception("failed to clear manager pending on agent_cancel")
        await self._edit_msg(context, query, t("msg.session.cancelled", lang))
        return True

    async def _cb_state_pick(self, *, data: str, chat_id: int, query, context) -> bool:
        lang = lang_from_query(query, self.bot_app.config)
        ui_key = self.bot_app.telegram_ui_key_from_query(query) or self.bot_app.telegram_ui_key(int(chat_id))
        _reply_chat_id, owner_chat_id, _session = self._callback_scope(chat_id, query)
        idx = int(str(data).split(":", 1)[1])
        keys = self.bot_app.ui_state.state_menu.get(ui_key, [])
        if idx < 0 or idx >= len(keys):
            await self._edit_msg(context, query, t("msg.error.choice_unavailable", lang))
            return True
        repo = self._state_repository()
        if repo is None:
            await self._edit_msg(context, query, t("msg.session.state_path_missing", lang))
            return True
        data_state = repo.load_state(chat_id=owner_chat_id)
        key = keys[idx]
        st = data_state.get(key)
        if not st:
            await self._edit_msg(context, query, t("msg.session.state_not_found", lang))
            return True
        text = format_session_state(st, self.bot_app._format_ts(st.updated_at), lang)
        await self._edit_msg(context, query, text)
        return True

    async def _cb_state_page(self, *, data: str, chat_id: int, query, context) -> bool:
        lang = lang_from_query(query, self.bot_app.config)
        ui_key = self.bot_app.telegram_ui_key_from_query(query) or self.bot_app.telegram_ui_key(int(chat_id))
        page = int(str(data).split(":", 1)[1])
        keys = self.bot_app.ui_state.state_menu.get(ui_key, [])
        if not keys:
            await self._edit_msg(context, query, t("msg.session.state_not_found", lang))
            return True
        self.bot_app.ui_state.state_menu_page[ui_key] = page
        await self._edit_msg(
            context,
            query,
            t("msg.session.state_choose", lang),
            reply_markup=self.bot_app._build_state_keyboard(ui_key),
        )
        return True

    async def _cb_close_pick(self, *, data: str, chat_id: int, query, context) -> bool:
        lang = lang_from_query(query, self.bot_app.config)
        ui_key = self.bot_app.telegram_ui_key_from_query(query) or self.bot_app.telegram_ui_key(int(chat_id))
        _reply_chat_id, owner_chat_id, _session = self._callback_scope(chat_id, query)
        idx = int(str(data).split(":", 1)[1])
        items = self.bot_app.ui_state.close_menu.get(ui_key, [])
        if idx < 0 or idx >= len(items):
            await self._edit_msg(context, query, t("msg.error.choice_unavailable", lang))
            return True
        sid = items[idx]
        ok = await self.bot_app.close_session_with_cleanup(sid, owner_chat_id, context)
        if ok:
            await self._edit_msg(context, query, t("msg.session.closed", lang))
        else:
            await self._edit_msg(context, query, t("msg.error.session_not_found", lang))
        return True

    async def _cb_sess_ssh_toggle(self, *, data: str, chat_id: int, query, context) -> bool:
        lang = lang_from_query(query, self.bot_app.config)
        payload = str(data or "").split(":", 1)[1].strip() if ":" in str(data or "") else ""
        session = self.bot_app.manager.get_by_uid(payload) if payload else None
        if not session:
            _reply_chat_id, owner_chat_id, session = self._callback_scope(chat_id, query)
        if not session:
            await self._edit_msg(context, query, t("msg.error.session_not_found", lang))
            return True

        if not ssh_remote_available(session.workdir):
            await query.answer(t("msg.session.ssh_unavailable", lang), show_alert=True)
            return True

        current = is_ssh_remote_enabled(session)
        set_ssh_remote_enabled(session, not current)

        owner_chat_id = int(getattr(session, "chat_id", 0) or chat_id)
        await self._persist_session_async(owner_chat_id, session.id)

        ssh_msg = t("msg.session.ssh_enabled", lang) if not current else t("msg.session.ssh_disabled", lang)
        await query.answer(ssh_msg)

        text, keyboard = self.bot_app.handlers.build_sessions_active_overview(owner_chat_id, session=session)
        await self._edit_msg(context, query, text=text, reply_markup=keyboard)
        return True

    async def _cb_sess_tmux_reread(self, *, data: str, chat_id: int, query, context) -> bool:
        lang = lang_from_query(query, self.bot_app.config)
        payload = str(data or "").split(":", 1)[1].strip() if ":" in str(data or "") else ""
        _reply_chat_id, owner_chat_id, scope_session = self._callback_scope(chat_id, query)
        session = self.bot_app.manager.get_by_uid(payload) if payload else scope_session
        if session is None:
            await self._edit_msg(context, query, t("msg.error.session_not_found", lang))
            return True

        # Кнопка показывается только админу, но callback_data приходит от клиента:
        # право проверяется здесь, а не только при отрисовке меню.
        admin_checker = getattr(getattr(self.bot_app, "handlers", None), "_is_admin", None)
        if callable(admin_checker) and not bool(admin_checker(int(chat_id))):
            await self._edit_msg(context, query, t("msg.error.session_unavailable", lang))
            return True

        reread = getattr(getattr(self.bot_app, "session_management", None), "reread_tmux_output", None)
        if not callable(reread):
            await query.answer(t("msg.session.tmux_reread_failed", lang), show_alert=True)
            return True
        try:
            outcome = str(await reread(session, context) or "failed")
        except Exception:
            logging.getLogger(__name__).exception(
                "tmux reread failed session_id=%s",
                getattr(session, "id", None),
            )
            outcome = "failed"

        answers = {
            "started": "msg.session.tmux_reread_started",
            "not_tmux": "msg.session.tmux_reread_not_tmux",
            "no_request": "msg.session.tmux_reread_no_request",
            "failed": "msg.session.tmux_reread_failed",
        }
        await query.answer(t(answers.get(outcome, answers["failed"]), lang), show_alert=outcome != "started")

        text, keyboard = self.bot_app.handlers.build_sessions_active_overview(owner_chat_id, session=session)
        await self._edit_msg(context, query, text=text, reply_markup=keyboard)
        return True

    async def _cb_sess_snapshot(self, *, data: str, chat_id: int, query, context) -> bool:
        lang = lang_from_query(query, self.bot_app.config)
        payload = str(data or "").split(":", 1)[1].strip() if ":" in str(data or "") else ""
        _reply_chat_id, owner_chat_id, scope_session = self._callback_scope(chat_id, query)
        session = self.bot_app.manager.get_by_uid(payload) if payload else scope_session
        if session is None:
            await self._edit_msg(context, query, t("msg.error.session_not_found", lang))
            return True

        visibility_checker = getattr(getattr(self.bot_app, "handlers", None), "_is_session_visible_for_chat", None)
        if callable(visibility_checker):
            try:
                if not bool(visibility_checker(owner_chat_id, session)):
                    await self._edit_msg(context, query, t("msg.error.session_unavailable", lang))
                    return True
            except Exception:
                logging.getLogger(__name__).exception("session snapshot visibility check failed")
                await self._edit_msg(context, query, t("msg.error.session_unavailable", lang))
                return True

        service = getattr(self.bot_app, "session_snapshot_report_service", None)
        if service is None:
            await self._edit_msg(context, query, t("msg.report.snapshot_unavailable", lang))
            return True
        try:
            summary = await asyncio.to_thread(service.save_html_report, session, lang=lang)
        except Exception:
            logging.getLogger(__name__).exception(
                "session snapshot report failed session_id=%s",
                getattr(session, "id", None),
            )
            await self._edit_msg(context, query, t("msg.report.snapshot_failed", lang))
            return True

        target_kwargs = self._session_reply_kwargs(session, ui_chat_id=int(chat_id))
        with open(summary.path, "rb") as f:
            ok = await self.bot_app._send_document(
                context,
                document=f,
                filename=summary.name,
                **target_kwargs,
            )
        if not ok:
            await self._edit_msg(context, query, t("msg.report.send_failed", lang))
            return True
        await self._edit_msg(
            context,
            query,
            t("msg.report.snapshot_generated", lang, name=summary.report_id),
        )
        return True

    async def _cb_lang_menu(self, *, data: str, chat_id: int, query, context) -> bool:
        """Show the language selection menu."""
        from_user = getattr(query, "from_user", None)
        user_id = getattr(from_user, "id", None)
        config = self.bot_app.config
        current_lang = resolve_language(user_id, None, config)
        text, keyboard = build_lang_menu(current_lang, back_callback="sess_active")
        await self._edit_msg(context, query, text=text, reply_markup=keyboard, md2=False)
        return True

    async def _cb_lang_set(self, *, data: str, chat_id: int, query, context) -> bool:
        """Handle language selection: persist and redraw session menu."""
        code = str(data).split(":", 1)[1].strip() if ":" in str(data) else ""
        if code not in SUPPORTED_LANGS:
            await query.answer(t("msg.lang.invalid", lang_from_query(query, self.bot_app.config)), show_alert=True)
            return True
        from_user = getattr(query, "from_user", None)
        user_id = getattr(from_user, "id", None)
        if user_id is None:
            return True
        config_service = getattr(self.bot_app, "config_service", None)
        if config_service is not None:
            result = await config_service.set_user_language(int(user_id), code)
            if getattr(result, "ok", False):
                # Keep the live in-memory config coherent so subsequent
                # resolve_user_lang() calls reflect the new choice immediately.
                self.bot_app.config.telegram.user_languages[int(user_id)] = code
        _native_names: dict[str, str] = {
            "ru": "Русский",
            "en": "English",
            "zh": "中文",
            "de": "Deutsch",
        }
        await query.answer(t("msg.lang.changed", code, lang=_native_names.get(code, code)))
        _reply_chat_id, owner_chat_id, session = self._callback_scope(chat_id, query)
        text, keyboard = self.bot_app.handlers.build_sessions_active_overview(
            owner_chat_id, session=session, lang=code
        )
        await self._edit_msg(context, query, text=text, reply_markup=keyboard)
        return True
