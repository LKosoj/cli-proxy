"""
Module containing command handlers for the Telegram bot.
"""

import asyncio
import copy
import json
import logging
import os
import re
import subprocess
import sys
import tempfile
import time
from typing import Optional

from pydantic import BaseModel, ConfigDict, model_validator
from telegram import BotCommand, BotCommandScopeChat, BotCommandScopeDefault, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    ContextTypes,
)

from modes.sdk.services.callback_data import (
    build_session_mode_pick_callback_data,
    build_session_overview_callback_data,
)
from app.services.menu_visibility_policy import (
    build_mode_menu_visibility,
    build_session_overview_visibility,
    call_mode_build_menu,
)
from app.services.input_dispatch_models import PendingInput as PendingInput  # noqa: F401
from app.services.session_files_service import FilesServiceError
from app.services.path_normalization import normalize_optional_state_path
from app.services.state_repository import get_state_repository
from session import (
    Session,
    get_session_execution_backend,
    session_runtime_uid,
    session_scoped_key,
)
from app.services.report_history_service import InvalidReportIdError, ReportNotFoundError
from sessions.session_state_access import get_active_mode, is_session_unread, is_ssh_remote_enabled
from app.services.ssh_config_loader import ssh_remote_available
from tg.command_registry import build_command_registry
from tg.markdown import escape_markdown_v2_all
from sessions.session_status import build_session_status_text, visible_modes
from tg.files_service_adapter import (
    files_display_path,
    files_rel_path,
    resolve_files_payload,
    session_files_service,
    session_uid_for_files,
)
from utils.ui import status_dot
from utils.lang import resolve_user_lang
from app.services.session_state import SessionState
from i18n import t, SUPPORTED_LANGS

_LANG_LABELS: dict[str, str] = {
    "ru": "🇷🇺 Русский",
    "en": "🇬🇧 English",
    "zh": "🇨🇳 中文",
    "de": "🇩🇪 Deutsch",
}


def build_lang_menu(
    current_lang: str,
    back_callback: str = "sess_active",
) -> tuple[str, InlineKeyboardMarkup]:
    """Build the language selection menu.

    Language labels are NOT localised — user must find their language
    regardless of the current locale.
    """
    rows = []
    for code, label in _LANG_LABELS.items():
        mark = "✅" if code == current_lang else "⬜"
        rows.append([InlineKeyboardButton(f"{mark} {label}", callback_data=f"lang_set:{code}")])
    rows.append([InlineKeyboardButton(t("btn.session.back", current_lang), callback_data=back_callback)])
    return t("msg.lang.choose", current_lang), InlineKeyboardMarkup(rows)


def format_session_state(st: SessionState, updated_at_str: str, lang: str = "ru") -> str:
    """Форматирует объект SessionState в читаемую строку для отображения в Telegram."""
    no = t("session_status.no", lang)
    return "\n".join([
        f"Session: {st.session_id or no}",
        f"Tool: {st.tool}",
        f"Workdir: {st.workdir}",
        f"Resume: {st.resume_token or no}",
        f"Name: {st.name or no}",
        f"Summary: {st.summary or no}",
        f"Updated: {updated_at_str}",
    ])


_GIT_REF_RE = re.compile(r"^[A-Za-z0-9_@/.{}^~][A-Za-z0-9_@/.{}^~-]{0,99}$")


class TelegramRuntimePayload(BaseModel):
    model_config = ConfigDict(extra="allow")

    chat_id: int
    session_uid: Optional[str] = None

    @model_validator(mode="after")
    def _require_session_uid(self) -> "TelegramRuntimePayload":
        if str(self.session_uid or "").strip():
            return self
        raise ValueError("telegram runtime payload is invalid: session_uid is required")


