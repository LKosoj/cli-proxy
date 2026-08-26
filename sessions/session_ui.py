import logging
from datetime import datetime
from typing import Callable, Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from i18n import t
from utils.lang import resolve_user_lang

from app.services.cli_session_history import CliSessionCandidate, list_recent_cli_sessions
from app.services.state_repository import get_state_repository
from app.services.telegram_ui_scope import TelegramUiKey
from app.services.advanced_orchestrator_service import ORCHESTRATOR_MODE_ID
from modes.sdk.services.callback_data import build_session_overview_callback_data
from session import (
    get_session_execution_backend,
    has_live_tmux,
    session_active_cli_name,
    set_session_execution_backend,
)
from sessions.session_state_access import (
    is_orchestrator_enabled,
    is_session_unread,
    reset_session_runtime_state,
    set_orchestrator_enabled,
    set_orchestrator_pending_input,
)
from sessions.session_status import build_session_status_text, visible_modes


logger = logging.getLogger(__name__)

# Telegram hard-limits callback_data to 64 bytes, so long CLI session ids are
# passed as a prefix and matched back against the on-disk list on click.
CALLBACK_DATA_MAX_LEN = 64


class SessionUI:
    def __init__(
        self,
        config,
        manager,
        send_message,
        edit_message,
        format_ts,
        short_label,
        on_close: Optional[Callable[[str], None]] = None,
        on_before_close: Optional[Callable[[str, int, ContextTypes.DEFAULT_TYPE], None]] = None,
        mode_registry=None,
        is_session_allowed: Optional[Callable[[int, object], bool]] = None,
        bot_app=None,
    ) -> None:
        self.config = config
        self.manager = manager
        self._send_message = send_message
        self._edit_message = edit_message
        self._format_ts = format_ts
        self._short_label = short_label
        self._on_close = on_close
        self._on_before_close = on_before_close
        self._mode_registry = mode_registry
        self._is_session_allowed = is_session_allowed
        self._bot_app = bot_app
        self._state_repo = get_state_repository(self.config.defaults.state_path)
        self.pending_session_rename: dict[TelegramUiKey, dict[str, object]] = {}
        self.pending_session_resume: dict[TelegramUiKey, dict[str, object]] = {}

    def _ui_key(self, chat_id: int, *, query=None, message_thread_id: Optional[int] = None) -> TelegramUiKey:
        if query is not None:
            ui_key = self._bot_app.telegram_ui_key_from_query(query)
            if ui_key is not None:
                return ui_key
        return self._bot_app.telegram_ui_key(int(chat_id), message_thread_id)

    def _resolve_owner_chat_id(self, chat_id: int, query=None) -> int:
        resolver = getattr(self._bot_app, "resolve_telegram_callback_scope", None)
        if callable(resolver) and query is not None:
            _reply_chat_id, _thread_id, owner_chat_id, _session = resolver(query)
            return int(owner_chat_id or chat_id)
        return int(chat_id)

    def _can_access_session(self, chat_id: int, session) -> bool:
        if self._is_session_allowed is None:
            return True
        try:
            return bool(self._is_session_allowed(int(chat_id), session))
        except Exception:
            return False

    def _persist_session(self, chat_id: int, session_id: str) -> None:
        if hasattr(self.manager, "persist_session"):
            try:
                if bool(self.manager.persist_session(int(chat_id), str(session_id))):
                    return
            except Exception:
                logger.exception(
                    "session_ui persist_session failed chat_id=%s session_id=%s",
                    chat_id,
                    session_id,
                )
        self.manager._persist_sessions()

    def _reset_session_fields(self, session, *, owner_chat_id: Optional[int] = None) -> None:
        access_policy = getattr(self._bot_app, "access_policy_service", None)
        default_mode_id = None
        if access_policy is not None and hasattr(access_policy, "default_mode_id_for_chat"):
            default_mode_id = access_policy.default_mode_id_for_chat(owner_chat_id)
        reset_session_runtime_state(session, default_mode_id=default_mode_id)

    async def _edit_msg(self, context: ContextTypes.DEFAULT_TYPE, query, text: str, *, reply_markup=None) -> bool:
        if not query.message:
            return False
        return await self._edit_message(
            context,
            chat_id=query.message.chat_id,
            message_id=query.message.message_id,
            text=text,
            md2=True,
            reply_markup=reply_markup,
        )

    def _resume_candidates(self, session) -> list[CliSessionCandidate]:
        return list_recent_cli_sessions(
            session_active_cli_name(session),
            str(getattr(session, "workdir", "") or ""),
        )

    @staticmethod
    def _resume_pick_callback(session_id: str, cli_session_id: str) -> str:
        prefix = f"sess_rpick:{session_id}:"
        return prefix + str(cli_session_id or "")[: CALLBACK_DATA_MAX_LEN - len(prefix)]

    def _match_resume_candidate(self, session, cli_session_prefix: str) -> Optional[str]:
        prefix = str(cli_session_prefix or "").strip()
        if not prefix:
            return None
        for candidate in self._resume_candidates(session):
            if candidate.session_id.startswith(prefix):
                return candidate.session_id
        return None

    def _build_resume_picker(self, session, *, lang: str) -> tuple[str, InlineKeyboardMarkup]:
        """Menu with the newest CLI conversations of this workdir plus manual input."""
        cli_name = session_active_cli_name(session)
        current_token = str(getattr(session, "resume_token", "") or "")
        candidates = self._resume_candidates(session)
        rows: list[list[InlineKeyboardButton]] = []
        for candidate in candidates:
            mark = "✅" if candidate.session_id == current_token else "▫️"
            stamp = (
                datetime.fromtimestamp(candidate.mtime).strftime("%d.%m %H:%M")
                if candidate.mtime
                else "—"
            )
            label = candidate.preview or candidate.session_id[:8]
            rows.append(
                [
                    InlineKeyboardButton(
                        f"{mark} {stamp} · {self._short_label(label, max_len=40)}",
                        callback_data=self._resume_pick_callback(session.id, candidate.session_id),
                    )
                ]
            )
        rows.append(
            [
                InlineKeyboardButton(
                    t("btn.session.resume_manual", lang),
                    callback_data=f"sess_rmanual:{session.id}",
                )
            ]
        )
        rows.append(
            [
                InlineKeyboardButton(
                    t("btn.session.back", lang),
                    callback_data=build_session_overview_callback_data(session),
                )
            ]
        )
        key = "msg.session.resume_picker" if candidates else "msg.session.resume_picker_empty"
        text = t(key, lang, cli=cli_name, current=current_token or t("session_status.no", lang))
        return text, InlineKeyboardMarkup(rows)

    def build_sessions_menu(
        self,
        chat_id: int,
        include_back: bool = False,
        back_callback: str = "sess_active",
        back_text: Optional[str] = None,
    ) -> InlineKeyboardMarkup:
        lang = resolve_user_lang(self.config, chat_id=chat_id)
        rows = []
        for sid, s in self.manager.sessions_for_chat(chat_id).items():
            if not self._can_access_session(chat_id, s):
                continue
            label = s.name or f"{s.tool.name} @ {s.workdir}"
            unread_mark = "🔵 " if is_session_unread(s, False) else ""
            text = self._short_label(f"{unread_mark}{sid}: {label}", max_len=60)
            rows.append([InlineKeyboardButton(text, callback_data=build_session_overview_callback_data(s))])
        if include_back:
            rows.append([InlineKeyboardButton(back_text or t("common.back", lang), callback_data=back_callback)])
        else:
            rows.append([InlineKeyboardButton(t("btn.session.close_menu", lang), callback_data="sess_close_menu")])
        return InlineKeyboardMarkup(rows)

    async def handle_pending_message(
        self,
        chat_id: int,
        text: str,
        context: ContextTypes.DEFAULT_TYPE,
        message_thread_id: Optional[int] = None,
    ) -> bool:
        ui_key = self._ui_key(chat_id, message_thread_id=message_thread_id)
        if ui_key in self.pending_session_rename:
            pending = self.pending_session_rename.pop(ui_key)
            owner_chat_id = int(pending.get("owner_chat_id") or chat_id)
            lang = resolve_user_lang(self.config, chat_id=owner_chat_id)
            session_id = str(pending.get("session_id") or "")
            session = self.manager.get(owner_chat_id, session_id)
            name = text.strip()
            if name in ("-", "отмена", "Отмена"):
                await self._send_message(context, text=t("msg.session.rename_cancelled", lang), **ui_key.reply_kwargs())
                return True
            if not name:
                await self._send_message(context, text=t("msg.session.rename_empty", lang), **ui_key.reply_kwargs())
                return True
            if not session:
                await self._send_message(context, text=t("msg.error.session_not_found", lang), **ui_key.reply_kwargs())
                return True
            session.name = name
            self._persist_session(owner_chat_id, session.id)
            manager = getattr(self._bot_app, "session_thread_manager", None)
            if manager is not None:
                try:
                    await manager.rename_topic_for_session(
                        owner_chat_id=owner_chat_id,
                        session=session,
                        bot=getattr(context, "bot", None),
                    )
                except Exception:
                    logging.getLogger(__name__).exception(
                        "session thread rename failed chat_id=%s session_id=%s",
                        chat_id,
                        session.id,
                    )
            await self._send_message(context, text=t("msg.session.renamed", lang), **ui_key.reply_kwargs())
            return True
        if ui_key in self.pending_session_resume:
            pending = self.pending_session_resume.pop(ui_key)
            owner_chat_id = int(pending.get("owner_chat_id") or chat_id)
            lang = resolve_user_lang(self.config, chat_id=owner_chat_id)
            session_id = str(pending.get("session_id") or "")
            session = self.manager.get(owner_chat_id, session_id)
            token = text.strip()
            if token in ("-", "отмена", "Отмена"):
                await self._send_message(context, text=t("msg.session.resume_token_cancelled", lang), **ui_key.reply_kwargs())
                return True
            if not session:
                await self._send_message(context, text=t("msg.error.session_not_found", lang), **ui_key.reply_kwargs())
                return True
            session.resume_token = token
            self._persist_session(owner_chat_id, session.id)
            await self._send_message(context, text=t("msg.session.resume_token_updated", lang), **ui_key.reply_kwargs())
            return True
        return False

    async def handle_callback(self, query, chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> bool:
        data = query.data or ""
        owner_chat_id = self._resolve_owner_chat_id(chat_id, query)
        lang = resolve_user_lang(self.config, chat_id=owner_chat_id)
        if data.startswith("sess_pick:"):
            session_id = data.split(":", 1)[1]
            session = self.manager.get(owner_chat_id, session_id)
            if not session:
                await self._edit_msg(context, query, t("msg.error.session_not_found", lang))
                return True
            if not self._can_access_session(owner_chat_id, session):
                await self._edit_msg(context, query, t("msg.error.session_unavailable", lang))
                return True
            text, keyboard = self._bot_app.handlers.build_sessions_active_overview(owner_chat_id, session=session)
            await self._edit_msg(context, query, text=text, reply_markup=keyboard)
            return True
        if data.startswith("sess_status:"):
            session_id = data.split(":", 1)[1]
            session = self.manager.get(owner_chat_id, session_id)
            if not session:
                await self._edit_msg(context, query, t("msg.error.session_not_found", lang))
                return True
            if not self._can_access_session(owner_chat_id, session):
                await self._edit_msg(context, query, t("msg.error.session_unavailable", lang))
                return True
            text = build_session_status_text(
                session,
                mode_registry=self._mode_registry,
                mode_items=visible_modes(
                    self._mode_registry,
                    chat_id=owner_chat_id,
                    access_policy=getattr(self._bot_app, "access_policy_service", None),
                ),
                show_orchestrator=bool(
                    getattr(
                        getattr(self._bot_app, "access_policy_service", None),
                        "is_orchestrator_allowed_for_chat",
                        lambda _chat_id: False,
                    )(owner_chat_id)
                ),
                title_prefix=t("session_status.session", lang),
                lang=lang,
            )
            await self._edit_msg(context, query, text)
            return True
        if data.startswith("sess_rename:"):
            session_id = data.split(":", 1)[1]
            session = self.manager.get(owner_chat_id, session_id)
            if not session:
                await self._edit_msg(context, query, t("msg.error.session_not_found", lang))
                return True
            if not self._can_access_session(owner_chat_id, session):
                await self._edit_msg(context, query, t("msg.error.session_unavailable", lang))
                return True
            self.pending_session_rename[self._ui_key(chat_id, query=query)] = {
                "owner_chat_id": owner_chat_id,
                "session_id": session_id,
            }
            await self._edit_msg(context, query, t("msg.session.rename_prompt", lang, session_id=session.id))
            return True
        if data.startswith("sess_resume:"):
            session_id = data.split(":", 1)[1]
            session = self.manager.get(owner_chat_id, session_id)
            if not session:
                await self._edit_msg(context, query, t("msg.error.session_not_found", lang))
                return True
            if not self._can_access_session(owner_chat_id, session):
                await self._edit_msg(context, query, t("msg.error.session_unavailable", lang))
                return True
            text, keyboard = self._build_resume_picker(session, lang=lang)
            await self._edit_msg(context, query, text=text, reply_markup=keyboard)
            return True
        if data.startswith("sess_rpick:"):
            parts = data.split(":", 2)
            session_id = parts[1] if len(parts) > 1 else ""
            cli_session_prefix = parts[2] if len(parts) > 2 else ""
            session = self.manager.get(owner_chat_id, session_id)
            if not session:
                await self._edit_msg(context, query, t("msg.error.session_not_found", lang))
                return True
            if not self._can_access_session(owner_chat_id, session):
                await self._edit_msg(context, query, t("msg.error.session_unavailable", lang))
                return True
            token = self._match_resume_candidate(session, cli_session_prefix)
            if not token:
                await self._edit_msg(context, query, t("msg.session.resume_not_found", lang))
                return True
            session.resume_token = token
            self._persist_session(owner_chat_id, session.id)
            text, keyboard = self._build_resume_picker(session, lang=lang)
            await self._edit_msg(
                context,
                query,
                text=f"{t('msg.session.resume_token_updated', lang)}\n\n{text}",
                reply_markup=keyboard,
            )
            return True
        if data.startswith("sess_rmanual:"):
            session_id = data.split(":", 1)[1]
            session = self.manager.get(owner_chat_id, session_id)
            if not session:
                await self._edit_msg(context, query, t("msg.error.session_not_found", lang))
                return True
            if not self._can_access_session(owner_chat_id, session):
                await self._edit_msg(context, query, t("msg.error.session_unavailable", lang))
                return True
            current = session.resume_token or t("session_status.no", lang)
            self.pending_session_resume[self._ui_key(chat_id, query=query)] = {
                "owner_chat_id": owner_chat_id,
                "session_id": session_id,
            }
            await self._edit_msg(context, query, t("msg.session.resume_prompt", lang, current=current))
            return True
        if data.startswith("sess_cli:"):
            # This callback is typically handled in callbacks.py (BotApp has better context),
            # but keep a small fallback here for robustness.
            parts = data.split(":", 2)
            session_id = str(parts[1] or "").strip() if len(parts) > 2 else ""
            cli = str(parts[2] or "").strip() if len(parts) > 2 else str(parts[1] or "").strip()
            resolver = getattr(self._bot_app, "resolve_telegram_callback_scope", None)
            session = None
            if session_id:
                session = self.manager.get(owner_chat_id, session_id)
            elif callable(resolver):
                _reply_chat_id, _thread_id, _resolved_owner_chat_id, session = resolver(query)
            if not session:
                await self._edit_msg(context, query, t("msg.error.session_no_context", lang))
                return True
            if getattr(session, "busy", False) or getattr(session, "run_lock", None) and session.run_lock.locked():
                await self._edit_msg(context, query, t("msg.error.session_busy", lang))
                return True
            try:
                await session.set_active_cli_persistent_when_idle(cli)
                self._persist_session(owner_chat_id, session.id)
                await self._edit_msg(context, query, t("msg.session.cli_active", lang, cli_name=session.tool.name))
            except Exception:
                logger.exception(
                    "session UI CLI switch failed session_id=%s target_cli=%s",
                    getattr(session, "id", ""),
                    cli,
                )
                await self._edit_msg(context, query, t("msg.error.cli_switch_failed", lang))
            return True
        if data.startswith("sess_backend:"):
            payload = data.split(":", 1)[1].strip() if ":" in data else ""
            if ":" not in payload:
                await self._edit_msg(context, query, t("msg.error.callback_format", lang))
                return True
            session_token, backend = payload.rsplit(":", 1)
            session_token = session_token.strip()
            backend = backend.strip()
            session = None
            getter = getattr(self.manager, "get_by_uid", None)
            if callable(getter) and session_token:
                session = getter(session_token)
            if session is None and session_token:
                session = self.manager.get(owner_chat_id, session_token)
            if not session:
                await self._edit_msg(context, query, t("msg.error.session_not_found", lang))
                return True
            if not self._can_access_session(owner_chat_id, session):
                await self._edit_msg(context, query, t("msg.error.session_unavailable", lang))
                return True
            result = set_session_execution_backend(session, backend)
            if result.reason:
                if result.reason in {"unsupported backend", "backend not available for cli"}:
                    await self._edit_msg(context, query, t("msg.error.execution_backend_unavailable", lang))
                    return True
                await self._edit_msg(
                    context,
                    query,
                    t("msg.error.execution_backend_switch_failed", lang, reason=result.reason),
                )
                return True
            self._persist_session(owner_chat_id, session.id)
            text, keyboard = self._bot_app.handlers.build_sessions_active_overview(owner_chat_id, session=session)
            await self._edit_msg(context, query, text, reply_markup=keyboard)
            return True
        if data.startswith("sess_state:"):
            session_id = data.split(":", 1)[1]
            session = self.manager.get(owner_chat_id, session_id)
            if not session:
                await self._edit_msg(context, query, t("msg.error.session_not_found", lang))
                return True
            if not self._can_access_session(owner_chat_id, session):
                await self._edit_msg(context, query, t("msg.error.session_unavailable", lang))
                return True
            st = self._state_repo.get_state(
                tool=session.tool.name,
                workdir=session.workdir,
                session_id=session.id,
                chat_id=owner_chat_id,
            )
            if not st:
                await self._edit_msg(context, query, t("msg.session.state_not_found", lang))
                return True
            no_val = t("session_status.no", lang)
            summary = st.summary or no_val
            header = t(
                "msg.session.state_header",
                lang,
                session_id=st.session_id or no_val,
                tool=st.tool,
                workdir=st.workdir,
                resume=st.resume_token or no_val,
            )
            footer = f"\nUpdated: {self._format_ts(st.updated_at)}"
            max_summary = 4096 - len(header) - len(footer) - 4
            if len(summary) > max_summary:
                summary = summary[:max_summary] + " ..."
            text = header + summary + footer
            await self._edit_msg(context, query, text)
            return True
        if data.startswith("sess_queue:"):
            session_id = data.split(":", 1)[1]
            session = self.manager.get(owner_chat_id, session_id)
            if not session:
                await self._edit_msg(context, query, t("msg.error.session_not_found", lang))
                return True
            if not self._can_access_session(owner_chat_id, session):
                await self._edit_msg(context, query, t("msg.error.session_unavailable", lang))
                return True
            if not session.queue:
                await self._edit_msg(context, query, t("msg.session.queue_empty", lang))
                return True
            await self._edit_msg(context, query, t("msg.session.queue_count", lang, n=len(session.queue)))
            return True
        if data.startswith("sess_clearqueue:"):
            session_id = data.split(":", 1)[1]
            session = self.manager.get(owner_chat_id, session_id)
            if not session:
                await self._edit_msg(context, query, t("msg.error.session_not_found", lang))
                return True
            if not self._can_access_session(owner_chat_id, session):
                await self._edit_msg(context, query, t("msg.error.session_unavailable", lang))
                return True
            if not session.queue:
                await self._edit_msg(context, query, t("msg.session.queue_empty", lang))
                return True
            session.queue.clear()
            self._persist_session(owner_chat_id, session.id)
            await self._edit_msg(context, query, t("msg.session.queue_cleared", lang))
            return True
        if data.startswith("sess_reset:"):
            session_id = data.split(":", 1)[1]
            session = self.manager.get(owner_chat_id, session_id)
            if not session:
                await self._edit_msg(context, query, t("msg.error.session_not_found", lang))
                return True
            if not self._can_access_session(owner_chat_id, session):
                await self._edit_msg(context, query, t("msg.error.session_unavailable", lang))
                return True
            if get_session_execution_backend(session) == "tmux" or has_live_tmux(session):
                await session.close_active_tmux_async()
            self._reset_session_fields(session, owner_chat_id=owner_chat_id)
            self._persist_session(owner_chat_id, session.id)
            await self._edit_msg(context, query, t("msg.session.reset_done", lang))
            return True
        if data.startswith("sess_orch_toggle:"):
            session_id = data.split(":", 1)[1]
            session = self.manager.get(owner_chat_id, session_id)
            if not session:
                await self._edit_msg(context, query, t("msg.error.session_not_found", lang))
                return True
            if not self._can_access_session(owner_chat_id, session):
                await self._edit_msg(context, query, t("msg.error.session_unavailable", lang))
                return True
            access_policy = getattr(self._bot_app, "access_policy_service", None)
            orchestrator_allowed_checker = (
                getattr(access_policy, "is_orchestrator_allowed_for_chat", None)
                if access_policy is not None else None
            )
            if callable(orchestrator_allowed_checker):
                try:
                    orchestrator_allowed = bool(orchestrator_allowed_checker(owner_chat_id))
                except Exception:
                    orchestrator_allowed = False
                if not orchestrator_allowed:
                    await self._edit_msg(context, query, t("msg.error.orch_unavailable_user", lang))
                    return True
            mode_allowed_checker = getattr(access_policy, "is_mode_allowed_for_chat", None) if access_policy is not None else None
            if orchestrator_allowed_checker is None and callable(mode_allowed_checker):
                try:
                    if not bool(mode_allowed_checker(owner_chat_id, ORCHESTRATOR_MODE_ID)):
                        await self._edit_msg(context, query, t("msg.error.orch_unavailable_user", lang))
                        return True
                except Exception:
                    await self._edit_msg(context, query, t("msg.error.orch_unavailable_user", lang))
                    return True
            current = is_orchestrator_enabled(session, False)
            set_orchestrator_enabled(session, not current)
            set_orchestrator_pending_input(session, None)
            self._persist_session(owner_chat_id, session.id)
            status = t("msg.session.orch_on", lang) if is_orchestrator_enabled(session, False) else t("msg.session.orch_off", lang)
            await self._edit_msg(context, query, t("msg.session.orch_toggled", lang, status=status))
            return True
        if data.startswith("sess_close:"):
            session_id = data.split(":", 1)[1]
            session = self.manager.get(owner_chat_id, session_id)
            if not session:
                await self._edit_msg(context, query, t("msg.error.session_not_found", lang))
                return True
            if not self._can_access_session(owner_chat_id, session):
                await self._edit_msg(context, query, t("msg.error.session_unavailable", lang))
                return True
            ok = await self._bot_app.close_session_with_cleanup(session_id, owner_chat_id, context)
            if ok:
                await self._edit_msg(context, query, t("msg.session.closed_and_removed", lang))
            else:
                await self._edit_msg(context, query, t("msg.error.session_not_found", lang))
            return True
        if data == "sess_close_menu":
            await self._edit_msg(context, query, t("msg.session.menu_closed", lang))
            return True
        return False
