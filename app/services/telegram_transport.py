import asyncio
from dataclasses import dataclass
import logging
from types import SimpleNamespace
from typing import Optional

from telegram import InlineKeyboardMarkup, Message
from telegram.error import BadRequest, NetworkError, TimedOut

from sessions.conversation_scope import ConversationScope
from tg.markdown import (
    escape_markdown_v2_all,
    split_telegram_entities,
    to_telegram_entities,
)
from tg.rich import build_input_rich_message, is_rich_markdown_eligible


_RAW_API_UNAVAILABLE = object()


@dataclass(frozen=True)
class TelegramTransportContext:
    raw_context: object
    chat_id: Optional[int] = None
    message_thread_id: Optional[int] = None
    direct_messages_topic_id: Optional[int] = None
    require_thread_id: bool = False
    session_uid: Optional[str] = None

    @property
    def bot(self):
        return getattr(self.raw_context, "bot", None)


class TelegramTransportService:
    _TELEGRAM_TEXT_LIMIT = 4090
    _MDV2_TOKENS_2CH = ("__", "||")
    _MDV2_TOKENS_1CH = ("*", "_", "~", "`")
    _RICH_SEND_KEYS = frozenset(
        (
            "business_connection_id",
            "chat_id",
            "message_thread_id",
            "direct_messages_topic_id",
            "disable_notification",
            "protect_content",
            "allow_paid_broadcast",
            "message_effect_id",
            "suggested_post_parameters",
            "reply_parameters",
            "reply_markup",
        )
    )
    _RICH_DRAFT_KEYS = frozenset(("chat_id", "message_thread_id"))
    _RICH_EDIT_KEYS = frozenset(("business_connection_id", "chat_id", "message_id", "inline_message_id", "reply_markup"))

    def __init__(self, bot_app):
        self.bot_app = bot_app

    @staticmethod
    def _unwrap_context(context):
        if isinstance(context, TelegramTransportContext):
            return context.raw_context, context
        return context, None

    def _prepare_send_kwargs(self, context, kwargs: dict, *, operation: str):
        raw_context, transport_context = self._unwrap_context(context)
        resolved = dict(kwargs or {})
        if transport_context is None:
            return raw_context, resolved

        if resolved.get("chat_id") is None and transport_context.chat_id is not None:
            resolved["chat_id"] = int(transport_context.chat_id)
        if (
            resolved.get("message_thread_id") is None
            and transport_context.message_thread_id is not None
        ):
            resolved["message_thread_id"] = int(transport_context.message_thread_id)
        if (
            resolved.get("direct_messages_topic_id") is None
            and transport_context.direct_messages_topic_id is not None
        ):
            resolved["direct_messages_topic_id"] = int(transport_context.direct_messages_topic_id)
        if transport_context.require_thread_id and resolved.get("message_thread_id") is None:
            chat_id = resolved.get("chat_id")
            if chat_id is None:
                chat_id = transport_context.chat_id
            session_uid = str(transport_context.session_uid or "").strip() or "-"
            reason = f"{operation} contract error: message_thread_id is required for Telegram outbound routing"
            self.bot_app._last_delivery_error = reason
            logging.getLogger(__name__).error(
                "telegram outbound contract error operation=%s chat_id=%s session_uid=%s",
                operation,
                chat_id,
                session_uid,
            )
            return raw_context, None
        return raw_context, resolved

    def _resolve_bot_method(self, context, kwargs: dict, *, operation: str, method_name: str):
        raw_context, transport_context = self._unwrap_context(context)
        bot = getattr(raw_context, "bot", None)
        method = getattr(bot, method_name, None)
        if callable(method):
            return method

        chat_id = (kwargs or {}).get("chat_id")
        if chat_id is None and transport_context is not None:
            chat_id = transport_context.chat_id
        session_uid = str(getattr(transport_context, "session_uid", "") or "").strip() or "-"
        context_type = type(raw_context).__name__ if raw_context is not None else "NoneType"
        reason = f"{operation} contract error: Telegram bot context is required"
        self.bot_app._last_delivery_error = reason
        logging.getLogger(__name__).warning(
            "telegram outbound contract error operation=%s chat_id=%s session_uid=%s "
            "reason=missing_bot_context context_type=%s method=%s",
            operation,
            chat_id,
            session_uid,
            context_type,
            method_name,
        )
        return None

    @classmethod
    def _resolve_scope(cls, context, kwargs: dict) -> Optional[ConversationScope]:
        _raw_context, transport_context = cls._unwrap_context(context)
        resolved_kwargs = dict(kwargs or {})
        chat_id = resolved_kwargs.get("chat_id")
        if chat_id is None and transport_context is not None and transport_context.chat_id is not None:
            chat_id = transport_context.chat_id
        if chat_id is None:
            return None
        message_thread_id = resolved_kwargs.get("message_thread_id")
        if (
            message_thread_id is None
            and transport_context is not None
            and transport_context.message_thread_id is not None
        ):
            message_thread_id = transport_context.message_thread_id
        return ConversationScope.from_parts(chat_id, message_thread_id)

    @classmethod
    def _route_log_fields(cls, context, kwargs: dict) -> dict[str, object]:
        _raw_context, transport_context = cls._unwrap_context(context)
        resolved = dict(kwargs or {})
        if transport_context is not None:
            if resolved.get("chat_id") is None and transport_context.chat_id is not None:
                resolved["chat_id"] = int(transport_context.chat_id)
            if resolved.get("message_thread_id") is None and transport_context.message_thread_id is not None:
                resolved["message_thread_id"] = int(transport_context.message_thread_id)
            if (
                resolved.get("direct_messages_topic_id") is None
                and transport_context.direct_messages_topic_id is not None
            ):
                resolved["direct_messages_topic_id"] = int(transport_context.direct_messages_topic_id)
        return {
            "chat_id": resolved.get("chat_id"),
            "message_thread_id": resolved.get("message_thread_id"),
            "direct_messages_topic_id": resolved.get("direct_messages_topic_id"),
            "session_uid": str(getattr(transport_context, "session_uid", "") or "").strip() or "-",
        }

    @staticmethod
    def _filter_payload_kwargs(kwargs: dict, allowed_keys: frozenset[str]) -> dict:
        return {
            key: value
            for key, value in dict(kwargs or {}).items()
            if key in allowed_keys and value is not None
        }

    async def _call_raw_bot_api(self, context, *, endpoint: str, data: dict, operation: str):
        raw_context, transport_context = self._unwrap_context(context)
        bot = getattr(raw_context, "bot", None)
        if bot is None:
            route = self._route_log_fields(context, data)
            logging.getLogger(__name__).warning(
                "telegram raw api unavailable operation=%s endpoint=%s chat_id=%s "
                "message_thread_id=%s direct_messages_topic_id=%s session_uid=%s reason=missing_bot_context",
                operation,
                endpoint,
                route.get("chat_id"),
                route.get("message_thread_id"),
                route.get("direct_messages_topic_id"),
                route.get("session_uid"),
            )
            return _RAW_API_UNAVAILABLE

        post = getattr(bot, "_post", None)
        if callable(post):
            return await post(endpoint, data=data)

        do_api_request = getattr(bot, "do_api_request", None)
        if callable(do_api_request):
            return await do_api_request(endpoint, api_kwargs=data)

        route = self._route_log_fields(context, data)
        session_uid = str(getattr(transport_context, "session_uid", "") or route.get("session_uid") or "-")
        logging.getLogger(__name__).debug(
            "telegram raw api unavailable operation=%s endpoint=%s chat_id=%s "
            "message_thread_id=%s direct_messages_topic_id=%s session_uid=%s reason=missing_raw_api_method",
            operation,
            endpoint,
            route.get("chat_id"),
            route.get("message_thread_id"),
            route.get("direct_messages_topic_id"),
            session_uid,
        )
        return _RAW_API_UNAVAILABLE

    @staticmethod
    def _message_from_raw_result(bot, result):
        if hasattr(result, "message_id"):
            return result
        if isinstance(result, dict):
            try:
                return Message.de_json(result, bot)
            except Exception:
                logging.getLogger(__name__).debug(
                    "telegram raw message decode failed",
                    exc_info=True,
                )
                return SimpleNamespace(message_id=result.get("message_id"), raw=result)
        return result

    @staticmethod
    def _is_missing_thread_error(exc: Exception) -> bool:
        message = str(exc or "").strip().lower()
        return any(
            marker in message
            for marker in (
                "message thread not found",
                "thread not found",
                "topic was deleted",
                "topic deleted",
                "topic not found",
            )
        )

    def _mark_missing_thread(self, context, kwargs: dict, exc: Exception) -> None:
        if not self._is_missing_thread_error(exc):
            return
        route = self._route_log_fields(context, kwargs)
        marker = getattr(self.bot_app, "mark_telegram_thread_delivery_failed", None)
        if callable(marker):
            marker(
                chat_id=route.get("chat_id"),
                message_thread_id=route.get("message_thread_id"),
                reason=str(exc),
            )

    async def _send_message_without_thread_fallback(
        self,
        context,
        *,
        current_kwargs: dict,
        raw_text: str,
        md2: bool,
        route_log: dict,
        reason: Exception,
    ) -> object | None:
        fallback_kwargs = dict(current_kwargs or {})
        stale_message_thread_id = fallback_kwargs.pop("message_thread_id", None)
        stale_direct_messages_topic_id = fallback_kwargs.pop("direct_messages_topic_id", None)
        if stale_message_thread_id is None and stale_direct_messages_topic_id is None:
            return None

        session_uid = str(route_log.get("session_uid") or "-")
        logging.getLogger(__name__).warning(
            "telegram send_message retrying without thread routing chat_id=%s "
            "stale_message_thread_id=%s stale_direct_messages_topic_id=%s session_uid=%s reason=%s",
            fallback_kwargs.get("chat_id"),
            stale_message_thread_id,
            stale_direct_messages_topic_id,
            session_uid,
            reason,
        )
        send_message = self._resolve_bot_method(
            context,
            fallback_kwargs,
            operation="send_message_threadless_fallback",
            method_name="send_message",
        )
        if send_message is None:
            return None

        fallback_route_log = {
            "chat_id": fallback_kwargs.get("chat_id"),
            "message_thread_id": None,
            "direct_messages_topic_id": None,
            "session_uid": session_uid,
        }
        try:
            if md2 and "text" in fallback_kwargs:
                message, last_exc, used_variant = await self._send_formatted_variants(
                    context,
                    send_message,
                    str(raw_text or ""),
                    fallback_kwargs,
                    fallback_route_log,
                )
                if message is None:
                    logging.getLogger(__name__).warning(
                        "telegram send_message threadless fallback failed chat_id=%s "
                        "session_uid=%s: %s",
                        fallback_kwargs.get("chat_id"),
                        session_uid,
                        last_exc,
                    )
                    return None
                if used_variant and used_variant not in {"rich_raw", "entities_preserve"}:
                    logging.getLogger(__name__).info(
                        "telegram send_message threadless fallback used variant=%s",
                        used_variant,
                    )
            elif "text" in fallback_kwargs:
                message = await self._send_chunked(
                    send_message,
                    fallback_kwargs,
                    str(raw_text or ""),
                )
            else:
                message = await send_message(**fallback_kwargs)
        except BadRequest as fallback_exc:
            logging.getLogger(__name__).warning(
                "telegram send_message threadless fallback rejected chat_id=%s "
                "session_uid=%s: %s",
                fallback_kwargs.get("chat_id"),
                session_uid,
                fallback_exc,
            )
            return None

        logging.getLogger(__name__).warning(
            "telegram send_message delivered without thread routing chat_id=%s "
            "stale_message_thread_id=%s stale_direct_messages_topic_id=%s session_uid=%s",
            fallback_kwargs.get("chat_id"),
            stale_message_thread_id,
            stale_direct_messages_topic_id,
            session_uid,
        )
        return message

    async def _enqueue_by_scope(self, context, kwargs: dict, *, operation: str, factory):
        scope = self._resolve_scope(context, kwargs)
        queue_service = getattr(self.bot_app, "notification_queue_service", None)
        if scope is None or queue_service is None:
            return await factory()
        if queue_service.is_executing_scope(scope):
            return await factory()
        return await queue_service.enqueue(
            scope,
            operation=operation,
            factory=factory,
        )

    @classmethod
    def _split_text(cls, text: str, limit: Optional[int] = None) -> list[str]:
        s = str(text or "")
        max_len = int(limit or cls._TELEGRAM_TEXT_LIMIT)
        if len(s) <= max_len:
            return [s]

        chunks: list[str] = []
        start = 0
        n = len(s)
        while start < n:
            end = min(start + max_len, n)
            if end < n:
                nl = s.rfind("\n", start, end)
                if nl > start:
                    end = nl + 1
                else:
                    sp = s.rfind(" ", start, end)
                    if sp > start:
                        end = sp + 1
            chunks.append(s[start:end])
            start = end
        return chunks

    @classmethod
    def _scan_markdown_v2_active(cls, text: str) -> list[tuple[str, int]]:
        active: list[tuple[str, int]] = []
        i = 0
        n = len(text)
        while i < n:
            token = None
            step = 1
            if text[i] == "\\" and i + 1 < n:
                i += 2
                continue
            for cand in cls._MDV2_TOKENS_2CH:
                if text.startswith(cand, i):
                    token = cand
                    step = 2
                    break
            if token is None and text[i] in cls._MDV2_TOKENS_1CH:
                token = text[i]
                step = 1
            if token is not None:
                if active and active[-1][0] == token:
                    active.pop()
                else:
                    active.append((token, i))
            i += step
        return active

    @classmethod
    def _find_split_point(cls, text: str, limit: int) -> int:
        end = min(len(text), limit)
        nl = text.rfind("\n", 0, end)
        if nl > 0:
            return nl + 1
        sp = text.rfind(" ", 0, end)
        if sp > 0:
            return sp + 1
        return end

    @classmethod
    def _split_markdown_v2_text(cls, text: str, limit: Optional[int] = None) -> list[str]:
        s = str(text or "")
        max_len = int(limit or cls._TELEGRAM_TEXT_LIMIT)
        if len(s) <= max_len:
            return [s]

        chunks: list[str] = []
        current = ""
        i = 0
        n = len(s)

        while i < n:
            if s[i] == "\\" and i + 1 < n:
                unit = s[i:i + 2]
                step = 2
            else:
                unit = s[i]
                step = 1
                for cand in cls._MDV2_TOKENS_2CH:
                    if s.startswith(cand, i):
                        unit = cand
                        step = 2
                        break

            if len(current) + len(unit) > max_len:
                split_at = cls._find_split_point(current, max_len)
                active = cls._scan_markdown_v2_active(current)
                if active:
                    first_open_pos = active[0][1]
                    if first_open_pos < split_at:
                        split_at = first_open_pos
                if split_at <= 0:
                    raise ValueError("Cannot split MarkdownV2 text without breaking formatting block.")
                chunks.append(current[:split_at])
                current = current[split_at:]
                continue

            current += unit
            i += step

        if current:
            chunks.append(current)
        return chunks

    async def _send_chunked(self, send_func, kwargs: dict, text: str, *, markdown_v2: bool = False) -> object | None:
        chunks = (
            self._split_markdown_v2_text(text)
            if markdown_v2
            else self._split_text(text)
        )
        last_message = None
        for idx, chunk in enumerate(chunks):
            chunk_kwargs = dict(kwargs)
            chunk_kwargs["text"] = chunk
            if idx > 0 and "reply_markup" in chunk_kwargs:
                chunk_kwargs.pop("reply_markup", None)
            last_message = await send_func(**chunk_kwargs)
        return last_message

    async def _send_chunked_entities(self, send_func, kwargs: dict, text: str, entities) -> object | None:
        entity_list = list(entities or [])
        if not entity_list:
            return await self._send_chunked(send_func, kwargs, text)

        chunks = split_telegram_entities(
            str(text or ""),
            entity_list,
            max_utf16_len=self._TELEGRAM_TEXT_LIMIT,
        )
        last_message = None
        for idx, (chunk_text, chunk_entities) in enumerate(chunks):
            chunk_kwargs = dict(kwargs)
            chunk_kwargs["text"] = str(chunk_text or "")
            chunk_kwargs["entities"] = list(chunk_entities or [])
            chunk_kwargs.pop("parse_mode", None)
            if idx > 0 and "reply_markup" in chunk_kwargs:
                chunk_kwargs.pop("reply_markup", None)
            last_message = await send_func(**chunk_kwargs)
        return last_message

    async def _send_raw_rich_chunks(self, context, raw: str, base_kwargs: dict, route_log: dict):
        if not is_rich_markdown_eligible(str(raw or "")):
            return None, None, ""
        payload = self._filter_payload_kwargs(base_kwargs, self._RICH_SEND_KEYS)
        payload["rich_message"] = build_input_rich_message(str(raw or ""))
        try:
            result = await self._call_raw_bot_api(
                context,
                endpoint="sendRichMessage",
                data=payload,
                operation="send_rich_message",
            )
        except BadRequest as exc:
            logging.getLogger(__name__).warning(
                "telegram send_message variant=rich_raw rejected chat_id=%s "
                "message_thread_id=%s direct_messages_topic_id=%s session_uid=%s: %s",
                route_log.get("chat_id"),
                route_log.get("message_thread_id"),
                route_log.get("direct_messages_topic_id"),
                route_log.get("session_uid"),
                exc,
            )
            return None, exc, "rich_raw"
        if result is _RAW_API_UNAVAILABLE:
            return None, None, ""
        raw_context, _transport_context = self._unwrap_context(context)
        return self._message_from_raw_result(getattr(raw_context, "bot", None), result), None, "rich_raw"

    def _record_message(self, chat_id, message) -> None:
        if not chat_id or not message:
            return
        for runtime in (getattr(self.bot_app, "iter_mode_runtimes", lambda: [])() or []):
            if runtime and hasattr(runtime, "record_message"):
                runtime.record_message(chat_id, message.message_id)

    async def _send_formatted_variants(self, context, send_message, raw: str, base_kwargs: dict, route_log: dict):
        message, last_exc, used_variant = await self._send_raw_rich_chunks(
            context,
            raw,
            base_kwargs,
            route_log,
        )
        if message is not None:
            return message, last_exc, used_variant

        rich_exc = last_exc
        message, last_exc, used_variant = await self._send_markdown_variants(
            send_message,
            raw,
            base_kwargs,
            route_log,
        )
        return message, last_exc or rich_exc, used_variant

    async def _send_markdown_variants(self, send_message, raw: str, base_kwargs: dict, route_log: dict):
        send_variants = [
            ("entities_preserve", dict(base_kwargs), "entities"),
            ("md2_safe", dict({**base_kwargs, "parse_mode": "MarkdownV2"}), "md2_safe"),
            ("plain", dict(base_kwargs), "plain"),
        ]

        message = None
        last_exc = None
        used_variant = ""
        for _variant_name, send_kwargs, transform_name in send_variants:
            try:
                if transform_name == "entities":
                    rendered_text, rendered_entities = to_telegram_entities(raw)
                    message = await self._send_chunked_entities(
                        send_message,
                        send_kwargs,
                        rendered_text,
                        rendered_entities,
                    )
                elif transform_name == "md2_safe":
                    message = await self._send_chunked(
                        send_message,
                        send_kwargs,
                        escape_markdown_v2_all(raw),
                        markdown_v2=True,
                    )
                else:
                    message = await self._send_chunked(
                        send_message,
                        send_kwargs,
                        raw,
                    )
                used_variant = _variant_name
                break
            except ValueError:
                if _variant_name == "md2_safe":
                    logging.getLogger(__name__).warning(
                        "telegram send_message variant=%s split failed; fallback to plain text",
                        _variant_name,
                    )
                    continue
                raise
            except BadRequest as exc:
                last_exc = exc
                if "Message text is empty" in str(exc):
                    self.bot_app._last_delivery_error = "send_message error: Message text is empty"
                    logging.warning("Ошибка отправки сообщения в Telegram: %s", exc)
                    return None, exc, ""
                logging.getLogger(__name__).warning(
                    "telegram send_message variant=%s rejected chat_id=%s "
                    "message_thread_id=%s direct_messages_topic_id=%s session_uid=%s: %s",
                    _variant_name,
                    route_log.get("chat_id"),
                    route_log.get("message_thread_id"),
                    route_log.get("direct_messages_topic_id"),
                    route_log.get("session_uid"),
                    exc,
                )
                continue
        return message, last_exc, used_variant

    async def _send_message_now(self, context, **kwargs):
        for attempt in range(5):
            current_kwargs = dict(kwargs or {})
            md2 = True
            raw_text = None
            try:
                # Never mutate caller kwargs across retries.
                _raw_context, current_kwargs = self._prepare_send_kwargs(
                    context,
                    kwargs,
                    operation="send_message",
                )
                if current_kwargs is None:
                    return
                md2 = bool(current_kwargs.pop("md2", True))
                raw_text = current_kwargs.get("text")

                if "text" in current_kwargs and (raw_text is None or str(raw_text).strip() == ""):
                    self.bot_app._last_delivery_error = "send_message error: Message text is empty"
                    logging.warning("Ошибка отправки сообщения в Telegram: текст сообщения пуст")
                    return

                send_message = self._resolve_bot_method(
                    context,
                    current_kwargs,
                    operation="send_message",
                    method_name="send_message",
                )
                if send_message is None:
                    return

                if not (md2 and "text" in current_kwargs):
                    if "text" in current_kwargs:
                        message = await self._send_chunked(send_message, current_kwargs, str(raw_text or ""))
                    else:
                        message = await send_message(**current_kwargs)
                    self.bot_app._last_delivery_error = None
                    self._record_message(current_kwargs.get("chat_id"), message)
                    return message

                raw = str(raw_text or "")
                base_kwargs = dict(current_kwargs)
                route_log = self._route_log_fields(context, base_kwargs)

                message, last_exc, used_variant = await self._send_formatted_variants(
                    context,
                    send_message,
                    raw,
                    base_kwargs,
                    route_log,
                )

                if message is None:
                    if last_exc is not None and "Message text is empty" in str(last_exc):
                        return
                    self.bot_app._last_delivery_error = f"send_message bad request: {last_exc}"
                    if last_exc is not None:
                        self._mark_missing_thread(context, base_kwargs, last_exc)
                        if self._is_missing_thread_error(last_exc):
                            fallback_message = await self._send_message_without_thread_fallback(
                                context,
                                current_kwargs=base_kwargs,
                                raw_text=raw,
                                md2=md2,
                                route_log=route_log,
                                reason=last_exc,
                            )
                            if fallback_message is not None:
                                self.bot_app._last_delivery_error = None
                                self._record_message(base_kwargs.get("chat_id"), fallback_message)
                                return fallback_message
                    logging.warning(
                        "Ошибка Telegram при отправке сообщения chat_id=%s message_thread_id=%s "
                        "direct_messages_topic_id=%s session_uid=%s: %s",
                        route_log.get("chat_id"),
                        route_log.get("message_thread_id"),
                        route_log.get("direct_messages_topic_id"),
                        route_log.get("session_uid"),
                        last_exc,
                    )
                    return
                if used_variant and used_variant not in {"rich_raw", "entities_preserve"}:
                    logging.getLogger(__name__).info(
                        "telegram send_message used fallback variant=%s",
                        used_variant,
                    )

                self.bot_app._last_delivery_error = None
                self._record_message(base_kwargs.get("chat_id"), message)
                return message
            except BadRequest as exc:
                if "Message text is empty" in str(exc):
                    self.bot_app._last_delivery_error = "send_message error: Message text is empty"
                    logging.warning("Ошибка отправки сообщения в Telegram: %s", exc)
                    return
                self._mark_missing_thread(context, current_kwargs, exc)
                route_log = self._route_log_fields(context, current_kwargs)
                if self._is_missing_thread_error(exc):
                    fallback_message = await self._send_message_without_thread_fallback(
                        context,
                        current_kwargs=current_kwargs,
                        raw_text=str(raw_text or ""),
                        md2=md2,
                        route_log=route_log,
                        reason=exc,
                    )
                    if fallback_message is not None:
                        self.bot_app._last_delivery_error = None
                        self._record_message(current_kwargs.get("chat_id"), fallback_message)
                        return fallback_message
                self.bot_app._last_delivery_error = f"send_message bad request: {exc}"
                logging.warning(
                    "Ошибка Telegram при отправке сообщения chat_id=%s message_thread_id=%s "
                    "direct_messages_topic_id=%s session_uid=%s: %s",
                    route_log.get("chat_id"),
                    route_log.get("message_thread_id"),
                    route_log.get("direct_messages_topic_id"),
                    route_log.get("session_uid"),
                    exc,
                )
                return
            except (NetworkError, TimedOut) as exc:
                if attempt == 4:
                    self.bot_app._last_delivery_error = f"send_message network error after retries: {exc}"
                    logging.warning("Ошибка сети при отправке сообщения в Telegram: %s", exc)
                    return
                await asyncio.sleep(2 * (2 ** attempt))
            except Exception as exc:
                self.bot_app._last_delivery_error = f"send_message unexpected error: {exc}"
                logging.getLogger(__name__).exception("Не удалось отправить сообщение в Telegram.")
                return

    async def send_message(self, context, **kwargs):
        return await self._enqueue_by_scope(
            context,
            kwargs,
            operation="send_message",
            factory=lambda: self._send_message_now(context, **kwargs),
        )

    async def send_rich_message_draft(self, context, *, draft_id: int, rich_message: dict, **kwargs) -> bool:
        current_kwargs = dict(kwargs or {})
        try:
            _raw_context, current_kwargs = self._prepare_send_kwargs(
                context,
                kwargs,
                operation="send_rich_message_draft",
            )
            if current_kwargs is None:
                return False
            payload = self._filter_payload_kwargs(current_kwargs, self._RICH_DRAFT_KEYS)
            if payload.get("chat_id") is None:
                return False
            payload["draft_id"] = int(draft_id)
            payload["rich_message"] = dict(rich_message or {})
            result = await self._call_raw_bot_api(
                context,
                endpoint="sendRichMessageDraft",
                data=payload,
                operation="send_rich_message_draft",
            )
            return result is not _RAW_API_UNAVAILABLE
        except (BadRequest, NetworkError, TimedOut) as exc:
            route_log = self._route_log_fields(context, current_kwargs)
            logging.getLogger(__name__).warning(
                "telegram send_rich_message_draft failed chat_id=%s message_thread_id=%s "
                "session_uid=%s: %s",
                route_log.get("chat_id"),
                route_log.get("message_thread_id"),
                route_log.get("session_uid"),
                exc,
            )
            return False
        except Exception:
            logging.getLogger(__name__).exception("Не удалось отправить rich draft в Telegram.")
            return False

    async def _send_document_now(self, context, **kwargs) -> bool:
        for attempt in range(5):
            current_kwargs = dict(kwargs or {})
            try:
                _raw_context, current_kwargs = self._prepare_send_kwargs(
                    context,
                    kwargs,
                    operation="send_document",
                )
                if current_kwargs is None:
                    return False
                send_document = self._resolve_bot_method(
                    context,
                    current_kwargs,
                    operation="send_document",
                    method_name="send_document",
                )
                if send_document is None:
                    return False
                await send_document(**current_kwargs)
                self.bot_app._last_delivery_error = None
                return True
            except BadRequest as exc:
                self._mark_missing_thread(context, current_kwargs, exc)
                self.bot_app._last_delivery_error = f"send_document bad request: {exc}"
                route_log = self._route_log_fields(context, current_kwargs)
                logging.warning(
                    "Ошибка Telegram при отправке файла chat_id=%s message_thread_id=%s "
                    "direct_messages_topic_id=%s session_uid=%s: %s",
                    route_log.get("chat_id"),
                    route_log.get("message_thread_id"),
                    route_log.get("direct_messages_topic_id"),
                    route_log.get("session_uid"),
                    exc,
                )
                return False
            except (NetworkError, TimedOut) as exc:
                if attempt == 4:
                    self.bot_app._last_delivery_error = f"send_document network error after retries: {exc}"
                    logging.warning("Ошибка сети при отправке файла в Telegram: %s", exc)
                    return False
                await asyncio.sleep(2 * (2 ** attempt))
            except Exception as exc:
                self.bot_app._last_delivery_error = f"send_document error: {exc}"
                logging.exception("Не удалось отправить файл в Telegram.")
                return False
        self.bot_app._last_delivery_error = "send_document failed: exhausted retries"
        return False

    async def send_document(self, context, **kwargs) -> bool:
        return await self._enqueue_by_scope(
            context,
            kwargs,
            operation="send_document",
            factory=lambda: self._send_document_now(context, **kwargs),
        )

    async def delete_message(self, context, chat_id: int, message_id: int) -> bool:
        raw_context, _transport_context = self._unwrap_context(context)
        try:
            await raw_context.bot.delete_message(chat_id=chat_id, message_id=message_id)
            return True
        except Exception:
            return False

    async def edit_message_reply_markup(
        self,
        context,
        chat_id: int,
        message_id: int,
        *,
        reply_markup: Optional[InlineKeyboardMarkup] = None,
    ) -> bool:
        raw_context, _transport_context = self._unwrap_context(context)
        try:
            await raw_context.bot.edit_message_reply_markup(
                chat_id=chat_id,
                message_id=message_id,
                reply_markup=reply_markup,
            )
            return True
        except Exception:
            return False

    async def edit_message(
        self,
        context,
        chat_id: int,
        message_id: int,
        text: str,
        *,
        md2: bool = True,
        reply_markup: Optional[InlineKeyboardMarkup] = None,
    ) -> bool:
        raw_context, _transport_context = self._unwrap_context(context)
        try:
            raw = str(text or "")
            if not md2:
                await raw_context.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=message_id,
                    text=raw,
                    reply_markup=reply_markup,
                )
                return True

            if len(raw) <= self._TELEGRAM_TEXT_LIMIT:
                payload = self._filter_payload_kwargs(
                    {
                        "chat_id": chat_id,
                        "message_id": message_id,
                        "reply_markup": reply_markup,
                    },
                    self._RICH_EDIT_KEYS,
                )
                payload["rich_message"] = build_input_rich_message(raw)
                try:
                    result = await self._call_raw_bot_api(
                        context,
                        endpoint="editMessageText",
                        data=payload,
                        operation="edit_message_rich",
                    )
                    if result is not _RAW_API_UNAVAILABLE:
                        return True
                except BadRequest as exc:
                    if "Message text is empty" in str(exc):
                        return False
                    logging.getLogger(__name__).warning(
                        "telegram edit_message variant=rich_raw rejected: %s",
                        exc,
                    )

            variants = [
                ("entities_preserve", None),
                ("md2_safe", dict(text=escape_markdown_v2_all(raw), parse_mode="MarkdownV2")),
                ("plain", dict(text=raw)),
            ]
            last_exc = None
            used_variant = ""
            for _variant_name, extra in variants:
                try:
                    payload = dict(extra or {})
                    if _variant_name == "entities_preserve":
                        rendered_text, rendered_entities = to_telegram_entities(raw)
                        payload = {"text": rendered_text, "entities": rendered_entities}
                    await raw_context.bot.edit_message_text(
                        chat_id=chat_id,
                        message_id=message_id,
                        reply_markup=reply_markup,
                        **payload,
                    )
                    used_variant = _variant_name
                    if used_variant != "entities_preserve":
                        logging.getLogger(__name__).info(
                            "telegram edit_message used fallback variant=%s",
                            used_variant,
                        )
                    return True
                except BadRequest as exc:
                    last_exc = exc
                    if "Message text is empty" in str(exc):
                        return False
                    logging.getLogger(__name__).warning(
                        "telegram edit_message variant=%s rejected: %s",
                        _variant_name,
                        exc,
                    )
                    continue
            if last_exc is not None:
                if used_variant and used_variant != "entities_preserve":
                    logging.getLogger(__name__).info(
                        "telegram edit_message used fallback variant=%s",
                        used_variant,
                    )
                return False
            return True
        except BadRequest:
            return False
        except Exception:
            return False
