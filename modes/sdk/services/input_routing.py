from __future__ import annotations

import contextlib
import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional

from app.services.session_tick_history_store import clear_session_ticks
from app.services.tool_availability import is_tool_available

from ..models import MessageModel
from .messaging import MessagingService
from .mode_registry import ModeRegistryService


CliFallbackFn = Callable[[Any, str, int, Any], Awaitable[None]]
logger = logging.getLogger(__name__)


def _session_active_cli_name(session: Any) -> str:
    return str(
        getattr(getattr(session, "cli", None), "active_cli", "")
        or getattr(session, "active_cli", "")
        or getattr(getattr(session, "tool", None), "name", "")
        or ""
    ).strip()


@dataclass
class ModeInputRoutingService:
    mode_registry: ModeRegistryService
    dialogs: Optional[Any] = None
    send_message: Optional[Callable[..., Awaitable[Any]]] = None
    send_output: Optional[Callable[..., Awaitable[Any]]] = None
    lint_evolution_hook: Optional[Callable[[Any], None]] = None

    @staticmethod
    def _reply_kwargs(dest: Optional[dict], *, chat_id: Any) -> dict[str, Any]:
        kwargs: dict[str, Any] = {"chat_id": chat_id}
        payload = dict(dest or {})
        if payload.get("message_thread_id") is not None:
            kwargs["message_thread_id"] = payload["message_thread_id"]
        return kwargs

    @staticmethod
    def _extract_image_paths(dest: Optional[dict]) -> list[str]:
        payload = dict(dest or {})
        raw_many = payload.get("image_paths")
        if isinstance(raw_many, list):
            return [str(p).strip() for p in raw_many if str(p).strip()]
        raw_one = str(payload.get("image_path") or "").strip()
        return [raw_one] if raw_one else []

    @staticmethod
    def _dest_without_images(dest: Optional[dict]) -> dict:
        payload = dict(dest or {})
        payload.pop("image_path", None)
        payload.pop("image_paths", None)
        payload.pop("cleanup_image", None)
        payload.pop("cleanup_images", None)
        return payload

    @staticmethod
    def _tool_supports_images(tool: Any) -> bool:
        name = str(getattr(tool, "name", "") or "").strip().lower()
        return bool(getattr(tool, "image_cmd", None)) or name in {"claude", "gemini", "qwen"}

    @staticmethod
    def _clear_mode_image_activity(session: Any) -> None:
        clear_session_ticks(session)
        setattr(session, "last_tick_ts", None)
        setattr(session, "last_tick_value", None)
        setattr(session, "tick_seen", 0)

    @classmethod
    def _image_analysis_candidates(cls, *, session: Any, bot_app: Any) -> list[str]:
        config = getattr(session, "config", None) or getattr(bot_app, "config", None)
        if config is None:
            return []
        configured = dict(getattr(config, "tools", None) or {})
        defaults = getattr(config, "defaults", None)
        raw_routing = getattr(defaults, "cli_routing", None)

        def _normalize_names(value: Any) -> list[str]:
            if isinstance(value, str):
                token = str(value or "").strip()
                return [token] if token else []
            if isinstance(value, list):
                return [str(item).strip() for item in value if str(item).strip()]
            return []

        routing_order: list[str] = []
        if isinstance(raw_routing, dict):
            routing_order.extend(_normalize_names(raw_routing.get("analytics")))
            routing_order.extend(_normalize_names(raw_routing.get("default")))

        preferred = _session_active_cli_name(session)
        fallback_order = ["gemini", "claude", "qwen", "codex"]
        seen: set[str] = set()
        out: list[str] = []
        for name in [preferred, *routing_order, *fallback_order, *configured.keys()]:
            cli_name = str(name or "").strip()
            if not cli_name or cli_name in seen:
                continue
            seen.add(cli_name)
            tool = configured.get(cli_name)
            if tool is None or not cls._tool_supports_images(tool):
                continue
            if not is_tool_available(config, cli_name):
                continue
            out.append(cli_name)
        return out

    @staticmethod
    def _build_image_analysis_prompt(text: str) -> str:
        user_text = str(text or "").strip()
        header = (
            "Проанализируй приложенные изображения для последующей передачи в активный режим. "
            "Верни короткую фактическую сводку: что изображено, какой текст виден, какие есть "
            "ключевые объекты, структура, ошибки, диаграммы и детали, полезные для дальнейшей обработки."
        )
        if not user_text:
            return header
        return f"{header}\n\nПодпись или запрос пользователя:\n{user_text}"

    @staticmethod
    def _compose_mode_image_text(*, text: str, analysis: str, cli_name: str, image_count: int) -> str:
        parts = [f"Пользователь приложил изображения ({int(image_count)} шт.)."]
        user_text = str(text or "").strip()
        if user_text:
            parts.append(f"Подпись пользователя:\n{user_text}")
        parts.append(f"Анализ изображений через CLI {cli_name}:\n{str(analysis or '').strip()}")
        return "\n\n".join(part for part in parts if part).strip()

    @staticmethod
    @contextlib.contextmanager
    def _temporary_session_cli(session: Any, cli_name: str):
        previous_cli = _session_active_cli_name(session) or str(getattr(getattr(session, "tool", None), "name", "") or "").strip()
        if not previous_cli:
            raise RuntimeError("active CLI is not set for image analysis")
        if previous_cli == cli_name:
            yield
            return
        setter = getattr(session, "set_active_cli", None)
        if not callable(setter):
            raise RuntimeError("session CLI switching is not available for image analysis")
        setter(cli_name)
        try:
            yield
        finally:
            try:
                setter(previous_cli)
            except Exception:
                logger.exception("failed to restore previous session CLI after image analysis")

    @classmethod
    def _is_busy_for_mode_images(cls, *, session: Any, bot_app: Any) -> bool:
        dispatcher = getattr(bot_app, "input_dispatch_service", None)
        checker = getattr(dispatcher, "_is_session_busy", None)
        if callable(checker):
            try:
                return bool(checker(session, bot_app))
            except Exception:
                logger.exception("mode image busy-check failed")
        run_lock = getattr(session, "run_lock", None)
        return bool(getattr(session, "busy", False) or bool(run_lock and run_lock.locked()) or getattr(session, "queue", None))

    async def _queue_busy_mode_image_input(
        self,
        *,
        bot_app: Any,
        session: Any,
        text: str,
        chat_id: Any,
        context: Any,
        dest: dict,
    ) -> bool:
        if not self._is_busy_for_mode_images(session=session, bot_app=bot_app):
            return False
        dispatcher = getattr(bot_app, "input_dispatch_service", None)
        if dispatcher is None or not hasattr(dispatcher, "handle_cli_input"):
            return False
        await dispatcher.handle_cli_input(
            session,
            text,
            chat_id,
            context,
            dest=dict(dest or {}),
            enforce_direct_cli_policy=False,
        )
        return True

    async def _run_mode_image_analysis(
        self,
        *,
        bot_app: Any,
        session: Any,
        active_mode: str,
        text: str,
        image_paths: list[str],
    ) -> tuple[str, str]:
        candidates = self._image_analysis_candidates(session=session, bot_app=bot_app)
        if not candidates:
            raise RuntimeError("Нет доступного CLI с поддержкой изображений для анализа входных картинок.")

        prompt = self._build_image_analysis_prompt(text)
        run_lock = getattr(session, "run_lock", None)
        had_busy_attr = hasattr(session, "busy")
        previous_busy = bool(getattr(session, "busy", False))
        tried: list[tuple[str, str]] = []

        async def _execute(cli_name: str) -> str:
            with self._temporary_session_cli(session, cli_name):
                return await session.run_prompt(prompt, image_paths=list(image_paths))

        for cli_name in candidates:
            try:
                if run_lock is not None:
                    async with run_lock:
                        setattr(session, "busy", True)
                        output = await _execute(cli_name)
                else:
                    setattr(session, "busy", True)
                    output = await _execute(cli_name)
                analysis = str(output or "").strip()
                if not analysis:
                    raise RuntimeError("CLI вернул пустой результат анализа изображений.")
                return cli_name, analysis
            except Exception as exc:
                tried.append((cli_name, str(exc or "")))
                logger.exception(
                    "mode image analysis failed session_id=%s mode=%s cli=%s",
                    str(getattr(session, "id", "") or ""),
                    str(active_mode or ""),
                    cli_name,
                )
                interrupter = getattr(session, "interrupt", None)
                if callable(interrupter):
                    try:
                        interrupter()
                    except Exception:
                        logger.exception("mode image analysis interrupt after failure failed cli=%s", cli_name)
            finally:
                if had_busy_attr:
                    setattr(session, "busy", previous_busy)
                else:
                    try:
                        delattr(session, "busy")
                    except Exception:
                        logger.exception("mode image analysis busy cleanup failed")
                self._clear_mode_image_activity(session)
        details = "; ".join(f"{cli}: {err}" for cli, err in tried if err) or "нет успешных попыток"
        raise RuntimeError(f"Не удалось проанализировать изображения через доступные CLI: {details}")

    async def _send_image_analysis_error(
        self,
        *,
        transport_context: Any,
        chat_id: Any,
        dest: dict,
        text: str,
    ) -> None:
        if not self.send_message:
            raise RuntimeError(text)
        send_kwargs = self._reply_kwargs(dest, chat_id=chat_id)
        target_chat_id = send_kwargs.pop("chat_id", chat_id)
        messaging = MessagingService(send_message=self.send_message, transport_context=transport_context)
        await messaging.send_plain_text(target_chat_id, str(text or ""), **send_kwargs)

    @staticmethod
    def _build_transport_context(
        bot_app: Any,
        *,
        context: Any,
        session: Any,
        chat_id: int,
        dest: Optional[dict],
        user_id: Optional[int],
    ) -> Any:
        builder = getattr(bot_app, "build_telegram_transport_context", None)
        if callable(builder):
            return builder(
                context,
                session=session,
                chat_id=chat_id,
                dest=dest,
                user_id=user_id,
            )
        return context

    @staticmethod
    def _normalize_dest(
        bot_app: Any,
        *,
        session: Any,
        chat_id: int,
        dest: Optional[dict],
        user_id: Optional[int],
    ) -> dict:
        normalized_dest = dict(dest or {})
        normalized_dest["kind"] = str(normalized_dest.get("kind") or "telegram")
        normalized_dest["chat_id"] = chat_id
        builder = getattr(bot_app, "build_telegram_reply_dest", None)
        if callable(builder):
            reply_dest = builder(session, chat_id, user_id=user_id)
            if normalized_dest.get("message_thread_id") is None and reply_dest.get("message_thread_id") is not None:
                normalized_dest["message_thread_id"] = reply_dest["message_thread_id"]
            if normalized_dest.get("user_id") is None and reply_dest.get("user_id") is not None:
                normalized_dest["user_id"] = reply_dest["user_id"]
        elif user_id is not None and normalized_dest.get("user_id") is None:
            normalized_dest["user_id"] = int(user_id)
        return normalized_dest

    async def _send_output_if_any(
        self,
        *,
        context: Any,
        chat_id: int,
        result: Any,
        session: Any,
        dest: Optional[dict],
        bot_app: Any,
        user_id: Optional[int],
    ) -> None:
        if not result or not getattr(result, "output", None):
            return
        output_text = str(result.output)
        normalized_dest = self._normalize_dest(
            bot_app,
            session=session,
            chat_id=chat_id,
            dest=dest,
            user_id=user_id,
        )
        transport_context = self._build_transport_context(
            bot_app,
            context=context,
            session=session,
            chat_id=chat_id,
            dest=normalized_dest,
            user_id=user_id,
        )
        if self.send_output and session is not None:
            try:
                await self.send_output(session, normalized_dest, output_text, transport_context, send_header=False)
                return
            except Exception as e:
                logger.exception("mode send_output failed, fallback to send_message: %s", e)
        if self.send_message:
            send_kwargs = self._reply_kwargs(normalized_dest, chat_id=chat_id)
            if normalized_dest.get("message_thread_id") is not None:
                send_kwargs["message_thread_id"] = normalized_dest["message_thread_id"]
            target_chat_id = send_kwargs.pop("chat_id", chat_id)
            messaging = MessagingService(send_message=self.send_message, transport_context=transport_context)
            await messaging.send_plain_text(target_chat_id, output_text, **send_kwargs)

    async def route_mode_or_cli(
        self,
        *,
        bot_app: Any,
        session: Any,
        text: str,
        chat_id: int,
        context: Any,
        dest: Optional[dict],
        user_id: Optional[int],
        cli_fallback: CliFallbackFn,
    ) -> Optional[Any]:
        if self.lint_evolution_hook is not None:
            try:
                self.lint_evolution_hook(session)
            except Exception:
                logger.exception("lint_evolution hook failed")
        active_mode = self.mode_registry.get_active_mode_id(session)
        normalized_dest = self._normalize_dest(
            bot_app,
            session=session,
            chat_id=chat_id,
            dest=dest,
            user_id=user_id,
        )
        transport_context = self._build_transport_context(
            bot_app,
            context=context,
            session=session,
            chat_id=chat_id,
            dest=normalized_dest,
            user_id=user_id,
        )
        prepared_text = str(text or "")
        prepared_dest = dict(normalized_dest)
        if active_mode:
            image_paths = self._extract_image_paths(prepared_dest)
            if image_paths:
                if await self._queue_busy_mode_image_input(
                    bot_app=bot_app,
                    session=session,
                    text=prepared_text,
                    chat_id=chat_id,
                    context=transport_context,
                    dest=prepared_dest,
                ):
                    return None
                try:
                    cli_name, analysis = await self._run_mode_image_analysis(
                        bot_app=bot_app,
                        session=session,
                        active_mode=active_mode,
                        text=prepared_text,
                        image_paths=image_paths,
                    )
                except Exception as exc:
                    await self._send_image_analysis_error(
                        transport_context=transport_context,
                        chat_id=chat_id,
                        dest=prepared_dest,
                        text=str(exc or "Не удалось проанализировать изображения."),
                    )
                    return None
                prepared_text = self._compose_mode_image_text(
                    text=prepared_text,
                    analysis=analysis,
                    cli_name=cli_name,
                    image_count=len(image_paths),
                )
                prepared_dest = self._dest_without_images(prepared_dest)
        if active_mode:
            if self.dialogs and session and getattr(session, "id", None):
                try:
                    if self.dialogs.is_active(chat_id=chat_id, session_id=session.id, mode_id=active_mode):
                        result = await self.dialogs.route_message(
                            MessageModel(text=prepared_text, chat_id=chat_id, user_id=user_id),
                            {
                                "bot_app": bot_app,
                                "session": session,
                                "chat_id": chat_id,
                                "context": transport_context,
                                "dest": prepared_dest,
                                "mode_id": active_mode,
                            },
                            session_id=session.id,
                            mode_id=active_mode,
                        )
                        await self._send_output_if_any(
                            context=context,
                            chat_id=chat_id,
                            result=result,
                            session=session,
                            dest=prepared_dest,
                            bot_app=bot_app,
                            user_id=user_id,
                        )
                        return result
                except Exception as e:
                    logger.exception(
                        "mode dialog handle_input failed mode=%s err=%s",
                        active_mode,
                        e,
                    )

            mode = self.mode_registry.get(active_mode)
            if mode is not None:
                try:
                    result = await mode.handle_input(
                        MessageModel(text=prepared_text, chat_id=chat_id, user_id=user_id),
                        {
                            "bot_app": bot_app,
                            "session": session,
                            "chat_id": chat_id,
                            "context": transport_context,
                            "dest": prepared_dest,
                            "mode_id": active_mode,
                        },
                    )
                    await self._send_output_if_any(
                        context=context,
                        chat_id=chat_id,
                        result=result,
                        session=session,
                        dest=prepared_dest,
                        bot_app=bot_app,
                        user_id=user_id,
                    )
                    return result
                except Exception as e:
                    logger.exception("mode handle_input failed mode=%s err=%s", active_mode, e)
                    raise

        await cli_fallback(session, text, chat_id, transport_context)
        return None
