"""Shared user/CLI input dispatch for BotApp and message processing."""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from typing import Any, Optional

from app.services.input_dispatch_models import (
    PENDING_ACTION_CONFIRM as MODEL_PENDING_ACTION_CONFIRM,
    PENDING_ACTION_ORCHESTRATOR_TRANSITION as MODEL_PENDING_ACTION_ORCHESTRATOR_TRANSITION,
    PENDING_ACTION_QUEUE_CHOICE as MODEL_PENDING_ACTION_QUEUE_CHOICE,
    PENDING_ACTION_QUEUE_CONFIRM as MODEL_PENDING_ACTION_QUEUE_CONFIRM,
    PendingInput,
    PendingInputDecision,
)
from session import session_runtime_uid
from app.services.telegram_ui_scope import TelegramUiKey
from sessions.queue_item import append_session_queue_item, normalize_queue_item_payload
from sessions.session_state_access import (
    get_active_mode,
    get_orchestrator_pending_input,
    is_orchestrator_enabled,
    set_orchestrator_pending_input,
)

logger = logging.getLogger(__name__)

_ARTIFACT_TRACE_KEYWORDS = (
    "пришли",
    "скинь",
    "отправь",
    "файл",
    "скачать",
    "перешли",
    "кинь",
    "дай",
)
_ARTIFACT_INTENT_MAX_TEXT_LENGTH = 250


