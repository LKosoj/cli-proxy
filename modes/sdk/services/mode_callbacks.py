from __future__ import annotations

from datetime import datetime, timezone
import logging
import sys
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional

from i18n import t
from modes.sdk.runtime.json_normalizer import loads_safe
from utils.lang import resolve_user_lang

from ..dirs_mode import decode_mode_dirs
from ..models import CallbackModel
from ..session_busy import is_session_busy
from .callback_data import MODE_ACTION_PREFIXES, parse_compact_callback_payload
from .mode_registry import ModeRegistryService


SendMessageFn = Callable[[Any, Any], Awaitable[Any]]
GetSessionFn = Callable[[int], Any]
ResolveSessionFn = Callable[[int, Optional[int]], Any]
GetDirsModeFn = Callable[[int, Optional[int]], str]
ClearDirsModeFn = Callable[[int, Optional[int]], None]
_MODE_AUDIT_LOGGER_NAME = "mode.audit.mode_callbacks"


def _get_mode_audit_logger() -> logging.Logger:
    logger = logging.getLogger(_MODE_AUDIT_LOGGER_NAME)
    has_stdout_handler = any(
        isinstance(h, logging.StreamHandler) and getattr(h, "stream", None) is sys.stdout
        for h in logger.handlers
    )
    if not has_stdout_handler:
        handler = logging.StreamHandler(stream=sys.stdout)
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s"))
        logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    return logger