class BotHandlers:
    """
    Class containing command handlers for the Telegram bot.
    """

    def __init__(self, bot_app):
        self.bot_app = bot_app

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

    @staticmethod
    def _project_root() -> str:
        return os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

    @staticmethod
    def _bot_service_name() -> str:
        return str(os.environ.get("CLI_PROXY_SERVICE_NAME") or "cli-proxy-bot").strip() or "cli-proxy-bot"

    @staticmethod
    def _trim_output(text: str, limit: int = 3500) -> str:
        value = str(text or "").strip()
        if len(value) <= int(limit):
            return value
        return "...\n" + value[-int(limit):]

    @staticmethod
    def _validate_telegram_runtime_payload(payload: dict) -> dict:
        TelegramRuntimePayload.model_validate(dict(payload or {}))
        return payload

    def _state_repository(self):
        cfg = getattr(self.bot_app, "config", None)
        defaults = getattr(cfg, "defaults", None) if cfg is not None else None
        raw_state_path = getattr(defaults, "state_path", None)
        try:
            state_path = normalize_optional_state_path(raw_state_path)
        except TypeError:
            logging.getLogger(__name__).warning(
                "telegram state repository disabled: invalid state_path type=%s",
                type(raw_state_path).__name__,
            )
            return None
        if not state_path:
            return None
        return get_state_repository(state_path)

    def _selfupdate_marker_path(self) -> Optional[str]:
        cfg = getattr(self.bot_app, "config", None)
        defaults = getattr(cfg, "defaults", None) if cfg is not None else None
        try:
            state_path = normalize_optional_state_path(getattr(defaults, "state_path", None))
        except TypeError:
            return None
        if not state_path:
            return None
        return f"{state_path}.selfupdate_pending.json"

    def _save_selfupdate_marker(
        self,
        *,
        chat_id: int,
        service_name: str,
        message_thread_id: Optional[int] = None,
    ) -> None:
        path = self._selfupdate_marker_path()
        if not path:
            return
        payload = {
            "chat_id": int(chat_id),
            "service_name": str(service_name or ""),
            "requested_at": float(time.time()),
        }
        if message_thread_id is not None:
            payload["message_thread_id"] = int(message_thread_id)
        directory = os.path.dirname(path) or "."
        os.makedirs(directory, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(dir=directory, prefix=".selfupdate_", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, path)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                logging.getLogger(__name__).exception("selfupdate marker cleanup failed tmp_path=%s", tmp_path)
            raise

    def _load_selfupdate_marker(self) -> Optional[dict]:
        path = self._selfupdate_marker_path()
        if not path or not os.path.exists(path):
            return None
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            logging.exception("selfupdate marker read failed")
            return None
        if not isinstance(data, dict):
            return None
        try:
            chat_id = int(data.get("chat_id"))
        except Exception:
            return None
        if chat_id <= 0:
            return None
        service_name = str(data.get("service_name") or "").strip()
        try:
            requested_at = float(data.get("requested_at") or 0.0)
        except Exception:
            requested_at = 0.0
        try:
            message_thread_id = int(data.get("message_thread_id")) if data.get("message_thread_id") is not None else None
        except Exception:
            message_thread_id = None
        return {
            "chat_id": chat_id,
            "service_name": service_name,
            "requested_at": requested_at,
            "message_thread_id": message_thread_id if message_thread_id is not None and message_thread_id > 0 else None,
        }

    def _clear_selfupdate_marker(self) -> None:
        path = self._selfupdate_marker_path()
        if not path:
            return
        try:
            if os.path.exists(path):
                os.remove(path)
        except Exception:
            logging.exception("selfupdate marker cleanup failed")

    async def notify_pending_selfupdate(self, application: Application) -> None:
        marker = self._load_selfupdate_marker()
        if not marker:
            return
        chat_id = int(marker["chat_id"])
        service_name = str(marker.get("service_name") or self._bot_service_name())
        requested_at = float(marker.get("requested_at") or 0.0)
        delay_sec = max(0, int(time.time() - requested_at)) if requested_at > 0 else None
        try:
            lang = resolve_user_lang(self.bot_app.config, chat_id=chat_id)
        except Exception:
            lang = "ru"
        text = t("msg.selfupdate.confirmed", lang, service_name=service_name)
        if delay_sec is not None:
            text += t("msg.selfupdate.startup_delay", lang, delay_sec=delay_sec)
        try:
            send_kwargs = {"chat_id": chat_id, "text": text}
            message_thread_id = marker.get("message_thread_id")
            if message_thread_id is not None:
                send_kwargs["message_thread_id"] = int(message_thread_id)
            await self.bot_app._send_message(application, **send_kwargs)
        except Exception as e:
            logging.exception("selfupdate startup confirmation failed: %s", e)
            return
        self._clear_selfupdate_marker()

    async def _run_subprocess(self, *argv: str, cwd: Optional[str] = None) -> tuple[int, str]:
        proc = await asyncio.create_subprocess_exec(
            *[str(x) for x in argv],
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        out, _ = await proc.communicate()
        return int(proc.returncode), (out or b"").decode(errors="replace")

    @staticmethod
    def _requirements_files_from_pull_output(output: str) -> list[str]:
        text = str(output or "").lower()
        found: list[str] = []
        for name in ("requirements.txt", "requirement.txt"):
            if name in text:
                found.append(name)
        return found

    @staticmethod
    def _venv_python_path(project_root: str) -> Optional[str]:
        root = str(project_root or "").strip()
        if not root:
            return None
        candidates = [
            os.path.join(root, ".venv", "bin", "python"),
            os.path.join(root, ".venv", "Scripts", "python.exe"),
        ]
        for candidate in candidates:
            if os.path.isfile(candidate):
                return candidate
        return None

    def _spawn_selfupdate_watchdog(self, *, marker_path: str, timeout_sec: int) -> None:
        token = str(getattr(getattr(getattr(self.bot_app, "config", None), "telegram", None), "token", "") or "").strip()
        if not token:
            return
        project_root = self._project_root()
        argv = [
            str(sys.executable),
            "-m",
            "app.services.selfupdate_watchdog",
            "--marker-path",
            str(marker_path),
            "--bot-token",
            token,
            "--timeout-sec",
            str(int(timeout_sec)),
        ]
        subprocess.Popen(
            argv,
            cwd=project_root,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            close_fds=True,
            start_new_session=True,
        )

    def _route_reply_kwargs(self, update: Update) -> Optional[dict]:
        resolver = getattr(self.bot_app, "resolve_telegram_inbound_route", None)
        if not callable(resolver):
            return None
        try:
            route = resolver(update)
        except Exception:
            logging.getLogger(__name__).exception("failed to resolve telegram inbound route reply kwargs")
            return None
        reply_builder = getattr(route, "reply_kwargs", None)
        if callable(reply_builder):
            try:
                kwargs = dict(reply_builder() or {})
            except Exception:
                logging.getLogger(__name__).exception("failed to build telegram inbound route reply kwargs")
                kwargs = {}
        else:
            kwargs = {}
            reply_chat_id = getattr(route, "reply_chat_id", None)
            if reply_chat_id is not None:
                kwargs["chat_id"] = int(reply_chat_id)
            message_thread_id = getattr(route, "message_thread_id", None)
            if message_thread_id is not None:
                kwargs["message_thread_id"] = int(message_thread_id)
        filtered = {
            key: value
            for key, value in kwargs.items()
            if key in {"chat_id", "message_thread_id", "direct_messages_topic_id"}
        }
        return filtered or None

    @staticmethod
    def _same_reply_scope(lhs: Optional[dict], rhs: Optional[dict]) -> bool:
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

    def _reply_kwargs(self, update: Update, session: Optional[Session] = None) -> dict:
        chat = getattr(update, "effective_chat", None)
        ui_chat_id = int(getattr(chat, "id", 0) or 0)
        builder = getattr(self.bot_app, "build_telegram_reply_dest", None)
        session_kwargs = None
        if callable(builder) and session is not None:
            dest = builder(session, ui_chat_id)
            session_kwargs = {
                key: value
                for key, value in dest.items()
                if key in {"chat_id", "message_thread_id", "direct_messages_topic_id"}
            }
        route_kwargs = self._route_reply_kwargs(update)
        if session_kwargs and not self._same_reply_scope(route_kwargs, session_kwargs):
            return session_kwargs
        if route_kwargs is not None:
            return route_kwargs
        if session_kwargs:
            return session_kwargs
        if callable(builder):
            dest = builder(session, ui_chat_id)
            return {
                key: value
                for key, value in dest.items()
                if key in {"chat_id", "message_thread_id", "direct_messages_topic_id"}
            }
        return {"chat_id": ui_chat_id}

    def _owner_chat_id(self, update: Update) -> int:
        chat = getattr(update, "effective_chat", None)
        chat_id = int(getattr(chat, "id", 0) or 0)
        resolver = getattr(self.bot_app, "resolve_telegram_inbound_route", None)
        if callable(resolver):
            try:
                return int(resolver(update).owner_chat_id)
            except Exception:
                logging.getLogger(__name__).exception("failed to resolve telegram inbound route owner chat")
        return chat_id

    async def _ensure_allowed(
        self,
        chat_id: int,
        context: ContextTypes.DEFAULT_TYPE,
        *,
        update: Optional[Update] = None,
        allow_outside_topic: bool = False,
    ) -> bool:
        if update is not None and hasattr(self.bot_app, "ensure_telegram_inbound_authorized"):
            route = await self.bot_app.ensure_telegram_inbound_authorized(
                update,
                context,
                allow_outside_topic=allow_outside_topic,
            )
            return bool(route is not None)
        return bool(await self.bot_app.access_policy_service.ensure_allowed(chat_id, context))

    async def _require_admin(
        self,
        chat_id: int,
        context: ContextTypes.DEFAULT_TYPE,
        *,
        scope: str,
        update: Optional[Update] = None,
        allow_outside_topic: bool = False,
    ) -> bool:
        if update is not None and hasattr(self.bot_app, "ensure_telegram_inbound_authorized"):
            route = await self.bot_app.ensure_telegram_inbound_authorized(
                update,
                context,
                scope=scope,
                require_admin=True,
                allow_outside_topic=allow_outside_topic,
            )
            return bool(route is not None)
        return bool(await self.bot_app.access_policy_service.require_admin(chat_id, context, scope=scope))

    async def _require_scope_session(
        self,
        chat_id: int,
        context: ContextTypes.DEFAULT_TYPE,
        *,
        auto_create: bool = False,
        update: Optional[Update] = None,
        allow_outside_topic: bool = False,
    ) -> Optional[Session]:
        if update is not None and hasattr(self.bot_app, "ensure_telegram_inbound_session"):
            _route, session = await self.bot_app.ensure_telegram_inbound_session(
                update,
                context,
                auto_create=auto_create,
                allow_outside_topic=allow_outside_topic,
            )
            return session
        return await self.bot_app.access_policy_service.require_scope_session(
            chat_id,
            context,
            auto_create=auto_create,
        )

    def _is_admin(self, chat_id: int) -> bool:
        policy = getattr(self.bot_app, "access_policy_service", None)
        checker = getattr(policy, "is_admin", None) if policy is not None else None
        if callable(checker):
            return bool(checker(int(chat_id)))
        fallback = getattr(self.bot_app, "is_admin", None)
        if callable(fallback):
            return bool(fallback(int(chat_id)))
        return False

    def _is_session_visible_for_chat(self, chat_id: int, session: Optional[Session]) -> bool:
        if session is None:
            return False
        if self._is_admin(chat_id):
            return True
        checker = getattr(self.bot_app, "is_session_allowed_for_chat", None)
        if not callable(checker):
            return False
        try:
            return bool(checker(int(chat_id), session))
        except Exception:
            logging.getLogger(__name__).exception(
                "failed to check session visibility chat_id=%s session_id=%s",
                chat_id,
                getattr(session, "id", None),
            )
            return False

    def _visible_sessions_for_chat(self, chat_id: int) -> list[Session]:
        manager = getattr(self.bot_app, "manager", None)
        getter = getattr(manager, "sessions_for_chat", None) if manager is not None else None
        if not callable(getter):
            return []
        sessions = list((getter(int(chat_id)) or {}).values())
        if self._is_admin(chat_id):
            return sessions
        return [item for item in sessions if self._is_session_visible_for_chat(chat_id, item)]

    def _is_workdir_visible_for_chat(self, chat_id: int, workdir: Optional[str]) -> bool:
        if self._is_admin(chat_id):
            return True
        getter = getattr(self.bot_app, "user_projects", None)
        if not callable(getter):
            return False
        try:
            allowed = {os.path.realpath(str(item)) for item in getter(int(chat_id)) or []}
            target = os.path.realpath(str(workdir or ""))
        except Exception:
            logging.getLogger(__name__).exception(
                "failed to check workdir visibility chat_id=%s workdir=%s",
                chat_id,
                workdir,
            )
            return False
        return bool(target) and target in allowed

    async def _cancel_mode_tasks_session(self, session_id: str) -> None:
        try:
            await self.bot_app.mode_session_control.cancel_session(session_id=session_id, timeout_s=0.2)
        except Exception as e:
            logging.exception("cancel mode tasks session=%s failed: %s", session_id, e)

    def _active_session_status_text(
        self, s: Session, *, chat_id: Optional[int] = None, lang: Optional[str] = None
    ) -> str:
        if lang is None:
            try:
                lang = resolve_user_lang(self.bot_app.config, chat_id=chat_id)
            except Exception:
                lang = "ru"
        return build_session_status_text(
            s,
            mode_registry=getattr(self.bot_app, "mode_registry_service", None),
            mode_items=self._registered_modes(chat_id=chat_id) if chat_id is not None else None,
            show_orchestrator=False,
            lang=lang,
        )

    def _registered_modes(self, *, chat_id: Optional[int] = None) -> list[tuple[str, str]]:
        svc = getattr(self.bot_app, "mode_registry_service", None)
        policy = getattr(self.bot_app, "access_policy_service", None)
        return visible_modes(svc, chat_id=chat_id, access_policy=policy)

    def _build_mode_buttons_rows(
        self,
        *,
        chat_id: int,
        session: Optional[Session] = None,
        active_mode: str = "",
    ) -> list[list[InlineKeyboardButton]]:
        modes = self._registered_modes(chat_id=chat_id)
        if not modes:
            return []
        rows: list[list[InlineKeyboardButton]] = []
        row: list[InlineKeyboardButton] = []
        for mode_id, label in modes:
            enabled = str(active_mode or "").strip() == mode_id
            row.append(
                InlineKeyboardButton(
                    f"{status_dot(enabled)} {label}",
                    callback_data=build_session_mode_pick_callback_data(session, mode_id),
                )
            )
            if len(row) >= 2:
                rows.append(row)
                row = []
        if row:
            rows.append(row)
        return rows

    def build_user_project_picker(
        self,
        owner_chat_id: int,
        *,
        session_uid: Optional[str] = None,
        tool_name: Optional[str] = None,
        force_new: bool = False,
        back_callback: Optional[str] = "sess_active",
        include_cancel: bool = False,
        lang: str = "ru",
    ) -> tuple[str, InlineKeyboardMarkup]:
        projects = self.bot_app.user_projects(owner_chat_id)
        if not projects:
            rows = [[InlineKeyboardButton(t("btn.session.cancel", lang), callback_data="sess_close_menu")]]
            return t("msg.session.no_projects", lang), InlineKeyboardMarkup(rows)

        callback_prefix = "user_project_pick_new" if force_new else "user_project_pick"
        token = str(session_uid or "").strip()
        tool = str(tool_name or "").strip()

        def _callback_data(idx: int) -> str:
            parts: list[str] = []
            if token:
                parts.append(token)
            if tool:
                parts.append(f"tool={tool}")
            parts.append(str(idx))
            return f"{callback_prefix}:{':'.join(parts)}"

        rows = [
            [
                InlineKeyboardButton(
                    self.bot_app._short_label(path, 60),
                    callback_data=_callback_data(idx),
                )
            ]
            for idx, path in enumerate(projects)
        ]
        if include_cancel:
            rows.append([InlineKeyboardButton(t("btn.session.cancel", lang), callback_data="sess_close_menu")])
        elif back_callback:
            rows.append([InlineKeyboardButton(t("btn.session.back", lang), callback_data=back_callback)])
        if tool:
            return t("msg.session.project_choose_tool", lang, tool=tool), InlineKeyboardMarkup(rows)
        return t("msg.session.project_choose", lang), InlineKeyboardMarkup(rows)

    def _resolve_overview_session(
        self,
        chat_id: int,
        *,
        session: Optional[Session] = None,
        session_uid: Optional[str] = None,
    ) -> Optional[Session]:
        if session is not None:
            return session
        uid = str(session_uid or "").strip()
        if uid:
            manager = getattr(self.bot_app, "manager", None)
            getter = getattr(manager, "get_by_uid", None) if manager is not None else None
            if callable(getter):
                resolved = getter(uid)
                if resolved is not None:
                    return resolved
        resolver = getattr(self.bot_app, "resolve_telegram_scope_session", None)
        if callable(resolver):
            return resolver(reply_chat_id=int(chat_id), owner_chat_id=int(chat_id))
        return None

    def _ssh_remote_button(self, session: Session, lang: str = "ru") -> Optional[InlineKeyboardButton]:
        if not ssh_remote_available(session.workdir):
            return None
        enabled = is_ssh_remote_enabled(session)
        label = t("btn.ssh.on", lang) if enabled else t("btn.ssh.off", lang)
        explicit_uid = session_runtime_uid(session)
        callback_data = f"sess_ssh_toggle:{explicit_uid}" if explicit_uid else f"sess_ssh_toggle:{session.id}"
        return InlineKeyboardButton(label, callback_data=callback_data)

    def _unread_toggle_button(self, session: Session, lang: str = "ru") -> InlineKeyboardButton:
        unread = is_session_unread(session)
        label = t("btn.session.mark_read", lang) if unread else t("btn.session.mark_unread", lang)
        explicit_uid = session_runtime_uid(session)
        callback_data = f"sess_unread_toggle:{explicit_uid}" if explicit_uid else f"sess_unread_toggle:{session.id}"
        return InlineKeyboardButton(label, callback_data=callback_data)

    def build_sessions_active_overview(
        self,
        chat_id: int,
        *,
        session: Optional[Session] = None,
        session_uid: Optional[str] = None,
        lang: Optional[str] = None,
    ) -> tuple[str, InlineKeyboardMarkup]:
        if lang is None:
            try:
                lang = resolve_user_lang(self.bot_app.config, chat_id=chat_id)
            except Exception:
                lang = "ru"
        is_admin = self._is_admin(chat_id)
        if not is_admin and not self.bot_app.user_projects(chat_id):
            keyboard = InlineKeyboardMarkup([[InlineKeyboardButton(t("btn.session.cancel", lang), callback_data="sess_close_menu")]])
            return t("msg.session.no_projects", lang), keyboard

        sessions = self._visible_sessions_for_chat(chat_id)
        if not sessions:
            keyboard = InlineKeyboardMarkup(
                [
                    [InlineKeyboardButton(t("btn.session.new", lang), callback_data="sess_new")],
                    [InlineKeyboardButton(t("btn.session.cancel", lang), callback_data="sess_close_menu")],
                ]
            )
            return t("msg.session.no_active", lang), keyboard
        s = self._resolve_overview_session(chat_id, session=session, session_uid=session_uid)
        if s and not is_admin and not self._is_session_visible_for_chat(chat_id, s):
            s = None
        if not s:
            keyboard = InlineKeyboardMarkup(
                [
                    [InlineKeyboardButton(t("btn.session.list", lang), callback_data="sess_list")],
                    [InlineKeyboardButton(t("btn.session.new", lang), callback_data="sess_new")],
                    [InlineKeyboardButton(t("btn.session.cancel", lang), callback_data="sess_close_menu")],
                ]
            )
            return t("msg.session.no_scope", lang), keyboard

        cli_rows: list[list[InlineKeyboardButton]] = []
        available = list(sorted(self.bot_app._available_tools()))
        explicit_session_uid = session_runtime_uid(s)
        modes = self._registered_modes(chat_id=chat_id)
        visibility = build_session_overview_visibility(
            session=s,
            chat_id=chat_id,
            access_policy=getattr(self.bot_app, "access_policy_service", None),
            available_tool_count=len(available),
            registered_mode_count=len(modes),
            visible_session_count=len(sessions),
            tmux_backend_active=get_session_execution_backend(s) == "tmux",
        )
        if visibility.allows("cli_selector") and available:
            row: list[InlineKeyboardButton] = []
            for name in available:
                prefix = "✅" if str(getattr(s, "active_cli", "") or s.tool.name) == name else "⬜"
                callback_data = (
                    f"sess_cli:{explicit_session_uid}:{name}"
                    if explicit_session_uid
                    else f"sess_cli:{name}"
                )
                row.append(InlineKeyboardButton(f"{prefix} {name}", callback_data=callback_data))
                if len(row) >= 2:
                    cli_rows.append(row)
                    row = []
            if row:
                cli_rows.append(row)
        elif visibility.allows("cli_selector"):
            cli_rows.append([InlineKeyboardButton(t("btn.cli.unavailable", lang), callback_data=build_session_overview_callback_data(s))])

        overview_buttons: list[InlineKeyboardButton] = []
        if visibility.allows("status"):
            overview_buttons.append(InlineKeyboardButton(t("btn.session.status", lang), callback_data=f"sess_status:{s.id}"))
        if visibility.allows("rename"):
            overview_buttons.append(InlineKeyboardButton(t("btn.session.rename", lang), callback_data=f"sess_rename:{s.id}"))
        if visibility.allows("resume"):
            overview_buttons.append(InlineKeyboardButton(t("btn.session.resume", lang), callback_data=f"sess_resume:{s.id}"))
        if visibility.allows("queue"):
            overview_buttons.append(InlineKeyboardButton(t("btn.session.queue", lang), callback_data=f"sess_queue:{s.id}"))
            overview_buttons.append(InlineKeyboardButton(t("btn.session.clearqueue", lang), callback_data=f"sess_clearqueue:{s.id}"))
        if visibility.allows("state"):
            overview_buttons.append(InlineKeyboardButton(t("btn.session.state", lang), callback_data=f"sess_state:{s.id}"))
        if visibility.allows("snapshot_report"):
            overview_buttons.append(
                InlineKeyboardButton(
                    t("btn.session.snapshot_report", lang),
                    callback_data=f"sess_snapshot:{explicit_session_uid or s.id}",
                )
            )
        if visibility.allows("tmux_reread"):
            overview_buttons.append(
                InlineKeyboardButton(
                    t("btn.session.tmux_reread", lang),
                    callback_data=f"sess_tmux_reread:{explicit_session_uid or s.id}",
                )
            )
        if visibility.allows("close"):
            overview_buttons.append(InlineKeyboardButton(t("btn.session.close", lang), callback_data=f"sess_close:{s.id}"))
        if visibility.allows("reset"):
            overview_buttons.append(InlineKeyboardButton(t("btn.session.reset", lang), callback_data=f"sess_reset:{s.id}"))

        keyboard_rows = list(cli_rows)
        for idx in range(0, len(overview_buttons), 2):
            keyboard_rows.append(overview_buttons[idx:idx + 2])

        ssh_btn = self._ssh_remote_button(s, lang)
        if ssh_btn and visibility.allows("ssh"):
            keyboard_rows.append([ssh_btn])

        if visibility.allows("unread"):
            keyboard_rows.append([self._unread_toggle_button(s, lang)])

        if visibility.allows("mode_selector"):
            keyboard_rows.extend(
                self._build_mode_buttons_rows(
                    chat_id=chat_id,
                    session=s,
                    active_mode=str(get_active_mode(s, "") or "").strip(),
                )
            )
        footer_buttons: list[InlineKeyboardButton] = []
        if visibility.allows("new_session"):
            footer_buttons.append(InlineKeyboardButton(t("btn.session.new", lang), callback_data="sess_new"))
        if visibility.allows("list_sessions"):
            footer_buttons.append(InlineKeyboardButton(t("btn.session.list", lang), callback_data="sess_list"))
        if footer_buttons:
            keyboard_rows.append(footer_buttons)
        keyboard_rows.append([InlineKeyboardButton(t("btn.session.lang", lang), callback_data="lang_menu")])
        keyboard_rows.append([InlineKeyboardButton(t("btn.session.cancel", lang), callback_data="sess_close_menu")])

        keyboard = InlineKeyboardMarkup(keyboard_rows)
        return self._active_session_status_text(s, chat_id=chat_id, lang=lang), keyboard

    async def show_new_session_menu(
        self,
        chat_id: int,
        context: ContextTypes.DEFAULT_TYPE,
        edit_message: Optional[object] = None,
        reply_kwargs: Optional[dict] = None,
        lang: Optional[str] = None,
    ) -> None:
        if lang is None:
            try:
                lang = resolve_user_lang(self.bot_app.config, chat_id=chat_id)
            except Exception:
                lang = "ru"
        tools = list(sorted(self.bot_app._available_tools()))
        if not tools:
            text = t("msg.session.no_tools", lang, expected=self.bot_app._expected_tools())
            if edit_message:
                if getattr(edit_message, "message", None):
                    await self.bot_app._edit_message(
                        context,
                        chat_id=edit_message.message.chat_id,
                        message_id=edit_message.message.message_id,
                        text=text,
                        md2=True,
                    )
            else:
                await self.bot_app._send_message(
                    context,
                    text=text,
                    **dict(reply_kwargs or {"chat_id": int(chat_id)}),
                )
            return
        rows = [[InlineKeyboardButton(tool, callback_data=f"new_tool:{tool}")] for tool in tools]
        rows.append([InlineKeyboardButton(t("btn.session.back", lang), callback_data="sess_active")])
        keyboard = InlineKeyboardMarkup(rows)
        menu_text = t("msg.session.tool_choose", lang)
        if edit_message:
            if getattr(edit_message, "message", None):
                await self.bot_app._edit_message(
                    context,
                    chat_id=edit_message.message.chat_id,
                    message_id=edit_message.message.message_id,
                    text=menu_text,
                    md2=True,
                    reply_markup=keyboard,
                )
        else:
            await self.bot_app._send_message(
                context,
                text=menu_text,
                reply_markup=keyboard,
                **dict(reply_kwargs or {"chat_id": int(chat_id)}),
            )

    async def cmd_tools(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        chat_id = update.effective_chat.id
        if not await self._ensure_allowed(chat_id, context, update=update, allow_outside_topic=True):
            return
        try:
            lang = resolve_user_lang(self.bot_app.config, chat_id=chat_id)
        except Exception:
            lang = "ru"
        reply_kwargs = self._reply_kwargs(update)
        tools = sorted(self.bot_app._available_tools())
        if not tools:
            await self.bot_app._send_message(
                context,
                text=t("msg.session.no_tools", lang, expected=self.bot_app._expected_tools()),
                **reply_kwargs,
            )
            return
        await self.bot_app._send_message(context, text=t("msg.session.available_tools", lang, tools=", ".join(tools)), **reply_kwargs)

    async def cmd_new(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        ui_chat_id = update.effective_chat.id
        owner_chat_id = self._owner_chat_id(update)
        if not await self._ensure_allowed(owner_chat_id, context, update=update):
            return
        if not await self._require_admin(owner_chat_id, context, scope="new_projects", update=update):
            return
        try:
            lang = resolve_user_lang(self.bot_app.config, chat_id=owner_chat_id)
        except Exception:
            lang = "ru"
        args = context.args
        if len(args) < 2:
            await self.show_new_session_menu(ui_chat_id, context, reply_kwargs=self._reply_kwargs(update))
            return
        tool, path = args[0], " ".join(args[1:])
        session, err = await self.bot_app.session_creation_service.create_session(
            owner_chat_id=owner_chat_id,
            tool=tool,
            path=path,
            bot=getattr(context, "bot", None),
            ui_chat_id=ui_chat_id,
            register_project=True,
        )
        if err:
            if err == "Инструмент не найден.":
                err = t("msg.error.unknown_tool", lang)
            await self.bot_app._send_message(context, text=err, **self._reply_kwargs(update))
            return
        await self.bot_app._send_message(
            context,
            text=t("msg.session.created", lang, id=session.id),
            **self._reply_kwargs(update, session),
        )

    async def cmd_newpath(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        ui_chat_id = update.effective_chat.id
        owner_chat_id = self._owner_chat_id(update)
        if not await self._ensure_allowed(owner_chat_id, context, update=update, allow_outside_topic=True):
            return
        if not await self._require_admin(
            owner_chat_id,
            context,
            scope="new_projects",
            update=update,
            allow_outside_topic=True,
        ):
            return
        try:
            lang = resolve_user_lang(self.bot_app.config, chat_id=owner_chat_id)
        except Exception:
            lang = "ru"
        if not context.args:
            await self.bot_app._send_message(context, text=t("cmd.newpath.usage", lang), **self._reply_kwargs(update))
            return
        path = " ".join(context.args)
        route = self.bot_app.resolve_telegram_inbound_route(update)
        ui_key = self.bot_app.telegram_ui_key_from_route(route, fallback_chat_id=ui_chat_id)
        root = self.bot_app.ui_state.dirs_root.get(ui_key, self.bot_app.config.defaults.workdir)
        session, err = await self.bot_app.session_creation_service.create_from_pending_tool(
            owner_chat_id=owner_chat_id,
            path=path,
            root=root,
            bot=getattr(context, "bot", None),
            message_thread_id=ui_key.message_thread_id,
            ui_chat_id=ui_key.chat_id,
        )
        if err:
            if err == "Инструмент не выбран.":
                err = t("msg.error.tool_not_selected", lang)
            await self.bot_app._send_message(context, text=err, **self._reply_kwargs(update))
            return
        if int(getattr(session, "chat_id", 0) or 0) != owner_chat_id:
            logging.getLogger(__name__).warning(
                "cmd_newpath created session outside inbound owner scope owner_chat_id=%s session_chat_id=%s",
                owner_chat_id,
                getattr(session, "chat_id", None),
            )
        await self.bot_app._send_message(
            context,
            text=t("msg.session.created", lang, id=session.id),
            **self._reply_kwargs(update, session),
        )

    async def cmd_sessions(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        route = await self.bot_app.ensure_telegram_inbound_authorized(
            update,
            context,
            allow_outside_topic=True,
        )
        if route is None:
            return
        owner_chat_id = int(route.owner_chat_id)
        text, keyboard = self.build_sessions_active_overview(
            owner_chat_id,
            session=route.session,
            session_uid=route.session_uid,
        )
        await self.bot_app._send_message(context, text=text, reply_markup=keyboard, **route.reply_kwargs())

    async def cmd_close(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        chat_id = update.effective_chat.id
        if not await self._ensure_allowed(chat_id, context, update=update):
            return
        if not await self._require_admin(chat_id, context, scope="new_projects", update=update):
            return
        owner_chat_id = self._owner_chat_id(update)
        try:
            lang = resolve_user_lang(self.bot_app.config, chat_id=owner_chat_id)
        except Exception:
            lang = "ru"
        route = self.bot_app.resolve_telegram_inbound_route(update)
        ui_key = self.bot_app.telegram_ui_key_from_route(route, fallback_chat_id=chat_id)
        if not context.args:
            items = list(self.bot_app.manager.sessions_for_chat(owner_chat_id).keys())
            if not items:
                await self.bot_app._send_message(context, text=t("msg.session.none", lang), **self._reply_kwargs(update))
                return
            self.bot_app.ui_state.close_menu[ui_key] = items
            rows = [
                [InlineKeyboardButton(sid, callback_data=f"close_pick:{i}")]
                for i, sid in enumerate(items)
            ]
            rows.append([InlineKeyboardButton(t("btn.session.cancel", lang), callback_data="agent_cancel")])
            keyboard = InlineKeyboardMarkup(rows)
            await self.bot_app._send_message(
                context,
                text=t("msg.session.choose_close", lang),
                reply_markup=keyboard,
                **self._reply_kwargs(update),
            )
            return
        ok = await self.bot_app.close_session_with_cleanup(context.args[0], owner_chat_id, context)
        if ok:
            await self.bot_app._send_message(context, text=t("msg.session.closed", lang), **self._reply_kwargs(update))
        else:
            await self.bot_app._send_message(context, text=t("msg.error.session_not_found", lang), **self._reply_kwargs(update))

    async def cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        chat_id = update.effective_chat.id
        if not await self._ensure_allowed(chat_id, context, update=update):
            return
        s = await self._require_scope_session(chat_id, context, auto_create=False, update=update)
        if not s:
            return
        await self.bot_app._send_message(
            context,
            text=self._active_session_status_text(s, chat_id=chat_id),
            **self._reply_kwargs(update, s),
        )

    async def cmd_reports(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        route, session = await self.bot_app.ensure_telegram_inbound_session(
            update,
            context,
            auto_create=False,
            scope="reports",
            allow_outside_topic=True,
        )
        if route is None or session is None:
            return
        try:
            lang = resolve_user_lang(self.bot_app.config, chat_id=int(route.owner_chat_id))
        except Exception:
            lang = "ru"

        service = getattr(self.bot_app, "report_history_service", None)
        if service is None:
            await self.bot_app._send_message(
                context,
                text=t("msg.report.unavailable", lang),
                **route.reply_kwargs(),
            )
            return

        args = [str(arg or "").strip() for arg in getattr(context, "args", []) if str(arg or "").strip()]
        action = str(args[0] if args else "").strip()
        if action.lower() == "generate":
            await self._generate_and_send_report(context, route, session, lang)
            return
        if action.lower() == "snapshot":
            await self._generate_and_send_session_snapshot(context, route, session, lang)
            return
        if action.lower() == "latest":
            await self._send_report_document(context, route, session, None, lang)
            return
        if action:
            await self._send_report_document(context, route, session, action, lang)
            return

        reports = service.list_reports(session, limit=10)
        if not reports:
            await self.bot_app._send_message(
                context,
                text=t("msg.report.empty", lang),
                **route.reply_kwargs(),
            )
            return

        lines = [
            t(
                "msg.report.menu_title",
                lang,
                session_id=str(getattr(session, "id", "") or "-"),
            ),
            "",
        ]
        for idx, report in enumerate(reports, start=1):
            lines.append(
                f"{idx}. `{report.report_id}` — {report.date}, {report.size} bytes"
            )
        lines.extend(
            [
                "",
                t("msg.report.usage", lang),
            ]
        )
        await self.bot_app._send_message(
            context,
            text="\n".join(lines),
            **route.reply_kwargs(),
        )

    async def _generate_and_send_report(
        self,
        context: ContextTypes.DEFAULT_TYPE,
        route,
        session: Session,
        lang: str,
    ) -> None:
        service = self.bot_app.report_history_service
        try:
            from modes.sdk.planning import load_plan

            plan = load_plan(session.workdir, scoped_key=session_scoped_key(session))
        except Exception:
            logging.getLogger(__name__).exception(
                "report plan load failed session_uid=%s",
                session_runtime_uid(session),
            )
            await self.bot_app._send_message(
                context,
                text=t("msg.report.generate_failed", lang),
                **route.reply_kwargs(),
            )
            return
        if not plan:
            await self.bot_app._send_message(
                context,
                text=t("msg.report.no_plan", lang),
                **route.reply_kwargs(),
            )
            return
        try:
            summary = service.save_manager_plan_report(session, plan)
        except Exception:
            logging.getLogger(__name__).exception(
                "report generate failed session_uid=%s",
                session_runtime_uid(session),
            )
            await self.bot_app._send_message(
                context,
                text=t("msg.report.generate_failed", lang),
                **route.reply_kwargs(),
            )
            return
        await self.bot_app._send_message(
            context,
            text=t("msg.report.generated", lang, name=summary.report_id),
            **route.reply_kwargs(),
        )
        await self._send_report_document(context, route, session, summary.report_id, lang)

    async def _generate_and_send_session_snapshot(
        self,
        context: ContextTypes.DEFAULT_TYPE,
        route,
        session: Session,
        lang: str,
    ) -> None:
        service = getattr(self.bot_app, "session_snapshot_report_service", None)
        if service is None:
            await self.bot_app._send_message(
                context,
                text=t("msg.report.snapshot_unavailable", lang),
                **route.reply_kwargs(),
            )
            return
        try:
            summary = await asyncio.to_thread(service.save_html_report, session, lang=lang)
        except Exception:
            logging.getLogger(__name__).exception(
                "session snapshot report failed session_uid=%s",
                session_runtime_uid(session),
            )
            await self.bot_app._send_message(
                context,
                text=t("msg.report.snapshot_failed", lang),
                **route.reply_kwargs(),
            )
            return
        await self.bot_app._send_message(
            context,
            text=t("msg.report.snapshot_generated", lang, name=summary.report_id),
            **route.reply_kwargs(),
        )
        await self._send_report_document(context, route, session, summary.report_id, lang)

    async def _send_report_document(
        self,
        context: ContextTypes.DEFAULT_TYPE,
        route,
        session: Session,
        report_id: Optional[str],
        lang: str,
    ) -> None:
        service = self.bot_app.report_history_service
        try:
            target_id = str(report_id or "").strip()
            if not target_id:
                reports = service.list_reports(session, limit=20)
                if not reports:
                    await self.bot_app._send_message(
                        context,
                        text=t("msg.report.empty", lang),
                        **route.reply_kwargs(),
                    )
                    return
                target_id = reports[0].report_id
            document = service.get_report(session, target_id)
        except InvalidReportIdError:
            await self.bot_app._send_message(
                context,
                text=t("msg.report.invalid_id", lang),
                **route.reply_kwargs(),
            )
            return
        except ReportNotFoundError:
            await self.bot_app._send_message(
                context,
                text=t("msg.report.not_found", lang),
                **route.reply_kwargs(),
            )
            return
        except Exception:
            logging.getLogger(__name__).exception(
                "report send failed session_uid=%s report_id=%s",
                session_runtime_uid(session),
                report_id,
            )
            await self.bot_app._send_message(
                context,
                text=t("msg.report.send_failed", lang),
                **route.reply_kwargs(),
            )
            return

        with open(document.summary.path, "rb") as f:
            ok = await self.bot_app._send_document(
                context,
                document=f,
                filename=document.summary.name,
                **route.reply_kwargs(),
            )
        if not ok:
            await self.bot_app._send_message(
                context,
                text=t("msg.report.send_failed", lang),
                **route.reply_kwargs(),
            )

    async def cmd_limits(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        route = await self.bot_app.ensure_telegram_inbound_authorized(
            update,
            context,
            allow_outside_topic=True,
        )
        if route is None:
            return
        manager = getattr(self.bot_app, "manager", None)
        service = getattr(self.bot_app, "cli_limits_service", None)
        try:
            lang = resolve_user_lang(self.bot_app.config, chat_id=int(route.owner_chat_id))
        except Exception:
            lang = "ru"
        if manager is None or not hasattr(manager, "sessions_for_chat") or service is None:
            await self.bot_app._send_message(
                context,
                text=t("msg.error.limits_unavailable", lang),
                **route.reply_kwargs(),
            )
            return
        try:
            sessions = self._visible_sessions_for_chat(int(route.owner_chat_id))
            config = getattr(self.bot_app, "config", None)
            tools = getattr(config, "tools", None)
            available_clis = None
            if isinstance(tools, dict):
                available_clis = [
                    name
                    for name, tool in tools.items()
                    if str(name or "").strip().lower() in service.SUPPORTED_CLI_NAMES
                    and bool(getattr(tool, "enabled", True))
                ]
            preferred_session = getattr(route, "session", None)
            if preferred_session is not None and not self._is_session_visible_for_chat(int(route.owner_chat_id), preferred_session):
                preferred_session = None
            preferred_workdir = str(getattr(preferred_session, "workdir", "") or "").strip() or None
            text = await service.describe_for_sessions(
                sessions,
                available_clis=available_clis,
                preferred_workdir=preferred_workdir,
            )
        except Exception:
            logging.getLogger(__name__).exception("limits command failed owner_chat_id=%s", route.owner_chat_id)
            text = t("msg.error.limits_fetch_failed", lang)
        await self.bot_app._send_message(context, text=text, **route.reply_kwargs())

    async def cmd_mode(self, update: Update, context: ContextTypes.DEFAULT_TYPE, mode_id: str) -> None:
        mode_id = str(mode_id or "").strip()
        args = list(getattr(context, "args", []) or [])
        subcommand = str(args[0] or "").strip().lower() if args else ""
        await self._show_mode_menu(update, context, mode_id, subcommand=subcommand, command_args=args)

    async def _show_mode_menu(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        mode_id: str,
        *,
        subcommand: str = "",
        command_args: Optional[list[str]] = None,
    ) -> None:
        chat_id = update.effective_chat.id
        if not await self._ensure_allowed(chat_id, context, update=update):
            return
        s = await self._require_scope_session(chat_id, context, auto_create=False, update=update)
        if not s:
            return
        try:
            lang = resolve_user_lang(self.bot_app.config, chat_id=chat_id)
        except Exception:
            lang = "ru"
        reply_kwargs = self._reply_kwargs(update, s)
        svc = getattr(self.bot_app, "mode_registry_service", None)
        plugin = svc.get(mode_id) if svc else None
        policy = getattr(self.bot_app, "access_policy_service", None)
        route = self.bot_app.resolve_telegram_inbound_route(update)
        policy_chat_id = int(route.owner_chat_id)
        is_mode_allowed = policy.is_mode_allowed_for_chat(policy_chat_id, mode_id) if policy else True
        if not is_mode_allowed:
            await self.bot_app._send_message(
                context, text=t("msg.error.mode_unavailable_user_named", lang, mode_id=mode_id), **reply_kwargs
            )
            return
        if plugin is None or not hasattr(plugin, "build_menu"):
            await self.bot_app._send_message(context, text=t("msg.error.mode_unavailable_named", lang, mode_id=mode_id), **reply_kwargs)
            return

        menu_visibility = build_mode_menu_visibility(
            session=s,
            mode_id=mode_id,
            access_policy=getattr(self.bot_app, "access_policy_service", None),
            user_id=getattr(getattr(update, "effective_user", None), "id", None),
        )
        back_text = t("btn.session.back", lang)
        text, keyboard = call_mode_build_menu(
            plugin,
            s,
            back_callback=build_session_overview_callback_data(s),
            back_text=back_text,
            menu_visibility=menu_visibility,
        )
        await self.bot_app._send_message(context, text=text, reply_markup=keyboard, **reply_kwargs)

    async def cmd_interrupt(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        chat_id = update.effective_chat.id
        if not await self._ensure_allowed(chat_id, context, update=update):
            return
        s = await self._require_scope_session(chat_id, context, auto_create=False, update=update)
        if not s:
            return
        reply_kwargs = self._reply_kwargs(update, s)
        try:
            lang = resolve_user_lang(self.bot_app.config, chat_id=chat_id)
        except Exception:
            lang = "ru"
        interrupt_runtime = getattr(getattr(self.bot_app, "session_management", None), "interrupt_session_runtime", None)
        message_text = t("msg.session.interrupt_sent", lang)
        if callable(interrupt_runtime):
            report = await interrupt_runtime(
                s,
                owner_chat_id=int(getattr(s, "chat_id", 0) or self._owner_chat_id(update)),
                reply_chat_id=reply_kwargs.get("chat_id"),
                message_thread_id=reply_kwargs.get("message_thread_id"),
                reason="telegram_command",
            )
            formatter = getattr(self.bot_app.session_management, "format_interrupt_user_message", None)
            if callable(formatter):
                message_text = str(formatter(report, lang=lang) or message_text)
        else:
            s.interrupt()
            try:
                await self._cancel_mode_tasks_session(session_runtime_uid(s))
            except Exception as e:
                logging.exception("failed to cancel mode tasks for session=%s: %s", s.id, e)
        await self.bot_app._send_message(context, text=message_text, **reply_kwargs)

    async def cmd_queue(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        chat_id = update.effective_chat.id
        if not await self._ensure_allowed(chat_id, context, update=update):
            return
        s = await self._require_scope_session(chat_id, context, auto_create=False, update=update)
        if not s:
            return
        try:
            lang = resolve_user_lang(self.bot_app.config, chat_id=chat_id)
        except Exception:
            lang = "ru"
        if not s.queue:
            await self.bot_app._send_message(context, text=t("msg.session.queue_empty", lang), **self._reply_kwargs(update, s))
            return
        await self.bot_app._send_message(context, text=t("msg.session.queue_count", lang, n=len(s.queue)), **self._reply_kwargs(update, s))

    async def cmd_clearqueue(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        chat_id = update.effective_chat.id
        if not await self._ensure_allowed(chat_id, context, update=update):
            return
        s = await self._require_scope_session(chat_id, context, auto_create=False, update=update)
        if not s:
            return
        try:
            lang = resolve_user_lang(self.bot_app.config, chat_id=chat_id)
        except Exception:
            lang = "ru"
        s.queue.clear()
        await self._persist_session_async(chat_id, s.id)
        await self.bot_app._send_message(context, text=t("msg.session.queue_cleared", lang), **self._reply_kwargs(update, s))

    async def cmd_rename(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        chat_id = update.effective_chat.id
        if not await self._ensure_allowed(chat_id, context, update=update):
            return
        route = self.bot_app.resolve_telegram_inbound_route(update)
        owner_chat_id = int(route.owner_chat_id)
        try:
            lang = resolve_user_lang(self.bot_app.config, chat_id=owner_chat_id)
        except Exception:
            lang = "ru"
        if not context.args:
            await self.bot_app._send_message(
                context,
                text=t("cmd.rename.usage", lang),
                **self._reply_kwargs(update, route.session),
            )
            return
        session = None
        if len(context.args) >= 2 and context.args[0] in self.bot_app.manager.sessions_for_chat(owner_chat_id):
            session = self.bot_app.manager.get(owner_chat_id, context.args[0])
            name = " ".join(context.args[1:])
        else:
            session = route.session or self.bot_app.resolve_telegram_scope_session(
                reply_chat_id=int(route.reply_chat_id),
                message_thread_id=route.message_thread_id,
                owner_chat_id=owner_chat_id,
            )
            name = " ".join(context.args)
        if not session:
            await self.bot_app._send_message(
                context,
                text=t("msg.error.session_no_scope", lang),
                **self._reply_kwargs(update),
            )
            return
        if not self._is_session_visible_for_chat(owner_chat_id, session):
            await self.bot_app._send_message(
                context,
                text=t("msg.error.session_unavailable", lang),
                **self._reply_kwargs(update),
            )
            return
        session.name = name.strip()
        await self._persist_session_async(owner_chat_id, session.id)
        thread_manager = getattr(self.bot_app, "session_thread_manager", None)
        if thread_manager is not None:
            try:
                await thread_manager.rename_topic_for_session(
                    owner_chat_id=owner_chat_id,
                    session=session,
                    bot=getattr(context, "bot", None),
                )
            except Exception as e:
                logging.exception("session topic rename failed: %s", e)
        await self.bot_app._send_message(context, text=t("msg.session.renamed", lang), **self._reply_kwargs(update, session))

    async def cmd_dirs(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        chat_id = update.effective_chat.id
        if not await self._ensure_allowed(chat_id, context, update=update):
            return
        if not await self._require_admin(chat_id, context, scope="new_projects", update=update):
            return
        try:
            lang = resolve_user_lang(self.bot_app.config, chat_id=chat_id)
        except Exception:
            lang = "ru"
        path = " ".join(context.args) if context.args else self.bot_app.config.defaults.workdir
        if not os.path.isdir(path):
            await self.bot_app._send_message(context, text=t("msg.error.dir_not_found", lang), **self._reply_kwargs(update))
            return
        route = self.bot_app.resolve_telegram_inbound_route(update)
        await self.bot_app.dirs_service.start_flow(
            chat_id,
            context,
            root=path,
            mode_token="browse",
            message_thread_id=route.message_thread_id,
        )

    async def cmd_cwd(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        ui_chat_id = update.effective_chat.id
        owner_chat_id = self._owner_chat_id(update)
        if not await self._ensure_allowed(owner_chat_id, context, update=update, allow_outside_topic=True):
            return
        if not await self._require_admin(
            owner_chat_id,
            context,
            scope="new_projects",
            update=update,
            allow_outside_topic=True,
        ):
            return
        try:
            lang = resolve_user_lang(self.bot_app.config, chat_id=owner_chat_id)
        except Exception:
            lang = "ru"
        if not context.args:
            await self.bot_app._send_message(context, text=t("cmd.cwd.usage", lang), **self._reply_kwargs(update))
            return
        path = " ".join(context.args)
        if not os.path.isdir(path):
            await self.bot_app._send_message(context, text=t("msg.error.dir_not_found", lang), **self._reply_kwargs(update))
            return
        s = await self._require_scope_session(
            ui_chat_id,
            context,
            auto_create=False,
            update=update,
            allow_outside_topic=True,
        )
        if not s:
            return
        session, err = await self.bot_app.session_creation_service.create_session(
            owner_chat_id=owner_chat_id,
            tool=s.tool.name,
            path=path,
            bot=getattr(context, "bot", None),
            ui_chat_id=ui_chat_id,
            register_project=True,
        )
        if err:
            await self.bot_app._send_message(context, text=err, **self._reply_kwargs(update, s))
            return
        await self.bot_app._send_message(
            context,
            text=t("msg.session.created_cwd", lang, id=session.id),
            **self._reply_kwargs(update, session),
        )

    async def cmd_git(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        chat_id = update.effective_chat.id
        if not await self._ensure_allowed(chat_id, context, update=update):
            return
        if not await self._require_admin(chat_id, context, scope="git", update=update):
            return
        route = self.bot_app.resolve_telegram_inbound_route(update)
        session = await self.bot_app.git.ensure_git_session(
            chat_id,
            context,
            message_thread_id=route.message_thread_id,
        )
        if not session:
            return
        if not await self.bot_app.git.ensure_git_repo(
            session,
            chat_id,
            context,
            message_thread_id=route.message_thread_id,
        ):
            return
        try:
            lang = resolve_user_lang(self.bot_app.config, chat_id=chat_id)
        except Exception:
            lang = "ru"
        reply_kwargs = self._reply_kwargs(update)
        await self.bot_app._send_message(
            context,
            text=t("msg.git.operations_menu", lang),
            reply_markup=self.bot_app.git.build_git_keyboard(),
            **reply_kwargs,
        )

    async def cmd_selfupdate(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        chat_id = update.effective_chat.id
        if not await self._ensure_allowed(chat_id, context, update=update, allow_outside_topic=True):
            return
        if not await self._require_admin(
            chat_id,
            context,
            scope="generic",
            update=update,
            allow_outside_topic=True,
        ):
            return
        try:
            lang = resolve_user_lang(self.bot_app.config, chat_id=chat_id)
        except Exception:
            lang = "ru"
        reply_kwargs = self._reply_kwargs(update)

        repo_root = self._project_root()
        if not os.path.isdir(os.path.join(repo_root, ".git")):
            await self.bot_app._send_message(
                context,
                text=t("msg.selfupdate.no_repo", lang, repo_root=repo_root),
                md2=True,
                **reply_kwargs,
            )
            return

        await self.bot_app._send_message(context, text=t("msg.selfupdate.pulling", lang), md2=True, **reply_kwargs)
        try:
            rc, output = await self._run_subprocess("git", "pull", "--ff-only", cwd=repo_root)
        except Exception as e:
            logging.exception("selfupdate git pull failed: %s", e)
            await self.bot_app._send_message(context, text=t("msg.selfupdate.pull_error", lang, e=e), md2=True, **reply_kwargs)
            return

        pull_out = self._trim_output(output)
        if rc != 0:
            text = t("msg.selfupdate.pull_failed", lang)
            if pull_out:
                text += f"\n\n{pull_out}"
            await self.bot_app._send_message(context, text=text, md2=True, **reply_kwargs)
            return

        req_files = self._requirements_files_from_pull_output(output)
        if req_files:
            req_file = None
            for candidate in ("requirements.txt", "requirement.txt"):
                if candidate in req_files and os.path.isfile(os.path.join(repo_root, candidate)):
                    req_file = candidate
                    break
            if req_file is None:
                for candidate in ("requirements.txt", "requirement.txt"):
                    if os.path.isfile(os.path.join(repo_root, candidate)):
                        req_file = candidate
                        break
            if req_file:
                venv_python = self._venv_python_path(repo_root)
                if not venv_python:
                    await self.bot_app._send_message(
                        context,
                        text=t("msg.selfupdate.no_venv", lang),
                        md2=True,
                        **reply_kwargs,
                    )
                    return
                await self.bot_app._send_message(
                    context,
                    text=t("msg.selfupdate.updating_deps", lang, req_file=req_file),
                    md2=True,
                    **reply_kwargs,
                )
                dep_rc, dep_output = await self._run_subprocess(
                    venv_python,
                    "-m",
                    "pip",
                    "install",
                    "-r",
                    req_file,
                    cwd=repo_root,
                )
                if dep_rc != 0:
                    dep_out = self._trim_output(dep_output)
                    text = t("msg.selfupdate.deps_failed", lang)
                    if dep_out:
                        text += f"\n\n{dep_out}"
                    await self.bot_app._send_message(context, text=text, md2=True, **reply_kwargs)
                    return

        service_name = self._bot_service_name()
        marker_path = self._selfupdate_marker_path()
        try:
            self._save_selfupdate_marker(
                chat_id=int(reply_kwargs.get("chat_id") or chat_id),
                service_name=service_name,
                message_thread_id=reply_kwargs.get("message_thread_id"),
            )
        except Exception as e:
            logging.exception("selfupdate marker save failed: %s", e)
        if marker_path:
            try:
                self._spawn_selfupdate_watchdog(marker_path=marker_path, timeout_sec=30)
            except Exception as e:
                logging.exception("selfupdate watchdog spawn failed: %s", e)

        restart_invoked = False
        try:
            restart_rc, restart_output = await self._run_subprocess(
                "systemctl",
                "restart",
                "--no-block",
                service_name,
            )
            restart_invoked = True
            logging.info("selfupdate: systemctl restart requested service=%s rc=%s", service_name, restart_rc)
        except Exception as e:
            logging.exception("selfupdate service restart failed: %s", e)
            restart_rc, restart_output = 1, str(e)

        if not restart_invoked:
            self._clear_selfupdate_marker()
            restart_out = self._trim_output(restart_output)
            text = t("msg.selfupdate.restart_failed_no_invoke", lang, service_name=service_name)
            if restart_out:
                text += f"\n\n{restart_out}"
            await self.bot_app._send_message(context, text=text, md2=True, **reply_kwargs)
            return

        restart_confirmed = restart_rc == 0
        restart_state = ""
        if not restart_confirmed:
            # systemctl can return non-zero in self-restart scenarios even when
            # the unit transitions to active/activating right after.
            try:
                state_rc, state_output = await self._run_subprocess(
                    "systemctl",
                    "is-active",
                    service_name,
                )
                restart_state = str(state_output or "").strip().splitlines()[-1].strip().lower() if state_output else ""
                logging.info(
                    "selfupdate: systemctl is-active service=%s rc=%s state=%s",
                    service_name,
                    state_rc,
                    restart_state,
                )
                if restart_state in {"active", "activating", "reloading", "deactivating"}:
                    restart_confirmed = True
            except Exception as e:
                logging.exception("selfupdate service state check failed: %s", e)

        if restart_confirmed:
            text = t("msg.selfupdate.success", lang)
            await self.bot_app._send_message(context, text=text, md2=True, **reply_kwargs)
            return

        if restart_state:
            restart_out = self._trim_output(restart_output)
            text = t("msg.selfupdate.restart_unconfirmed", lang, service_name=service_name)
            text += t("msg.selfupdate.restart_status", lang, restart_rc=restart_rc, restart_state=restart_state)
            if restart_out:
                text += f"\n\n{restart_out}"
            await self.bot_app._send_message(context, text=text, md2=True, **reply_kwargs)
            return

        restart_out = self._trim_output(restart_output)
        text = t("msg.selfupdate.restart_failed", lang, service_name=service_name)
        if restart_out:
            text += f"\n\n{restart_out}"
        await self.bot_app._send_message(context, text=text, md2=True, **reply_kwargs)

    async def cmd_setprompt(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        chat_id = update.effective_chat.id
        if not await self._ensure_allowed(chat_id, context, update=update):
            return
        if not await self._require_admin(chat_id, context, scope="generic", update=update):
            return
        try:
            lang = resolve_user_lang(self.bot_app.config, chat_id=chat_id)
        except Exception:
            lang = "ru"
        reply_kwargs = self._reply_kwargs(update)
        args = context.args
        if len(args) < 2:
            await self.bot_app._send_message(context, text=t("cmd.setprompt.usage", lang), **reply_kwargs)
            return
        tool_name = args[0]
        regex = " ".join(args[1:])
        container = getattr(self.bot_app, "container", None)
        config_service = getattr(container, "config_service", None)
        if config_service is None:
            await self.bot_app._send_message(
                context,
                text=t("msg.error.config_service_unavailable", lang),
                **reply_kwargs,
            )
            return

        current_config = await config_service.load()
        tool = current_config.tools.get(tool_name)
        if not tool:
            await self.bot_app._send_message(context, text=t("msg.error.tool_not_found", lang), **reply_kwargs)
            return
        expected_revision = await config_service.current_revision(current_config)
        draft_config = copy.deepcopy(current_config)
        draft_config.tools[tool_name].prompt_regex = regex

        result = await config_service.save_config_draft_with_revision(
            draft_config,
            expected_revision=expected_revision,
        )
        reload_result = None
        if result.ok:
            reload_runtime_config = getattr(self.bot_app, "reload_runtime_config", None)
            if callable(reload_runtime_config):
                reload_result = await reload_runtime_config()

        success_prefix = t("msg.setprompt.saved", lang) if result.ok else t("msg.setprompt.not_saved", lang)
        text = self._config_save_summary(
            result,
            success_prefix=success_prefix,
            reload_result=reload_result,
        )
        await self.bot_app._send_message(context, text=text, **reply_kwargs)

    @staticmethod
    def _config_save_summary(result, *, success_prefix: str, reload_result: Optional[dict] = None) -> str:
        def _paths(values) -> str:
            items = [str(value) for value in (values or [])]
            return ", ".join(items) if items else "none"

        lines = [
            success_prefix,
            f"changed: {'yes' if bool(result.changed) else 'no'}",
            f"restart_required: {_paths(result.restart_required)}",
            f"reloadable: {_paths(result.reloadable)}",
            f"not_applied: {_paths(getattr(result, 'not_applied', []))}",
            f"errors: {_paths(result.errors)}",
        ]
        if result.backup_path:
            lines.append(f"backup_path: {result.backup_path}")
        if reload_result is None:
            return "\n".join(lines)

        lines.append(f"runtime_reload: {reload_result.get('status', 'unknown')}")
        lines.append(f"runtime_applied: {_paths(reload_result.get('applied'))}")
        lines.append(f"runtime_restart_required: {_paths(reload_result.get('restart_required'))}")
        warnings = reload_result.get("warnings") or []
        if warnings:
            lines.append(f"runtime_warnings: {_paths(warnings)}")
        return "\n".join(lines)

    async def cmd_resume(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        chat_id = update.effective_chat.id
        if not await self._ensure_allowed(chat_id, context, update=update):
            return
        s = await self._require_scope_session(chat_id, context, auto_create=False, update=update)
        if not s:
            return
        try:
            lang = resolve_user_lang(self.bot_app.config, chat_id=chat_id)
        except Exception:
            lang = "ru"
        if not context.args:
            token = s.resume_token or t("session_status.no", lang)
            await self.bot_app._send_message(
                context, text=t("msg.session.resume_current", lang, token=token), **self._reply_kwargs(update, s)
            )
            return
        token = " ".join(context.args).strip()
        s.resume_token = token
        await self._persist_session_async(chat_id, s.id)
        await self.bot_app._send_message(context, text=t("msg.session.resume_saved", lang), **self._reply_kwargs(update, s))

    async def cmd_state(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        chat_id = update.effective_chat.id
        if not await self._ensure_allowed(chat_id, context, update=update):
            return
        route = self.bot_app.resolve_telegram_inbound_route(update)
        owner_chat_id = int(route.owner_chat_id)
        try:
            lang = resolve_user_lang(self.bot_app.config, chat_id=owner_chat_id)
        except Exception:
            lang = "ru"
        s = route.session or self.bot_app.resolve_telegram_scope_session(
            reply_chat_id=int(route.reply_chat_id),
            message_thread_id=route.message_thread_id,
            owner_chat_id=owner_chat_id,
        )
        repo = self._state_repository()
        if repo is None:
            await self.bot_app._send_message(
                context,
                text=t("msg.session.state_path_missing", lang),
                **self._reply_kwargs(update, s),
            )
            return
        if context.args:
            # Prefer session_id to avoid ambiguity when multiple sessions share tool/workdir.
            st = None
            sid = context.args[0]
            if sid in self.bot_app.manager.sessions_for_chat(owner_chat_id):
                s0 = self.bot_app.manager.get(owner_chat_id, sid)
                if s0 and self._is_session_visible_for_chat(owner_chat_id, s0):
                    st = repo.get_state(
                        tool=s0.tool.name,
                        workdir=s0.workdir,
                        session_id=s0.id,
                        chat_id=owner_chat_id,
                    )
                elif s0 is not None:
                    await self.bot_app._send_message(
                        context,
                        text=t("msg.error.session_unavailable", lang),
                        **self._reply_kwargs(update, s),
                    )
                    return
            if not st and len(context.args) >= 2:
                tool = context.args[0]
                workdir = " ".join(context.args[1:])
                if self._is_workdir_visible_for_chat(owner_chat_id, workdir):
                    st = repo.get_state(tool=tool, workdir=workdir, chat_id=owner_chat_id)
            if not st:
                await self.bot_app._send_message(
                    context,
                    text=t("msg.session.state_not_found_hint", lang),
                    **self._reply_kwargs(update, s),
                )
                return
            text = format_session_state(st, self.bot_app._format_ts(st.updated_at), lang)
            await self.bot_app._send_message(context, text=text, **self._reply_kwargs(update, s))
            return
        if not s:
            await self.bot_app._send_message(
                context,
                text=t("msg.error.session_no_scope", lang),
                **self._reply_kwargs(update),
            )
            return
        try:
            data = repo.load_state(chat_id=owner_chat_id)
        except Exception as e:
            logging.exception(f"tool failed {str(e)}")
            await self.bot_app._send_message(context, text=t("msg.error.state_read_error", lang, e=e), **self._reply_kwargs(update, s))
            return
        if not data:
            await self.bot_app._send_message(context, text=t("msg.session.state_not_found", lang), **self._reply_kwargs(update, s))
            return
        keys = list(data.keys())
        route = self.bot_app.resolve_telegram_inbound_route(update)
        ui_key = self.bot_app.telegram_ui_key_from_route(route, fallback_chat_id=chat_id)
        self.bot_app.ui_state.state_menu[ui_key] = keys
        self.bot_app.ui_state.state_menu_page[ui_key] = 0
        keyboard = self.bot_app._build_state_keyboard(ui_key)
        await self.bot_app._send_message(context,
                                         text=t("msg.session.state_choose", lang),
                                         reply_markup=keyboard,
                                         **self._reply_kwargs(update, s),
                                         )

    async def cmd_send(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        chat_id = update.effective_chat.id
        if not await self._ensure_allowed(chat_id, context, update=update):
            return
        if not context.args:
            try:
                lang = resolve_user_lang(self.bot_app.config, chat_id=chat_id)
            except Exception:
                lang = "ru"
            await self.bot_app._send_message(context, text=t("cmd.send.usage", lang), **self._reply_kwargs(update))
            return
        _route, session = await self.bot_app.ensure_telegram_inbound_session(update, context, auto_create=True)
        if not session:
            return
        text = " ".join(context.args)
        await self.bot_app._handle_cli_input(
            session,
            text,
            chat_id,
            context,
            dest=self.bot_app.build_telegram_reply_dest(
                session,
                chat_id,
                user_id=getattr(getattr(update, "effective_user", None), "id", None),
            ),
        )

    def _bot_commands(self, *, include_admin: bool = False, lang: str = "ru") -> list[BotCommand]:
        commands = []
        for entry in build_command_registry(self.bot_app):
            if not entry["menu"]:
                continue
            if bool(entry.get("admin_only")) and not include_admin:
                continue
            desc_key = entry.get("desc_key")
            desc_params = dict(entry.get("desc_params") or {})
            if desc_key:
                desc = t(desc_key, lang, **desc_params)
            else:
                desc = str(entry.get("desc", ""))
            desc = desc[:256]
            commands.append(BotCommand(command=entry["name"], description=desc))
        return commands

    async def set_bot_commands(self, app: Application) -> None:
        default_lang = getattr(
            getattr(self.bot_app.config, "defaults", None), "default_language", "ru"
        ) or "ru"
        await app.bot.set_my_commands(
            self._bot_commands(include_admin=False, lang=default_lang),
            scope=BotCommandScopeDefault(),
        )
        for lang in SUPPORTED_LANGS:
            await app.bot.set_my_commands(
                self._bot_commands(include_admin=False, lang=lang),
                scope=BotCommandScopeDefault(),
                language_code=lang,
            )
        admin_commands_by_lang = {
            lang: self._bot_commands(include_admin=True, lang=lang)
            for lang in SUPPORTED_LANGS
        }
        admin_commands_default = self._bot_commands(include_admin=True, lang=default_lang)
        for chat_id in list(getattr(self.bot_app.config.telegram, "admlist_chat_ids", []) or []):
            # Default (no language_code) scope so admins whose Telegram language is
            # outside the supported set still get admin commands.
            await app.bot.set_my_commands(
                admin_commands_default,
                scope=BotCommandScopeChat(chat_id=int(chat_id)),
            )
            for lang in SUPPORTED_LANGS:
                await app.bot.set_my_commands(
                    admin_commands_by_lang[lang],
                    scope=BotCommandScopeChat(chat_id=int(chat_id)),
                    language_code=lang,
                )

    async def cmd_files(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        chat_id = update.effective_chat.id
        if not await self._ensure_allowed(chat_id, context, update=update):
            return
        if not await self._require_admin(chat_id, context, scope="files", update=update):
            return
        _route, session = await self.bot_app.ensure_telegram_inbound_session(update, context, auto_create=True)
        if not session:
            return
        try:
            lang = resolve_user_lang(self.bot_app.config, chat_id=chat_id)
        except Exception:
            lang = "ru"
        base = session.workdir
        owner_chat_id = int(getattr(session, "chat_id", None) or chat_id)
        try:
            await resolve_files_payload(
                session_files_service(self.bot_app).meta(
                    owner_chat_id,
                    session_uid_for_files(owner_chat_id, session),
                    ".",
                    protect_sensitive=False,
                )
            )
        except FilesServiceError:
            await self.bot_app._send_message(
                context, text=t("msg.session.workdir_unavailable", lang), **self._reply_kwargs(update, session)
            )
            return
        route = self.bot_app.resolve_telegram_inbound_route(update)
        ui_key = self.bot_app.telegram_ui_key_from_route(route, fallback_chat_id=chat_id)
        self.bot_app.ui_state.files_dir[ui_key] = base
        self.bot_app.ui_state.files_page[ui_key] = 0
        await self.bot_app._send_files_menu(
            chat_id,
            session,
            context,
            edit_message=None,
            message_thread_id=ui_key.message_thread_id,
        )

    async def _send_dirs_menu(
        self,
        chat_id: int,
        context: ContextTypes.DEFAULT_TYPE,
        base: str,
        *,
        message_thread_id: Optional[int] = None,
    ) -> None:
        await self.bot_app.dirs_service.send_menu(
            chat_id,
            context,
            base,
            message_thread_id=message_thread_id,
        )

    async def _send_files_menu(
        self,
        chat_id: int,
        session: Session,
        context: ContextTypes.DEFAULT_TYPE,
        edit_message: Optional[object],
        message_thread_id: Optional[int] = None,
    ) -> None:
        owner_chat_id_for_lang = int(getattr(session, "chat_id", None) or chat_id)
        try:
            lang = resolve_user_lang(self.bot_app.config, chat_id=owner_chat_id_for_lang)
        except Exception:
            lang = "ru"
        if edit_message and getattr(edit_message, "message", None):
            ui_key = self.bot_app.telegram_ui_key_from_query(edit_message) or self.bot_app.telegram_ui_key(
                chat_id,
                message_thread_id,
            )
        else:
            scope = getattr(session, "conversation_scope", None)
            thread_id = message_thread_id
            if (
                thread_id is None
                and getattr(scope, "message_thread_id", None) is not None
                and int(getattr(scope, "chat_id", 0) or 0) == int(chat_id)
            ):
                thread_id = int(scope.message_thread_id)
            ui_key = self.bot_app.telegram_ui_key(chat_id, thread_id)
        base = self.bot_app.ui_state.files_dir.get(ui_key, session.workdir)
        owner_chat_id = int(getattr(session, "chat_id", None) or chat_id)
        session_uid = session_uid_for_files(owner_chat_id, session)
        rel_base = files_rel_path(session, base)
        try:
            tree = await resolve_files_payload(
                session_files_service(self.bot_app).tree(
                    owner_chat_id,
                    session_uid,
                    rel_base,
                    protect_sensitive=False,
                )
            )
        except FilesServiceError:
            rel_base = "."
            base = session.workdir
            self.bot_app.ui_state.files_dir[ui_key] = base
            self.bot_app.ui_state.files_page[ui_key] = 0
            try:
                tree = await resolve_files_payload(
                    session_files_service(self.bot_app).tree(
                        owner_chat_id,
                        session_uid,
                        ".",
                        protect_sensitive=False,
                    )
                )
            except FilesServiceError:
                tree = {"path": ".", "items": []}
        current_rel_path = str(tree.get("path") or rel_base or ".")
        base = files_display_path(session, current_rel_path)
        self.bot_app.ui_state.files_dir[ui_key] = base
        entries = []
        for item in list(tree.get("items") or []):
            if not isinstance(item, dict):
                continue
            rel_path = str(item.get("path") or "")
            if not rel_path:
                continue
            entry = dict(item)
            entry["rel_path"] = rel_path
            entry["path"] = files_display_path(session, rel_path)
            entries.append(entry)
        self.bot_app.ui_state.files_entries[ui_key] = entries
        page = max(0, self.bot_app.ui_state.files_page.get(ui_key, 0))
        page_size = 20
        start = page * page_size
        end = start + page_size
        page_entries = entries[start:end]
        total_pages = max(1, (len(entries) + page_size - 1) // page_size)
        if page >= total_pages:
            page = max(0, total_pages - 1)
            self.bot_app.ui_state.files_page[ui_key] = page
            start = page * page_size
            end = start + page_size
            page_entries = entries[start:end]
        rows = []
        for idx, entry in enumerate(page_entries, start=start):
            if entry["is_dir"]:
                open_cb = f"file_nav:open:{idx}"
                label = f"📁 {entry['name']}"
            else:
                open_cb = f"file_pick:{idx}"
                label = f"📄 {entry['name']}"
            rows.append(
                [
                    InlineKeyboardButton(self.bot_app._short_label(label, 60), callback_data=open_cb),
                    InlineKeyboardButton("✏️", callback_data=f"file_rename:{idx}"),
                    InlineKeyboardButton("🗑", callback_data=f"file_del:{idx}"),
                ]
            )
        nav_row = []
        nav_row.append(InlineKeyboardButton(t("btn.files.up", lang), callback_data="file_nav:up"))
        if page > 0:
            nav_row.append(InlineKeyboardButton("◀️", callback_data="file_nav:prev"))
        if page < total_pages - 1:
            nav_row.append(InlineKeyboardButton("▶️", callback_data="file_nav:next"))
        if nav_row:
            rows.append(nav_row)
        if current_rel_path not in ("", "."):
            rows.append([InlineKeyboardButton(t("btn.files.delete_dir", lang), callback_data="file_del_current")])
        rows.append([InlineKeyboardButton(t("btn.files.save_here", lang), callback_data="file_save_here")])
        rows.append([InlineKeyboardButton(t("btn.session.cancel", lang), callback_data="file_nav:cancel")])
        text = t("msg.files.dir_page", lang, base=base, page=page + 1, total=total_pages)
        pending_upload = self.bot_app.ui_state.files_pending_upload.get(ui_key)
        if pending_upload:
            expires_at = float(pending_upload.get("expires_at", 0.0))
            remaining = max(0, int(expires_at - time.time()))
            pending_dir = os.path.abspath(str(pending_upload.get("dir") or ""))
            if pending_dir == os.path.abspath(base):
                text += t("msg.files.upload_pending", lang, remaining=remaining)
            else:
                text += t("msg.files.upload_pending_other_dir", lang, remaining=remaining)
        keyboard = InlineKeyboardMarkup(rows)
        if edit_message:
            if getattr(edit_message, "message", None):
                await self.bot_app._edit_message(
                    context,
                    chat_id=edit_message.message.chat_id,
                    message_id=edit_message.message.message_id,
                    text=text,
                    md2=True,
                    reply_markup=keyboard,
                )
        else:
            await self.bot_app._send_message(
                context,
                text=text,
                reply_markup=keyboard,
                **ui_key.reply_kwargs(),
            )

    async def cmd_preset(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        chat_id = update.effective_chat.id
        if not await self._ensure_allowed(chat_id, context, update=update):
            return
        try:
            lang = resolve_user_lang(self.bot_app.config, chat_id=chat_id)
        except Exception:
            lang = "ru"
        presets = self.bot_app._preset_commands()
        keyboard = InlineKeyboardMarkup(
            [[InlineKeyboardButton(k, callback_data=f"preset_run:{k}")] for k in presets.keys()]
            + [[InlineKeyboardButton(t("btn.session.cancel", lang), callback_data="preset_run:cancel")]]
        )
        await self.bot_app._send_message(
            context,
            text=t("msg.preset.choose", lang),
            reply_markup=keyboard,
            **self._reply_kwargs(update),
        )

    async def cmd_metrics(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        chat_id = update.effective_chat.id
        if not await self._ensure_allowed(chat_id, context, update=update):
            return
        await self.bot_app._send_message(context, text=self.bot_app.metrics.snapshot(), **self._reply_kwargs(update))

    def _lint_evolution_workdir(self, update: Update) -> str:
        chat_id = int(getattr(update.effective_chat, "id", 0) or 0)
        thread_id = getattr(getattr(update, "effective_message", None), "message_thread_id", None)
        session = self.bot_app.resolve_telegram_scope_session(
            reply_chat_id=chat_id,
            message_thread_id=thread_id,
        )
        wd = str(getattr(session, "workdir", "") or "").strip() if session else ""
        if wd:
            return wd
        return str(self.bot_app.config.defaults.workdir or self._project_root())

    async def _lint_evolution_admin_guard(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> bool:
        chat_id = int(getattr(update.effective_chat, "id", 0) or 0)
        if not await self._ensure_allowed(chat_id, context, update=update, allow_outside_topic=True):
            return False
        if not await self._require_admin(
            chat_id, context, scope="generic", update=update, allow_outside_topic=True
        ):
            return False
        return True

    async def cmd_lint_evolution_status(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        if not await self._lint_evolution_admin_guard(update, context):
            return
        from app.services.lint_evolution import autopause as _ap
        from app.services.lint_evolution import (
            rules_store as _rs,
            schema_store as _ss,
            state as _state_store,
            weights_store as _ws,
        )
        from app.services.lint_evolution.paths import lint_root, project_id_for

        workdir = self._lint_evolution_workdir(update)
        pid = project_id_for(workdir)
        st = _state_store.load_state(workdir)
        project = st.projects.get(pid)
        ap = _ap.status(workdir)

        lines = [
            "Lint Evolution status",
            f"workdir: {workdir}",
            f"project_id: {pid}",
            f"lint_root: {lint_root(workdir)}",
            f"active_rules: {sum(1 for r in _rs.load_rules(workdir) if r.state == 'active')}",
            f"schema_version: {_ss.load_state(workdir).active_version}",
            f"weights_history: {_ws.history_count(workdir)}",
        ]
        if project is not None:
            for level_name, lvl in (
                ("L1", project.level1),
                ("L2", project.level2),
                ("L3", project.level3),
            ):
                lines.append(
                    f"{level_name}: last_run={int(lvl.last_run_ts)} "
                    f"fails={lvl.consecutive_failures} lock={lvl.lock_owner or '-'}"
                )
        for key in ("1", "2", "3"):
            entry = ap.get(key)
            if entry and entry.paused:
                lines.append(f"autopause L{key}: PAUSED ({entry.reason})")
            else:
                lines.append(f"autopause L{key}: ok")

        await self.bot_app._send_message(
            context, text="\n".join(lines), **self._reply_kwargs(update)
        )

    async def cmd_lint_autopause_resume(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        if not await self._lint_evolution_admin_guard(update, context):
            return
        from app.services.lint_evolution import autopause as _ap

        args = list(getattr(context, "args", None) or [])
        chat_id_lint = int(getattr(getattr(update, "effective_chat", None), "id", 0) or 0)
        try:
            lang = resolve_user_lang(self.bot_app.config, chat_id=chat_id_lint)
        except Exception:
            lang = "ru"
        if not args:
            await self.bot_app._send_message(
                context,
                text=t("cmd.lint_autopause_resume.usage", lang),
                **self._reply_kwargs(update),
            )
            return
        try:
            level = int(args[0])
        except (TypeError, ValueError):
            await self.bot_app._send_message(
                context, text=t("msg.lint.level_not_int", lang), **self._reply_kwargs(update)
            )
            return
        if level not in (1, 2, 3):
            await self.bot_app._send_message(
                context, text=t("msg.lint.level_out_of_range", lang), **self._reply_kwargs(update)
            )
            return

        workdir = self._lint_evolution_workdir(update)
        try:
            resumed = _ap.resume(workdir, level)
        except ValueError as exc:
            await self.bot_app._send_message(
                context, text=t("msg.lint.error", lang, exc=exc), **self._reply_kwargs(update)
            )
            return
        if resumed:
            text = t("msg.lint.autopause_resumed", lang, level=level)
        else:
            text = t("msg.lint.autopause_not_active", lang, level=level)
        await self.bot_app._send_message(context, text=text, **self._reply_kwargs(update))

    async def cmd_lint_schema_history(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        if not await self._lint_evolution_admin_guard(update, context):
            return
        from app.services.lint_evolution import schema_store as _ss

        workdir = self._lint_evolution_workdir(update)
        state = _ss.load_state(workdir)
        fields = _ss.existing_field_names(workdir)
        proposals = _ss.load_proposals(workdir)
        deprecated = _ss.load_deprecated(workdir)
        lines = [
            f"Schema active_version: {state.active_version}",
            f"last_bump_ts: {int(state.last_bump_ts)}",
            f"fields ({len(fields)}): {', '.join(fields) or '-'}",
            f"pending proposals: {len(proposals)}",
            f"deprecated fields: {len(deprecated)}",
        ]
        if proposals:
            for p in proposals[:5]:
                name = p.get("proposed_name") or "?"
                decision = p.get("decision") or "?"
                lines.append(f"  · {name} → {decision}")
        await self.bot_app._send_message(
            context, text="\n".join(lines), **self._reply_kwargs(update)
        )

    async def cmd_lint_gate_dry_run(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        if not await self._lint_evolution_admin_guard(update, context):
            return
        from pathlib import Path as _Path

        from app.services.lint_evolution import rules_store as _rs
        from app.services.lint_evolution.gate_service import LintGateService

        chat_id_gate = int(getattr(getattr(update, "effective_chat", None), "id", 0) or 0)
        try:
            lang = resolve_user_lang(self.bot_app.config, chat_id=chat_id_gate)
        except Exception:
            lang = "ru"
        workdir = self._lint_evolution_workdir(update)
        project_root = _Path(workdir)
        active = [r for r in _rs.load_rules(workdir) if r.state == "active"]
        if not active:
            await self.bot_app._send_message(
                context,
                text=t("msg.lint.no_active_rules", lang),
                **self._reply_kwargs(update),
            )
            return

        py_files = sorted(project_root.rglob("*.py"))[:200]
        try:
            gate = LintGateService(workdir=workdir, project_root=project_root)
            result = gate.run_on_files(py_files)
        except Exception as exc:
            logging.exception("lint_gate_dry_run failed: %s", exc)
            await self.bot_app._send_message(
                context, text=t("msg.lint.gate_error", lang, exc=exc), **self._reply_kwargs(update)
            )
            return

        lines = [
            f"Lint Gate dry-run: rules={result.rules_evaluated} files={result.files_scanned}",
            f"skipped (non-regex): {result.skipped_rules}",
            f"findings: {len(result.findings)}",
        ]
        for f in result.findings[:10]:
            try:
                rel = _Path(f.file).relative_to(project_root)
            except ValueError:
                rel = _Path(f.file)
            lines.append(f"  · {f.rule_id} {rel}:{f.line}")
        if len(result.findings) > 10:
            lines.append(t("msg.lint.more_findings", lang, n=len(result.findings) - 10))
        await self.bot_app._send_message(
            context, text="\n".join(lines), **self._reply_kwargs(update)
        )

    async def cmd_git_branch(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        chat_id = update.effective_chat.id
        if not await self._ensure_allowed(chat_id, context, update=update):
            return
        if not await self._require_admin(chat_id, context, scope="git", update=update):
            return
        try:
            lang = resolve_user_lang(self.bot_app.config, chat_id=chat_id)
        except Exception:
            lang = "ru"
        if not context.args:
            await self.bot_app._send_message(
                context,
                text=t("msg.git.branch_usage", lang),
                **self._reply_kwargs(update),
            )
            return
        branch_name = context.args[0].strip()
        route = self.bot_app.resolve_telegram_inbound_route(update)
        session = await self.bot_app.git.ensure_git_session(
            chat_id, context, message_thread_id=route.message_thread_id
        )
        if not session:
            return
        if not await self.bot_app.git.ensure_git_repo(
            session, chat_id, context, message_thread_id=route.message_thread_id
        ):
            return
        if not await self.bot_app.git.ensure_git_not_busy(
            session, chat_id, context, message_thread_id=route.message_thread_id
        ):
            return
        try:
            _code, output = await self.bot_app.git.git_branch_create(session, branch_name)
            text = t("msg.git.branch_created", lang, branch=escape_markdown_v2_all(branch_name))
            if output.strip():
                text += f"\n`{escape_markdown_v2_all(output.strip()[:2000])}`"
            await self.bot_app._send_message(
                context, text=text, parse_mode="MarkdownV2", **self._reply_kwargs(update)
            )
        except Exception as exc:
            logger = logging.getLogger(__name__)
            logger.exception("cmd_git_branch failed: %s", exc)
            await self.bot_app._send_message(
                context,
                text=escape_markdown_v2_all(str(exc)),
                **self._reply_kwargs(update),
            )

    async def cmd_git_checkout(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        chat_id = update.effective_chat.id
        if not await self._ensure_allowed(chat_id, context, update=update):
            return
        if not await self._require_admin(chat_id, context, scope="git", update=update):
            return
        try:
            lang = resolve_user_lang(self.bot_app.config, chat_id=chat_id)
        except Exception:
            lang = "ru"
        if not context.args:
            await self.bot_app._send_message(
                context,
                text=t("msg.git.checkout_usage", lang),
                **self._reply_kwargs(update),
            )
            return
        branch_name = context.args[0].strip()
        route = self.bot_app.resolve_telegram_inbound_route(update)
        session = await self.bot_app.git.ensure_git_session(
            chat_id, context, message_thread_id=route.message_thread_id
        )
        if not session:
            return
        if not await self.bot_app.git.ensure_git_repo(
            session, chat_id, context, message_thread_id=route.message_thread_id
        ):
            return
        if not await self.bot_app.git.ensure_git_not_busy(
            session, chat_id, context, message_thread_id=route.message_thread_id
        ):
            return
        try:
            _code, output = await self.bot_app.git.git_checkout(session, branch_name)
            text = t("msg.git.checkout_done", lang, branch=escape_markdown_v2_all(branch_name))
            if output.strip():
                text += f"\n`{escape_markdown_v2_all(output.strip()[:2000])}`"
            await self.bot_app._send_message(
                context, text=text, parse_mode="MarkdownV2", **self._reply_kwargs(update)
            )
        except Exception as exc:
            logger = logging.getLogger(__name__)
            logger.exception("cmd_git_checkout failed: %s", exc)
            await self.bot_app._send_message(
                context,
                text=escape_markdown_v2_all(str(exc)),
                **self._reply_kwargs(update),
            )

    async def cmd_git_stash_pop(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        chat_id = update.effective_chat.id
        if not await self._ensure_allowed(chat_id, context, update=update):
            return
        if not await self._require_admin(chat_id, context, scope="git", update=update):
            return
        try:
            lang = resolve_user_lang(self.bot_app.config, chat_id=chat_id)
        except Exception:
            lang = "ru"
        route = self.bot_app.resolve_telegram_inbound_route(update)
        session = await self.bot_app.git.ensure_git_session(
            chat_id, context, message_thread_id=route.message_thread_id
        )
        if not session:
            return
        if not await self.bot_app.git.ensure_git_repo(
            session, chat_id, context, message_thread_id=route.message_thread_id
        ):
            return
        if not await self.bot_app.git.ensure_git_not_busy(
            session, chat_id, context, message_thread_id=route.message_thread_id
        ):
            return
        try:
            _code, output = await self.bot_app.git.git_stash_pop(session)
            text = t("msg.git.stash_pop_done", lang)
            if output.strip():
                text += f"\n`{escape_markdown_v2_all(output.strip()[:2000])}`"
            await self.bot_app._send_message(
                context, text=text, parse_mode="MarkdownV2", **self._reply_kwargs(update)
            )
        except Exception as exc:
            logger = logging.getLogger(__name__)
            logger.exception("cmd_git_stash_pop failed: %s", exc)
            await self.bot_app._send_message(
                context,
                text=escape_markdown_v2_all(str(exc)),
                **self._reply_kwargs(update),
            )

    async def cmd_git_show(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        chat_id = update.effective_chat.id
        if not await self._ensure_allowed(chat_id, context, update=update):
            return
        if not await self._require_admin(chat_id, context, scope="git", update=update):
            return
        try:
            lang = resolve_user_lang(self.bot_app.config, chat_id=chat_id)
        except Exception:
            lang = "ru"
        ref = context.args[0].strip() if context.args else "HEAD"
        if not _GIT_REF_RE.match(ref) or ".." in ref:
            await self.bot_app._send_message(
                context,
                text=t("msg.git.show_invalid_ref", lang),
                **self._reply_kwargs(update),
            )
            return
        route = self.bot_app.resolve_telegram_inbound_route(update)
        session = await self.bot_app.git.ensure_git_session(
            chat_id, context, message_thread_id=route.message_thread_id
        )
        if not session:
            return
        if not await self.bot_app.git.ensure_git_repo(
            session, chat_id, context, message_thread_id=route.message_thread_id
        ):
            return
        try:
            _code, output = await self.bot_app.git.git_show(session, ref)
            header = t("msg.git.show_done", lang, ref=escape_markdown_v2_all(ref))
            body = escape_markdown_v2_all(output.strip()[:3800]) if output.strip() else ""
            text = f"{header}\n`{body}`" if body else header
            await self.bot_app._send_message(
                context, text=text, parse_mode="MarkdownV2", **self._reply_kwargs(update)
            )
        except Exception as exc:
            logger = logging.getLogger(__name__)
            logger.exception("cmd_git_show failed: %s", exc)
            await self.bot_app._send_message(
                context,
                text=escape_markdown_v2_all(str(exc)),
                **self._reply_kwargs(update),
            )

    async def cmd_sessions_search(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        chat_id = update.effective_chat.id
        if not await self._ensure_allowed(chat_id, context, update=update, allow_outside_topic=True):
            return
        owner_chat_id = self._owner_chat_id(update)
        try:
            lang = resolve_user_lang(self.bot_app.config, chat_id=owner_chat_id)
        except Exception:
            lang = "ru"
        if not context.args:
            await self.bot_app._send_message(
                context,
                text=t("cmd.sessions_search.usage", lang),
                **self._reply_kwargs(update),
            )
            return
        query = " ".join(context.args).strip().lower()
        sessions = self._visible_sessions_for_chat(owner_chat_id)
        matched = [
            s for s in sessions
            if query in s.id.lower()
            or query in str(s.name or "").lower()
            or query in str(s.workdir or "").lower()
        ]
        if not matched:
            await self.bot_app._send_message(
                context,
                text=t("cmd.sessions_search.not_found", lang, query=query),
                **self._reply_kwargs(update),
            )
            return
        lines = [t("cmd.sessions_search.header", lang, n=len(matched))]
        for s in matched:
            name_str = str(s.name or "").strip() or "—"
            tool_str = str(getattr(s.tool, "name", "") or "").strip() or "—"
            workdir_str = str(s.workdir or "").strip() or "—"
            lines.append(
                f"• *{escape_markdown_v2_all(s.id)}*"
                f" \\[{escape_markdown_v2_all(tool_str)}\\]"
                f" {escape_markdown_v2_all(name_str)}"
                f"\n  `{escape_markdown_v2_all(workdir_str)}`"
            )
        await self.bot_app._send_message(
            context,
            text="\n".join(lines),
            parse_mode="MarkdownV2",
            **self._reply_kwargs(update),
        )

    # ------------------------------------------------------------------
    # Remote git commands (admin-only, run git on remote SSH hosts)
    # ------------------------------------------------------------------

    def _remote_git_workdir(self) -> str:
        """Return the bot's primary workdir for SSH config lookup."""
        return str(self.bot_app.config.defaults.workdir or "")

    async def _remote_git_send(
        self,
        context: ContextTypes.DEFAULT_TYPE,
        update: Update,
        lang: str,
        host: str,
        output: str,
        done_key: str,
        error_key: str,
        error: str = "",
    ) -> None:
        """Send a remote git result to the user in MarkdownV2."""
        if error:
            text = t(error_key, lang, host=host, error=error[:800])
            if output.strip():
                text += f"\n{t('msg.git.remote_output_prefix', lang)}\n" \
                        f"`{escape_markdown_v2_all(output.strip()[:2000])}`"
        else:
            text = t(done_key, lang, host=host)
            if output.strip():
                text += f"\n`{escape_markdown_v2_all(output.strip()[:2000])}`"
        await self.bot_app._send_message(
            context,
            text=text,
            parse_mode="MarkdownV2",
            **self._reply_kwargs(update),
        )

    async def cmd_remote_git_pull(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Handler for /remote_git_pull <host> [ff|merge|rebase]."""
        chat_id = update.effective_chat.id
        if not await self._ensure_allowed(chat_id, context, update=update):
            return
        if not await self._require_admin(chat_id, context, scope="remote_git_pull", update=update):
            return
        try:
            lang = resolve_user_lang(self.bot_app.config, chat_id=chat_id)
        except Exception:
            lang = "ru"
        if not context.args:
            await self.bot_app._send_message(
                context,
                text=t("msg.git.remote_pull_usage", lang),
                **self._reply_kwargs(update),
            )
            return
        host = context.args[0].strip()
        strategy = context.args[1].strip() if len(context.args) > 1 else "ff"
        workdir = self._remote_git_workdir()
        try:
            result = await self.bot_app.remote_git.pull(
                workdir, host, strategy=strategy
            )
        except Exception as exc:
            logging.getLogger(__name__).exception("cmd_remote_git_pull failed: %s", exc)
            await self.bot_app._send_message(
                context,
                text=escape_markdown_v2_all(str(exc)),
                **self._reply_kwargs(update),
            )
            return
        if not result.git_available:
            await self.bot_app._send_message(
                context,
                text=t("msg.git.remote_not_git", lang, host=host),
                **self._reply_kwargs(update),
            )
            return
        await self._remote_git_send(
            context, update, lang, host,
            output=result.output,
            done_key="msg.git.remote_pull_done",
            error_key="msg.git.remote_pull_error",
            error=result.error or "",
        )

    async def cmd_remote_git_push(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Handler for /remote_git_push <host>."""
        chat_id = update.effective_chat.id
        if not await self._ensure_allowed(chat_id, context, update=update):
            return
        if not await self._require_admin(chat_id, context, scope="remote_git_push", update=update):
            return
        try:
            lang = resolve_user_lang(self.bot_app.config, chat_id=chat_id)
        except Exception:
            lang = "ru"
        if not context.args:
            await self.bot_app._send_message(
                context,
                text=t("msg.git.remote_push_usage", lang),
                **self._reply_kwargs(update),
            )
            return
        host = context.args[0].strip()
        workdir = self._remote_git_workdir()
        try:
            result = await self.bot_app.remote_git.push(workdir, host)
        except Exception as exc:
            logging.getLogger(__name__).exception("cmd_remote_git_push failed: %s", exc)
            await self.bot_app._send_message(
                context,
                text=escape_markdown_v2_all(str(exc)),
                **self._reply_kwargs(update),
            )
            return
        if not result.git_available:
            await self.bot_app._send_message(
                context,
                text=t("msg.git.remote_not_git", lang, host=host),
                **self._reply_kwargs(update),
            )
            return
        await self._remote_git_send(
            context, update, lang, host,
            output=result.output,
            done_key="msg.git.remote_push_done",
            error_key="msg.git.remote_push_error",
            error=result.error or "",
        )

    async def cmd_remote_git_fetch(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Handler for /remote_git_fetch <host>."""
        chat_id = update.effective_chat.id
        if not await self._ensure_allowed(chat_id, context, update=update):
            return
        if not await self._require_admin(chat_id, context, scope="remote_git_fetch", update=update):
            return
        try:
            lang = resolve_user_lang(self.bot_app.config, chat_id=chat_id)
        except Exception:
            lang = "ru"
        if not context.args:
            await self.bot_app._send_message(
                context,
                text=t("msg.git.remote_fetch_usage", lang),
                **self._reply_kwargs(update),
            )
            return
        host = context.args[0].strip()
        workdir = self._remote_git_workdir()
        try:
            result = await self.bot_app.remote_git.fetch(workdir, host)
        except Exception as exc:
            logging.getLogger(__name__).exception("cmd_remote_git_fetch failed: %s", exc)
            await self.bot_app._send_message(
                context,
                text=escape_markdown_v2_all(str(exc)),
                **self._reply_kwargs(update),
            )
            return
        if not result.git_available:
            await self.bot_app._send_message(
                context,
                text=t("msg.git.remote_not_git", lang, host=host),
                **self._reply_kwargs(update),
            )
            return
        await self._remote_git_send(
            context, update, lang, host,
            output=result.output,
            done_key="msg.git.remote_fetch_done",
            error_key="msg.git.remote_fetch_error",
            error=result.error or "",
        )
