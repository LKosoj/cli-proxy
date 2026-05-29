from __future__ import annotations

import copy
import logging
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, Callable, Iterable, Mapping, Optional

from app.events.bus import (
    DesktopCommandEvent,
    MiniAppCommandEvent,
    ModeLaunchCompletedEvent,
    ModeLaunchRequestedEvent,
    ScheduledJobEvent,
    WebhookReceivedEvent,
)
from app.security.errors import DenyReasonCode
from sessions.conversation_scope import ConversationScope
from sessions.session_state_access import get_active_mode, set_active_mode


logger = logging.getLogger(__name__)


class ModeLaunchAdapterError(RuntimeError):
    """Base error for event-driven mode launch adapter."""


@dataclass(frozen=True)
class ModeLaunchScope:
    kind: str
    session_uid: str
    chat_id: Optional[int] = None
    message_thread_id: Optional[int] = None


@dataclass(frozen=True)
class ModeLaunchRequest:
    scope: ModeLaunchScope
    mode_id: str
    project: dict[str, Any] = field(default_factory=dict)
    payload: dict[str, Any] = field(default_factory=dict)
    prompt: str = ""
    actor: dict[str, Any] = field(default_factory=dict)
    origin: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ModeLaunchPolicyDecision:
    allowed: bool
    reason: str
    origin_key: str
    mode_id: str


class ModeLaunchPolicy:
    def __init__(self, allowlist: Mapping[str, Iterable[str]] | None = None) -> None:
        self._allowlist = {
            str(origin or "").strip(): frozenset(
                str(mode_id or "").strip()
                for mode_id in list(modes or [])
                if str(mode_id or "").strip()
            )
            for origin, modes in dict(allowlist or {}).items()
            if str(origin or "").strip()
        }

    @classmethod
    def for_mode_registry(cls, mode_registry_service: Any) -> "ModeLaunchPolicy":
        mode_ids = [str(mode_id or "").strip() for mode_id, _label in list(mode_registry_service.list_modes() or [])]
        normalized_ids = [mode_id for mode_id in mode_ids if mode_id]
        return cls(
            {
                "scheduler": normalized_ids,
                "desktop": normalized_ids,
                "miniapp": normalized_ids,
                "webhook:*": normalized_ids,
            }
        )

    def decide(self, *, origin_key: str, mode_id: str) -> ModeLaunchPolicyDecision:
        key = str(origin_key or "").strip()
        mid = str(mode_id or "").strip()
        candidates = [key]
        if ":" in key:
            prefix = key.split(":", 1)[0]
            candidates.append(f"{prefix}:*")
        candidates.append("*")
        seen: set[str] = set()
        for candidate in candidates:
            token = str(candidate or "").strip()
            if not token or token in seen:
                continue
            seen.add(token)
            allowed_modes = self._allowlist.get(token)
            if allowed_modes is None:
                continue
            if mid in allowed_modes:
                return ModeLaunchPolicyDecision(True, "", origin_key=key, mode_id=mid)
            return ModeLaunchPolicyDecision(False, "mode_not_allowlisted", origin_key=key, mode_id=mid)
        return ModeLaunchPolicyDecision(False, "origin_not_allowlisted", origin_key=key, mode_id=mid)