@dataclass
class ModeCallbackRouterService:
    mode_registry: ModeRegistryService
    dialogs: Optional[Any] = None
    send_message: Optional[SendMessageFn] = None
    send_output: Optional[Callable[..., Awaitable[Any]]] = None
    get_session: Optional[GetSessionFn] = None
    resolve_session: Optional[ResolveSessionFn] = None
    get_dirs_mode_token: Optional[GetDirsModeFn] = None
    clear_dirs_mode_token: Optional[ClearDirsModeFn] = None

    @staticmethod
    def _extract_message_thread_id(query: Any) -> Optional[int]:
        try:
            raw_value = getattr(getattr(query, "message", None), "message_thread_id", None)
        except Exception:
            raw_value = None
        try:
            thread_id = int(raw_value) if raw_value is not None else 0
        except Exception:
            thread_id = 0
        return thread_id if thread_id > 0 else None

    @staticmethod
    def _build_reply_dest(
        bot_app: Any,
        *,
        session: Any,
        chat_id: Any,
        user_id: Optional[int],
        message_thread_id: Optional[int],
    ) -> dict:
        builder = getattr(bot_app, "build_telegram_reply_dest", None)
        if callable(builder):
            dest = builder(session, chat_id, user_id=user_id)
        else:
            dest = {"kind": "telegram", "chat_id": chat_id}
            if user_id is not None and str(chat_id).lstrip("-").isdigit():
                dest["user_id"] = int(user_id)
        if dest.get("message_thread_id") is None and message_thread_id is not None:
            dest["message_thread_id"] = int(message_thread_id)
        return dest

    @staticmethod
    def _telegram_reply_kwargs(dest: dict) -> dict:
        kwargs: dict[str, Any] = {}
        if dest.get("chat_id") is not None:
            kwargs["chat_id"] = dest["chat_id"]
        if dest.get("message_thread_id") is not None:
            kwargs["message_thread_id"] = dest["message_thread_id"]
        return kwargs

    @staticmethod
    def _build_transport_context(
        bot_app: Any,
        *,
        context: Any,
        session: Any,
        chat_id: Any,
        dest: dict,
        user_id: Optional[int],
        message_thread_id: Optional[int],
    ) -> Any:
        builder = getattr(bot_app, "build_telegram_transport_context", None)
        if callable(builder):
            return builder(
                context,
                session=session,
                chat_id=chat_id,
                dest=dest,
                user_id=user_id,
                message_thread_id=message_thread_id,
            )
        return context

    @staticmethod
    def _is_session_busy_for_mode_changes(session: Any) -> bool:
        if session is None:
            return False
        run_lock = getattr(session, "run_lock", None)
        queue_len = len(getattr(session, "queue", []) or [])
        return bool(is_session_busy(session, run_lock) or queue_len > 0)

    @staticmethod
    def _is_destructive_mode_action(action: str) -> bool:
        token = str(action or "").strip().lower().replace("-", "_")
        if not token:
            return False
        parts = {p for p in token.split("_") if p}
        if {"reset", "clean", "disconnect"} & parts:
            return True
        if token.startswith("clean"):
            return True
        return False

    def _resolve_callback_session(self, *, chat_id: int, message_thread_id: Optional[int]) -> Any:
        if hasattr(self, "resolve_session") and callable(self.resolve_session):
            return self.resolve_session(chat_id, message_thread_id)
        if hasattr(self, "get_session") and callable(self.get_session):
            return self.get_session(chat_id)
        return None

    @staticmethod
    def _is_explicit_session_access_allowed(*, bot_app: Any, chat_id: int, resolved: Any) -> bool:
        """Ownership gate for sessions resolved by a client-forgeable UID (full-UID/
        get_session fallback branches of `_resolve_explicit_session`). Chat-scoped
        short session_id lookups are already chat-scoped and do not call this."""
        is_admin_fn = getattr(bot_app, "is_admin", None)
        is_allowed_fn = getattr(bot_app, "is_session_allowed_for_chat", None)
        try:
            if callable(is_admin_fn) and bool(is_admin_fn(int(chat_id))):
                return True
            if callable(is_allowed_fn):
                return bool(is_allowed_fn(int(chat_id), resolved))
            # No access API on bot_app (test doubles): trust a plain chat_id match
            # only, never the client-supplied UID.
            return int(getattr(resolved, "chat_id", 0) or 0) == int(chat_id)
        except Exception:
            logging.getLogger(__name__).exception(
                "explicit mode callback session ownership check failed chat_id=%s", chat_id,
            )
            return False

    def _authorize_explicit_session(
        self, *, bot_app: Any, chat_id: int, token: str, resolved: Any,
    ) -> tuple[Any, bool]:
        if self._is_explicit_session_access_allowed(bot_app=bot_app, chat_id=chat_id, resolved=resolved):
            return resolved, False
        logging.getLogger(__name__).warning(
            "explicit mode callback session access denied chat_id=%s resolved_chat_id=%s session_uid=%s",
            chat_id, getattr(resolved, "chat_id", None), token,
        )
        return None, True

    def _resolve_explicit_session(
        self, *, session_uid: str, bot_app: Any, chat_id: int = 0, access_chat_id: Optional[int] = None,
    ) -> tuple[Any, bool]:
        """Resolve a session_uid taken from client-controlled callback_data.

        `chat_id` addresses sessions and replies (the message chat); rights are
        keyed by the requester's own id, which differs from it in group thread
        mode - pass it as `access_chat_id`, otherwise `chat_id` is used.

        Returns `(session, access_denied)`. `access_denied` is True only when a
        session WAS found via the global full-UID/get_session fallback but the
        requesting chat is not its owner/admin - callers must abort the callback
        in that case rather than silently falling back to the caller's own
        session (see `handle_mode_action_callback`).
        """
        owner_chat_id = int(access_chat_id) if access_chat_id is not None else int(chat_id)
        token = str(session_uid or "").strip()
        if not token:
            return None, False
        manager = getattr(bot_app, "manager", None)
        # Short session_id (e.g. "s1"): look up in chat-scoped sessions. Already
        # scoped to chat_id, so no extra ownership check is needed.
        if manager is not None and chat_id and ":" not in token:
            sessions_for_chat = getattr(manager, "sessions_for_chat", None)
            if callable(sessions_for_chat):
                try:
                    chat_sessions = sessions_for_chat(int(chat_id))
                    if token in (chat_sessions or {}):
                        return chat_sessions[token], False
                except Exception:
                    logging.getLogger(__name__).exception(
                        "short session_id resolve failed chat_id=%s session_id=%s", chat_id, token,
                    )
        # Full UID fallback (backward compat with old-format buttons). Searches the
        # global cross-chat index, so ownership must be verified before returning.
        get_by_uid = getattr(manager, "get_by_uid", None) if manager is not None else None
        if callable(get_by_uid):
            try:
                resolved = get_by_uid(token)
            except Exception:
                logging.getLogger(__name__).exception("explicit mode callback session resolve failed session_uid=%s", token)
                resolved = None
            if resolved is not None:
                return self._authorize_explicit_session(
                    bot_app=bot_app, chat_id=owner_chat_id, token=token, resolved=resolved,
                )
        if hasattr(self, "get_session") and callable(self.get_session):
            try:
                resolved = self.get_session(token)
            except Exception:
                logging.getLogger(__name__).exception(
                    "explicit mode callback get_session fallback failed session_uid=%s",
                    token,
                )
                resolved = None
            if resolved is not None:
                return self._authorize_explicit_session(
                    bot_app=bot_app, chat_id=owner_chat_id, token=token, resolved=resolved,
                )
        return None, False

    def _payload_from_raw(self, payload_raw: str) -> dict:
        if not payload_raw:
            return {}
        if any(marker in payload_raw for marker in ("=", "|", "&")):
            compact = parse_compact_callback_payload(payload_raw)
            if compact:
                # Normalise short key "val" → "value" for consumer compatibility.
                if "val" in compact and "value" not in compact:
                    compact["value"] = compact.pop("val")
                return compact
        try:
            payload = loads_safe(payload_raw, strict_first=False)
            if isinstance(payload, dict):
                return payload
            return {"value": payload}
        except Exception:
            return {"value": payload_raw}

    @staticmethod
    def _extract_session_override(payload_raw: str) -> str:
        compact = parse_compact_callback_payload(payload_raw)
        token = str(compact.get("s") or compact.get("session_uid") or "").strip()
        if token:
            return token
        try:
            payload = loads_safe(payload_raw, strict_first=False)
        except Exception:
            payload = None
        if isinstance(payload, dict):
            return str(payload.get("s") or payload.get("session_uid") or "").strip()
        return ""

    @staticmethod
    def _is_shared_run_operation(action: str) -> bool:
        return str(action or "").strip().lower() in {"doctor", "recover", "resume", "apply_recommendation"}

    @staticmethod
    def _is_shared_skill_operation(action: str) -> bool:
        return str(action or "").strip().lower() in {"promote_skills"}

    @staticmethod
    def _build_mode_launch_security(bot_app: Any) -> Any:
        security = getattr(bot_app, "security", None)
        if security is not None and hasattr(security, "authorize_mode_launch"):
            return security

        from app.security import SecurityFacade

        is_admin_check = getattr(bot_app, "is_admin", None)
        is_user_check = getattr(bot_app, "is_user", None)
        is_allowed_check = getattr(bot_app, "is_allowed", None)
        defaults = getattr(getattr(bot_app, "config", None), "defaults", None)
        default_state_path = getattr(defaults, "state_path", None)

        def _is_admin(chat_id: int) -> bool:
            if not callable(is_admin_check):
                return False
            return bool(is_admin_check(str(chat_id)))

        def _is_user(chat_id: int) -> bool:
            if callable(is_user_check):
                return bool(is_user_check(str(chat_id)))
            if callable(is_allowed_check):
                return bool(is_allowed_check(str(chat_id)))
            return _is_admin(str(chat_id))

        security = SecurityFacade.from_config(
            None,
            is_admin_fn=_is_admin,
            is_user_fn=_is_user,
            system_event_bus=getattr(bot_app, "system_event_bus", None),
            default_audit_state_path=default_state_path,
            default_rate_limit_state_path=default_state_path,
        )
        try:
            setattr(bot_app, "security", security)
        except Exception:
            logging.getLogger(__name__).exception("failed to cache mode launch security on bot_app")
        return security

    @staticmethod
    def _strip_mode_action_prefix(data: str) -> str:
        for prefix in MODE_ACTION_PREFIXES:
            if data.startswith(prefix):
                return data[len(prefix):]
        return data

    def _split_mode_action(self, data: str, active_mode: str) -> tuple[str, str, str]:
        rest = self._strip_mode_action_prefix(str(data or ""))
        first, rem = (rest.split(":", 1) + [""])[:2]
        token0 = str(first or "").strip()
        token_rem = str(rem or "")
        known = self.mode_registry.get(token0) is not None
        if known and token_rem:
            second, payload_raw = (token_rem.split(":", 1) + [""])[:2]
            return token0, str(second or "").strip(), payload_raw
        return str(active_mode or "").strip(), token0, token_rem

    async def _send_output_if_any(
        self,
        context: Any,
        chat_id: int,
        result: Any,
        session: Any,
        *,
        bot_app: Any,
        user_id: Optional[int],
        message_thread_id: Optional[int],
    ) -> None:
        if not result or not getattr(result, "output", None):
            return
        output_text = str(result.output)
        reply_dest = self._build_reply_dest(
            bot_app,
            session=session,
            chat_id=chat_id,
            user_id=user_id,
            message_thread_id=message_thread_id,
        )
        transport_context = self._build_transport_context(
            bot_app,
            context=context,
            session=session,
            chat_id=chat_id,
            dest=reply_dest,
            user_id=user_id,
            message_thread_id=message_thread_id,
        )
        if self.send_output and session is not None:
            try:
                await self.send_output(
                    session,
                    reply_dest,
                    output_text,
                    transport_context,
                    send_header=False,
                )
                return
            except Exception as e:
                logging.getLogger(__name__).exception("mode callback send_output failed, fallback to send_message: %s", e)
        if self.send_message:
            send_kwargs = {"chat_id": chat_id, "text": output_text, "md2": False}
            if reply_dest.get("message_thread_id") is not None:
                send_kwargs["message_thread_id"] = reply_dest["message_thread_id"]
            await self.send_message(transport_context, **send_kwargs)

    async def handle_mode_action_callback(
        self,
        *,
        data: str,
        chat_id: int,
        query: Any,
        context: Any,
        bot_app: Any,
        owner_chat_id: Optional[int] = None,
    ) -> bool:
        message_thread_id = self._extract_message_thread_id(query)
        # Сессии и ответы адресуются чатом сообщения, а права пользователя
        # (admlist, whitelist, user_workdirs, user_modes) ключуются его личным
        # id: в group-режиме это разные чаты, и путать их нельзя ни в одну
        # сторону - ни отказать владельцу, ни выдать права всей группе.
        access_chat_id = int(owner_chat_id) if owner_chat_id is not None else int(chat_id)
        scoped_session = self._resolve_callback_session(chat_id=chat_id, message_thread_id=message_thread_id)
        active_mode = self.mode_registry.get_active_mode_id(scoped_session) if scoped_session else ""
        target_mode, action, payload_raw = self._split_mode_action(str(data or ""), active_mode)
        explicit_session_uid = self._extract_session_override(payload_raw)
        lang = resolve_user_lang(getattr(bot_app, "config", None), chat_id=chat_id)
        session = scoped_session
        session_access_denied = False
        if explicit_session_uid:
            resolved_session, session_access_denied = self._resolve_explicit_session(
                session_uid=explicit_session_uid,
                bot_app=bot_app,
                chat_id=chat_id,
                access_chat_id=access_chat_id,
            )
            if resolved_session is not None:
                session = resolved_session
        session_id = getattr(session, "id", None) if session else None
        user_id = getattr(getattr(query, "from_user", None), "id", None)
        reply_dest = self._build_reply_dest(
            bot_app,
            session=session,
            chat_id=chat_id,
            user_id=user_id,
            message_thread_id=message_thread_id,
        )
        transport_context = self._build_transport_context(
            bot_app,
            context=context,
            session=session,
            chat_id=chat_id,
            dest=reply_dest,
            user_id=user_id,
            message_thread_id=message_thread_id,
        )
        _get_mode_audit_logger().info(
            "mode_callback_entry timestamp=%s session_id=%s mode=%s action=%s chat_id=%s",
            datetime.now(timezone.utc).isoformat(),
            str(session_id or "-"),
            str(target_mode or "-"),
            str(action or "-"),
            str(chat_id),
        )
        if not target_mode:
            return False
        if session_access_denied:
            if self.send_message:
                await self.send_message(
                    transport_context,
                    text=t("msg.error.session_unavailable", lang),
                    md2=True,
                    **self._telegram_reply_kwargs(reply_dest),
                )
            return True
        action_token = str(action or "").strip()
        busy_for_mode_changes = self._is_session_busy_for_mode_changes(session)
        if action_token in ("enable", "on", "disable", "off") and busy_for_mode_changes:
            if self.send_message:
                await self.send_message(
                    transport_context,
                    text=t("msg.mode.session_busy_switch", lang),
                    md2=True,
                    **self._telegram_reply_kwargs(reply_dest),
                )
            return True
        if self._is_destructive_mode_action(action_token) and busy_for_mode_changes:
            if self.send_message:
                await self.send_message(
                    transport_context,
                    text=t("msg.mode.session_busy_destructive", lang),
                    md2=True,
                    **self._telegram_reply_kwargs(reply_dest),
                )
            return True
        policy = getattr(bot_app, "access_policy_service", None)
        if policy is None:
            is_mode_allowed = True
        else:
            is_mode_allowed = policy.is_mode_allowed_for_chat(access_chat_id, target_mode)
        security = self._build_mode_launch_security(bot_app)
        if action_token in ("enable", "on"):
            if security is None or not hasattr(security, "authorize_mode_launch"):
                raise RuntimeError("SecurityFacade.authorize_mode_launch is not configured for mode launches")
            decision = await security.authorize_mode_launch(
                str(access_chat_id),
                mode_id=target_mode,
                is_mode_allowed=bool(is_mode_allowed),
                action=action_token,
                session_id=str(session_id or ""),
                context={
                    "callback_data": str(data or ""),
                    "user_id": getattr(getattr(query, "from_user", None), "id", None),
                },
            )
            if not bool(decision.allowed):
                if self.send_message:
                    from app.security.errors import DenyReasonCode

                    text = (
                        t("msg.mode.not_allowed_for_user", lang)
                        if str(decision.reason or "") == DenyReasonCode.MODE_NOT_ALLOWED
                        else t("msg.mode.launch_denied_policy", lang)
                    )
                    await self.send_message(
                        transport_context,
                        text=text,
                        md2=True,
                        **self._telegram_reply_kwargs(reply_dest),
                    )
                return True
        if not bool(is_mode_allowed):
            if self.send_message:
                await self.send_message(
                    transport_context,
                    text=t("msg.mode.not_allowed_for_user", lang),
                    md2=True,
                    **self._telegram_reply_kwargs(reply_dest),
                )
            return True

        mode = self.mode_registry.get(target_mode)

        if self._is_shared_run_operation(action_token):
            from app.services.run_operations_service import (
                blocked_run_operation_message,
                blocked_run_operation_signals,
            )
            from app.services.run_operations_policy import RunOperationsPolicy

            service = getattr(bot_app, "mode_run_operations", None)
            if session is None:
                if self.send_message:
                    await self.send_message(
                        transport_context,
                        text=t("msg.run.session_undefined", lang),
                        md2=True,
                        **self._telegram_reply_kwargs(reply_dest),
                    )
                return True
            try:
                is_admin = bool(policy.is_admin(access_chat_id))
            except Exception:
                logging.getLogger(__name__).exception(
                    "mode callback run operation policy admin check failed chat_id=%s operation=%s",
                    access_chat_id,
                    action_token,
                )
                is_admin = False
            decision = RunOperationsPolicy().can_run_operation(
                operation=action_token,
                user_id=access_chat_id,
                is_admin=is_admin,
                session=session,
                surface="telegram",
            )
            if not bool(getattr(decision, "allowed", False)):
                if self.send_message:
                    await self.send_message(
                        transport_context,
                        text=t("msg.run.policy_denied", lang, reason=decision.reason),
                        md2=True,
                        **self._telegram_reply_kwargs(reply_dest),
                    )
                return True
            blocked_by = blocked_run_operation_signals(session, action_token)
            if blocked_by:
                if self.send_message:
                    await self.send_message(
                        transport_context,
                        text=blocked_run_operation_message(blocked_by),
                        md2=True,
                        **self._telegram_reply_kwargs(reply_dest),
                    )
                return True
            if service is None:
                if self.send_message:
                    await self.send_message(
                        transport_context,
                        text=t("msg.run.unavailable", lang),
                        md2=True,
                        **self._telegram_reply_kwargs(reply_dest),
                    )
                return True
            method = getattr(service, f"{action_token}_run", None)
            if not callable(method):
                if self.send_message:
                    await self.send_message(
                        transport_context,
                        text=t("msg.run.not_supported", lang),
                        md2=True,
                        **self._telegram_reply_kwargs(reply_dest),
                    )
                return True
            operation_kwargs = {
                "session": session,
                "mode_id": target_mode,
                "context": transport_context,
                "dest": dict(reply_dest),
            }
            result = await method(**operation_kwargs)
            text = str(getattr(result, "message", "") or t("msg.run.done", lang))
            if self.send_message:
                await self.send_message(
                    transport_context,
                    text=text,
                    md2=True,
                    **self._telegram_reply_kwargs(reply_dest),
                )
            return True

        if self._is_shared_skill_operation(action_token):
            get_mode_service = getattr(mode, "get_service", None) if mode is not None else None
            skill_runtime = get_mode_service("skill_runtime") if callable(get_mode_service) else None
            artifact_store = get_mode_service("run_artifacts") if callable(get_mode_service) else None
            if session is None:
                if self.send_message:
                    await self.send_message(
                        transport_context,
                        text=t("msg.skill.promote_cb_session_undefined", lang),
                        md2=True,
                        **self._telegram_reply_kwargs(reply_dest),
                    )
                return True
            if skill_runtime is None or not hasattr(skill_runtime, "promote_run_skills") or artifact_store is None:
                if self.send_message:
                    await self.send_message(
                        transport_context,
                        text=t("msg.skill.promote_cb_unavailable", lang),
                        md2=True,
                        **self._telegram_reply_kwargs(reply_dest),
                    )
                return True
            result = skill_runtime.promote_run_skills(
                session=session,
                run_artifact_store=artifact_store,
                mode_id=target_mode,
                actor_chat_id=access_chat_id,
                access_policy=policy,
                context=transport_context,
                dest=dict(reply_dest),
            )
            text = str(getattr(result, "message", "") or t("msg.skill.promote_cb_done", lang))
            if self.send_message:
                await self.send_message(
                    transport_context,
                    text=text,
                    md2=True,
                    **self._telegram_reply_kwargs(reply_dest),
                )
            return True

        if self.dialogs and session_id:
            try:
                if self.dialogs.is_active(chat_id=chat_id, session_id=session_id, mode_id=target_mode):
                    result = await self.dialogs.route_callback(
                        CallbackModel(
                            action="mode_action",
                            chat_id=str(chat_id),
                            payload={"data": data},
                            user_id=getattr(getattr(query, "from_user", None), "id", None),
                            message_id=getattr(getattr(query, "message", None), "message_id", None),
                            raw={"query": query, "data": data},
                        ),
                        {
                            "bot_app": bot_app,
                            "session": session,
                            "chat_id": chat_id,
                            "context": transport_context,
                            "query": query,
                            "mode_id": target_mode,
                        },
                        session_id=session_id,
                        mode_id=target_mode,
                    )
                    await self._send_output_if_any(
                        transport_context,
                        chat_id,
                        result,
                        session,
                        bot_app=bot_app,
                        user_id=user_id,
                        message_thread_id=message_thread_id,
                    )
                    return True
            except Exception as e:
                logging.getLogger(__name__).exception("dialog callback routing failed: %s", e)

        if mode is None or not session:
            return False
        try:
            result = await mode.handle_callback(
                CallbackModel(
                    action=str(action or "").strip() or "action",
                    chat_id=str(chat_id),
                    payload=self._payload_from_raw(payload_raw),
                    user_id=getattr(getattr(query, "from_user", None), "id", None),
                    message_id=getattr(getattr(query, "message", None), "message_id", None),
                    raw={"query": query, "data": data},
                ),
                {
                    "bot_app": bot_app,
                    "session": session,
                    "chat_id": chat_id,
                    "access_chat_id": access_chat_id,
                    "context": transport_context,
                    "query": query,
                    "mode_id": target_mode,
                },
            )
            await self._send_output_if_any(
                transport_context,
                chat_id,
                result,
                session,
                bot_app=bot_app,
                user_id=user_id,
                message_thread_id=message_thread_id,
            )
            return True
        except Exception as e:
            logging.getLogger(__name__).exception("mode callback failed mode=%s err=%s", target_mode, e)
            return False

    def resolve_dirs_mode_plugin(
        self,
        chat_id: int,
        message_thread_id: Optional[int] = None,
    ) -> tuple[str, Optional[str], Optional[str], Optional[Any]]:
        if not self.get_dirs_mode_token:
            return "", None, None, None
        mode_raw = str(self.get_dirs_mode_token(int(chat_id), message_thread_id) or "").strip()
        mode_id, flow = decode_mode_dirs(mode_raw)
        if not mode_id or not flow:
            return mode_raw, None, None, None
        return mode_raw, mode_id, flow, self.mode_registry.get(mode_id)

    async def dispatch_dirs_event(
        self,
        *,
        chat_id: int,
        message_thread_id: Optional[int],
        context: Any,
        event: str,
        path: str,
        bot_app: Any,
    ) -> Optional[Any]:
        _raw, mode_id, flow, plugin = self.resolve_dirs_mode_plugin(chat_id, message_thread_id)
        if not mode_id or not flow or plugin is None or not hasattr(plugin, "handle_dirs_selection"):
            return None
        session = self._resolve_callback_session(chat_id=chat_id, message_thread_id=message_thread_id)
        result = await plugin.handle_dirs_selection(
            flow=flow,
            event=event,
            path=path,
            ctx={
                "bot_app": bot_app,
                "session": session,
                "chat_id": chat_id,
                "context": self._build_transport_context(
                    bot_app,
                    context=context,
                    session=session,
                    chat_id=chat_id,
                    dest=self._build_reply_dest(
                        bot_app,
                        session=session,
                        chat_id=chat_id,
                        user_id=None,
                        message_thread_id=message_thread_id,
                    ),
                    user_id=None,
                    message_thread_id=message_thread_id,
                ),
                "mode_id": mode_id,
            },
        )
        if result is not None and self.clear_dirs_mode_token:
            try:
                self.clear_dirs_mode_token(int(chat_id), message_thread_id)
            except Exception:
                logging.getLogger(__name__).exception(
                    "failed to clear dirs mode token chat_id=%s mode_id=%s",
                    chat_id,
                    mode_id,
                )
        return result