class InputDispatchService:
    PENDING_INPUT_LIMIT = 5
    DIRECT_CLI_DENIED_TEXT = "Прямой CLI недоступен для вашего пользователя."
    PENDING_ACTION_CONFIRM = MODEL_PENDING_ACTION_CONFIRM
    PENDING_ACTION_QUEUE_CONFIRM = MODEL_PENDING_ACTION_QUEUE_CONFIRM
    PENDING_ACTION_QUEUE_CHOICE = MODEL_PENDING_ACTION_QUEUE_CHOICE
    PENDING_ACTION_ORCHESTRATOR_TRANSITION = MODEL_PENDING_ACTION_ORCHESTRATOR_TRANSITION

    def __init__(self, bot_app, *, pending_input_ui=None):
        self.bot_app = bot_app
        self.pending_input_ui = pending_input_ui or getattr(bot_app, "pending_input_ui", None)

    @staticmethod
    def _artifact_trace_enabled(text: str) -> bool:
        raw = str(text or "")
        lowered = raw.lower()
        return "/" in raw or "\\" in raw or any(token in lowered for token in _ARTIFACT_TRACE_KEYWORDS)

    @staticmethod
    def _artifact_intent_text_too_long(text: str) -> bool:
        return len(str(text or "")) > _ARTIFACT_INTENT_MAX_TEXT_LENGTH

    @staticmethod
    def _artifact_preview(text: str, *, limit: int = 200) -> str:
        preview = " ".join(str(text or "").split())
        if len(preview) > limit:
            return preview[: limit - 3] + "..."
        return preview

    def _log_artifact_intent(self, session, event: str, *, text: str = "", **fields: Any) -> None:
        payload = {
            "session_id": str(getattr(session, "id", "") or ""),
            "session_uid": session_runtime_uid(session),
            "event": event,
        }
        if text:
            payload["text"] = self._artifact_preview(text)
        payload.update({key: value for key, value in fields.items() if value is not None})
        details = " ".join(f"{key}={value!r}" for key, value in payload.items())
        logger.info("artifact intent trace %s", details)

    @staticmethod
    def _pending_ui_key(dest: Optional[dict], fallback_chat_id: Any) -> Any:
        payload = dict(dest or {})
        if str(payload.get("kind") or "").strip().lower() == "desktop":
            return str(payload.get("session_uid") or fallback_chat_id or "").strip()
        return TelegramUiKey.from_dest(dest, fallback_chat_id=fallback_chat_id)

    @staticmethod
    def _is_mode_task_busy(session, bot_app=None) -> bool:
        mode_task_busy = False
        if bot_app is not None:
            try:
                mode_tasks = getattr(bot_app, "mode_tasks", None)
                session_uid = session_runtime_uid(session)
                active_mode = str(get_active_mode(session, "") or "").strip()
                if mode_tasks is not None and session_uid and active_mode and hasattr(mode_tasks, "list"):
                    mode_task_busy = bool(mode_tasks.list(session_uid=session_uid, mode_id=active_mode))
            except Exception:
                logger.exception("input dispatch: mode task busy check failed")
        return mode_task_busy

    @classmethod
    def _is_session_running(cls, session, bot_app=None) -> bool:
        run_lock = getattr(session, "run_lock", None)
        run_lock_busy = bool(run_lock and run_lock.locked())
        tick_busy = False
        try:
            is_active_by_tick = getattr(session, "is_active_by_tick", None)
            if callable(is_active_by_tick):
                tick_busy = bool(is_active_by_tick())
        except Exception:
            logger.exception("input dispatch: is_active_by_tick check failed")

        mode_task_busy = cls._is_mode_task_busy(session, bot_app)
        return bool(getattr(session, "busy", False) or tick_busy or run_lock_busy or mode_task_busy)

    @classmethod
    def _is_session_busy(cls, session, bot_app=None) -> bool:
        queue_busy = bool(getattr(session, "queue", None))
        return bool(cls._is_session_running(session, bot_app) or queue_busy)

    def _is_shutting_down(self) -> bool:
        return bool(getattr(self.bot_app, "_shutdown_in_progress", False))

    def _persist_sessions_best_effort(self) -> None:
        try:
            mode_session_control = getattr(self.bot_app, "mode_session_control", None)
            if mode_session_control is not None and hasattr(mode_session_control, "persist"):
                mode_session_control.persist()
                return
        except Exception:
            logger.exception("input dispatch: mode_session_control.persist failed")
        try:
            manager = getattr(self.bot_app, "manager", None)
            if manager is not None and hasattr(manager, "_persist_sessions"):
                manager._persist_sessions()
        except Exception:
            logger.exception("input dispatch: manager._persist_sessions failed")

    def _pending_input_confirmation_enabled(self) -> bool:
        config = getattr(self.bot_app, "config", None)
        defaults = getattr(config, "defaults", None) if config is not None else None
        return bool(getattr(defaults, "pending_input_confirmation_enabled", True))

    def _safe_create_task(self, coro, *, label: str) -> Optional[asyncio.Task]:
        if self._is_shutting_down():
            try:
                coro.close()
            except Exception:
                logging.getLogger(__name__).exception("failed to close coroutine during shutdown: %s", label)
            logging.getLogger(__name__).info("skip task scheduling during shutdown: %s", label)
            return None
        try:
            return asyncio.create_task(coro)
        except RuntimeError:
            try:
                coro.close()
            except Exception:
                logging.getLogger(__name__).exception("failed to close coroutine after create_task error: %s", label)
            logging.getLogger(__name__).exception("failed to create task: %s", label)
            return None

    @staticmethod
    def _normalize_chat_id(chat_id):
        if isinstance(chat_id, int):
            return chat_id
        try:
            return int(chat_id)
        except (TypeError, ValueError):
            return str(chat_id)

    def _reply_dest(
        self,
        session,
        chat_id,
        *,
        user_id: Optional[int] = None,
        direct_messages_topic_id: Optional[int] = None,
    ) -> dict:
        normalized_chat_id = self._normalize_chat_id(chat_id)
        builder = getattr(self.bot_app, "build_telegram_reply_dest", None)
        if callable(builder):
            kwargs = {"user_id": user_id}
            if direct_messages_topic_id is not None:
                kwargs["direct_messages_topic_id"] = direct_messages_topic_id
            return builder(
                session,
                normalized_chat_id,
                **kwargs,
            )
        dest = {"kind": "telegram", "chat_id": normalized_chat_id}
        if user_id is not None:
            dest["user_id"] = int(user_id)
        if direct_messages_topic_id is not None:
            dest["direct_messages_topic_id"] = int(direct_messages_topic_id)
        return dest

    @staticmethod
    def _resolve_positive_chat_id(value: Any) -> Optional[int]:
        try:
            resolved = int(value)
        except (TypeError, ValueError):
            return None
        return resolved if resolved > 0 else None

    @classmethod
    def _resolve_policy_chat_id(cls, chat_id: Any, dest: Optional[dict]) -> Optional[int]:
        if isinstance(dest, dict):
            for key in ("user_id", "owner_chat_id", "chat_id"):
                resolved = cls._resolve_positive_chat_id(dest.get(key))
                if resolved is not None:
                    return resolved
        return cls._resolve_positive_chat_id(chat_id)

    @classmethod
    def _pending_queue(cls, pending_map: dict, key: Any):
        raw = pending_map.get(key)
        if isinstance(raw, deque):
            return raw
        items = deque()
        if raw is not None:
            items.append(raw)
        pending_map[key] = items
        return items

    @classmethod
    def pending_head(cls, pending_map: Optional[dict], key: Any):
        if not isinstance(pending_map, dict):
            return None
        items = cls._pending_queue(pending_map, key)
        if not items:
            pending_map.pop(key, None)
            return None
        return items[0]

    @classmethod
    def pop_pending(cls, pending_map: Optional[dict], key: Any):
        if not isinstance(pending_map, dict):
            return None
        items = cls._pending_queue(pending_map, key)
        if not items:
            pending_map.pop(key, None)
            return None
        item = items.popleft()
        if not items:
            pending_map.pop(key, None)
        return item

    @classmethod
    def enqueue_pending(
        cls,
        pending_map: Optional[dict],
        key: Any,
        pending_input: PendingInput,
    ) -> bool:
        if not isinstance(pending_map, dict):
            return False
        items = cls._pending_queue(pending_map, key)
        if len(items) >= int(cls.PENDING_INPUT_LIMIT):
            return False
        items.append(pending_input)
        return True

    @classmethod
    def upsert_pending(
        cls,
        pending_map: Optional[dict],
        key: Any,
        pending_input: PendingInput,
    ) -> tuple[bool, PendingInput]:
        if not isinstance(pending_map, dict):
            return False, pending_input
        items = cls._pending_queue(pending_map, key)
        for idx, existing in enumerate(list(items)):
            if cls._same_pending_session(existing, pending_input):
                merged = cls._merge_pending_inputs(existing, pending_input, action=getattr(pending_input, "action", None))
                items[idx] = merged
                return True, merged
        if len(items) >= int(cls.PENDING_INPUT_LIMIT):
            return False, pending_input
        items.append(pending_input)
        return True, pending_input

    @classmethod
    def pending_limit_overflow_text(cls) -> str:
        return (
            "Очередь ожидающего ввода переполнена "
            f"(лимит {int(cls.PENDING_INPUT_LIMIT)}). Дождитесь обработки текущих запросов."
        )

    @classmethod
    def take_in_work_prompt_text(cls) -> str:
        return "Сообщение получено. Взять в работу?"

    @classmethod
    def busy_prompt_text(cls) -> str:
        return "В очереди уже есть сообщение. Что сделать с новым вводом?"

    @classmethod
    def queue_confirm_prompt_text(cls) -> str:
        return "Сессия занята. Поставить сообщение в очередь?"

    @staticmethod
    def queued_immediately_text() -> str:
        return "Ввод добавлен в очередь."

    @classmethod
    def pending_input_decision_for_action(
        cls,
        action: str,
        *,
        pending_input: Optional[PendingInput] = None,
    ) -> PendingInputDecision:
        normalized = str(action or cls.PENDING_ACTION_CONFIRM).strip() or cls.PENDING_ACTION_CONFIRM
        if normalized == cls.PENDING_ACTION_QUEUE_CHOICE:
            text = cls.busy_prompt_text()
        elif normalized == cls.PENDING_ACTION_QUEUE_CONFIRM:
            text = cls.queue_confirm_prompt_text()
        else:
            normalized = cls.PENDING_ACTION_CONFIRM
            text = cls.take_in_work_prompt_text()
        payload: dict[str, Any] = {"pending_action": normalized}
        if pending_input is not None:
            payload.update(
                {
                    "session_id": str(getattr(pending_input, "session_id", "") or ""),
                    "session_uid": str(getattr(pending_input, "session_uid", "") or ""),
                    "dest": dict(getattr(pending_input, "dest", {}) or {}),
                }
            )
        return PendingInputDecision(action=normalized, text=text, payload=payload)

    @classmethod
    def pending_overflow_decision(cls) -> PendingInputDecision:
        return PendingInputDecision(
            action="pending_overflow",
            text=cls.pending_limit_overflow_text(),
            payload={},
        )

    @staticmethod
    def orchestrator_transition_decision(
        *,
        text: str,
        session_uid: str,
        target_mode_id: str,
    ) -> PendingInputDecision:
        return PendingInputDecision(
            action=MODEL_PENDING_ACTION_ORCHESTRATOR_TRANSITION,
            text=str(text or ""),
            payload={
                "session_uid": str(session_uid or "").strip(),
                "target_mode_id": str(target_mode_id or "").strip(),
            },
        )

    def _pending_map(self) -> dict:
        ui_state = getattr(self.bot_app, "ui_state", None)
        pending_map = getattr(ui_state, "pending", None) if ui_state is not None else None
        if isinstance(pending_map, dict):
            return pending_map
        fixed: dict = {}
        if ui_state is not None:
            try:
                ui_state.pending = fixed
                return fixed
            except Exception:
                logger.exception("failed to repair pending map on ui_state")
        setattr(self.bot_app, "pending", fixed)
        return fixed

    def _pending_prompt_messages(self) -> dict:
        ui_state = getattr(self.bot_app, "ui_state", None)
        store = getattr(ui_state, "pending_prompt_messages", None) if ui_state is not None else None
        if isinstance(store, dict):
            return store
        fixed: dict = {}
        if ui_state is not None:
            try:
                ui_state.pending_prompt_messages = fixed
                return fixed
            except Exception:
                logger.exception("failed to repair pending_prompt_messages on ui_state")
        setattr(self.bot_app, "pending_prompt_messages", fixed)
        return fixed

    @staticmethod
    def _join_text_parts(*parts: str) -> str:
        normalized = [str(part or "").strip() for part in parts if str(part or "").strip()]
        return "\n\n".join(normalized)

    @staticmethod
    def _normalize_image_paths(*, image_path: Optional[str], image_paths: Optional[list[str]], dest: Optional[dict]) -> list[str]:
        combined: list[str] = []
        raw_many = list(image_paths or [])
        if not raw_many and isinstance(dest, dict):
            raw_many = list(dest.get("image_paths") or [])
        raw_one = str(image_path or "").strip()
        if not raw_one and isinstance(dest, dict):
            raw_one = str((dest or {}).get("image_path") or "").strip()
        for candidate in raw_many:
            value = str(candidate or "").strip()
            if value and value not in combined:
                combined.append(value)
        if raw_one and raw_one not in combined:
            combined.append(raw_one)
        return combined

    @classmethod
    def _apply_images_to_dest(cls, dest: dict, *, image_paths: list[str]) -> dict:
        merged = dict(dest or {})
        merged.pop("image_path", None)
        merged.pop("cleanup_image", None)
        if image_paths:
            merged["image_paths"] = list(image_paths)
            merged["cleanup_images"] = True
        else:
            merged.pop("image_paths", None)
            merged.pop("cleanup_images", None)
        return merged

    @classmethod
    def _merge_pending_inputs(
        cls,
        current: PendingInput,
        incoming: PendingInput,
        *,
        action: Optional[str] = None,
    ) -> PendingInput:
        image_paths = cls._normalize_image_paths(
            image_path=getattr(current, "image_path", None),
            image_paths=getattr(current, "image_paths", None),
            dest=getattr(current, "dest", None),
        )
        for item in cls._normalize_image_paths(
            image_path=getattr(incoming, "image_path", None),
            image_paths=getattr(incoming, "image_paths", None),
            dest=getattr(incoming, "dest", None),
        ):
            if item not in image_paths:
                image_paths.append(item)
        merged_dest = cls._apply_images_to_dest(
            {
                **dict(getattr(current, "dest", {}) or {}),
                **dict(getattr(incoming, "dest", {}) or {}),
            },
            image_paths=image_paths,
        )
        session_uid = str(getattr(incoming, "session_uid", "") or getattr(current, "session_uid", "") or "").strip()
        return PendingInput(
            session_id=str(getattr(incoming, "session_id", "") or getattr(current, "session_id", "") or ""),
            text=cls._join_text_parts(getattr(current, "text", ""), getattr(incoming, "text", "")),
            dest=merged_dest,
            session_uid=session_uid or None,
            image_path=image_paths[0] if len(image_paths) == 1 else None,
            image_paths=list(image_paths) if image_paths else None,
            action=str(
                action
                or getattr(incoming, "action", None)
                or getattr(current, "action", None)
                or cls.PENDING_ACTION_CONFIRM
            ),
        )

    @staticmethod
    def _same_pending_session(lhs: PendingInput, rhs: PendingInput) -> bool:
        lhs_uid = str(getattr(lhs, "session_uid", "") or "").strip()
        rhs_uid = str(getattr(rhs, "session_uid", "") or "").strip()
        if lhs_uid and rhs_uid:
            return lhs_uid == rhs_uid
        return str(getattr(lhs, "session_id", "") or "") == str(getattr(rhs, "session_id", "") or "")

    def _build_pending_input(
        self,
        *,
        session,
        text: str,
        chat_id: Any,
        dest: Optional[dict],
        image_path: Optional[str],
        image_paths: Optional[list[str]],
        action: str,
    ) -> PendingInput:
        resolved_dest = dict(dest or self._reply_dest(session, chat_id))
        normalized_images = self._normalize_image_paths(
            image_path=image_path,
            image_paths=image_paths,
            dest=resolved_dest,
        )
        resolved_dest = self._apply_images_to_dest(resolved_dest, image_paths=normalized_images)
        resolved_session_uid = session_runtime_uid(session) or str(getattr(session, "id", "") or "").strip()
        return PendingInput(
            str(getattr(session, "id", "") or ""),
            str(text or ""),
            resolved_dest,
            session_uid=resolved_session_uid or None,
            image_path=normalized_images[0] if len(normalized_images) == 1 else None,
            image_paths=list(normalized_images) if normalized_images else None,
            action=str(action or self.PENDING_ACTION_CONFIRM),
        )

    def _remember_pending_prompt_message(self, ui_key: Any, message_id: Any) -> None:
        try:
            resolved = int(message_id)
        except (TypeError, ValueError):
            return
        if resolved <= 0:
            return
        self._pending_prompt_messages()[ui_key] = resolved

    def clear_pending_prompt_record(self, ui_key: Any) -> None:
        self._pending_prompt_messages().pop(ui_key, None)

    async def _retire_pending_prompt(
        self,
        *,
        ui_key: Any,
        context,
        dest: Optional[dict],
        stale_text: str = "Сообщение обновлено.",
    ) -> None:
        message_id = self._pending_prompt_messages().pop(ui_key, None)
        if not message_id:
            return
        adapter = self.pending_input_ui
        retire_prompt = getattr(adapter, "retire_prompt", None) if adapter is not None else None
        if callable(retire_prompt):
            retired = await retire_prompt(
                context,
                dest=dest,
                message_id=int(message_id),
                stale_text=stale_text,
                ui_key=ui_key,
            )
            if retired:
                return
        if adapter is None:
            logger.warning("input dispatch transport adapter missing for pending prompt retire ui_key=%s", ui_key)
            return
        logger.warning("input dispatch transport adapter cannot retire pending prompt ui_key=%s", ui_key)

    def _require_transport_method(self, method_name: str):
        adapter = self.pending_input_ui
        method = getattr(adapter, method_name, None) if adapter is not None else None
        if callable(method):
            return method
        raise RuntimeError(f"input dispatch transport adapter missing method: {method_name}")

    async def _send_text(
        self,
        context,
        *,
        text: str,
        dest: Optional[dict],
        chat_id: Any,
        md2: bool = True,
    ) -> Any:
        sender = self._require_transport_method("send_text")
        return await sender(
            context,
            text=str(text or ""),
            dest=dest,
            fallback_chat_id=chat_id,
            md2=bool(md2),
        )

    async def _send_document(
        self,
        context,
        *,
        document: Any,
        filename: str,
        caption: str,
        dest: Optional[dict],
        chat_id: Any,
    ) -> Any:
        sender = self._require_transport_method("send_document")
        return await sender(
            context,
            document=document,
            filename=str(filename or ""),
            caption=str(caption or ""),
            dest=dest,
            fallback_chat_id=chat_id,
        )

    async def _send_router_message(self, context, **kwargs: Any) -> Any:
        dest = {"chat_id": kwargs.get("chat_id")}
        if kwargs.get("message_thread_id") is not None:
            dest["message_thread_id"] = kwargs["message_thread_id"]
        if kwargs.get("direct_messages_topic_id") is not None:
            dest["direct_messages_topic_id"] = kwargs["direct_messages_topic_id"]
        return await self._send_text(
            context,
            text=str(kwargs.get("text") or ""),
            dest=dest,
            chat_id=kwargs.get("chat_id"),
            md2=bool(kwargs.get("md2", True)),
        )

    async def send_pending_input_decision(
        self,
        *,
        context,
        decision: PendingInputDecision,
        dest: Optional[dict],
        chat_id: Any,
        ui_key: Any = None,
    ) -> Any:
        sender = self._require_transport_method("send_decision")
        message = await sender(
            context,
            decision,
            dest=dest,
            fallback_chat_id=chat_id,
        )
        if ui_key is not None:
            self._remember_pending_prompt_message(ui_key, getattr(message, "message_id", None))
        return message

    async def _show_pending_prompt(self, *, ui_key: Any, pending_input: PendingInput, chat_id: Any, context) -> None:
        await self._retire_pending_prompt(ui_key=ui_key, context=context, dest=getattr(pending_input, "dest", None))
        action = str(getattr(pending_input, "action", "") or "")
        await self.send_pending_input_decision(
            context=context,
            decision=self.pending_input_decision_for_action(action, pending_input=pending_input),
            dest=getattr(pending_input, "dest", None),
            chat_id=chat_id,
            ui_key=ui_key,
        )

    def _record_queue_metric(self) -> None:
        metrics = getattr(self.bot_app, "metrics", None)
        if metrics is not None and hasattr(metrics, "inc"):
            metrics.inc("queued")

    @classmethod
    def queue_item_from_pending(cls, pending_input: PendingInput) -> dict:
        item = {
            "text": str(getattr(pending_input, "text", "") or ""),
            "dest": dict(getattr(pending_input, "dest", {}) or {}),
        }
        image_paths = cls._normalize_image_paths(
            image_path=getattr(pending_input, "image_path", None),
            image_paths=getattr(pending_input, "image_paths", None),
            dest=getattr(pending_input, "dest", None),
        )
        if image_paths:
            item["image_paths"] = list(image_paths)
        return normalize_queue_item_payload(item)

    @staticmethod
    def append_queue_item(session, item: Any, *, fallback_dest: Optional[dict] = None) -> bool:
        return append_session_queue_item(session, item, fallback_dest=fallback_dest)

    @classmethod
    def append_pending_to_queue_tail(cls, session, pending_input: PendingInput) -> bool:
        queue_ref = getattr(session, "queue", None)
        if queue_ref is None:
            return False
        item = cls.queue_item_from_pending(pending_input)
        if not queue_ref:
            return cls.append_queue_item(session, item)
        tail = queue_ref[-1]
        if isinstance(tail, dict):
            current = PendingInput(
                session_id=str(getattr(session, "id", "") or ""),
                text=str((tail or {}).get("text") or ""),
                dest=dict((tail or {}).get("dest") or {}),
                image_path=str((tail or {}).get("image_path") or "").strip() or None,
                image_paths=list((tail or {}).get("image_paths") or []) or None,
            )
            queue_ref[-1] = cls.queue_item_from_pending(cls._merge_pending_inputs(current, pending_input))
            return True
        if isinstance(tail, str):
            queue_ref[-1] = cls.queue_item_from_pending(
                cls._merge_pending_inputs(
                    PendingInput(
                        session_id=str(getattr(session, "id", "") or ""),
                        text=str(tail or ""),
                        dest=dict(getattr(pending_input, "dest", {}) or {}),
                    ),
                    pending_input,
                )
            )
            return True
        return False

    async def _handle_busy_pending_input(
        self,
        *,
        session,
        pending_input: PendingInput,
        chat_id: Any,
        context,
    ) -> None:
        pending_map = self._pending_map()
        ui_key = self._pending_ui_key(getattr(pending_input, "dest", None), chat_id)
        queue_ref = getattr(session, "queue", None)
        if queue_ref is None:
            queue_ref = []
            setattr(session, "queue", queue_ref)
        queue_has_items = bool(queue_ref)
        target_action = (
            self.PENDING_ACTION_QUEUE_CHOICE
            if queue_has_items
            else self.PENDING_ACTION_QUEUE_CONFIRM
        )
        pending_input.action = target_action
        accepted, merged = self.upsert_pending(pending_map, ui_key, pending_input)
        if not accepted:
            await self.send_pending_input_decision(
                context=context,
                decision=self.pending_overflow_decision(),
                dest=getattr(pending_input, "dest", None),
                chat_id=chat_id,
            )
            return
        self._record_queue_metric()
        await self._show_pending_prompt(ui_key=ui_key, pending_input=merged, chat_id=chat_id, context=context)

    async def stage_user_input(
        self,
        session,
        text: str,
        chat_id: Any,
        context,
        *,
        dest: Optional[dict] = None,
        image_path: Optional[str] = None,
        image_paths: Optional[list[str]] = None,
    ) -> None:
        pending_input = self._build_pending_input(
            session=session,
            text=text,
            chat_id=chat_id,
            dest=dest,
            image_path=image_path,
            image_paths=image_paths,
            action=self.PENDING_ACTION_CONFIRM,
        )

        pending_map = self._pending_map()
        ui_key = self._pending_ui_key(getattr(pending_input, "dest", None), chat_id)
        if self._pending_input_confirmation_enabled():
            pending_input.action = self.PENDING_ACTION_CONFIRM
            accepted, merged = self.upsert_pending(pending_map, ui_key, pending_input)
            if not accepted:
                await self.send_pending_input_decision(
                    context=context,
                    decision=self.pending_overflow_decision(),
                    dest=getattr(pending_input, "dest", None),
                    chat_id=chat_id,
                )
                return
            await self._show_pending_prompt(ui_key=ui_key, pending_input=merged, chat_id=chat_id, context=context)
            return

        if self._is_session_busy(session, self.bot_app):
            await self._handle_busy_pending_input(
                session=session,
                pending_input=pending_input,
                chat_id=chat_id,
                context=context,
            )
            return

        await self._handle_user_input_impl(
            session=session,
            text=str(getattr(pending_input, "text", "") or ""),
            chat_id=chat_id,
            context=context,
            dest=dict(getattr(pending_input, "dest", {}) or {}),
            allow_orchestration=True,
        )

    async def handle_cli_input(
        self,
        session,
        text: str,
        chat_id: int,
        context,
        *,
        dest: Optional[dict] = None,
        image_path: Optional[str] = None,
        image_paths: Optional[list[str]] = None,
        enforce_direct_cli_policy: bool = True,
    ) -> None:
        if dest is None:
            dest = self._reply_dest(session, chat_id)
        effective_image_paths = self._normalize_image_paths(
            image_path=image_path,
            image_paths=image_paths,
            dest=dest,
        )
        effective_image_path = effective_image_paths[0] if len(effective_image_paths) == 1 else None
        policy = getattr(self.bot_app, "access_policy_service", None)
        checker = getattr(policy, "is_direct_cli_allowed_for_chat", None) if policy is not None else None
        resolved_chat_id = self._resolve_policy_chat_id(chat_id, dest)
        if enforce_direct_cli_policy and resolved_chat_id is not None and callable(checker):
            try:
                direct_cli_allowed = bool(checker(resolved_chat_id))
            except Exception:
                logger.exception("direct CLI policy check failed chat_id=%s", resolved_chat_id)
                direct_cli_allowed = False
            if not direct_cli_allowed:
                denied_text = str(
                    getattr(policy, "DIRECT_CLI_DENIED_TEXT", "") or self.DIRECT_CLI_DENIED_TEXT
                ).strip() or self.DIRECT_CLI_DENIED_TEXT
                await self._send_text(
                    context,
                    text=denied_text,
                    dest=dest,
                    chat_id=chat_id,
                )
                return
        dest = self._apply_images_to_dest(dict(dest or {}), image_paths=effective_image_paths)
        if self._is_session_busy(session, self.bot_app):
            await self._handle_busy_pending_input(
                session=session,
                pending_input=self._build_pending_input(
                    session=session,
                    text=text,
                    chat_id=chat_id,
                    dest=dest,
                    image_path=effective_image_path,
                    image_paths=list(effective_image_paths) if effective_image_paths else None,
                    action=self.PENDING_ACTION_QUEUE_CHOICE,
                ),
                chat_id=chat_id,
                context=context,
            )
            return
        session_management = getattr(self.bot_app, "session_management", None)
        start_prompt_task = getattr(session_management, "start_prompt_task", None)
        if callable(start_prompt_task):
            start_prompt_task(
                session,
                text,
                dest,
                context,
                task_name="input_dispatch.handle_cli_input.run_prompt",
            )
            return
        self._safe_create_task(
            self.bot_app.run_prompt(session, text, dest, context),
            label="input_dispatch.handle_cli_input.run_prompt",
        )

    async def handle_user_input(
        self,
        session,
        text: str,
        chat_id: int,
        context,
        *,
        dest: Optional[dict] = None,
    ) -> None:
        await self._handle_user_input_impl(
            session=session,
            text=text,
            chat_id=chat_id,
            context=context,
            dest=dest,
            allow_orchestration=True,
        )

    async def handle_user_input_no_orchestration(
        self,
        session,
        text: str,
        chat_id: int,
        context,
        *,
        dest: Optional[dict] = None,
    ) -> None:
        await self._handle_user_input_impl(
            session=session,
            text=text,
            chat_id=chat_id,
            context=context,
            dest=dest,
            allow_orchestration=False,
        )

    async def _handle_user_input_impl(
        self,
        *,
        session,
        text: str,
        chat_id: int,
        context,
        dest: Optional[dict],
        allow_orchestration: bool,
    ) -> None:
        msg_user_id = None
        try:
            if isinstance(dest, dict) and (dest or {}).get("user_id") is not None:
                msg_user_id = int((dest or {}).get("user_id"))
        except Exception:
            msg_user_id = None

        if allow_orchestration:
            if is_orchestrator_enabled(session, False):
                pending = get_orchestrator_pending_input(session, None)
                if not pending:
                    orch = getattr(self.bot_app, "advanced_orchestrator_service", None)
                    mode_registry = getattr(self.bot_app, "mode_registry_service", None)
                    if orch is not None:
                        proposal = None
                        try:
                            proposal = await orch.propose_transition_hybrid(
                                session=session,
                                text=str(text or ""),
                                mode_registry=mode_registry,
                                app_config=getattr(self.bot_app, "config", None),
                                llm_router_fn=getattr(self.bot_app, "orchestrator_chat_completion", None),
                            )
                        except Exception:
                            logging.getLogger(__name__).exception("hybrid orchestrator proposal failed")
                        if proposal is not None:
                            handoff_text = orch.build_handoff_input(
                                session=session,
                                original_user_text=str(text or ""),
                            )
                            set_orchestrator_pending_input(
                                session,
                                {
                                    "text": handoff_text,
                                    "dest": dict(dest or self._reply_dest(session, chat_id)),
                                    "target_mode_id": str(proposal.target_mode_id),
                                    "disable_orchestrator_on_cancel": False,
                                },
                            )
                            current_label = orch.current_mode_label(session=session, mode_registry=mode_registry)
                            await self.send_pending_input_decision(
                                context=context,
                                decision=self.orchestrator_transition_decision(
                                    text=orch.build_confirm_text(current_mode_label=current_label, proposal=proposal),
                                    session_uid=str(
                                        getattr(getattr(session, "conversation_scope", None), "session_uid", "") or ""
                                    ),
                                    target_mode_id=proposal.target_mode_id,
                                ),
                                dest=dest,
                                chat_id=chat_id,
                            )
                            return

        # Artifact intent: detect "send me a file" requests for non-Desktop transports.
        active_mode = str(get_active_mode(session, "") or "").strip()
        artifact_trace = self._artifact_trace_enabled(text)
        if active_mode:
            if artifact_trace:
                self._log_artifact_intent(session, "skip_active_mode", text=text, active_mode=active_mode)
        else:
            artifact_text = str(text or "")
            resolved_dest = dest or self._reply_dest(session, chat_id)
            dest_kind = str((resolved_dest or {}).get("kind", "")).strip().lower()
            if dest_kind != "desktop":
                if self._artifact_intent_text_too_long(artifact_text):
                    if artifact_trace:
                        self._log_artifact_intent(
                            session,
                            "skip_text_too_long",
                            text=artifact_text,
                            length=len(artifact_text),
                            limit=_ARTIFACT_INTENT_MAX_TEXT_LENGTH,
                        )
                else:
                    if artifact_trace:
                        self._log_artifact_intent(session, "attempt", text=text, dest_kind=dest_kind or "telegram")
                    handled = await self._try_artifact_intent(
                        session=session,
                        text=artifact_text,
                        chat_id=chat_id,
                        context=context,
                        dest=resolved_dest,
                    )
                    if handled:
                        return
                    if artifact_trace:
                        self._log_artifact_intent(session, "fallthrough_cli", text=text)
            elif artifact_trace:
                self._log_artifact_intent(session, "skip_desktop_dest", text=text, dest_kind=dest_kind)

        await self._route_mode_or_cli(
            session=session,
            text=str(text or ""),
            chat_id=str(chat_id),
            context=context,
            dest=dest,
            user_id=msg_user_id,
        )

    async def _try_artifact_intent(
        self,
        *,
        session,
        text: str,
        chat_id: int,
        context,
        dest: dict,
    ) -> bool:
        """Detect and handle 'send me a file' intent. Returns True if handled."""
        if self._artifact_intent_text_too_long(text):
            if self._artifact_trace_enabled(text):
                self._log_artifact_intent(
                    session,
                    "skip_text_too_long",
                    text=text,
                    length=len(str(text or "")),
                    limit=_ARTIFACT_INTENT_MAX_TEXT_LENGTH,
                )
            return False

        if "/" not in text and "\\" not in text:
            if self._artifact_trace_enabled(text):
                self._log_artifact_intent(session, "skip_no_path_separators", text=text)
            return False

        artifact_intent_service = getattr(self.bot_app, "artifact_intent_service", None)
        if artifact_intent_service is None:
            self._log_artifact_intent(session, "skip_service_missing", text=text)
            return False

        app_config = getattr(self.bot_app, "config", None)
        llm_fn = getattr(self.bot_app, "orchestrator_chat_completion", None)
        if app_config is None or llm_fn is None:
            self._log_artifact_intent(
                session,
                "skip_llm_unavailable",
                text=text,
                app_config=app_config is not None,
                llm_fn=llm_fn is not None,
            )
            return False

        # Resolve model: prefer openai_big_model from config.
        model = None
        defaults = getattr(app_config, "defaults", None)
        if defaults is not None:
            model = getattr(defaults, "openai_big_model", None)

        intent = await artifact_intent_service.classify(
            text,
            app_config=app_config,
            llm_fn=llm_fn,
            model=model,
        )
        if intent is None:
            self._log_artifact_intent(session, "classify_none", text=text, model=model)
            return False
        self._log_artifact_intent(
            session,
            "classify_match",
            text=text,
            file_pattern=getattr(intent, "file_pattern", ""),
            confidence=getattr(intent, "confidence", None),
        )

        project_root = getattr(session, "project_root", None)
        workdir = getattr(session, "workdir", None)
        result = artifact_intent_service.resolve(intent, project_root or workdir)

        if result.error:
            self._log_artifact_intent(session, "resolve_error", text=text, error=result.error)
            await self._send_text(
                context,
                text=result.error,
                dest=dest,
                chat_id=chat_id,
            )
            return True

        import os
        filename = os.path.basename(result.resolved_path)
        try:
            self._log_artifact_intent(
                session,
                "send_document",
                text=text,
                resolved_path=result.resolved_path,
                filename=filename,
            )
            with open(result.resolved_path, "rb") as f:
                success = await self._send_document(
                    context,
                    document=f,
                    filename=filename,
                    caption=filename,
                    dest=dest,
                    chat_id=chat_id,
                )
            if not success:
                self._log_artifact_intent(session, "send_failed", text=text, filename=filename)
                await self._send_text(
                    context,
                    text="Не удалось отправить файл.",
                    dest=dest,
                    chat_id=chat_id,
                )
            else:
                self._log_artifact_intent(session, "send_success", text=text, filename=filename)
        except Exception:
            self._log_artifact_intent(session, "send_exception", text=text, filename=filename)
            logger.exception("artifact intent: failed to send file %s", result.resolved_path)
            await self._send_text(
                context,
                text="Ошибка при отправке файла.",
                dest=dest,
                chat_id=chat_id,
            )
        return True

    async def _route_mode_or_cli(
        self,
        *,
        session,
        text: str,
        chat_id: int,
        context,
        dest: Optional[dict],
        user_id: Optional[int],
    ) -> None:
        async def _cli_fallback(_session, _text: str, _chat_id: int, _context) -> None:
            await self.bot_app._handle_cli_input(_session, _text, _chat_id, _context, dest=dest)

        # Keep router dependencies dynamic so tests/runtime monkeypatching
        # of bot_app services is respected.
        router = self.bot_app.mode_input_router
        router.send_message = self._send_router_message
        router.dialogs = getattr(self.bot_app, "mode_dialogs", None)
        router.mode_registry = getattr(self.bot_app, "mode_registry_service", router.mode_registry)

        try:
            await self.bot_app.mode_input_router.route_mode_or_cli(
                bot_app=self.bot_app,
                session=session,
                text=str(text or ""),
                chat_id=str(chat_id),
                context=context,
                dest=dest,
                user_id=user_id,
                cli_fallback=_cli_fallback,
            )
        except Exception:
            logger.exception(
                "input dispatch mode routing failed session_id=%s active_mode=%s",
                str(getattr(session, "id", "") or ""),
                str(get_active_mode(session, "") or ""),
            )
            raise