class ModeLaunchAdapterService:
    def __init__(
        self,
        bot_app: Any = None,
        *,
        bot_app_provider: Callable[[], Any] | None = None,
        policy: Optional[ModeLaunchPolicy] = None,
        logger_: Optional[logging.Logger] = None,
    ) -> None:
        self._bot_app_provider = bot_app_provider or (lambda: bot_app)
        self._logger = logger_ or logging.getLogger(__name__)
        self._policy = policy or self._build_policy_from_runtime()
        self._unsubscribers: list[Any] = []
        self._context_source: Any = None
        self._started = False

    @property
    def bot_app(self) -> Any:
        bot_app = self._bot_app_provider()
        if bot_app is None:
            raise ModeLaunchAdapterError("bot_app runtime is not configured")
        return bot_app

    def _build_policy_from_runtime(self) -> ModeLaunchPolicy:
        bot_app = self.bot_app
        return ModeLaunchPolicy.for_mode_registry(bot_app.mode_registry_service)

    async def start(self, *, application: Any = None) -> None:
        if self._started:
            return
        self._context_source = application
        bus = getattr(self.bot_app, "system_event_bus", None)
        if bus is None:
            raise ModeLaunchAdapterError("system_event_bus is not configured")
        self._unsubscribers.append(bus.subscribe(ScheduledJobEvent, self._handle_scheduled_job_event))
        self._unsubscribers.append(bus.subscribe(WebhookReceivedEvent, self._handle_webhook_received_event))
        self._unsubscribers.append(bus.subscribe(DesktopCommandEvent, self._handle_desktop_command_event))
        self._unsubscribers.append(bus.subscribe(MiniAppCommandEvent, self._handle_miniapp_command_event))
        self._unsubscribers.append(bus.subscribe(ModeLaunchRequestedEvent, self._handle_mode_launch_requested_event))
        self._started = True

    async def stop(self) -> None:
        for unsubscribe in self._unsubscribers:
            try:
                unsubscribe()
            except Exception:
                self._logger.exception("mode launch adapter unsubscribe failed")
        self._unsubscribers = []
        self._context_source = None
        self._started = False

    async def _handle_scheduled_job_event(self, event: ScheduledJobEvent) -> None:
        request = self._build_scheduler_launch_request(event)
        if request is None:
            return
        await self._dispatch_request(event, request)

    async def _handle_webhook_received_event(self, event: WebhookReceivedEvent) -> None:
        request = self._build_webhook_launch_request(event)
        if request is None:
            return
        await self._dispatch_request(event, request)

    async def _handle_desktop_command_event(self, event: DesktopCommandEvent) -> None:
        request = self._build_desktop_launch_request(event)
        if request is None:
            return
        await self._dispatch_request(event, request)

    async def _handle_miniapp_command_event(self, event: MiniAppCommandEvent) -> None:
        request = self._build_miniapp_launch_request(event)
        if request is None:
            return
        await self._dispatch_request(event, request)

    async def _handle_mode_launch_requested_event(self, event: ModeLaunchRequestedEvent) -> None:
        request = self._build_generic_launch_request(event)
        if request is None:
            return
        await self._dispatch_request(event, request)

    async def _dispatch_request(self, event: Any, request: ModeLaunchRequest) -> None:
        self._logger.info(
            "event mode launch received correlation_id=%s origin=%s provider=%s mode_id=%s session_uid=%s dry_run=%s",
            self._correlation_id(request),
            str(request.origin.get("key", "") or ""),
            self._provider(request),
            str(request.mode_id or ""),
            str(request.scope.session_uid or ""),
            self._is_dry_run(request),
        )
        decision = self._policy.decide(
            origin_key=str(request.origin.get("key", "") or ""),
            mode_id=str(request.mode_id or ""),
        )
        if not bool(decision.allowed):
            await self._emit_external_launch_audit(
                request,
                reason=str(decision.reason or ""),
                actor_chat_id=self._security_actor_chat_id(request.actor),
            )
            self._log_deny(request, decision.reason)
            await self._publish_completed(
                request=request,
                status="denied",
                result={"error": str(decision.reason or "")},
            )
            return

        security_denied = await self._authorize_external_launch(request)
        if security_denied is not None:
            self._log_deny(request, security_denied)
            await self._publish_completed(
                request=request,
                status="denied",
                result={"error": security_denied},
            )
            return

        session = self._resolve_session(request.scope.session_uid)
        if session is None:
            self._log_deny(request, "session_not_found")
            await self._publish_completed(
                request=request,
                status="denied",
                result={"error": "session_not_found"},
            )
            return

        mode = self.bot_app.mode_registry_service.get(str(request.mode_id or ""))
        if mode is None:
            self._log_deny(request, "mode_not_found")
            await self._publish_completed(
                request=request,
                status="denied",
                result={"error": "mode_not_found"},
            )
            return

        chat_id, dest, user_id = self._build_delivery_target(session=session, request=request)
        if chat_id is None:
            self._log_deny(request, "chat_id_required")
            await self._publish_completed(
                request=request,
                status="denied",
                result={"error": "chat_id_required"},
            )
            return

        if self._is_dry_run(request):
            self._logger.info(
                (
                    "event mode launch dry_run skipped correlation_id=%s origin=%s "
                    "provider=%s mode_id=%s session_uid=%s project_slug=%s"
                ),
                self._correlation_id(request),
                str(request.origin.get("key", "") or ""),
                self._provider(request),
                str(request.mode_id or ""),
                str(request.scope.session_uid or ""),
                str(request.project.get("slug", "") or ""),
            )
            await self._publish_completed(request=request, status="dry_run", result={})
            return

        context_source = self._build_context_source(event, request)
        if not await self._ensure_mode_enabled(session=session, mode=mode, request=request, context=context_source):
            await self._publish_completed(
                request=request,
                status="enable_failed",
                result={"error": "enable_failed"},
            )
            return

        async def _cli_fallback(_session, _text: str, _chat_id: int, _context: Any) -> None:
            raise ModeLaunchAdapterError("event-driven mode launch unexpectedly fell back to CLI")

        try:
            await self.bot_app.mode_input_router.route_mode_or_cli(
                bot_app=self.bot_app,
                session=session,
                text=str(request.prompt or ""),
                chat_id=chat_id,
                context=context_source,
                dest=dest,
                user_id=user_id,
                cli_fallback=_cli_fallback,
            )
        except Exception:
            self._logger.exception(
                "event mode launch failed correlation_id=%s origin=%s provider=%s mode_id=%s session_uid=%s",
                self._correlation_id(request),
                request.origin.get("key", ""),
                self._provider(request),
                request.mode_id,
                request.scope.session_uid,
            )
            await self._publish_completed(
                request=request,
                status="failed",
                result={"error": "dispatch_failed"},
            )
            raise

        self._logger.info(
            (
                "event mode launch dispatched correlation_id=%s origin=%s "
                "provider=%s mode_id=%s session_uid=%s project_slug=%s"
            ),
            self._correlation_id(request),
            request.origin.get("key", ""),
            self._provider(request),
            request.mode_id,
            request.scope.session_uid,
            request.project.get("slug", ""),
        )
        await self._publish_completed(
            request=request,
            status="dispatched",
            result={},
        )

    async def _ensure_mode_enabled(self, *, session: Any, mode: Any, request: ModeLaunchRequest, context: Any) -> bool:
        target_mode_id = str(request.mode_id or "").strip()
        if str(get_active_mode(session, "") or "").strip() == target_mode_id:
            return True

        chat_id = request.scope.chat_id
        dest = {
            "kind": "telegram" if str(request.scope.kind or "") == "telegram" else str(request.scope.kind or "unknown"),
            "chat_id": int(chat_id) if chat_id is not None else request.scope.session_uid,
        }
        if str(request.scope.kind or "") == "desktop":
            dest["session_id"] = str(request.scope.session_uid or "")
        if request.scope.message_thread_id is not None:
            dest["message_thread_id"] = int(request.scope.message_thread_id)

        try:
            result = await mode.on_enable(
                {
                    "bot_app": self.bot_app,
                    "session": session,
                    "chat_id": chat_id,
                    "context": context,
                    "dest": dest,
                    "mode_id": target_mode_id,
                    "launch_request": request,
                }
            )
        except Exception:
            self._logger.exception(
                "event mode launch on_enable failed mode_id=%s session_uid=%s",
                target_mode_id,
                request.scope.session_uid,
            )
            self._log_deny(request, "enable_failed")
            return False

        if result is not None and not bool(getattr(result, "success", True)):
            self._log_deny(request, "enable_failed")
            return False

        if str(get_active_mode(session, "") or "").strip() != target_mode_id:
            set_active_mode(session, target_mode_id)
            try:
                self.bot_app.manager._persist_sessions()
            except Exception:
                self._logger.exception(
                    (
                        "event mode launch persist after implicit activation failed "
                        "mode_id=%s session_uid=%s"
                    ),
                    target_mode_id,
                    request.scope.session_uid,
                )
        return True

    def _build_delivery_target(
        self,
        *,
        session: Any,
        request: ModeLaunchRequest,
    ) -> tuple[Any, dict[str, Any], int | None]:
        scope = getattr(session, "conversation_scope", None)
        kind = str(request.scope.kind or "").strip() or "unknown"
        chat_id: Any = request.scope.chat_id
        if chat_id is None and isinstance(scope, ConversationScope):
            chat_id = int(scope.chat_id)
        if chat_id is None:
            chat_id = str(request.scope.session_uid or "")
        dest: dict[str, Any] = {
            "kind": "telegram" if kind == "telegram" else kind,
            "chat_id": chat_id,
        }
        if kind == "desktop":
            dest["session_id"] = str(request.scope.session_uid or "")
        if request.scope.message_thread_id is not None:
            dest["message_thread_id"] = int(request.scope.message_thread_id)
        actor_user_id = self._maybe_int(request.actor.get("user_id"))
        if actor_user_id is not None:
            dest["user_id"] = actor_user_id
        return chat_id, dest, actor_user_id

    def _resolve_session(self, session_uid: str) -> Optional[Any]:
        manager = getattr(self.bot_app, "manager", None)
        if manager is None or not hasattr(manager, "get_by_uid"):
            return None
        try:
            return manager.get_by_uid(str(session_uid or ""))
        except Exception:
            self._logger.exception("event mode launch session lookup failed session_uid=%s", session_uid)
            return None

    def _build_context_source(self, event: Any, request: ModeLaunchRequest) -> Any:
        raw_context = self._context_source
        bot = getattr(raw_context, "bot", None)
        return SimpleNamespace(
            bot=bot,
            launch_request=request,
            launch_payload=self._copy_mapping(request.payload),
            system_event=event,
        )

    def _build_scheduler_launch_request(self, event: ScheduledJobEvent) -> Optional[ModeLaunchRequest]:
        payload = self._copy_mapping(event.payload)
        session_uid = str((event.notification_target or {}).get("telegram_session_uid", "") or "").strip()
        if not session_uid:
            self._logger.warning(
                "event mode launch skipped scheduler event without notification target job_id=%s",
                event.job_id,
            )
            return None
        scope = self._parse_scope(session_uid)
        if scope is None:
            self._logger.warning(
                "event mode launch skipped scheduler event with invalid scope job_id=%s session_uid=%s",
                event.job_id,
                session_uid,
            )
            return None
        project_payload = self._copy_mapping(payload.get("project"))
        project_slug = str(payload.get("project_slug") or project_payload.get("slug") or "").strip()
        if project_slug and not str(project_payload.get("slug") or "").strip():
            project_payload["slug"] = project_slug
        prompt = str(payload.get("prompt", "") or "").strip() or str(event.job_name or event.job_id or "").strip()
        owner_id = getattr(event, "owner_id", "")
        owner_value: Any
        owner_token = str(owner_id or "").strip()
        if owner_token and owner_token.lstrip("-").isdigit():
            owner_value = int(owner_token)
        else:
            owner_value = owner_id
        return ModeLaunchRequest(
            scope=scope,
            mode_id=str(event.target_mode or "").strip(),
            project=project_payload,
            payload=payload,
            prompt=prompt,
            actor={"kind": "scheduler", "owner_id": owner_value},
            origin={
                "kind": "scheduler",
                "key": "scheduler",
                "provider": "scheduler",
                "correlation_id": str(event.correlation_id or ""),
                "dry_run": bool(event.dry_run),
                "job_id": str(event.job_id or ""),
                "job_name": str(event.job_name or ""),
                "status": str(event.status or ""),
            },
        )

    def _build_webhook_launch_request(self, event: WebhookReceivedEvent) -> Optional[ModeLaunchRequest]:
        payload = self._copy_mapping(event.payload)
        launch_payload = self._copy_mapping(payload.get("launch"))

        mode_id = str(
            launch_payload.get("mode_id")
            or payload.get("mode_id")
            or ""
        ).strip()
        if not mode_id:
            return None

        notification_target = launch_payload.get("notification_target")
        if not isinstance(notification_target, Mapping):
            notification_target = payload.get("notification_target")
        notification_target_payload = dict(notification_target) if isinstance(notification_target, Mapping) else {}
        session_uid = str(
            launch_payload.get("session_uid")
            or payload.get("session_uid")
            or notification_target_payload.get("telegram_session_uid")
            or ""
        ).strip()
        if not session_uid:
            self._logger.warning(
                "event mode launch skipped webhook event without session_uid source=%s path=%s",
                event.source,
                event.path,
            )
            return None
        scope = self._parse_scope(session_uid)
        if scope is None:
            self._logger.warning(
                "event mode launch skipped webhook event with invalid scope source=%s session_uid=%s",
                event.source,
                session_uid,
            )
            return None

        actor = launch_payload.get("actor")
        if not isinstance(actor, Mapping):
            actor = payload.get("actor")
        actor_payload = self._copy_mapping(actor) if isinstance(actor, Mapping) else {"kind": "webhook"}
        if not actor_payload:
            actor_payload = {"kind": "webhook"}
        if not str(actor_payload.get("kind") or "").strip():
            actor_payload["kind"] = "webhook"
        for key in ("actor_id", "user_id", "chat_id", "owner_id"):
            if actor_payload.get(key) not in (None, ""):
                continue
            raw_value = launch_payload.get(key)
            if raw_value in (None, ""):
                raw_value = payload.get(key)
            if raw_value not in (None, ""):
                actor_payload[key] = raw_value

        project = launch_payload.get("project")
        if not isinstance(project, Mapping):
            project = payload.get("project")
        project_payload = self._copy_mapping(project)
        project_slug = str(
            launch_payload.get("project_slug")
            or payload.get("project_slug")
            or project_payload.get("slug")
            or ""
        ).strip()
        if project_slug and not project_payload:
            project_payload = {"slug": project_slug}

        prompt = str(launch_payload.get("prompt") or payload.get("prompt") or "").strip()
        if not prompt:
            prompt = str(launch_payload.get("body") or payload.get("body") or "").strip()
        if not prompt:
            prompt = str(payload.get("message") or "").strip()
        if not prompt:
            prompt = str(event.source or "").strip()

        return ModeLaunchRequest(
            scope=scope,
            mode_id=mode_id,
            project=project_payload,
            payload=payload,
            prompt=prompt,
            actor=actor_payload,
            origin={
                "kind": "webhook",
                "key": f"webhook:{str(event.source or '').strip() or '*'}",
                "provider": str(event.source or ""),
                "correlation_id": str(event.correlation_id or ""),
                "dry_run": bool(event.dry_run),
                "source": str(event.source or ""),
                "path": str(event.path or ""),
                "method": str(event.method or ""),
            },
        )

    def _build_desktop_launch_request(self, event: DesktopCommandEvent) -> Optional[ModeLaunchRequest]:
        payload = self._copy_mapping(event.payload)
        mode_id = str(payload.get("mode_id") or event.command or "").strip()
        if not mode_id:
            return None
        return ModeLaunchRequest(
            scope=ModeLaunchScope(kind="desktop", session_uid=str(event.session_uid or "")),
            mode_id=mode_id,
            project={"slug": str(event.project_slug or "").strip()} if str(event.project_slug or "").strip() else {},
            payload=payload,
            prompt=str(payload.get("prompt") or payload.get("text") or "").strip(),
            actor=dict(payload.get("actor") or {"kind": "desktop"}),
            origin={
                "kind": "desktop",
                "key": "desktop",
                "provider": "desktop",
                "correlation_id": str(event.correlation_id or ""),
                "dry_run": bool(payload.get("dry_run", False)),
            },
        )

    def _build_miniapp_launch_request(self, event: MiniAppCommandEvent) -> Optional[ModeLaunchRequest]:
        payload = self._copy_mapping(event.payload)
        mode_id = str(payload.get("mode_id") or event.command or "").strip()
        if not mode_id:
            return None
        actor_payload = self._copy_mapping(payload.get("actor"))
        if not actor_payload:
            actor_payload = {"kind": "miniapp"}
        if not str(actor_payload.get("kind") or "").strip():
            actor_payload["kind"] = "miniapp"
        event_actor_id = str(event.user_id or "").strip()
        if event_actor_id and not str(actor_payload.get("actor_id") or "").strip():
            actor_payload["actor_id"] = event_actor_id
        if actor_payload.get("user_id") in (None, ""):
            event_actor_chat_id = self._actor_chat_id_from_actor_id(event_actor_id)
            if event_actor_chat_id is not None:
                actor_payload["user_id"] = event_actor_chat_id
        return ModeLaunchRequest(
            scope=ModeLaunchScope(kind="miniapp", session_uid=str(event.session_uid or "")),
            mode_id=mode_id,
            project={"slug": str(event.project_slug or "").strip()} if str(event.project_slug or "").strip() else {},
            payload=payload,
            prompt=str(payload.get("prompt") or payload.get("text") or "").strip(),
            actor=actor_payload,
            origin={
                "kind": "miniapp",
                "key": "miniapp",
                "provider": "miniapp",
                "correlation_id": str(event.correlation_id or ""),
                "dry_run": bool(payload.get("dry_run", False)),
            },
        )

    def _build_generic_launch_request(self, event: ModeLaunchRequestedEvent) -> Optional[ModeLaunchRequest]:
        scope = self._parse_scope(event.session_uid)
        if scope is None:
            return None
        return ModeLaunchRequest(
            scope=scope,
            mode_id=str(event.mode_id or "").strip(),
            project={"slug": str(event.project_slug or "").strip()} if str(event.project_slug or "").strip() else {},
            payload=self._copy_mapping(event.payload),
            prompt=str(event.prompt or "").strip(),
            actor=dict(event.actor or {}),
            origin={
                "kind": str(event.origin or "").strip() or "system",
                "key": str(event.origin or "").strip() or "system",
                "provider": str(event.origin or "").strip() or "system",
                "correlation_id": str(event.correlation_id or ""),
                "dry_run": bool(event.dry_run),
            },
        )

    @staticmethod
    def _parse_scope(session_uid: str) -> Optional[ModeLaunchScope]:
        token = str(session_uid or "").strip()
        if not token:
            return None
        if token.startswith("desktop:"):
            return ModeLaunchScope(kind="desktop", session_uid=token)
        if token.startswith("miniapp:"):
            return ModeLaunchScope(kind="miniapp", session_uid=token)
        if token.startswith("thread:"):
            parts = token.split(":", 2)
            if len(parts) != 3:
                return None
            try:
                return ModeLaunchScope(
                    kind="telegram",
                    session_uid=token,
                    chat_id=int(parts[1]),
                    message_thread_id=int(parts[2]),
                )
            except Exception:
                return None
        if token.startswith("chat:"):
            parts = token.split(":", 2)
            if len(parts) < 2:
                return None
            try:
                return ModeLaunchScope(
                    kind="telegram",
                    session_uid=token,
                    chat_id=int(parts[1]),
                    message_thread_id=None,
                )
            except Exception:
                return None
        parts = token.split(":", 1)
        if len(parts) == 2 and parts[0].lstrip("-").isdigit():
            return ModeLaunchScope(kind="telegram", session_uid=token, chat_id=int(parts[0]))
        return ModeLaunchScope(kind="unknown", session_uid=token)

    def _log_deny(self, request: ModeLaunchRequest, reason: str) -> None:
        self._logger.warning(
            (
                "event mode launch denied reason=%s correlation_id=%s origin=%s "
                "provider=%s mode_id=%s session_uid=%s project_slug=%s"
            ),
            str(reason or "").strip() or "unknown",
            self._correlation_id(request),
            str(request.origin.get("key", "") or ""),
            self._provider(request),
            str(request.mode_id or ""),
            str(request.scope.session_uid or ""),
            str(request.project.get("slug", "") or ""),
        )

    @staticmethod
    def _provider(request: ModeLaunchRequest) -> str:
        return str(request.origin.get("provider", "") or request.origin.get("kind", "") or "").strip()

    @staticmethod
    def _correlation_id(request: ModeLaunchRequest) -> str:
        return str(request.origin.get("correlation_id", "") or "").strip()

    @staticmethod
    def _is_dry_run(request: ModeLaunchRequest) -> bool:
        return bool(request.origin.get("dry_run", False))

    async def _authorize_external_launch(self, request: ModeLaunchRequest) -> str | None:
        security = getattr(self.bot_app, "security", None)
        if security is None or not hasattr(security, "authorize_mode_launch"):
            return None
        is_mode_allowed = True
        actor_chat_id = self._security_actor_chat_id(request.actor)
        if self._is_desktop_request(request):
            desktop_policy = self._copy_mapping(request.payload.get("launch_policy"))
            actor_chat_id = self._maybe_int(desktop_policy.get("actor_chat_id"))
            if actor_chat_id is None:
                await self._emit_external_launch_audit(
                    request,
                    reason=str(desktop_policy.get("reason") or "actor_unresolved"),
                    actor_chat_id=0,
                )
                return str(desktop_policy.get("reason") or "actor_unresolved")
            is_mode_allowed = bool(desktop_policy.get("is_mode_allowed", False))
        if self._is_miniapp_request(request):
            actor_chat_id = self._actor_chat_id_from_actor_id(str(request.actor.get("actor_id") or "").strip())
            if actor_chat_id is None:
                await self._emit_external_launch_audit(
                    request,
                    reason="actor_unresolved",
                    actor_chat_id=0,
                )
                return "actor_unresolved"
            policy = getattr(self.bot_app, "access_policy_service", None)
            if policy is not None and hasattr(policy, "is_mode_allowed_for_chat"):
                try:
                    is_mode_allowed = bool(policy.is_mode_allowed_for_chat(actor_chat_id, str(request.mode_id or "")))
                except Exception:
                    self._logger.exception(
                        "event mode launch policy check failed mode_id=%s session_uid=%s actor_id=%s",
                        request.mode_id,
                        request.scope.session_uid,
                        str(request.actor.get("actor_id") or ""),
                    )
                    return "security_error"
        if self._is_webhook_request(request):
            if actor_chat_id <= 0:
                actor_chat_id = self._actor_chat_id_from_actor_id(str(request.actor.get("actor_id") or "").strip()) or 0
            if actor_chat_id <= 0:
                await self._emit_external_launch_audit(
                    request,
                    reason="actor_unresolved",
                    actor_chat_id=0,
                )
                return "actor_unresolved"
            policy = getattr(self.bot_app, "access_policy_service", None)
            if policy is not None and hasattr(policy, "is_mode_allowed_for_chat"):
                try:
                    is_mode_allowed = bool(policy.is_mode_allowed_for_chat(actor_chat_id, str(request.mode_id or "")))
                except Exception:
                    self._logger.exception(
                        "event mode launch policy check failed mode_id=%s session_uid=%s actor=%s",
                        request.mode_id,
                        request.scope.session_uid,
                        dict(request.actor or {}),
                    )
                    return "security_error"
        try:
            decision = await security.authorize_mode_launch(
                actor_chat_id,
                mode_id=str(request.mode_id or ""),
                is_mode_allowed=bool(is_mode_allowed),
                action="event_launch",
                session_id=str(request.scope.session_uid or ""),
                context={
                    "actor": dict(request.actor or {}),
                    "actor_id": str(
                        request.actor.get("actor_id")
                        or request.actor.get("owner_id")
                        or request.actor.get("user_id")
                        or request.actor.get("chat_id")
                        or ""
                    ).strip(),
                    "origin": str(request.origin.get("key", "") or ""),
                    "project_slug": str(request.project.get("slug", "") or ""),
                },
            )
        except Exception:
            self._logger.exception(
                "event mode launch security check failed mode_id=%s session_uid=%s",
                request.mode_id,
                request.scope.session_uid,
            )
            return "security_error"
        if bool(getattr(decision, "allowed", False)):
            return None
        decision_reason = str(getattr(decision, "reason", "") or "security_denied")
        if (
            self._is_miniapp_request(request) or self._is_webhook_request(request)
        ) and decision_reason == DenyReasonCode.MODE_NOT_ALLOWED:
            return "mode_not_allowlisted"
        return decision_reason

    async def _publish_completed(
        self,
        *,
        request: ModeLaunchRequest,
        status: str,
        result: dict[str, Any],
    ) -> None:
        bus = getattr(self.bot_app, "system_event_bus", None)
        if bus is None or not hasattr(bus, "publish"):
            return
        payload = {
            "job_id": str(request.origin.get("job_id", "") or ""),
            "provider": str(request.origin.get("provider", "") or ""),
        }
        await bus.publish(
            ModeLaunchCompletedEvent(
                origin=str(request.origin.get("kind", "") or request.origin.get("key", "") or ""),
                mode_id=str(request.mode_id or ""),
                session_uid=str(request.scope.session_uid or ""),
                project_slug=str(request.project.get("slug", "") or ""),
                correlation_id=str(request.origin.get("correlation_id", "") or ""),
                status=str(status or ""),
                result=dict(result or {}),
                payload=payload,
            )
        )

    @staticmethod
    def _security_actor_chat_id(actor: Mapping[str, Any]) -> int:
        for key in ("user_id", "chat_id", "owner_id"):
            value = actor.get(key)
            if value is None:
                continue
            parsed = ModeLaunchAdapterService._maybe_int(value)
            if parsed is not None:
                return parsed
            token = str(value or "").strip()
            if ":" in token:
                parsed = ModeLaunchAdapterService._maybe_int(token.rsplit(":", 1)[-1])
                if parsed is not None:
                    return parsed
        return 0

    @staticmethod
    def _actor_chat_id_from_actor_id(actor_id: Any) -> int | None:
        token = str(actor_id or "").strip()
        if not token:
            return None
        if ":" not in token:
            return ModeLaunchAdapterService._maybe_int(token)
        return ModeLaunchAdapterService._maybe_int(token.rsplit(":", 1)[-1])

    @staticmethod
    def _is_miniapp_request(request: ModeLaunchRequest) -> bool:
        return str(request.origin.get("kind", "") or request.origin.get("key", "") or "").strip() == "miniapp"

    @staticmethod
    def _is_webhook_request(request: ModeLaunchRequest) -> bool:
        return str(request.origin.get("kind", "") or request.origin.get("key", "") or "").strip() == "webhook"

    @staticmethod
    def _is_desktop_request(request: ModeLaunchRequest) -> bool:
        return str(request.origin.get("kind", "") or request.origin.get("key", "") or "").strip() == "desktop"

    async def _emit_external_launch_audit(
        self,
        request: ModeLaunchRequest,
        *,
        reason: str,
        actor_chat_id: int,
    ) -> None:
        security = getattr(self.bot_app, "security", None)
        if security is None or not hasattr(security, "emit_audit"):
            return
        actor_id = str(
            request.actor.get("actor_id")
            or request.actor.get("owner_id")
            or request.actor.get("user_id")
            or request.actor.get("chat_id")
            or ""
        ).strip()
        try:
            await security.emit_audit(
                category="mode_launch",
                action="event_launch",
                status="denied",
                user_id=actor_id or int(actor_chat_id or 0),
                subject=str(request.mode_id or ""),
                scope=f"mode.launch.{str(request.mode_id or '').strip() or 'unknown'}",
                reason=str(reason or ""),
                context={
                    "actor": dict(request.actor or {}),
                    "actor_id": actor_id,
                    "chat_id": int(actor_chat_id or 0),
                    "mode_id": str(request.mode_id or ""),
                    "origin": str(request.origin.get("key", "") or ""),
                    "project_slug": str(request.project.get("slug", "") or ""),
                    "session_id": str(request.scope.session_uid or ""),
                },
                details={
                    "action": "event_launch",
                    "actor_id": actor_id,
                    "allowed": False,
                    "chat_id": int(actor_chat_id or 0),
                    "mode_id": str(request.mode_id or ""),
                    "session_id": str(request.scope.session_uid or ""),
                },
            )
        except Exception:
            self._logger.exception(
                "event mode launch audit emit failed reason=%s mode_id=%s session_uid=%s",
                str(reason or ""),
                request.mode_id,
                request.scope.session_uid,
            )

    @staticmethod
    def _maybe_int(value: Any) -> int | None:
        try:
            return int(value)
        except Exception:
            return None

    @staticmethod
    def _copy_mapping(payload: Any) -> dict[str, Any]:
        return copy.deepcopy(dict(payload)) if isinstance(payload, Mapping) else {}
