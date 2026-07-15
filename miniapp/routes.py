import asyncio
import base64
import datetime
import hashlib
import hmac
import inspect
import json
import logging
import mimetypes
import os
import re
import secrets
import time
from collections import deque
from types import SimpleNamespace
from typing import Any, Dict, List, Optional
from urllib.parse import quote

from aiohttp import web

from app.events import MiniAppCommandEvent
from app.security import DenyReasonCode, SecurityRateLimitError, serialize_security_error
from app.services.config_service import ConfigService
from app.services.admin_config_service import AdminConfigService
from app.services.actor_identity import miniapp_actor_id
from app.services.telegram_ui_scope import TelegramUiKey
from app.services.session_tick_history_store import load_session_ticks
from app.services.runtime_progress_service import build_runtime_progress_payload
from app.services.run_artifact_store import is_terminal_status
from app.services.run_utils import clean_text as clean_run_listing_text, summarize_run_skill_log
from app.services.run_operations_policy import RunOperationsPolicy
from i18n import t
from i18n.resolver import SUPPORTED_LANGS
from modes.sdk.runtime.json_normalizer import loads_safe
from modes.sdk.session_busy import is_session_busy
from modes.analyst.state_store import AnalystStateStore, build_context_key
from modes.agent.mode import agent_project_scope_key, agent_project_session_key, normalize_agent_project_pending_entry
from modes.analyst.ui import build_analyst_status_payload, build_analyst_status_text
from modes.agent.ui import build_agent_status_payload, build_agent_status_text
from modes.sdk import CallbackModel, decode_mode_dirs
from modes.sdk.planning import format_manager_status_brief, load_plan
from modes.sdk.services import ModeStatusService
from modes.webmaster.state_store import WebmasterStateStore, build_user_key
from session import session_runtime_uid, session_scoped_key
from sessions.session_state_access import (
    get_active_mode,
    get_orchestrator_last_mode_id,
    get_orchestrator_last_mode_output,
    get_orchestrator_pending_input,
    is_orchestrator_enabled,
)
from sessions.session_status import build_session_status_text
from utils.paths import cli_proxy_artifact_path
from utils.ui import format_session_selector_label
from app.services.session_files_service import (
    FilesServiceError,
    RevisionConflictError,
    SessionFilesService,
)
from app.services.scheduler_presentation_service import SchedulerPresentationService
from .services.logs_service import LogsService, LogsServiceError
from .route_context import MiniAppRouteContext
from .routes_admin import AdminRouteServices, register_admin_routes
from .routes_config import ConfigRouteServices, register_config_routes
from .routes_foundation import FoundationRouteServices, register_foundation_routes
from .routes_json import JsonRouteServices, register_json_routes
from .routes_logs import LogsRouteServices, register_logs_routes
from .routes_scheduler import SchedulerRouteServices, register_scheduler_routes
from .routes_ssh import SshRouteServices, register_ssh_routes
from .routes_reports import ReportsRouteServices, register_reports_routes
from .routes_tasks import TasksRouteServices, register_tasks_routes

logger = logging.getLogger("miniapp")


class MiniAppRoutes:
    _CHAT_SESSION_PAIR_RE = re.compile(r"^-?\d+:[^:\s]+$")

    @staticmethod
    def _container_config_service(bot_app: Any) -> ConfigService:
        container = getattr(bot_app, "container", None)
        config_service = getattr(container, "config_service", None)
        if config_service is None:
            raise RuntimeError("ApplicationContainer.config_service is required for MiniApp config routes")
        return config_service

    def __init__(self, bot_app: Any):
        self.bot_app = bot_app
        self.route_context = MiniAppRouteContext(bot_app=bot_app, logger=logger)
        self.foundation_route_services = FoundationRouteServices()
        self.json_route_services = JsonRouteServices()
        self.run_operations_policy = RunOperationsPolicy()
        self.config_route_services = ConfigRouteServices(
            config_service=self._container_config_service(bot_app),
            require_admin=self._require_admin,
            read_json_object=self._read_json_object,
            json_error=self._json_error,
        )
        self.admin_route_services = AdminRouteServices(
            admin_config_service=AdminConfigService(bot_app),
            require_admin=self._require_admin,
            read_json_object=self._read_json_object,
            json_error=self._json_error,
        )
        self.scheduler_presentation_service = SchedulerPresentationService(self._scheduler_service)
        self.scheduler_route_services = SchedulerRouteServices(
            scheduler_service=self._scheduler_service,
            presentation_service=self.scheduler_presentation_service,
            require_access=self._require_access,
            read_json_object=self._read_json_object,
            require_object_body=self._require_object_body,
            json_error=self._json_error,
            list_owned_projects=self._list_owned_projects,
            require_owned_project=self._require_owned_project,
            list_notification_targets=self._list_scheduler_notification_targets,
            require_notification_target=self._require_project_notification_target,
        )
        self.files = SessionFilesService(bot_app)
        self.logs = LogsService(bot_app)
        self._ws_ticket_ttl_sec = 60
        self._status_poll_interval_sec = 1.0
        self.logs_route_services = LogsRouteServices(
            logs=self.logs,
            require_access=self._require_access,
            json_error=self._json_error,
            issue_ws_ticket=self._issue_ws_ticket,
            consume_ws_ticket=self._consume_ws_ticket,
            validate_session_uid=self._validate_session_uid_input,
            consume_ws_messages=self._consume_ws_messages,
            ws_ticket_ttl_sec=self._ws_ticket_ttl_sec,
        )
        self.ssh_route_services = SshRouteServices(
            require_access=self._require_access,
            require_admin=self._require_admin,
            read_json_object=self._read_json_object,
            json_error=self._json_error,
        )
        self.tasks_route_services = TasksRouteServices(
            require_access=self._require_access,
            json_error=self._json_error,
        )
        self.reports_route_services = ReportsRouteServices(
            require_access=self._require_access,
            json_error=self._json_error,
        )

    async def _json_error(self, status: int, message: Any) -> web.Response:
        payload = {"ok": False, "error": str(message or "")}
        if isinstance(message, BaseException):
            payload["security"] = serialize_security_error(message)
            payload["error"] = str(payload["security"].get("message") or payload["error"])
        return web.json_response(payload, status=status)

    @staticmethod
    def _extract_init_data(request: web.Request) -> str:
        return str(request.headers.get("X-Telegram-Init-Data", "") or "").strip()

    async def _require_access(self, request: web.Request) -> Dict[str, Any]:
        return await self._require_access_real(request)

    async def _require_access_real(self, request: web.Request) -> Dict[str, Any]:
        init_data = self._extract_init_data(request)
        if not init_data:
            raise web.HTTPUnauthorized(reason="missing X-Telegram-Init-Data")

        auth_result = self.bot_app.security.authenticate(
            {"init_data": init_data},
            strategy="telegram_init_data",
        )
        if not auth_result.authenticated:
            logger.warning(
                "miniapp auth fail",
                extra={
                    "chat_id": 0,
                    "user_id": 0,
                    "action": "auth",
                    "path": request.path,
                    "status": "fail",
                    "error": str(auth_result.reason or DenyReasonCode.INVALID_INIT_DATA),
                },
            )
            raise web.HTTPUnauthorized(reason="invalid initData")

        claims = dict(auth_result.claims or {})
        try:
            user_id = int(claims.get("user_id"))
        except Exception as exc:
            logger.warning(
                "miniapp auth fail",
                extra={
                    "chat_id": 0,
                    "user_id": 0,
                    "action": "auth",
                    "path": request.path,
                    "status": "fail",
                    "error": "invalid user_id claim",
                },
            )
            raise web.HTTPUnauthorized(reason="invalid initData") from exc

        actor_id = miniapp_actor_id(user_id)
        consume_rate_limit = getattr(self.bot_app.security, "consume_rate_limit", None)
        try:
            rate_decision = (
                consume_rate_limit(
                    "miniapp.ingress",
                    actor_id,
                    limit=120,
                    window_sec=60,
                    burst_limit=30,
                    burst_window_sec=10,
                )
                if callable(consume_rate_limit)
                else None
            )
        except ValueError:
            rate_decision = None
        if rate_decision is not None and not rate_decision.allowed:
            emit_audit = getattr(self.bot_app.security, "emit_audit", None)
            if callable(emit_audit):
                await emit_audit(
                    category="rate_limit_denied",
                    action="miniapp_access",
                    status="denied",
                    user_id=actor_id,
                    subject=request.path,
                    scope="miniapp.ingress",
                    reason=str(rate_decision.reason or ""),
                    context={"path": request.path},
                    details=rate_decision.__dict__,
                )
            raise SecurityRateLimitError(
                str(rate_decision.reason or DenyReasonCode.WINDOW_LIMIT_EXCEEDED),
                "miniapp rate limit exceeded",
                details={"path": request.path},
            )

        decision = self.bot_app.security.authorize(user_id, scope="miniapp")
        if not decision.allowed:
            emit_audit = getattr(self.bot_app.security, "emit_audit", None)
            if callable(emit_audit):
                await emit_audit(
                    category="auth",
                    action="miniapp_access",
                    status="denied",
                    user_id=actor_id,
                    subject=request.path,
                    scope="miniapp",
                    reason=str(decision.reason or DenyReasonCode.NOT_ALLOWED),
                    context={"path": request.path},
                    details={"allowed": False},
                )
            logger.warning(
                "miniapp auth forbidden",
                extra={
                    "chat_id": user_id,
                    "user_id": user_id,
                    "action": "auth",
                    "path": request.path,
                    "status": "forbidden",
                    "error": str(decision.reason or DenyReasonCode.NOT_ALLOWED),
                },
            )
            raise web.HTTPForbidden(reason="access denied")

        is_admin = bool(decision.is_admin)
        logger.info(
            "miniapp auth ok",
            extra={
                "chat_id": user_id,
                "user_id": user_id,
                "action": "auth",
                "path": request.path,
                "status": "ok_admin" if is_admin else "ok_user",
                "error": "",
            },
        )
        return {
            "user_id": user_id,
            "actor_id": actor_id,
            "is_admin": is_admin,
            "username": str(claims.get("username") or ""),
            "first_name": str(claims.get("first_name") or ""),
            "language_code": str(claims.get("language_code") or ""),
        }

    def _ws_ticket_secret(self) -> bytes:
        token = str(getattr(self.bot_app.config.telegram, "token", "") or "")
        return hmac.new(token.encode("utf-8"), b"miniapp-ws-ticket", hashlib.sha256).digest()

    def _issue_ws_ticket(self, user: Dict[str, Any]) -> str:
        payload = {
            "uid": int(user["user_id"]),
            "adm": 1 if bool(user.get("is_admin", False)) else 0,
            "exp": int(time.time()) + int(self._ws_ticket_ttl_sec),
            "nonce": secrets.token_urlsafe(8),
        }
        raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
        body = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
        signature = hmac.new(self._ws_ticket_secret(), body.encode("utf-8"), hashlib.sha256).hexdigest()
        return f"{body}.{signature}"

    def _consume_ws_ticket(self, token: str) -> Dict[str, Any]:
        key = str(token or "").strip()
        if not key:
            raise web.HTTPUnauthorized(reason="missing ws ticket")
        if "." not in key:
            raise web.HTTPUnauthorized(reason="invalid ws ticket")
        body, signature = key.rsplit(".", 1)
        expected = hmac.new(self._ws_ticket_secret(), body.encode("utf-8"), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            raise web.HTTPUnauthorized(reason="invalid ws ticket")
        try:
            padded = body + "=" * (-len(body) % 4)
            payload_raw = base64.urlsafe_b64decode(padded.encode("ascii"))
            payload = loads_safe(payload_raw.decode("utf-8"), strict_first=True)
        except Exception as exc:
            raise web.HTTPUnauthorized(reason="invalid ws ticket") from exc
        exp = int(payload.get("exp") or 0)
        if exp <= int(time.time()):
            raise web.HTTPUnauthorized(reason="expired ws ticket")
        return {
            "user_id": int(payload.get("uid")),
            "is_admin": bool(int(payload.get("adm") or 0)),
            "username": "",
            "first_name": "",
        }

    @staticmethod
    def _attachment_disposition(filename: str) -> str:
        raw = str(filename or "").strip() or "download.txt"
        fallback = re.sub(r"[^A-Za-z0-9._-]+", "_", raw).strip("._")
        if not fallback:
            suffix = re.sub(r"[^A-Za-z0-9.]+", "", os.path.splitext(raw)[1] or "")
            fallback = f"download{suffix}" if suffix else "download.txt"
        encoded = quote(raw, safe="")
        return f"attachment; filename=\"{fallback}\"; filename*=UTF-8''{encoded}"

    async def _require_admin(self, request: web.Request) -> Dict[str, Any]:
        user = await self._require_access(request)
        decision = self.bot_app.security.authorize(int(user["user_id"]), scope="miniapp.admin", require_admin=True)
        if decision.allowed:
            user["is_admin"] = True
            return user
        logger.warning(
            "miniapp admin endpoint forbidden",
            extra={
                "chat_id": int(user["user_id"]),
                "user_id": int(user["user_id"]),
                "action": "admin_guard",
                "path": request.path,
                "status": "forbidden",
                "error": str(decision.reason or DenyReasonCode.ADMIN_REQUIRED),
            },
        )
        raise web.HTTPForbidden(reason="admin required")

    @staticmethod
    def _session_owner_chat_id(session: Any) -> int | None:
        raw_chat_id = getattr(session, "chat_id", None)
        if raw_chat_id not in (None, ""):
            try:
                return int(raw_chat_id)
            except Exception:
                return None
        scope = getattr(session, "conversation_scope", None)
        try:
            return int(getattr(scope, "chat_id"))
        except Exception:
            return None

    def _mode_registry(self) -> Any:
        return getattr(self.bot_app, "mode_registry_service", None) or getattr(self.bot_app, "mode_registry", None)

    def _mode_plugin(self, mode_id: str) -> Any:
        registry = self._mode_registry()
        getter = getattr(registry, "get", None)
        if not callable(getter):
            return None
        try:
            return getter(str(mode_id or "").strip())
        except Exception:
            logger.exception("miniapp settings failed to resolve mode plugin")
            return None

    def _list_mode_items(self) -> List[Dict[str, str]]:
        registry = self._mode_registry()
        if registry is None or not hasattr(registry, "list_modes"):
            return []
        try:
            return [
                {"id": str(mode_id or ""), "label": str(label or "")}
                for mode_id, label in list(registry.list_modes() or [])
            ]
        except Exception:
            logger.exception("miniapp settings failed to list modes")
            return []

    def _available_mode_items_for_user(
        self,
        mode_items: List[Dict[str, str]],
        *,
        chat_id: int,
        is_admin: bool,
    ) -> List[Dict[str, str]]:
        if bool(is_admin):
            return mode_items
        policy = getattr(self.bot_app, "access_policy_service", None)
        checker = getattr(policy, "is_mode_allowed_for_chat", None) if policy is not None else None
        if not callable(checker):
            return mode_items
        out: List[Dict[str, str]] = []
        for item in mode_items:
            mode_id = str(item.get("id") or "").strip()
            if not mode_id:
                continue
            try:
                if bool(checker(int(chat_id), mode_id)):
                    out.append(item)
            except Exception:
                logger.exception("miniapp settings failed to check mode access")
        return out

    def _is_direct_cli_allowed_for_user(self, *, chat_id: int, is_admin: bool) -> bool:
        if bool(is_admin):
            return True
        policy = getattr(self.bot_app, "access_policy_service", None)
        direct_checker = getattr(policy, "is_direct_cli_allowed_for_chat", None) if policy is not None else None
        if callable(direct_checker):
            try:
                return bool(direct_checker(int(chat_id)))
            except Exception:
                logger.exception("miniapp failed to check direct cli access")
                return False
        mode_checker = getattr(policy, "is_mode_allowed_for_chat", None) if policy is not None else None
        if callable(mode_checker):
            try:
                return bool(mode_checker(int(chat_id), "direct_cli"))
            except Exception:
                logger.exception("miniapp failed to check direct cli mode access")
                return False
        return True

    @staticmethod
    def _session_busy_for_mode_change(session: Any) -> bool:
        if session is None:
            return False
        run_lock = getattr(session, "run_lock", None)
        queue_len = len(getattr(session, "queue", []) or [])
        return bool(is_session_busy(session, run_lock) or queue_len > 0)

    def _mode_enable_preflight_error(self, mode: Any, session: Any) -> str:
        checker = getattr(mode, "_enable_requirements_error", None)
        if not callable(checker):
            mode_id = str(getattr(mode, "mode_id", "") or "").strip()
            if mode_id not in {"agent", "analyst", "manager", "webmaster"}:
                return ""
            defaults = getattr(getattr(self.bot_app, "config", None), "defaults", None)
            if not getattr(defaults, "openai_api_key", None) or not getattr(defaults, "openai_model", None):
                return "ERR_OPENAI_REQUIRED"
            workdir = str(getattr(session, "workdir", "") or "")
            if not workdir or not os.path.isdir(workdir):
                return "ERR_SESSION_WORKDIR_MISSING"
            return ""
        try:
            raw = str(checker(self.bot_app, session) or "").strip()
            return t(raw, "ru") if raw else ""
        except Exception:
            logger.exception("miniapp settings mode enable preflight failed")
            return "active_mode enable failed"

    @staticmethod
    def _snapshot_mode_runtime_state(session: Any, active_mode: str) -> Dict[str, Any]:
        cli = getattr(session, "cli", None)
        return {
            "active_mode": str(active_mode or "").strip(),
            "cli_work_type": getattr(session, "cli_work_type", None),
            "executor_profile": getattr(session, "executor_profile", None),
            "nested_cli": cli,
            "nested_cli_work_type": getattr(cli, "cli_work_type", None) if cli is not None else None,
        }

    @staticmethod
    def _restore_mode_runtime_state(session: Any, snapshot: Dict[str, Any]) -> None:
        from sessions.session_state_access import set_active_mode

        active_mode = str((snapshot or {}).get("active_mode") or "").strip()
        set_active_mode(session, active_mode or None)
        setattr(session, "cli_work_type", (snapshot or {}).get("cli_work_type"))
        setattr(session, "executor_profile", (snapshot or {}).get("executor_profile"))
        cli = (snapshot or {}).get("nested_cli")
        if cli is not None:
            try:
                cli.cli_work_type = (snapshot or {}).get("nested_cli_work_type")
            except Exception:
                setattr(session, "cli_work_type", (snapshot or {}).get("nested_cli_work_type"))

    def _persist_session_if_possible(self, session: Any) -> None:
        owner_chat_id = self._session_owner_chat_id(session)
        manager = getattr(self.bot_app, "manager", None)
        session_id = str(getattr(session, "id", "") or "")
        if manager is None or owner_chat_id is None or not session_id:
            return
        try:
            manager.persist_session(int(owner_chat_id), session_id)
        except Exception:
            logger.exception("miniapp settings failed to persist restored mode state")

    @classmethod
    def _snapshot_session_settings_state(cls, session: Any) -> Dict[str, Any]:
        from sessions.session_state_access import (
            get_active_mode,
            get_remote_control_host_alias,
            is_remote_control_enabled,
            is_ssh_remote_enabled,
        )

        active_mode = str(get_active_mode(session, "") or "").strip()
        return {
            "mode_runtime": cls._snapshot_mode_runtime_state(session, active_mode),
            "ssh_remote_enabled": is_ssh_remote_enabled(session),
            "remote_control_enabled": is_remote_control_enabled(session),
            "remote_control_host_alias": get_remote_control_host_alias(session),
        }

    @classmethod
    def _restore_session_settings_state(cls, session: Any, snapshot: Dict[str, Any]) -> None:
        from sessions.session_state_access import (
            set_remote_control_enabled,
            set_remote_control_host_alias,
            set_ssh_remote_enabled,
        )

        cls._restore_mode_runtime_state(session, dict((snapshot or {}).get("mode_runtime") or {}))
        set_ssh_remote_enabled(session, bool((snapshot or {}).get("ssh_remote_enabled")))
        set_remote_control_host_alias(session, (snapshot or {}).get("remote_control_host_alias"))
        set_remote_control_enabled(session, bool((snapshot or {}).get("remote_control_enabled")))

    def _rollback_session_settings_state(self, session: Any, snapshot: Dict[str, Any]) -> None:
        self._restore_session_settings_state(session, snapshot)
        self._persist_session_if_possible(session)

    @staticmethod
    def _remote_control_validation_session(session: Any, *, ssh_remote_enabled: bool) -> Any:
        proxy = SimpleNamespace(
            busy=getattr(session, "busy", False),
            run_lock=getattr(session, "run_lock", None),
            modes=SimpleNamespace(ssh_remote_enabled=bool(ssh_remote_enabled)),
        )
        is_active_by_tick = getattr(session, "is_active_by_tick", None)
        if callable(is_active_by_tick):
            proxy.is_active_by_tick = is_active_by_tick
        return proxy

    def _remote_control_preflight_failure_response(
        self,
        *,
        session: Any,
        actor: str,
        admin_override: bool,
        host_alias: str,
        host_cfg: Any,
        preflight: Any,
        changed: List[str],
    ) -> web.Response:
        reason = str(getattr(preflight, "error", "") or "")
        response_payload: Dict[str, Any] = {
            "ok": False,
            "changed": list(changed),
            "preflight": {
                "ok": False,
                "host_alias": getattr(preflight, "host_alias", host_alias),
                "remote_project_root": getattr(preflight, "remote_project_root", ""),
                "checked_at": getattr(preflight, "checked_at", None),
                "error": reason,
            },
        }
        self._log_remote_control_audit(
            session=session,
            actor=actor,
            surface="miniapp",
            action="remote_control_preflight_failed",
            host_alias=host_alias,
            host_cfg=host_cfg,
            result="error",
            reason=reason,
        )
        if admin_override:
            self._log_remote_control_audit(
                session=session,
                actor=actor,
                surface="miniapp",
                action="admin_remote_override",
                host_alias=host_alias,
                host_cfg=host_cfg,
                result="error",
                reason=reason,
            )
        return web.json_response(response_payload, status=409)

    async def _apply_active_mode_setting(
        self,
        *,
        session: Any,
        mode_id: str,
        actor_chat_id: int,
        is_admin: bool,
        allow_empty_noop: bool = False,
    ) -> web.Response | None:
        from sessions.session_state_access import get_active_mode, set_active_mode

        requested = str(mode_id or "").strip()
        current = str(get_active_mode(session, "") or "").strip()
        if allow_empty_noop and not requested and not current:
            return None
        auth_mode_id = requested or "direct_cli"
        all_mode_items = self._list_mode_items()
        known_modes = {str(item.get("id") or "").strip() for item in all_mode_items if str(item.get("id") or "").strip()}
        if requested and requested not in known_modes:
            return await self._json_error(400, "active_mode is not registered")

        if requested:
            allowed_items = self._available_mode_items_for_user(
                all_mode_items,
                chat_id=int(actor_chat_id),
                is_admin=bool(is_admin),
            )
            allowed_modes = {
                str(item.get("id") or "").strip()
                for item in allowed_items
                if str(item.get("id") or "").strip()
            }
            is_mode_allowed = requested in allowed_modes
        else:
            is_mode_allowed = self._is_direct_cli_allowed_for_user(
                chat_id=int(actor_chat_id),
                is_admin=bool(is_admin),
            )

        security = getattr(self.bot_app, "security", None)
        if security is not None and hasattr(security, "authorize_mode_launch"):
            try:
                decision = await security.authorize_mode_launch(
                    int(actor_chat_id),
                    mode_id=auth_mode_id,
                    is_mode_allowed=bool(is_mode_allowed),
                    action="enable" if requested else "direct_cli",
                    session_id=str(getattr(session, "id", "") or ""),
                    context={
                        "actor_id": miniapp_actor_id(actor_chat_id),
                        "surface": "miniapp_settings",
                    },
                )
            except Exception:
                logger.exception("miniapp settings mode launch authorization failed")
                return await self._json_error(500, "active_mode authorization failed")
            if not bool(getattr(decision, "allowed", False)):
                return await self._json_error(403, "active_mode is not allowed for this user")
        elif not is_mode_allowed:
            return await self._json_error(403, "active_mode is not allowed for this user")

        if requested != current and self._session_busy_for_mode_change(session):
            return await self._json_error(409, "session is busy")

        ctx = {
            "bot_app": self.bot_app,
            "session": session,
            "chat_id": int(actor_chat_id),
            "context": None,
            "query": None,
            "dest": {"kind": "miniapp", "chat_id": int(actor_chat_id)},
        }
        new_mode = None
        if requested:
            new_mode = self._mode_plugin(requested)
            if new_mode is None:
                return await self._json_error(400, "active_mode is not registered")
            if current != requested:
                preflight_error = self._mode_enable_preflight_error(new_mode, session)
                if preflight_error:
                    return await self._json_error(409, preflight_error)

        mode_runtime_snapshot = self._snapshot_mode_runtime_state(session, current)
        if current and current != requested:
            old_mode = self._mode_plugin(current)
            if old_mode is not None and hasattr(old_mode, "on_disable"):
                try:
                    result = await old_mode.on_disable(ctx)
                except Exception:
                    logger.exception("miniapp settings active_mode disable failed")
                    self._restore_mode_runtime_state(session, mode_runtime_snapshot)
                    self._persist_session_if_possible(session)
                    return await self._json_error(500, "active_mode disable failed")
                if result is not None and not bool(getattr(result, "success", False)):
                    self._restore_mode_runtime_state(session, mode_runtime_snapshot)
                    self._persist_session_if_possible(session)
                    return await self._json_error(409, getattr(result, "error", None) or "active_mode disable failed")
            if str(get_active_mode(session, "") or "").strip() == current:
                set_active_mode(session, None)

        if requested:
            if current != requested and hasattr(new_mode, "on_enable"):
                try:
                    result = await new_mode.on_enable(ctx)
                except Exception:
                    logger.exception("miniapp settings active_mode enable failed")
                    self._restore_mode_runtime_state(session, mode_runtime_snapshot)
                    self._persist_session_if_possible(session)
                    return await self._json_error(500, "active_mode enable failed")
                if result is not None and not bool(getattr(result, "success", False)):
                    self._restore_mode_runtime_state(session, mode_runtime_snapshot)
                    self._persist_session_if_possible(session)
                    return await self._json_error(409, getattr(result, "error", None) or "active_mode enable failed")
            if str(get_active_mode(session, "") or "").strip() != requested:
                set_active_mode(session, requested)
        else:
            set_active_mode(session, None)
        return None

    @staticmethod
    def _log_remote_control_audit(
        *,
        session: Any,
        actor: str,
        surface: str,
        action: str,
        host_alias: str | None,
        host_cfg: Any,
        result: str,
        reason: str = "",
    ) -> None:
        from app.services.remote_control_service import build_remote_control_audit_extra

        logger.info(
            action,
            extra=build_remote_control_audit_extra(
                session=session,
                actor=actor,
                surface=surface,
                action=action,
                host_alias=host_alias,
                host_cfg=host_cfg,
                result=result,
                reason=reason,
            ),
        )

    @classmethod
    def _validate_session_uid_input(cls, session_uid: Any, *, field_name: str = "session_uid") -> str:
        token = str(session_uid or "").strip()
        if token and cls._CHAT_SESSION_PAIR_RE.fullmatch(token):
            raise web.HTTPBadRequest(
                reason=f"{field_name} chat_id:session_id format is not supported; use canonical session_uid"
            )
        return token

    def _resolve_visible_session(
        self,
        *,
        user_id: int,
        is_admin: bool,
        session_uid: str,
    ) -> tuple[str, Any] | tuple[str, None]:
        token = self._validate_session_uid_input(session_uid)
        if not token:
            return "", None
        visible_sessions = self._collect_visible_sessions(user_id=int(user_id), is_admin=bool(is_admin))
        session = visible_sessions.get(token)
        if session is not None:
            return token, session
        return "", None

    def _run_operations_service(self) -> Any:
        service = getattr(self.bot_app, "mode_run_operations", None)
        if service is None:
            raise web.HTTPServiceUnavailable(reason="run operations unavailable")
        return service

    def _run_artifact_store(self) -> Any:
        store = getattr(self._run_operations_service(), "artifact_store", None)
        if store is None:
            raise web.HTTPServiceUnavailable(reason="run artifact store unavailable")
        return store

    @staticmethod
    def _miniapp_run_execution_vector(
        *,
        user: Dict[str, Any],
        session: Any,
    ) -> tuple[Any, Dict[str, Any]]:
        user_id = int(user["user_id"])
        actor_id = str(user.get("actor_id") or miniapp_actor_id(user_id))
        session_uid = session_runtime_uid(session)
        dest = {
            "kind": "miniapp",
            "session_uid": session_uid,
            "user_id": user_id,
            "actor_id": actor_id,
        }
        context = SimpleNamespace(
            transport="miniapp",
            session_uid=session_uid,
            user_id=user_id,
            actor_id=actor_id,
            is_admin=bool(user.get("is_admin", False)),
        )
        return context, dest

    def _resolve_accessible_run_session(
        self,
        *,
        user: Dict[str, Any],
        session_uid: str,
    ) -> tuple[str, Any]:
        canonical_uid, session = self._resolve_visible_session(
            user_id=int(user["user_id"]),
            is_admin=bool(user.get("is_admin", False)),
            session_uid=session_uid,
        )
        if session is None or not canonical_uid:
            raise web.HTTPNotFound(reason="session is not found")
        return canonical_uid, session

    def _resolve_accessible_files_session(
        self,
        *,
        user: Dict[str, Any],
        session_uid: str,
    ) -> tuple[str, Any]:
        canonical_uid, session = self._resolve_visible_session(
            user_id=int(user["user_id"]),
            is_admin=bool(user.get("is_admin", False)),
            session_uid=session_uid,
        )
        if session is None or not canonical_uid:
            raise web.HTTPNotFound(reason="session not found")
        return canonical_uid, session

    def _project_local_selected_skill_ids(self, *, session: Any, skill_ids: List[str]) -> List[str]:
        cleaned = [
            clean_run_listing_text(item, max_len=64)
            for item in list(skill_ids or [])
            if clean_run_listing_text(item, max_len=64)
        ]
        if not cleaned:
            return []
        skill_runtime = getattr(self.bot_app, "mode_skill_runtime", None)
        registry_service = getattr(skill_runtime, "registry_service", None) if skill_runtime is not None else None
        if registry_service is None or not hasattr(registry_service, "load_registry"):
            return []
        try:
            snapshot = registry_service.load_registry(session=session)
        except Exception:
            logger.exception(
                "miniapp run serialization failed to resolve project-local skills session_uid=%s",
                session_runtime_uid(session),
            )
            return []
        return [
            skill_id
            for skill_id in cleaned
            if snapshot.project_manifests.get(skill_id) is not None
        ]

    def _serialize_run_listing(self, *, store: Any, run: Any, session: Any) -> Dict[str, Any]:
        state = store.load_state(run)
        recovery = store.load_recovery(run)
        events = store.load_events_tail(run, limit=24)
        status = clean_run_listing_text(state.get("status"), max_len=32) or "running"
        phase = clean_run_listing_text(state.get("phase"), max_len=64) or "unknown"
        mode_context = state.get("mode_context") if isinstance(state.get("mode_context"), dict) else {}
        issues = recovery.get("issues") if isinstance(recovery.get("issues"), list) else []
        issue_codes = [
            clean_run_listing_text(item.get("code"), max_len=64)
            for item in issues
            if isinstance(item, dict) and clean_run_listing_text(item.get("code"), max_len=64)
        ]
        recommended_action = clean_run_listing_text(recovery.get("recommended_action"), max_len=64) or None
        if recommended_action == "no_action":
            recommended_action = None
        can_resume = bool(recovery.get("can_resume")) if recovery else False
        can_recover = bool(recommended_action) and recommended_action in {
            "rollback_to_checkpoint",
            "restart_from_phase",
            "replay_finalize",
            "mark_failed",
        }
        terminal_actions_blocked = status in {"completed", "superseded"}
        if terminal_actions_blocked:
            can_resume = False
            can_recover = False
        last_requested_operation = (
            dict(recovery.get("last_requested_operation"))
            if isinstance(recovery.get("last_requested_operation"), dict)
            else None
        )
        selected_skill_ids = [
            clean_run_listing_text(item, max_len=64)
            for item in list(state.get("selected_skill_ids") or [])
            if clean_run_listing_text(item, max_len=64)
        ]
        finished_at_raw = state.get("finished_at")
        finished_at = float(finished_at_raw or 0.0) if finished_at_raw not in (None, "") else None
        can_apply_recommendation = (
            str(run.mode_id or "").strip() == "codebase_mapper"
            and (recommended_action or "") in {"rerun_same_operation", "run_validate", "run_repair"}
        )
        return {
            "session_uid": str(run.session_uid),
            "mode_id": str(run.mode_id),
            "run_id": str(run.run_id),
            "status": status,
            "phase": phase,
            "started_at": float(state.get("started_at") or 0.0),
            "updated_at": float(state.get("updated_at") or 0.0),
            "finished_at": finished_at,
            "active": not finished_at and not is_terminal_status(status),
            "current_unit_id": clean_run_listing_text(
                state.get("current_unit_id") or state.get("current_step_id"),
                max_len=128,
            )
            or None,
            "recommended_action": recommended_action,
            "can_resume": can_resume,
            "can_recover": can_recover,
            "terminal_actions_blocked": terminal_actions_blocked,
            "can_apply_recommendation": can_apply_recommendation,
            "issue_codes": issue_codes,
            "last_requested_operation": last_requested_operation,
            "skill_log": summarize_run_skill_log(events, state),
            "selected_skill_ids": selected_skill_ids,
            "project_local_skill_ids": self._project_local_selected_skill_ids(
                session=session,
                skill_ids=selected_skill_ids,
            ),
            "cli_work_type": clean_run_listing_text(mode_context.get("cli_work_type"), max_len=64) or None,
            "executor_profile": (
                clean_run_listing_text(mode_context.get("executor_profile"), max_len=64) or None
            ),
        }

    def _serialize_run_detail(self, *, store: Any, run: Any, session: Any) -> Dict[str, Any]:
        listing = self._serialize_run_listing(store=store, run=run, session=session)
        state = store.load_state(run)
        plan = store.load_plan(run)
        checkpoints = store.load_checkpoints(run)
        recovery = store.load_recovery(run)
        metrics = store.load_metrics(run)
        events = store.load_events_tail(run, limit=48)
        detail = dict(listing)
        detail.update(
            {
                "state": state,
                "plan": plan,
                "checkpoints": checkpoints,
                "recovery": recovery,
                "metrics": metrics,
                "events_tail": events,
            }
        )
        return detail

    def _resolve_run_handle(
        self,
        *,
        user: Dict[str, Any],
        session_uid: str,
        run_id: str,
        mode_id: str | None = None,
    ) -> tuple[str, Any, Any]:
        requested_run_id = clean_run_listing_text(run_id, max_len=128)
        if not requested_run_id:
            raise web.HTTPBadRequest(reason="run_id is required")
        canonical_uid, session = self._resolve_accessible_run_session(user=user, session_uid=session_uid)
        store = self._run_artifact_store()
        handles = store.list_runs(
            session=session,
            mode_id=(str(mode_id or "").strip() or None),
            limit=200,
        )
        for handle in handles:
            if str(handle.run_id) == requested_run_id:
                if mode_id and str(handle.mode_id) != str(mode_id):
                    continue
                return canonical_uid, session, handle
        raise web.HTTPNotFound(reason="run is not found")

    @staticmethod
    def _serialize_run_operation_result(result: Any) -> Dict[str, Any]:
        recommended_action = str(getattr(result, "recommended_action", "") or "") or None
        if recommended_action == "no_action":
            recommended_action = None
        return {
            "operation": str(getattr(result, "operation", "") or ""),
            "status": str(getattr(result, "status", "") or ""),
            "mode_id": str(getattr(result, "mode_id", "") or ""),
            "phase": str(getattr(result, "phase", "") or ""),
            "message": str(getattr(result, "message", "") or ""),
            "run_id": str(getattr(result, "run_id", "") or "") or None,
            "recommended_action": recommended_action,
            "blocked_by": list(getattr(result, "blocked_by", ()) or ()),
            "report": (
                dict(getattr(result, "report", {}) or {})
                if isinstance(getattr(result, "report", None), dict)
                else None
            ),
        }

    def _require_object_body(self, body: Any) -> Dict[str, Any]:
        return self.json_route_services.require_object_body(body)

    async def _read_json_object(self, request: web.Request) -> Dict[str, Any]:
        return await self.json_route_services.read_json_object(request)

    def _scheduler_service(self):
        service = getattr(self.bot_app, "scheduler_service", None)
        if service is None:
            raise web.HTTPServiceUnavailable(reason="scheduler service unavailable")
        return service

    def _list_owned_projects(self, *, user_id: int) -> List[Dict[str, Any]]:
        registry = getattr(self.bot_app, "project_registry", None)
        if registry is None:
            return []
        self._sync_owned_projects_from_sessions(user_id=int(user_id))
        owner_id = miniapp_actor_id(user_id)
        return [
            {
                "slug": str(item.slug or ""),
                "name": str(item.name or item.slug or ""),
                "path": str(item.path or ""),
                "enabled": bool(item.enabled),
            }
            for item in registry.list_projects(owner_id=owner_id)
        ]

    def _sync_owned_projects_from_sessions(self, *, user_id: int) -> None:
        registry = getattr(self.bot_app, "project_registry", None)
        manager = getattr(self.bot_app, "manager", None)
        session_creation_service = getattr(self.bot_app, "session_creation_service", None)
        if registry is None or manager is None or session_creation_service is None:
            return
        if not hasattr(manager, "sessions_for_chat") or not hasattr(session_creation_service, "register_project"):
            return
        try:
            sessions = dict(manager.sessions_for_chat(int(user_id)) or {})
        except Exception:
            logger.exception("miniapp failed to collect owned sessions for project sync")
            return

        synced_paths: set[str] = set()
        for session in sessions.values():
            workdir = str(getattr(session, "project_root", None) or getattr(session, "workdir", "") or "").strip()
            if not workdir or workdir in synced_paths or not os.path.isdir(workdir):
                continue
            synced_paths.add(workdir)
            try:
                if registry.get_by_path(workdir) is not None:
                    continue
            except Exception:
                logger.exception("miniapp failed to inspect project registry before sync path=%s", workdir)
                continue
            error = session_creation_service.register_project(int(user_id), workdir)
            if error:
                logger.warning(
                    "miniapp project sync skipped user_id=%s path=%s error=%s",
                    int(user_id),
                    workdir,
                    error,
                )

    def _require_owned_project(self, *, user_id: int, project_slug: str) -> Dict[str, Any]:
        slug = str(project_slug or "").strip()
        if not slug:
            raise web.HTTPBadRequest(reason="project_slug is required")
        for item in self._list_owned_projects(user_id=int(user_id)):
            if str(item.get("slug") or "") == slug:
                return item
        raise web.HTTPForbidden(reason="project access denied")

    def _collect_owned_scheduler_sessions(self, *, user_id: int) -> Dict[str, Any]:
        manager = getattr(self.bot_app, "manager", None)
        if manager is None or not hasattr(manager, "sessions_for_chat"):
            return {}
        try:
            return dict(manager.sessions_for_chat(int(user_id)) or {})
        except Exception:
            logger.exception("miniapp scheduler failed to collect sessions for chat")
            return {}

    @staticmethod
    def _resolve_scheduler_session_scope_uid(session: Any, *, user_id: int) -> str:
        return session_runtime_uid(session)

    @staticmethod
    def _is_session_within_project(*, session: Any, project_path: str) -> bool:
        session_root = os.path.realpath(
            str(getattr(session, "project_root", None) or getattr(session, "workdir", "") or "")
        )
        project_root = os.path.realpath(str(project_path or ""))
        if not session_root or not project_root:
            return False
        try:
            return os.path.commonpath([session_root, project_root]) == project_root
        except Exception:
            return False

    def _list_scheduler_notification_targets(
        self,
        *,
        user_id: int,
        project_path: str,
        is_admin: bool,
    ) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        seen: set[str] = set()
        for session_id, session in self._collect_owned_scheduler_sessions(user_id=int(user_id)).items():
            if not self._is_session_within_project(session=session, project_path=str(project_path or "")):
                continue
            telegram_session_uid = self._resolve_scheduler_session_scope_uid(session, user_id=int(user_id))
            if not telegram_session_uid:
                continue
            if telegram_session_uid in seen:
                continue
            seen.add(telegram_session_uid)
            session_name = str(getattr(session, "name", "") or session_id or telegram_session_uid).strip()
            owner_chat_id = self._session_owner_chat_id(session)
            session_title = format_session_selector_label(
                session,
                telegram_user_id=owner_chat_id if is_admin else None,
            )
            out.append(
                {
                    "telegram_session_uid": telegram_session_uid,
                    "label": session_title,
                    "session_name": session_name,
                }
            )
        return out

    def _require_project_notification_target(
        self,
        *,
        user_id: int,
        is_admin: bool,
        project: Dict[str, Any],
        telegram_session_uid: str,
    ) -> str:
        token = self._validate_session_uid_input(
            telegram_session_uid,
            field_name="notification_target.telegram_session_uid",
        )
        if not token:
            raise web.HTTPBadRequest(reason="notification_target.telegram_session_uid is required")
        allowed = {
            str(item.get("telegram_session_uid") or "")
            for item in self._list_scheduler_notification_targets(
                user_id=int(user_id),
                project_path=str(project.get("path") or ""),
                is_admin=bool(is_admin),
            )
        }
        if token not in allowed:
            raise web.HTTPForbidden(reason="notification target is not allowed for project")
        return token

    async def auth_me(self, request: web.Request) -> web.Response:
        user = await self._require_access(request)
        return web.json_response(
            {
                "user_id": user["user_id"],
                "is_admin": bool(user.get("is_admin", False)),
                "username": user.get("username") or "",
            }
        )

    async def i18n_catalog_get(self, request: web.Request) -> web.Response:
        lang = str(request.match_info.get("lang", "") or "").lower().strip()
        if lang not in SUPPORTED_LANGS:
            lang = "ru"
        catalog_path = os.path.join(os.path.dirname(__file__), "..", "locales", f"{lang}.json")
        try:
            with open(catalog_path, encoding="utf-8") as f:
                catalog = json.load(f)
        except (OSError, json.JSONDecodeError):
            fallback_path = os.path.join(os.path.dirname(__file__), "..", "locales", "ru.json")
            try:
                with open(fallback_path, encoding="utf-8") as f:
                    catalog = json.load(f)
            except (OSError, json.JSONDecodeError):
                catalog = {}
        return web.json_response(catalog, headers={"Cache-Control": "max-age=3600"})

    async def i18n_user_lang_get(self, request: web.Request) -> web.Response:
        user = await self._require_access(request)
        user_id = user["user_id"]
        cfg = self.bot_app.config
        user_languages = getattr(getattr(cfg, "telegram", None), "user_languages", {}) or {}
        saved = user_languages.get(user_id)
        if saved and saved in SUPPORTED_LANGS:
            return web.json_response({"lang": saved})
        default = str(getattr(getattr(cfg, "defaults", None), "default_language", "") or "").strip()
        if default and default in SUPPORTED_LANGS:
            return web.json_response({"lang": default})
        return web.json_response({"lang": "ru"})

    async def i18n_user_lang_put(self, request: web.Request) -> web.Response:
        user = await self._require_access(request)
        body = await self._read_json_object(request)
        lang = str(body.get("lang") or "").strip().lower()
        if lang not in SUPPORTED_LANGS:
            return await self._json_error(400, "unsupported language")
        config_service = self._container_config_service(self.bot_app)
        await config_service.set_user_language(user["user_id"], lang)
        return web.json_response({"ok": True, "lang": lang})

    async def files_ws_ticket(self, request: web.Request) -> web.Response:
        user = await self._require_access(request)
        ticket = self._issue_ws_ticket(user)
        return web.json_response({"ticket": ticket, "expires_in": int(self._ws_ticket_ttl_sec)})

    async def status_ws_ticket(self, request: web.Request) -> web.Response:
        user = await self._require_access(request)
        ticket = self._issue_ws_ticket(user)
        return web.json_response({"ticket": ticket, "expires_in": int(self._ws_ticket_ttl_sec)})

    async def _consume_ws_messages(self, ws: web.WebSocketResponse) -> None:
        async for msg in ws:
            if msg.type in (web.WSMsgType.CLOSE, web.WSMsgType.CLOSING, web.WSMsgType.CLOSED):
                return
            if msg.type == web.WSMsgType.ERROR:
                raise RuntimeError("miniapp logs websocket receive error") from ws.exception()

    @staticmethod
    def _session_sort_key(session_id: str) -> tuple[int, int, str]:
        sid = str(session_id or "")
        match = re.fullmatch(r"s(\d+)", sid)
        if match:
            return (0, int(match.group(1)), sid)
        return (1, 0, sid)

    @staticmethod
    def _safe_status_value(value: Any, *, depth: int = 0) -> Any:
        max_depth = 2
        max_items = 25
        max_text = 600
        if value is None or isinstance(value, (bool, int, float)):
            return value
        if isinstance(value, str):
            if len(value) <= max_text:
                return value
            return f"{value[:max_text]}... [truncated {len(value) - max_text} chars]"
        if depth >= max_depth:
            rendered = repr(value)
            if len(rendered) <= max_text:
                return rendered
            return f"{rendered[:max_text]}... [truncated {len(rendered) - max_text} chars]"

        if isinstance(value, dict):
            items = list(value.items())
            out: Dict[str, Any] = {}
            for key, nested in items[:max_items]:
                out[str(key)] = MiniAppRoutes._safe_status_value(nested, depth=depth + 1)
            if len(items) > max_items:
                out["__truncated_items__"] = int(len(items) - max_items)
            return out

        if isinstance(value, (list, tuple, set, frozenset, deque)):
            seq = list(value)
            out = [MiniAppRoutes._safe_status_value(item, depth=depth + 1) for item in seq[:max_items]]
            if len(seq) > max_items:
                out.append(f"... {len(seq) - max_items} more items")
            return out

        rendered = repr(value)
        if len(rendered) <= max_text:
            return rendered
        return f"{rendered[:max_text]}... [truncated {len(rendered) - max_text} chars]"

    def _build_session_payload(
        self,
        session: Any,
        *,
        session_chat_id: int | None = None,
        is_admin: bool = False,
    ) -> Dict[str, Any]:
        now = time.time()
        owner_chat_id = session_chat_id
        if owner_chat_id is None:
            owner_chat_id = self._session_owner_chat_id(session)
        scope = getattr(session, "conversation_scope", None)
        session_ui_key = (
            TelegramUiKey.from_parts(
                owner_chat_id,
                getattr(scope, "message_thread_id", None),
            )
            if owner_chat_id is not None
            else None
        )
        session_uid = session_runtime_uid(session)
        tool_name = str(getattr(getattr(session, "tool", None), "name", "") or "")
        cli_state = getattr(session, "cli", None)
        git_state = getattr(session, "git", None)
        modes_state = getattr(session, "modes", None)
        active_cli = str(
            getattr(cli_state, "active_cli", getattr(session, "active_cli", "")) or tool_name or ""
        )
        active_resume_token = getattr(session, "resume_token", None)
        active_mode = str(get_active_mode(session, "") or "").strip()
        cli_work_type = str(
            getattr(cli_state, "cli_work_type", getattr(session, "cli_work_type", "")) or ""
        )
        git_busy = bool(getattr(git_state, "busy", getattr(session, "git_busy", False)))
        git_conflict = bool(getattr(git_state, "conflict", getattr(session, "git_conflict", False)))
        git_conflict_kind = getattr(git_state, "conflict_kind", getattr(session, "git_conflict_kind", None))
        git_conflict_files = getattr(git_state, "conflict_files", getattr(session, "git_conflict_files", []))
        resume_tokens = getattr(cli_state, "resume_tokens", getattr(session, "resume_tokens", {}))
        auto_commands_ran = bool(getattr(cli_state, "auto_commands_ran", getattr(session, "auto_commands_ran", False)))
        try:
            from session import (
                available_execution_backends,
                get_session_execution_backend,
            )
            from app.services.cli_backends.tmux_backend import TmuxExecutionBackend

            execution_backend = get_session_execution_backend(session)
            available_backends = available_execution_backends(session)
            active_execution_backend = str(getattr(session, "_active_execution_backend", "") or "none")
            tmux_paths = TmuxExecutionBackend().paths(session)
            tmux_state = TmuxExecutionBackend._read_state(tmux_paths)
            tmux_status = (
                {
                    "state": str(tmux_state.get("state") or "unknown"),
                    "session_name": str(tmux_state.get("session_name") or tmux_paths["session_name"]),
                    "pane_target": str(tmux_state.get("pane_target") or tmux_paths["pane_target"]),
                    "last_activity_at": self._safe_status_value(tmux_state.get("last_activity_at")),
                }
                if tmux_state
                else None
            )
            # Reality check independent of configured backend
            has_live_tmux = bool(tmux_status) and TmuxExecutionBackend().is_tmux_live(session)
        except Exception:
            execution_backend = "headless"
            available_backends = ["headless"]
            active_execution_backend = str(getattr(session, "_active_execution_backend", "") or "none")
            tmux_status = None
            has_live_tmux = False
        analyst_template_id = str(
            getattr(
                modes_state,
                "analyst_template_id",
                getattr(session, "analyst_template_id", ""),
            )
            or ""
        )
        manager_quiet_mode = bool(
            getattr(modes_state, "manager_quiet_mode", getattr(session, "manager_quiet_mode", False))
        )
        manager_plan_status: str | None = None
        agent_mode_status: str | None = None
        agent_mode_status_details: Dict[str, Any] | None = None
        analyst_mode_status: str | None = None
        analyst_mode_status_details: Dict[str, Any] | None = None
        webmaster_mode_status: str | None = None
        runtime_progress = build_runtime_progress_payload(session, recent_limit=12)
        runtime_status: str | None = None
        try:
            r_source = str(runtime_progress.get("last_source") or "").strip()
            r_phase = str(runtime_progress.get("last_phase") or "").strip()
            r_state = str(runtime_progress.get("last_status") or "").strip()
            r_msg = str(runtime_progress.get("last_message") or "").strip()
            parts = [x for x in [r_source, r_phase, r_state] if x]
            if parts or r_msg:
                runtime_status = f"{'/'.join(parts) if parts else '-'}: {r_msg}" if r_msg else "/".join(parts)
        except Exception:
            runtime_status = None
        try:
            from utils.lang import resolve_user_lang as _resolve_user_lang
            _mode_status_lang = _resolve_user_lang(self.bot_app.config, chat_id=owner_chat_id)
        except Exception:
            _mode_status_lang = "ru"
        if active_mode == "manager":
            try:
                plan = load_plan(
                    str(getattr(session, "workdir", "") or ""),
                    scoped_key=session_scoped_key(session),
                )
                if plan is not None:
                    manager_plan_status = str(format_manager_status_brief(plan) or "").strip() or None
            except Exception:
                logger.exception("miniapp status failed to load manager plan")
        elif active_mode == "agent":
            try:
                mode_tasks = getattr(self.bot_app, "mode_tasks", None)
                running = False
                if mode_tasks is not None and hasattr(mode_tasks, "list"):
                    running = bool(
                        mode_tasks.list(
                            session_id=str(getattr(session, "id", "") or ""),
                            mode_id="agent",
                        )
                    )

                flow_tokens: list[str] = []
                if session_ui_key is not None:
                    token = str(self.bot_app.ui_state.dirs_mode.get(session_ui_key, "") or "")
                    mode_id, flow = decode_mode_dirs(token)
                    if mode_id == "agent" and flow:
                        flow_tokens.append(f"dirs:{flow}")
                    pending_store = getattr(self.bot_app, "mode_agent_project_pending_by_chat", None)
                    if pending_store is not None:
                        pending_entry = normalize_agent_project_pending_entry(
                            pending_store.get(
                                agent_project_scope_key(session_ui_key.chat_id, session_ui_key.message_thread_id),
                            )
                        )
                        if pending_entry is None:
                            pending_entry = normalize_agent_project_pending_entry(pending_store.get(str(owner_chat_id)))
                        if pending_entry is not None:
                            pending_session_key = str(pending_entry.get("session_scoped_key") or "").strip()
                            current_session_key = agent_project_session_key(session)
                            if (
                                (pending_session_key and pending_session_key == current_session_key)
                                or (
                                    not pending_session_key
                                    and str(pending_entry.get("session_id") or "").strip()
                                    == str(getattr(session, "id", "") or "").strip()
                                )
                            ):
                                flow_tokens.append("project_connect")
                flow_tokens_dedup: list[str] = []
                for item in flow_tokens:
                    val = str(item or "").strip()
                    if not val or val in flow_tokens_dedup:
                        continue
                    flow_tokens_dedup.append(val)
                flow_value = ",".join(flow_tokens_dedup)
                agent_mode_status_details = build_agent_status_payload(
                    session,
                    mode_id="agent",
                    agent_running=running,
                    pending_questions=self.bot_app.ui_state.pending_questions,
                    active_plugin_flow=flow_value,
                    runtime_progress=runtime_progress,
                )
                agent_mode_status = str(
                    build_agent_status_text(
                        session,
                        mode_id="agent",
                        agent_running=running,
                        pending_questions=self.bot_app.ui_state.pending_questions,
                        active_plugin_flow=flow_value,
                        runtime_progress=runtime_progress,
                        lang=_mode_status_lang,
                    )
                    or ""
                ).strip() or None
            except Exception:
                logger.exception("miniapp status failed to build agent mode status")
        elif active_mode == "analyst":
            try:
                workdir = str(getattr(session, "workdir", "") or "").strip()
                state_root = (
                    cli_proxy_artifact_path(workdir, ".analyst_data")
                    if workdir
                    else cli_proxy_artifact_path(os.getcwd(), ".analyst_data")
                )
                store = AnalystStateStore(state_root)
                ctx = store.load(
                    build_context_key(
                        session_chat_id if session_chat_id is not None else getattr(session, "chat_id", None),
                        str(getattr(session, "id", "") or "").strip() or "default",
                    )
                )
                mode_tasks = getattr(self.bot_app, "mode_tasks", None)
                running = False
                if mode_tasks is not None and hasattr(mode_tasks, "list"):
                    running = bool(
                        mode_tasks.list(
                            session_id=str(getattr(session, "id", "") or ""),
                            mode_id="analyst",
                        )
                    )
                analyst_mode_status_details = build_analyst_status_payload(
                    session,
                    analyst_context=ctx,
                    analyst_running=running,
                    pending_questions=self.bot_app.ui_state.pending_questions,
                    mode_id="analyst",
                )
                analyst_mode_status = str(
                    build_analyst_status_text(
                        session,
                        analyst_context=ctx,
                        analyst_running=running,
                        pending_questions=self.bot_app.ui_state.pending_questions,
                        mode_id="analyst",
                        lang=_mode_status_lang,
                    )
                    or ""
                ).strip() or None
            except Exception:
                logger.exception("miniapp status failed to build analyst mode status")
        elif active_mode == "webmaster":
            try:
                workdir = str(getattr(session, "workdir", "") or "").strip()
                state_root = (
                    cli_proxy_artifact_path(workdir, ".webmaster_data")
                    if workdir
                    else cli_proxy_artifact_path(os.getcwd(), ".webmaster_data")
                )
                owner_chat_id = session_chat_id
                if owner_chat_id is None:
                    raw_chat_id = getattr(session, "chat_id", None)
                    if raw_chat_id is not None:
                        owner_chat_id = int(raw_chat_id)
                wm_chat_id = int(owner_chat_id or 0)
                wm_user_id = wm_chat_id if wm_chat_id > 0 else 0
                store = WebmasterStateStore(state_root)
                wm_ctx = store.load(
                    build_user_key(
                        wm_chat_id,
                        wm_user_id,
                        str(getattr(session, "id", "") or "").strip() or None,
                    )
                )
                mode_tasks = getattr(self.bot_app, "mode_tasks", None)
                running = False
                if mode_tasks is not None and hasattr(mode_tasks, "list"):
                    running = bool(
                        mode_tasks.list(
                            session_id=str(getattr(session, "id", "") or ""),
                            mode_id="webmaster",
                        )
                    )
                stage = ModeStatusService.build_webmaster_mode_stage(
                    enabled=True,
                    running=running,
                    busy=bool(getattr(session, "busy", False)),
                    queue_len=ModeStatusService.get_session_queue_len(session),
                    wm_stage=str(getattr(wm_ctx, "stage", "idle") or "idle"),
                )
                webmaster_mode_status = str(
                    ModeStatusService.build_mode_status_text(
                        session,
                        title=t("session_status.webmaster_title", _mode_status_lang),
                        stage=stage,
                        enabled=True,
                        task_suffix=f"{t('session_status.task', _mode_status_lang)}: "
                        f"{t('session_status.task_active', _mode_status_lang) if running else t('session_status.none', _mode_status_lang)}",
                        extra_sections=[
                            (
                                t("session_status.wm_task_kind", _mode_status_lang),
                                str(getattr(wm_ctx, "task_kind", "unknown") or "unknown"),
                            ),
                            (
                                t("session_status.wm_prompt_version", _mode_status_lang),
                                str(getattr(wm_ctx, "active_prompt_version", 1)),
                            ),
                            (
                                t("session_status.wm_last_class", _mode_status_lang),
                                str(
                                    getattr(wm_ctx, "last_feedback_class", "")
                                    or t("session_status.none", _mode_status_lang)
                                ),
                            ),
                        ],
                        lang=_mode_status_lang,
                    )
                    or ""
                ).strip() or None
            except Exception:
                logger.exception("miniapp status failed to build webmaster mode status")

        def _age(ts: Any) -> int | None:
            if ts is None:
                return None
            try:
                return max(0, int(now - float(ts)))
            except Exception:
                return None

        tick_history = load_session_ticks(session, limit=100)
        last_assistant_text_ts = getattr(session, "last_assistant_text_ts", None)
        last_assistant_text_value = getattr(session, "last_assistant_text_value", None)
        if last_assistant_text_value is None:
            for item in reversed(tick_history):
                if str(item.get("kind") or "").strip().lower() != "assistant_text":
                    continue
                last_assistant_text_value = item.get("value")
                last_assistant_text_ts = item.get("ts")
                break
        assistant_tick_count = sum(
            1 for item in tick_history if str(item.get("kind") or "").strip().lower() == "assistant_text"
        )
        queue_items = list(getattr(session, "queue", []) or [])
        queue_preview: List[Dict[str, Any]] = []
        for item in queue_items[:8]:
            if isinstance(item, dict):
                queue_preview.append(
                    {
                        "text": str(item.get("text", ""))[:280],
                        "dest": self._safe_status_value(item.get("dest")),
                    }
                )
            else:
                queue_preview.append({"item": self._safe_status_value(item)})

        child = getattr(session, "child", None)
        child_pid = int(getattr(child, "pid", 0) or 0) or None if child is not None else None
        child_alive = None
        if child is not None:
            isalive = getattr(child, "isalive", None)
            if callable(isalive):
                try:
                    child_alive = bool(isalive())
                except Exception:
                    child_alive = None

        proc = getattr(session, "current_proc", None)
        proc_pid = int(getattr(proc, "pid", 0) or 0) or None if proc is not None else None
        proc_returncode = getattr(proc, "returncode", None) if proc is not None else None

        mode_registry = getattr(self.bot_app, "mode_registry_service", None) or getattr(self.bot_app, "mode_registry", None)
        try:
            from utils.lang import resolve_user_lang

            status_lang = resolve_user_lang(self.bot_app.config, chat_id=owner_chat_id)
        except Exception:
            status_lang = "ru"
        status_text = build_session_status_text(session, mode_registry=mode_registry, lang=status_lang)

        from app.services.remote_control_service import ExecutionTarget, RemoteControlService
        from app.services.ssh_config_loader import load_ssh_config
        rc_service = RemoteControlService()
        workdir = str(getattr(session, "workdir", "") or "").strip()
        all_hosts = load_ssh_config(workdir) if workdir else {}
        effective_state = rc_service.compute_effective_state(session, all_hosts)
        git_available = effective_state.git_available

        # REQ: When Remote Control = ON, local git state must not leak.
        # Fetch actual remote git state via RemoteShellService.
        if effective_state.execution_target == ExecutionTarget.REMOTE:
            git_busy = False
            git_conflict = False
            git_conflict_kind = None
            git_conflict_files = []

            ssh_svc = getattr(self.bot_app, "ssh_service", None)
            if ssh_svc and effective_state.host_alias and effective_state.remote_project_root:
                try:
                    import asyncio as _aio
                    from app.services.remote_shell_service import RemoteShellService
                    shell = RemoteShellService(ssh_svc)
                    loop = _aio.new_event_loop()
                    try:
                        remote_git = loop.run_until_complete(
                            shell.git_status(
                                workdir,
                                effective_state.host_alias,
                                effective_state.remote_project_root,
                            )
                        )
                    finally:
                        loop.close()
                    git_available = bool(remote_git.git_available)
                    if remote_git.git_available:
                        for entry in remote_git.entries:
                            if hasattr(entry, "status") and entry.status in ("U", "UU", "AA", "DD"):
                                git_conflict = True
                                break
                except Exception:
                    logger.debug("Failed to fetch remote git state", exc_info=True)

        raw_fields: Dict[str, Any] = {}
        for field_name, value in vars(session).items():
            if field_name in {"run_lock", "send_lock"}:
                continue
            if field_name == "state_summary":
                raw_fields[str(field_name)] = value
                continue
            raw_fields[str(field_name)] = self._safe_status_value(value)
        raw_fields["active_mode"] = str(get_active_mode(session, "") or "")
        raw_fields["advanced_orchestrator_enabled"] = bool(is_orchestrator_enabled(session, False))
        raw_fields["orchestrator_pending_input"] = self._safe_status_value(get_orchestrator_pending_input(session, None))
        raw_fields["orchestrator_last_mode_output"] = self._safe_status_value(
            get_orchestrator_last_mode_output(session, None)
        )
        raw_fields["orchestrator_last_mode_id"] = self._safe_status_value(
            get_orchestrator_last_mode_id(session, None)
        )

        return {
            "id": str(getattr(session, "id", "") or ""),
            "session_uid": session_uid,
            "name": str(getattr(session, "name", "") or ""),
            "display_title": format_session_selector_label(
                session,
                telegram_user_id=owner_chat_id if is_admin else None,
            ),
            "workdir": str(getattr(session, "workdir", "") or ""),
            "execution_target": effective_state.execution_target.value,
            "remote_host_alias": effective_state.host_alias,
            "remote_project_root": effective_state.remote_project_root,
            "git_available": git_available,
            "tool": tool_name,
            "active_cli": active_cli,
            "active_mode": str(get_active_mode(session, "") or ""),
            "executor_profile": str(getattr(session, "executor_profile", "") or ""),
            "cli_work_type": cli_work_type,
            "execution_backend": execution_backend,
            "has_live_tmux": has_live_tmux,
            "available_execution_backends": self._safe_status_value(available_backends),
            "active_execution_backend": active_execution_backend,
            "backend_switch_allowed": False,
            "backend_switch_blockers": self._safe_status_value(["configured in settings"]),
            "tmux_status": self._safe_status_value(tmux_status),
            "busy": bool(getattr(session, "busy", False)),
            "git_busy": git_busy,
            "git_conflict": git_conflict,
            "git_conflict_kind": self._safe_status_value(git_conflict_kind),
            "git_conflict_files": self._safe_status_value(git_conflict_files),
            "queue_len": int(len(queue_items)),
            "queue_preview": queue_preview,
            "resume_token_present": bool(active_resume_token),
            "active_resume_token": self._safe_status_value(active_resume_token),
            "resume_tokens": self._safe_status_value(resume_tokens),
            "auto_commands_ran": auto_commands_ran,
            "started_at": self._safe_status_value(getattr(session, "started_at", None)),
            "started_age_sec": _age(getattr(session, "started_at", None)),
            "last_output_ts": self._safe_status_value(getattr(session, "last_output_ts", None)),
            "last_output_age_sec": _age(getattr(session, "last_output_ts", None)),
            "last_tick_ts": self._safe_status_value(getattr(session, "last_tick_ts", None)),
            "last_tick_age_sec": _age(getattr(session, "last_tick_ts", None)),
            "last_tick_value": (
                None
                if getattr(session, "last_tick_value", None) is None
                else str(getattr(session, "last_tick_value", ""))
            ),
            "last_assistant_text_ts": self._safe_status_value(last_assistant_text_ts),
            "last_assistant_text_age_sec": _age(last_assistant_text_ts),
            "last_assistant_text_value": (
                None if last_assistant_text_value is None else str(last_assistant_text_value)
            ),
            "assistant_tick_count": int(assistant_tick_count),
            "tick_history": tick_history,
            "tick_seen": int(getattr(session, "tick_seen", 0) or 0),
            "analyst_template_id": analyst_template_id,
            "manager_quiet_mode": manager_quiet_mode,
            "advanced_orchestrator_enabled": bool(is_orchestrator_enabled(session, False)),
            "orchestrator_pending_input": self._safe_status_value(get_orchestrator_pending_input(session, None)),
            "orchestrator_last_mode_output": self._safe_status_value(get_orchestrator_last_mode_output(session, None)),
            "orchestrator_last_mode_id": self._safe_status_value(get_orchestrator_last_mode_id(session, None)),
            "project_root": self._safe_status_value(getattr(session, "project_root", None)),
            "state_summary": getattr(session, "state_summary", None),
            "manager_plan_status": manager_plan_status,
            "agent_mode_status": agent_mode_status,
            "agent_mode_status_details": self._safe_status_value(agent_mode_status_details),
            "runtime_status": runtime_status,
            "runtime_progress": runtime_progress,
            "analyst_mode_status": analyst_mode_status,
            "analyst_mode_status_details": self._safe_status_value(analyst_mode_status_details),
            "webmaster_mode_status": webmaster_mode_status,
            "state_updated_at": self._safe_status_value(getattr(session, "state_updated_at", None)),
            "headless_forced_stop": self._safe_status_value(getattr(session, "headless_forced_stop", None)),
            "child_pid": child_pid,
            "child_alive": child_alive,
            "current_proc_pid": proc_pid,
            "current_proc_returncode": self._safe_status_value(proc_returncode),
            "status_text": status_text,
            "fields": raw_fields,
        }

    def _collect_visible_sessions(self, *, user_id: int, is_admin: bool) -> Dict[str, Any]:
        out: Dict[str, Any] = {}
        manager = getattr(self.bot_app, "manager", None)
        if manager is None:
            return out

        if is_admin:
            by_chat = dict(getattr(manager, "sessions_by_chat", {}) or {})
            for chat_id, by_id in by_chat.items():
                if not isinstance(by_id, dict):
                    continue
                for sid, session in by_id.items():
                    suid = session_runtime_uid(session)
                    if not suid:
                        continue
                    out[suid] = session
            return out

        try:
            by_id = dict(manager.sessions_for_chat(int(user_id)) or {})
        except Exception:
            logger.exception("miniapp status failed to collect user sessions")
            return out
        for sid, session in by_id.items():
            suid = session_runtime_uid(session)
            if not suid:
                continue
            out[suid] = session
        return out

    def _visible_session_inventory_signature(self, *, user_id: int, is_admin: bool) -> str:
        manager = getattr(self.bot_app, "manager", None)
        if manager is None:
            return ""

        if is_admin:
            parts: List[str] = []
            by_chat = dict(getattr(manager, "sessions_by_chat", {}) or {})
            for chat_key in sorted(by_chat.keys(), key=lambda value: str(value)):
                by_id = by_chat.get(chat_key)
                if not isinstance(by_id, dict):
                    continue
                for session_id in sorted(str(key) for key in by_id.keys()):
                    parts.append(f"{chat_key}\u0000{session_id}")
            return "\u0001".join(parts)

        try:
            by_id = dict(manager.sessions_for_chat(int(user_id)) or {})
        except Exception:
            logger.exception("miniapp status failed to build session inventory signature")
            return ""
        return "\u0001".join(sorted(str(key) for key in by_id.keys()))

    def _resolve_admin_status_session(
        self,
        *,
        user: Dict[str, Any],
        session_uid: str,
    ) -> tuple[int, str, Any]:
        session_uid_value = self._validate_session_uid_input(session_uid)
        if not session_uid_value:
            raise web.HTTPBadRequest(reason="session_uid is required")
        canonical_uid, session = self._resolve_visible_session(
            user_id=int(user["user_id"]),
            is_admin=bool(user.get("is_admin", False)),
            session_uid=session_uid_value,
        )
        if session is None:
            raise web.HTTPNotFound(reason="session not found")
        owner_chat_id = self._session_owner_chat_id(session)
        if owner_chat_id is None:
            raise web.HTTPNotFound(reason="session not found")
        return (int(owner_chat_id), canonical_uid, session)

    @staticmethod
    def _map_admin_action_to_callback(
        *, action: str, body: Dict[str, Any]
    ) -> tuple[str, Dict[str, Any]]:
        action_norm = str(action or "").strip().lower()
        payload: Dict[str, Any] = {}
        if action_norm == "ack_incident":
            incident_id = str(body.get("incident_id") or body.get("id") or "").strip()
            payload["id"] = incident_id
            return ("ack", payload)
        if action_norm == "revoke_approval":
            override_id = str(body.get("override_id") or body.get("id") or "").strip()
            payload["id"] = override_id
            return ("revoke", payload)
        if action_norm == "approvals_clear":
            return ("approvals_clear", payload)
        if action_norm == "mute":
            minutes = body.get("minutes")
            if minutes is not None:
                payload["m"] = minutes
            return ("mute", payload)
        if action_norm == "unmute":
            return ("unmute", payload)
        if action_norm in {"set_dry_run", "dryrun_toggle"}:
            return ("dryrun_toggle", payload)
        return (action_norm, payload)

    def _resolve_admin_mode_plugin(self) -> Any:
        initializer = getattr(self.bot_app, "_initialize_mode_plugins", None)
        if callable(initializer):
            try:
                initializer()
            except Exception:
                logger.exception("miniapp failed to initialize mode plugins before admin action")
        mode_registry = getattr(self.bot_app, "mode_registry_service", None) or getattr(self.bot_app, "mode_registry", None)
        if mode_registry is None or not hasattr(mode_registry, "get"):
            return None
        return mode_registry.get("admin")

    async def _build_admin_status_payload(
        self,
        *,
        user: Dict[str, Any],
        session_uid: str,
    ) -> Dict[str, Any]:
        resolved_chat_id, canonical_uid, session = self._resolve_admin_status_session(
            user=user,
            session_uid=session_uid,
        )
        admin_mode = self._resolve_admin_mode_plugin()
        if admin_mode is None or not hasattr(admin_mode, "build_status_payload"):
            raise web.HTTPServiceUnavailable(reason="admin mode unavailable")
        payload = await asyncio.to_thread(
            admin_mode.build_status_payload,
            bot_app=self.bot_app,
            session=session,
            chat_id=int(resolved_chat_id),
        )
        return {
            **dict(payload or {}),
            "session_uid": str(canonical_uid or ""),
            "chat_id": int(resolved_chat_id),
        }

    async def _run_admin_session_action(
        self,
        *,
        user: Dict[str, Any],
        session_uid: str,
        action: str,
        payload: Optional[Dict[str, Any]] = None,
    ) -> bool:
        resolved_chat_id, _canonical_uid, session = self._resolve_admin_status_session(
            user=user,
            session_uid=session_uid,
        )
        admin_mode = self._resolve_admin_mode_plugin()
        if admin_mode is None or not hasattr(admin_mode, "handle_callback"):
            raise web.HTTPServiceUnavailable(reason="admin mode unavailable")
        result = await admin_mode.handle_callback(
            CallbackModel(
                action=str(action or "").strip(),
                chat_id=int(resolved_chat_id),
                user_id=int(user["user_id"]),
                payload=dict(payload or {}),
                raw={},
            ),
            {
                "bot_app": self.bot_app,
                "session": session,
                "chat_id": int(resolved_chat_id),
                "context": None,
                "query": None,
                "mode_id": "admin",
            },
        )
        return bool(getattr(result, "success", True))

    @staticmethod
    def _build_session_option(*, session_uid: str, session: Any, is_admin: bool) -> Dict[str, Any]:
        session_id = str(getattr(session, "id", "") or "").strip()
        session_name = str(getattr(session, "name", "") or session_id or session_uid)
        tool_name = str(getattr(getattr(session, "tool", None), "name", "") or "")
        owner_chat_id = MiniAppRoutes._session_owner_chat_id(session)
        session_title = format_session_selector_label(
            session,
            telegram_user_id=owner_chat_id if is_admin else None,
        )
        return {
            "session_uid": str(session_uid),
            "chat_id": int(owner_chat_id) if owner_chat_id is not None else None,
            "session_id": str(session_id),
            "session_name": session_name,
            "tool": tool_name,
            "label": session_title,
        }

    def _build_status_payload(
        self,
        user: Dict[str, Any],
        *,
        session_uid_filter: str | None = None,
        permissions_cache: Dict[str, Any] | None = None,
    ) -> Dict[str, Any]:
        user_id = int(user["user_id"])
        is_admin = bool(user.get("is_admin", False))
        try:
            from utils.lang import resolve_user_lang as _rsl
            _build_status_lang = _rsl(self.bot_app.config, chat_id=user_id)
        except Exception:
            _build_status_lang = "ru"

        if permissions_cache is not None and "visible_sessions" in permissions_cache:
            visible_sessions = permissions_cache["visible_sessions"]
        else:
            visible_sessions = self._collect_visible_sessions(user_id=user_id, is_admin=is_admin)
            if permissions_cache is not None:
                permissions_cache["visible_sessions"] = visible_sessions

        selected_session = None
        selected_session_uid = str(session_uid_filter or "").strip()
        ordered_visible_sessions = sorted(
            visible_sessions.items(),
            key=lambda item: (
                str(getattr(item[1], "name", "") or "").lower(),
                self._session_sort_key(str(item[0]).split(":", 1)[-1]),
            ),
        )

        if selected_session_uid:
            # We already have visible_sessions, so we can just look up.
            # No need to call self._resolve_visible_session which would re-collect everything.
            selected_session = visible_sessions.get(selected_session_uid)
            if selected_session is None:
                selected_session_uid = ""
        else:
            selected_session_uid = ""
            selected_session = None

        mode_items = self._available_mode_items_for_user(
            self._list_mode_items(),
            chat_id=int(user_id),
            is_admin=is_admin,
        )
        direct_cli_allowed = self._is_direct_cli_allowed_for_user(
            chat_id=int(user_id),
            is_admin=is_admin,
        )

        available_sessions = [
            self._build_session_option(session_uid=suid, session=session, is_admin=is_admin)
            for suid, session in ordered_visible_sessions
        ]
        selected_chat_id = self._session_owner_chat_id(selected_session) if selected_session is not None else None
        selected_payload = (
            self._build_session_payload(
                selected_session,
                session_chat_id=selected_chat_id,
                is_admin=is_admin,
            )
            if selected_session is not None
            else None
        )

        return {
            "server_time_epoch": int(time.time()),
            "server_time_iso": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "user": {
                "user_id": user_id,
                "username": str(user.get("username") or ""),
                "first_name": str(user.get("first_name") or ""),
                "is_admin": is_admin,
            },
            "scope": "admin" if is_admin else "user",
            "selected_session_uid": selected_session_uid,
            "session_count": int(len(available_sessions)),
            "modes": mode_items,
            "direct_cli_allowed": direct_cli_allowed,
            "available_sessions": available_sessions,
            "active_session": selected_payload,
            "sessions": [selected_payload] if selected_payload is not None else [],
            "status_text": (
                selected_payload.get("status_text")
                if isinstance(selected_payload, dict)
                else (
                    t("miniapp.status.session_unavailable", _build_status_lang)
                    if selected_session_uid
                    else t("miniapp.status.no_session", _build_status_lang)
                )
            ),
        }

    async def status_ws(self, request: web.Request) -> web.StreamResponse:
        ticket = str(request.query.get("ticket", "") or "").strip()
        if ticket:
            try:
                user = self._consume_ws_ticket(ticket)
            except web.HTTPException as exc:
                return await self._json_error(int(exc.status), str(exc.reason or "unauthorized"))
        else:
            user = await self._require_access(request)

        session_uid_filter = str(request.query.get("session_uid", "") or "").strip()
        if session_uid_filter:
            try:
                canonical_uid, resolved_session = self._resolve_visible_session(
                    user_id=int(user["user_id"]),
                    is_admin=bool(user.get("is_admin", False)),
                    session_uid=session_uid_filter,
                )
                if resolved_session is not None and canonical_uid:
                    session_uid_filter = canonical_uid
                self.logs.ensure_session_scope_allowed(
                    user_id=int(user["user_id"]),
                    is_admin=bool(user.get("is_admin", False)),
                    session_uid=session_uid_filter,
                )
            except LogsServiceError as exc:
                return await self._json_error(getattr(exc, "status", 400), str(exc))
            except web.HTTPException as exc:
                return await self._json_error(int(exc.status), str(exc.reason or "invalid request"))

        ws = web.WebSocketResponse(heartbeat=20.0)
        await ws.prepare(request)

        try:
            await self._run_status_ws_stream(ws, user=user, session_uid_filter=session_uid_filter or None)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "miniapp status websocket failed",
                extra={
                    "chat_id": int(user["user_id"]),
                    "user_id": int(user["user_id"]),
                    "action": "status_ws",
                    "path": "status",
                    "status": "error",
                    "error": "",
                },
            )
            if not ws.closed:
                await ws.send_json({"type": "error", "error": "status stream failed"})
        finally:
            if not ws.closed:
                await ws.close()
        return ws

    async def _run_status_ws_stream(
        self,
        ws: web.WebSocketResponse,
        *,
        user: Dict[str, Any],
        session_uid_filter: str | None,
    ) -> None:
        receive_task = asyncio.create_task(self._consume_ws_messages(ws))
        stream_task = asyncio.create_task(self._stream_status_updates(ws, user=user, session_uid_filter=session_uid_filter))
        try:
            done, pending = await asyncio.wait({receive_task, stream_task}, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            for task in done:
                exc = task.exception()
                if exc is not None:
                    raise exc
        finally:
            for task in (receive_task, stream_task):
                if not task.done():
                    task.cancel()
            await asyncio.gather(receive_task, stream_task, return_exceptions=True)

    async def _stream_status_updates(
        self,
        ws: web.WebSocketResponse,
        *,
        user: Dict[str, Any],
        session_uid_filter: str | None,
    ) -> None:
        first_message = True
        permissions_cache: Dict[str, Any] = {}
        last_permissions_ts = 0.0
        permissions_ttl = 30.0
        last_inventory_signature: str | None = None
        user_id = int(user["user_id"])
        is_admin = bool(user.get("is_admin", False))

        while not ws.closed:
            try:
                now_ts = time.monotonic()
                inventory_signature = self._visible_session_inventory_signature(user_id=user_id, is_admin=is_admin)
                if inventory_signature != last_inventory_signature:
                    permissions_cache.clear()
                    last_permissions_ts = now_ts
                    last_inventory_signature = inventory_signature
                if now_ts - last_permissions_ts >= permissions_ttl:
                    permissions_cache.clear()
                    last_permissions_ts = now_ts

                status_payload = await asyncio.to_thread(
                    self._build_status_payload,
                    user,
                    session_uid_filter=session_uid_filter,
                    permissions_cache=permissions_cache,
                )
                await ws.send_json({"type": "snapshot" if first_message else "update", "status": status_payload})
                first_message = False
            except asyncio.CancelledError:
                raise
            except Exception:
                if ws.closed:
                    break
                logger.exception("miniapp status stream iteration failed")
                await asyncio.sleep(0.5)
                continue
            await asyncio.sleep(float(self._status_poll_interval_sec))

    async def admin_status(self, request: web.Request) -> web.Response:
        user = await self._require_admin(request)
        session_uid = str(request.query.get("session_uid", "") or "").strip()

        try:
            payload = await self._build_admin_status_payload(
                user=user,
                session_uid=session_uid,
            )
        except web.HTTPException as exc:
            return await self._json_error(int(exc.status), str(exc.reason or "request failed"))
        except Exception:
            logger.exception(
                "miniapp admin status failed",
                extra={
                    "chat_id": int(user["user_id"]),
                    "user_id": int(user["user_id"]),
                    "action": "admin_status",
                    "path": str(session_uid),
                    "status": "error",
                    "error": "",
                },
            )
            return await self._json_error(500, "admin status unavailable")

        return web.json_response(payload)

    async def admin_action(self, request: web.Request) -> web.Response:
        user = await self._require_admin(request)
        try:
            body = await self._read_json_object(request)
        except web.HTTPException as exc:
            return await self._json_error(int(exc.status), str(exc.reason or "invalid request"))

        action = str(body.get("action", "") or "").strip().lower()
        allowed_actions = {
            "enable",
            "disable",
            "rescan",
            "approve_skill_install",
            "reject_skill_install",
            "ack_incident",
            "revoke_approval",
            "approvals_clear",
            "mute",
            "unmute",
            "set_dry_run",
            "dryrun_toggle",
        }
        if action not in allowed_actions:
            return await self._json_error(400, "unsupported admin action")
        session_uid = str(body.get("session_uid", "") or "").strip()
        approval_id = str(body.get("approval_id", "") or "").strip()
        if action in {"approve_skill_install", "reject_skill_install"} and not approval_id:
            return await self._json_error(400, "approval_id is required")

        try:
            result_payload = None
            if action in {"approve_skill_install", "reject_skill_install"}:
                _resolved_chat_id, _canonical_uid, session = self._resolve_admin_status_session(
                    user=user,
                    session_uid=session_uid,
                )
                skill_runtime = getattr(self.bot_app, "mode_skill_runtime", None)
                if skill_runtime is None:
                    raise web.HTTPServiceUnavailable(reason="skill runtime unavailable")
                method = (
                    skill_runtime.approve_pending_install
                    if action == "approve_skill_install"
                    else skill_runtime.reject_pending_install
                )
                result = await asyncio.to_thread(
                    method,
                    session=session,
                    approval_id=approval_id,
                    actor_chat_id=int(user["user_id"]),
                    is_admin=True,
                )
                result_payload = result.to_dict()
                ok = str(result_payload.get("status") or "").strip() == "ok"
            else:
                callback_action, callback_payload = self._map_admin_action_to_callback(
                    action=action, body=body
                )
                ok = await self._run_admin_session_action(
                    user=user,
                    session_uid=session_uid,
                    action=callback_action,
                    payload=callback_payload,
                )
            payload = await self._build_admin_status_payload(
                user=user,
                session_uid=session_uid,
            )
        except web.HTTPException as exc:
            return await self._json_error(int(exc.status), str(exc.reason or "request failed"))
        except Exception:
            logger.exception(
                "miniapp admin action failed",
                extra={
                    "chat_id": int(user["user_id"]),
                    "user_id": int(user["user_id"]),
                    "action": f"admin_{action}",
                    "path": str(session_uid),
                    "status": "error",
                    "error": "",
                },
            )
            return await self._json_error(500, "admin action failed")

        response_payload = {"ok": bool(ok), "action": action, "status": payload}
        if result_payload is not None:
            response_payload["result"] = result_payload
        return web.json_response(response_payload)

    async def _admin_workdir_for_session(self, request, session_uid: str):
        user = await self._require_admin(request)
        try:
            _resolved_chat_id, _canonical_uid, session = self._resolve_admin_status_session(
                user=user,
                session_uid=session_uid,
            )
        except web.HTTPException as exc:
            return None, await self._json_error(int(exc.status), str(exc.reason or "request failed"))
        workdir = str(getattr(session, "workdir", "") or "").strip()
        if not workdir:
            return None, await self._json_error(400, "session workdir is not set")
        return workdir, None

    async def admin_hosts_list(self, request: web.Request) -> web.Response:
        session_uid = str(request.query.get("session_uid", "") or "").strip()
        workdir, error = await self._admin_workdir_for_session(request, session_uid)
        if error is not None:
            return error
        from app.services.ssh_config_loader import load_ssh_config
        hosts = load_ssh_config(workdir)
        items = [{
            "alias": "local",
            "target": "local",
            "host": "",
            "user": "",
            "port": 0,
        }]
        for alias, cfg in hosts.items():
            items.append({
                "alias": str(alias),
                "target": "ssh",
                "host": str(cfg.host or ""),
                "user": str(cfg.user or ""),
                "port": int(cfg.port or 22),
            })
        return web.json_response({"ok": True, "hosts": items})

    async def admin_actions_ssh_get(self, request: web.Request) -> web.Response:
        session_uid = str(request.query.get("session_uid", "") or "").strip()
        workdir, error = await self._admin_workdir_for_session(request, session_uid)
        if error is not None:
            return error
        from modes.admin.config_store import AdminConfigStore, AdminConfigStoreError
        try:
            store = AdminConfigStore(workdir)
            store.ensure_config()
            payload = store.load_config()
        except AdminConfigStoreError as exc:
            return await self._json_error(400, str(exc))
        admin_cfg = payload.get("admin") if isinstance(payload, dict) else None
        actions_cfg = admin_cfg.get("actions") if isinstance(admin_cfg, dict) else None
        ssh_actions = actions_cfg.get("ssh") if isinstance(actions_cfg, dict) else None
        items: list = []
        if isinstance(ssh_actions, dict):
            for action_id, action_payload in ssh_actions.items():
                if not isinstance(action_payload, dict):
                    continue
                argv = action_payload.get("argv")
                argv_list = [str(x) for x in argv] if isinstance(argv, (list, tuple)) else []
                timeout_raw = action_payload.get("timeout_sec")
                try:
                    timeout_sec = float(timeout_raw) if timeout_raw not in (None, "") else None
                except (TypeError, ValueError):
                    timeout_sec = None
                risk_level = str(action_payload.get("risk_level") or "").strip().lower() or "low"
                items.append({
                    "action_id": str(action_id),
                    "argv": argv_list,
                    "timeout_sec": timeout_sec,
                    "risk_level": risk_level,
                    "read_only": bool(action_payload.get("read_only")),
                    "description": str(action_payload.get("description") or ""),
                })
        return web.json_response({"ok": True, "actions": items})

    async def admin_actions_ssh_put(self, request: web.Request) -> web.Response:
        try:
            body = await self._read_json_object(request)
        except web.HTTPException as exc:
            return await self._json_error(int(exc.status), str(exc.reason or "invalid request"))
        session_uid = str(body.get("session_uid", "") or "").strip()
        workdir, error = await self._admin_workdir_for_session(request, session_uid)
        if error is not None:
            return error
        raw_actions = body.get("actions", [])
        if not isinstance(raw_actions, list):
            return await self._json_error(400, "actions must be a list")
        valid_risk = {"low", "medium", "high"}
        normalized: dict = {}
        for item in raw_actions:
            if not isinstance(item, dict):
                return await self._json_error(400, "each action must be an object")
            action_id = str(item.get("action_id") or "").strip()
            if not action_id or not re.match(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$", action_id):
                return await self._json_error(400, f"invalid action_id: {action_id or '(empty)'}")
            argv = item.get("argv")
            if not isinstance(argv, list) or not argv:
                return await self._json_error(400, f"action {action_id}: argv must be non-empty list")
            argv_list = [str(x) for x in argv if str(x).strip()]
            if not argv_list:
                return await self._json_error(400, f"action {action_id}: argv items must be non-empty")
            timeout_raw = item.get("timeout_sec")
            try:
                timeout_sec = float(timeout_raw) if timeout_raw not in (None, "") else 30.0
            except (TypeError, ValueError):
                return await self._json_error(400, f"action {action_id}: timeout_sec must be numeric")
            if timeout_sec <= 0:
                return await self._json_error(400, f"action {action_id}: timeout_sec must be > 0")
            risk_level = str(item.get("risk_level") or "low").strip().lower()
            if risk_level not in valid_risk:
                return await self._json_error(400, f"action {action_id}: risk_level must be low|medium|high")
            read_only = bool(item.get("read_only"))
            if read_only and risk_level != "low":
                return await self._json_error(
                    400,
                    f"action {action_id}: read_only actions must have risk_level=low",
                )
            row: dict = {
                "argv": argv_list,
                "timeout_sec": timeout_sec,
                "risk_level": risk_level,
            }
            if read_only:
                row["read_only"] = True
            description = str(item.get("description") or "").strip()
            if description:
                row["description"] = description
            normalized[action_id] = row
        from modes.admin.config_store import AdminConfigStore, AdminConfigStoreError
        try:
            store = AdminConfigStore(workdir)
            store.ensure_config()
            payload = store.load_config()
            admin_cfg = payload.get("admin") if isinstance(payload, dict) else None
            if not isinstance(admin_cfg, dict):
                return await self._json_error(400, "admin config is missing `admin` mapping")
            actions_cfg = admin_cfg.get("actions")
            if not isinstance(actions_cfg, dict):
                actions_cfg = {}
            actions_cfg["ssh"] = normalized
            admin_cfg["actions"] = actions_cfg
            payload["admin"] = admin_cfg
            store.validate_config(payload)
            await asyncio.to_thread(store._write_config, payload)
        except AdminConfigStoreError as exc:
            return await self._json_error(400, str(exc))
        except Exception:
            logger.exception(
                "miniapp admin_actions_ssh_put failed session_uid=%s",
                session_uid,
            )
            return await self._json_error(500, "admin ssh actions write failed")
        return web.json_response({"ok": True})

    # ---------- admin chat ----------

    def _resolve_admin_chat_service(self) -> Any:
        modes = getattr(self.bot_app, "modes", None)
        if modes is None:
            modes = getattr(self.bot_app, "mode_registry", None)
        plugin = None
        if modes is not None:
            try:
                plugin = modes.get("admin")
            except Exception:
                plugin = None
        return getattr(plugin, "_chat_service", None) if plugin is not None else None

    async def _resolve_session_for_chat(
        self, user: Dict[str, Any], session_uid: str
    ) -> Any:
        try:
            _rid, _cuid, session = self._resolve_admin_status_session(
                user=user, session_uid=session_uid
            )
        except web.HTTPException:
            raise
        return session

    async def admin_chat_messages_get(self, request: web.Request) -> web.Response:
        user = await self._require_admin(request)
        session_uid = str(request.query.get("session_uid", "") or "").strip()
        service = self._resolve_admin_chat_service()
        if service is None:
            return await self._json_error(503, "chat service unavailable")
        try:
            session = await self._resolve_session_for_chat(user, session_uid)
        except web.HTTPException as exc:
            return await self._json_error(int(exc.status), str(exc.reason or "request failed"))
        workdir = str(getattr(session, "workdir", "") or "").strip()
        if not workdir:
            return await self._json_error(400, "session workdir is empty")
        try:
            messages = await asyncio.to_thread(service.list_messages, workdir)
        except Exception:
            logger.exception(
                "miniapp admin_chat_messages_get failed session_uid=%s", session_uid
            )
            return await self._json_error(500, "chat messages load failed")
        return web.json_response({"ok": True, "messages": messages})

    async def admin_chat_messages_post(self, request: web.Request) -> web.Response:
        user = await self._require_admin(request)
        body = await self._read_json_object(request)
        session_uid = str(body.get("session_uid", "") or "").strip()
        text = str(body.get("text", "") or "").strip()
        if not text:
            return await self._json_error(400, "text is required")
        service = self._resolve_admin_chat_service()
        if service is None:
            return await self._json_error(503, "chat service unavailable")
        try:
            session = await self._resolve_session_for_chat(user, session_uid)
        except web.HTTPException as exc:
            return await self._json_error(int(exc.status), str(exc.reason or "request failed"))
        workdir = str(getattr(session, "workdir", "") or "").strip()
        if not workdir:
            return await self._json_error(400, "session workdir is empty")
        try:
            result = await service.send(
                session=session, bot_app=self.bot_app, text=text,
            )
        except Exception:
            logger.exception(
                "miniapp admin_chat_messages_post failed session_uid=%s", session_uid
            )
            return await self._json_error(500, "chat send failed")
        return web.json_response(result)

    async def admin_chat_pending_get(self, request: web.Request) -> web.Response:
        user = await self._require_admin(request)
        session_uid = str(request.query.get("session_uid", "") or "").strip()
        service = self._resolve_admin_chat_service()
        if service is None:
            return await self._json_error(503, "chat service unavailable")
        try:
            session = await self._resolve_session_for_chat(user, session_uid)
        except web.HTTPException as exc:
            return await self._json_error(int(exc.status), str(exc.reason or "request failed"))
        workdir = str(getattr(session, "workdir", "") or "").strip()
        if not workdir:
            return await self._json_error(400, "session workdir is empty")
        try:
            items = await asyncio.to_thread(service.list_pending, workdir)
        except Exception:
            logger.exception(
                "miniapp admin_chat_pending_get failed session_uid=%s", session_uid
            )
            return await self._json_error(500, "chat pending load failed")
        return web.json_response({"ok": True, "items": items})

    async def admin_chat_pending_approve(self, request: web.Request) -> web.Response:
        user = await self._require_admin(request)
        body = await self._read_json_object(request)
        session_uid = str(body.get("session_uid", "") or "").strip()
        approval_id = str(
            request.match_info.get("approval_id")
            or body.get("approval_id")
            or ""
        ).strip()
        if not approval_id:
            return await self._json_error(400, "approval_id is required")
        service = self._resolve_admin_chat_service()
        if service is None:
            return await self._json_error(503, "chat service unavailable")
        try:
            session = await self._resolve_session_for_chat(user, session_uid)
        except web.HTTPException as exc:
            return await self._json_error(int(exc.status), str(exc.reason or "request failed"))
        from utils.lang import resolve_user_lang
        approve_lang = resolve_user_lang(self.bot_app.config, chat_id=self._session_owner_chat_id(session))
        try:
            result = await service.execute_pending(
                session=session, approval_id=approval_id, lang=approve_lang,
            )
        except Exception:
            logger.exception(
                "miniapp admin_chat_pending_approve failed session_uid=%s approval_id=%s",
                session_uid, approval_id,
            )
            return await self._json_error(500, "approve failed")
        return web.json_response(result)

    async def admin_chat_pending_reject(self, request: web.Request) -> web.Response:
        user = await self._require_admin(request)
        body = await self._read_json_object(request)
        session_uid = str(body.get("session_uid", "") or "").strip()
        approval_id = str(
            request.match_info.get("approval_id")
            or body.get("approval_id")
            or ""
        ).strip()
        if not approval_id:
            return await self._json_error(400, "approval_id is required")
        service = self._resolve_admin_chat_service()
        if service is None:
            return await self._json_error(503, "chat service unavailable")
        try:
            session = await self._resolve_session_for_chat(user, session_uid)
        except web.HTTPException as exc:
            return await self._json_error(int(exc.status), str(exc.reason or "request failed"))
        workdir = str(getattr(session, "workdir", "") or "").strip()
        if not workdir:
            return await self._json_error(400, "session workdir is empty")
        try:
            result = await asyncio.to_thread(
                service.reject_pending, workdir, approval_id=approval_id,
            )
        except Exception:
            logger.exception(
                "miniapp admin_chat_pending_reject failed session_uid=%s approval_id=%s",
                session_uid, approval_id,
            )
            return await self._json_error(500, "reject failed")
        return web.json_response(result)

    async def admin_chat_memory_get(self, request: web.Request) -> web.Response:
        user = await self._require_admin(request)
        session_uid = str(request.query.get("session_uid", "") or "").strip()
        service = self._resolve_admin_chat_service()
        if service is None:
            return await self._json_error(503, "chat service unavailable")
        try:
            session = await self._resolve_session_for_chat(user, session_uid)
        except web.HTTPException as exc:
            return await self._json_error(int(exc.status), str(exc.reason or "request failed"))
        workdir = str(getattr(session, "workdir", "") or "").strip()
        if not workdir:
            return await self._json_error(400, "session workdir is empty")
        try:
            text = await asyncio.to_thread(service.get_memory_md, workdir)
        except Exception:
            logger.exception(
                "miniapp admin_chat_memory_get failed session_uid=%s", session_uid
            )
            return await self._json_error(500, "memory read failed")
        return web.json_response({"ok": True, "text": text})

    async def admin_chat_memory_put(self, request: web.Request) -> web.Response:
        user = await self._require_admin(request)
        body = await self._read_json_object(request)
        session_uid = str(body.get("session_uid", "") or "").strip()
        text = str(body.get("text", "") or "")
        service = self._resolve_admin_chat_service()
        if service is None:
            return await self._json_error(503, "chat service unavailable")
        try:
            session = await self._resolve_session_for_chat(user, session_uid)
        except web.HTTPException as exc:
            return await self._json_error(int(exc.status), str(exc.reason or "request failed"))
        workdir = str(getattr(session, "workdir", "") or "").strip()
        if not workdir:
            return await self._json_error(400, "session workdir is empty")
        try:
            await asyncio.to_thread(service.save_memory_md, workdir, text=text)
        except Exception:
            logger.exception(
                "miniapp admin_chat_memory_put failed session_uid=%s", session_uid
            )
            return await self._json_error(500, "memory write failed")
        return web.json_response({"ok": True})

    async def admin_runs(self, request: web.Request) -> web.Response:
        user = await self._require_admin(request)
        session_uid = str(request.query.get("session_uid", "") or "").strip()
        try:
            limit = int(request.query.get("limit", "20") or "20")
        except (TypeError, ValueError):
            limit = 20
        try:
            _resolved_chat_id, _canonical_uid, session = self._resolve_admin_status_session(
                user=user,
                session_uid=session_uid,
            )
        except web.HTTPException as exc:
            return await self._json_error(int(exc.status), str(exc.reason or "request failed"))
        # TODO(M3): obtain RunArtifactStore via service injection instead of bot_app attribute introspection.
        artifact_store = getattr(self.bot_app, "mode_run_artifact_store", None)
        if artifact_store is None:
            return web.json_response({"ok": True, "runs": []})
        try:
            handles = await asyncio.to_thread(
                artifact_store.list_runs,
                session=session,
                mode_id="admin",
                limit=int(max(1, limit)),
            )
        except Exception:
            logger.exception("miniapp admin_runs list failed session_uid=%s", session_uid)
            return await self._json_error(500, "admin runs unavailable")
        rows: List[Dict[str, Any]] = []
        for handle in handles or []:
            try:
                state = await asyncio.to_thread(artifact_store.load_state, handle) or {}
            except Exception:
                state = {}
            rows.append(
                {
                    "run_id": str(handle.run_id),
                    "status": str(state.get("status") or "-"),
                    "phase": str(state.get("phase") or "-"),
                    "started_at": state.get("started_at") or state.get("created_at") or "",
                    "finished_at": state.get("finished_at") or "",
                }
            )
        return web.json_response({"ok": True, "runs": rows})

    async def admin_run_detail(self, request: web.Request) -> web.Response:
        user = await self._require_admin(request)
        session_uid = str(request.query.get("session_uid", "") or "").strip()
        run_id = str(request.match_info.get("run_id", "") or "").strip()
        if not run_id:
            return await self._json_error(400, "run_id is required")
        try:
            events_limit = int(request.query.get("events_limit", "50") or "50")
        except (TypeError, ValueError):
            events_limit = 50
        try:
            _resolved_chat_id, _canonical_uid, session = self._resolve_admin_status_session(
                user=user,
                session_uid=session_uid,
            )
        except web.HTTPException as exc:
            return await self._json_error(int(exc.status), str(exc.reason or "request failed"))
        # TODO(M3): obtain RunArtifactStore via service injection instead of bot_app attribute introspection.
        artifact_store = getattr(self.bot_app, "mode_run_artifact_store", None)
        if artifact_store is None:
            return await self._json_error(503, "run artifact store unavailable")
        try:
            handle = await asyncio.to_thread(
                artifact_store.get_run, session=session, mode_id="admin", run_id=run_id
            )
        except Exception:
            logger.exception(
                "miniapp admin_run_detail get_run failed session_uid=%s run_id=%s",
                session_uid,
                run_id,
            )
            return await self._json_error(500, "admin run detail unavailable")
        if handle is None:
            return await self._json_error(404, "run not found")
        try:
            state = await asyncio.to_thread(artifact_store.load_state, handle) or {}
            plan = await asyncio.to_thread(artifact_store.load_plan, handle) or {}
            checkpoints = await asyncio.to_thread(artifact_store.load_checkpoints, handle) or {}
            events = await asyncio.to_thread(
                artifact_store.load_events_tail, handle, limit=int(max(1, events_limit))
            ) or []
        except Exception:
            logger.exception(
                "miniapp admin_run_detail load failed session_uid=%s run_id=%s",
                session_uid,
                run_id,
            )
            return await self._json_error(500, "admin run detail unavailable")
        return web.json_response(
            {
                "ok": True,
                "run_id": str(handle.run_id),
                "session_uid": str(handle.session_uid),
                "mode_id": str(handle.mode_id),
                "state": dict(state),
                "plan": dict(plan),
                "checkpoints": dict(checkpoints),
                "events": list(events),
            }
        )

    # ------------------------------------------------------------------
    # Admin Autonomy (baseline/drift/memory/runbooks/snapshots) endpoints
    # ------------------------------------------------------------------

    async def _autonomy_resolve_service(
        self, request: web.Request, *, session_uid: str
    ) -> tuple[Any, Any]:
        """Resolve session + AdminAutonomyService or raise HTTPException."""
        user = await self._require_admin(request)
        _resolved_chat_id, _canonical_uid, session = self._resolve_admin_status_session(
            user=user,
            session_uid=session_uid,
        )
        workdir = str(getattr(session, "workdir", "") or "").strip()
        if not workdir:
            raise web.HTTPBadRequest(reason="session workdir is not set")
        from modes.admin.facade import AdminAutonomyService
        service = AdminAutonomyService(workdir)
        return service, user

    def _autonomy_handle_facade_error(self, exc: BaseException) -> web.Response:
        from modes.admin.baseline import BaselineError
        from modes.admin.snapshot_store import AdminSnapshotStoreError
        from modes.admin.memory import ServerMemoryError
        from modes.admin.runbooks import RunbookError
        from modes.admin.runbook_builder import RunbookBuilderError
        from modes.admin.runbook_promoter import RunbookPromoteError
        from modes.admin.runbook_validator import RunbookValidatorError
        from modes.admin.script_runner import ScriptRunnerError
        from modes.admin.script_sources import ScriptSourceError
        user_errors = (
            ValueError,
            BaselineError,
            AdminSnapshotStoreError,
            ServerMemoryError,
            RunbookError,
            RunbookBuilderError,
            RunbookPromoteError,
            RunbookValidatorError,
            ScriptRunnerError,
            ScriptSourceError,
        )
        if isinstance(exc, ScriptRunnerError) and "checksum mismatch" in str(exc):
            return web.json_response(
                {"ok": False, "error": str(exc), "code": "checksum_mismatch"},
                status=409,
            )
        if isinstance(exc, FileNotFoundError):
            return web.json_response({"ok": False, "error": str(exc) or "not found"}, status=404)
        if isinstance(exc, PermissionError):
            return web.json_response({"ok": False, "error": str(exc) or "permission denied"}, status=403)
        if isinstance(exc, user_errors):
            return web.json_response({"ok": False, "error": str(exc)}, status=400)
        return web.json_response({"ok": False, "error": "autonomy operation failed"}, status=500)

    @staticmethod
    def _autonomy_serialize_report(report: Any) -> Dict[str, Any]:
        import dataclasses
        if dataclasses.is_dataclass(report):
            return dataclasses.asdict(report)
        if hasattr(report, "to_dict"):
            return report.to_dict()
        return dict(report or {})

    @staticmethod
    def _autonomy_session_uid_from_request(request: web.Request) -> str:
        return str(request.query.get("session_uid", "") or "").strip()

    async def _autonomy_session_uid_from_body(self, body: Dict[str, Any]) -> str:
        return str(body.get("session_uid", "") or "").strip()

    async def admin_autonomy_dashboard(self, request: web.Request) -> web.Response:
        session_uid = self._autonomy_session_uid_from_request(request)
        try:
            service, _user = await self._autonomy_resolve_service(request, session_uid=session_uid)
        except web.HTTPException as exc:
            return await self._json_error(int(exc.status), str(exc.reason or "request failed"))
        try:
            global_summary = await asyncio.to_thread(service.global_summary)
            servers = await asyncio.to_thread(service.list_servers)
        except Exception as exc:
            logger.exception("miniapp admin_autonomy_dashboard failed session_uid=%s", session_uid)
            return self._autonomy_handle_facade_error(exc)
        return web.json_response(
            {
                "ok": True,
                "global": global_summary,
                "servers": [s.to_dict() for s in servers or []],
            }
        )

    async def admin_autonomy_servers(self, request: web.Request) -> web.Response:
        session_uid = self._autonomy_session_uid_from_request(request)
        try:
            service, _user = await self._autonomy_resolve_service(request, session_uid=session_uid)
        except web.HTTPException as exc:
            return await self._json_error(int(exc.status), str(exc.reason or "request failed"))
        try:
            servers = await asyncio.to_thread(service.list_servers)
        except Exception as exc:
            logger.exception("miniapp admin_autonomy_servers failed session_uid=%s", session_uid)
            return self._autonomy_handle_facade_error(exc)
        return web.json_response({"ok": True, "servers": [s.to_dict() for s in servers or []]})

    async def admin_autonomy_server_detail(self, request: web.Request) -> web.Response:
        session_uid = self._autonomy_session_uid_from_request(request)
        server_id = str(request.match_info.get("server_id", "") or "").strip()
        if not server_id:
            return await self._json_error(400, "server_id is required")
        try:
            service, _user = await self._autonomy_resolve_service(request, session_uid=session_uid)
        except web.HTTPException as exc:
            return await self._json_error(int(exc.status), str(exc.reason or "request failed"))
        try:
            summary = await asyncio.to_thread(service.get_server_summary, server_id)
            if summary is None:
                return await self._json_error(404, f"server {server_id!r} not found")
            dossier = await asyncio.to_thread(service.get_dossier, server_id)
        except Exception as exc:
            logger.exception(
                "miniapp admin_autonomy_server_detail failed session_uid=%s server_id=%s",
                session_uid,
                server_id,
            )
            return self._autonomy_handle_facade_error(exc)
        return web.json_response(
            {
                "ok": True,
                "summary": summary.to_dict(),
                "dossier": dossier,
            }
        )

    async def admin_autonomy_rescan_server(self, request: web.Request) -> web.Response:
        session_uid = str(request.query.get("session_uid", "") or "").strip()
        if not session_uid:
            # support body-based session_uid as well
            try:
                body = await self._read_json_object(request)
            except web.HTTPException:
                body = {}
            session_uid = str(body.get("session_uid", "") or "").strip()
        server_id = str(request.match_info.get("server_id", "") or "").strip()
        if not server_id:
            return await self._json_error(400, "server_id is required")
        try:
            service, _user = await self._autonomy_resolve_service(request, session_uid=session_uid)
        except web.HTTPException as exc:
            return await self._json_error(int(exc.status), str(exc.reason or "request failed"))
        try:
            report = await service.rescan_server(server_id)
        except ValueError as exc:
            return await self._json_error(404, str(exc))
        except Exception as exc:
            logger.exception(
                "miniapp admin_autonomy_rescan_server failed session_uid=%s server_id=%s",
                session_uid,
                server_id,
            )
            return self._autonomy_handle_facade_error(exc)
        return web.json_response({"ok": True, "report": self._autonomy_serialize_report(report)})

    async def admin_autonomy_rescan_all(self, request: web.Request) -> web.Response:
        session_uid = str(request.query.get("session_uid", "") or "").strip()
        if not session_uid:
            try:
                body = await self._read_json_object(request)
            except web.HTTPException:
                body = {}
            session_uid = str(body.get("session_uid", "") or "").strip()
        try:
            service, _user = await self._autonomy_resolve_service(request, session_uid=session_uid)
        except web.HTTPException as exc:
            return await self._json_error(int(exc.status), str(exc.reason or "request failed"))
        try:
            reports = await service.rescan_all()
        except Exception as exc:
            logger.exception("miniapp admin_autonomy_rescan_all failed session_uid=%s", session_uid)
            return self._autonomy_handle_facade_error(exc)
        return web.json_response(
            {
                "ok": True,
                "reports": [self._autonomy_serialize_report(r) for r in reports or []],
            }
        )

    async def admin_autonomy_baseline_get(self, request: web.Request) -> web.Response:
        session_uid = self._autonomy_session_uid_from_request(request)
        server_id = str(request.match_info.get("server_id", "") or "").strip()
        if not server_id:
            return await self._json_error(400, "server_id is required")
        try:
            service, _user = await self._autonomy_resolve_service(request, session_uid=session_uid)
        except web.HTTPException as exc:
            return await self._json_error(int(exc.status), str(exc.reason or "request failed"))
        try:
            payload = await asyncio.to_thread(service.get_baseline, server_id)
        except Exception as exc:
            logger.exception(
                "miniapp admin_autonomy_baseline_get failed session_uid=%s server_id=%s",
                session_uid,
                server_id,
            )
            return self._autonomy_handle_facade_error(exc)
        return web.json_response({"ok": True, "baseline": payload})

    async def admin_autonomy_baseline_accept(self, request: web.Request) -> web.Response:
        server_id = str(request.match_info.get("server_id", "") or "").strip()
        if not server_id:
            return await self._json_error(400, "server_id is required")
        try:
            body = await self._read_json_object(request)
        except web.HTTPException as exc:
            return await self._json_error(int(exc.status), str(exc.reason or "invalid request"))
        session_uid = str(body.get("session_uid", "") or request.query.get("session_uid", "") or "").strip()
        try:
            service, _user = await self._autonomy_resolve_service(request, session_uid=session_uid)
        except web.HTTPException as exc:
            return await self._json_error(int(exc.status), str(exc.reason or "request failed"))
        try:
            result = await asyncio.to_thread(service.accept_baseline, server_id)
        except Exception as exc:
            logger.exception(
                "miniapp admin_autonomy_baseline_accept failed session_uid=%s server_id=%s",
                session_uid,
                server_id,
            )
            return self._autonomy_handle_facade_error(exc)
        return web.json_response({"ok": True, "result": result})

    async def admin_autonomy_baseline_discard(self, request: web.Request) -> web.Response:
        server_id = str(request.match_info.get("server_id", "") or "").strip()
        if not server_id:
            return await self._json_error(400, "server_id is required")
        session_uid = str(request.query.get("session_uid", "") or "").strip()
        if not session_uid:
            try:
                body = await self._read_json_object(request)
            except web.HTTPException:
                body = {}
            session_uid = str(body.get("session_uid", "") or "").strip()
        try:
            service, _user = await self._autonomy_resolve_service(request, session_uid=session_uid)
        except web.HTTPException as exc:
            return await self._json_error(int(exc.status), str(exc.reason or "request failed"))
        try:
            removed = await asyncio.to_thread(service.discard_baseline_proposal, server_id)
        except Exception as exc:
            logger.exception(
                "miniapp admin_autonomy_baseline_discard failed session_uid=%s server_id=%s",
                session_uid,
                server_id,
            )
            return self._autonomy_handle_facade_error(exc)
        return web.json_response({"ok": True, "removed": bool(removed)})

    async def admin_autonomy_drifts_list(self, request: web.Request) -> web.Response:
        session_uid = self._autonomy_session_uid_from_request(request)
        server_id = str(request.match_info.get("server_id", "") or "").strip()
        if not server_id:
            return await self._json_error(400, "server_id is required")
        try:
            limit = int(request.query.get("limit", "50") or "50")
        except (TypeError, ValueError):
            limit = 50
        severity_min = str(request.query.get("severity_min", "") or "").strip() or None
        open_only_raw = str(request.query.get("open_only", "true") or "true").strip().lower()
        open_only = open_only_raw not in {"0", "false", "no", ""}
        try:
            service, _user = await self._autonomy_resolve_service(request, session_uid=session_uid)
        except web.HTTPException as exc:
            return await self._json_error(int(exc.status), str(exc.reason or "request failed"))
        try:
            drifts = await asyncio.to_thread(
                service.list_drifts,
                server_id,
                limit=limit,
                severity_min=severity_min,
                open_only=open_only,
            )
        except Exception as exc:
            logger.exception(
                "miniapp admin_autonomy_drifts_list failed session_uid=%s server_id=%s",
                session_uid,
                server_id,
            )
            return self._autonomy_handle_facade_error(exc)
        return web.json_response({"ok": True, "drifts": list(drifts or [])})

    async def admin_autonomy_drift_ack(self, request: web.Request) -> web.Response:
        server_id = str(request.match_info.get("server_id", "") or "").strip()
        drift_id_raw = str(request.match_info.get("drift_id", "") or "").strip()
        if not server_id or not drift_id_raw:
            return await self._json_error(400, "server_id and drift_id are required")
        try:
            drift_id = int(drift_id_raw)
        except (TypeError, ValueError):
            return await self._json_error(400, "drift_id must be integer")
        try:
            body = await self._read_json_object(request)
        except web.HTTPException:
            body = {}
        session_uid = str(body.get("session_uid", "") or request.query.get("session_uid", "") or "").strip()
        by = str(body.get("by") or "").strip() or None
        try:
            service, _user = await self._autonomy_resolve_service(request, session_uid=session_uid)
        except web.HTTPException as exc:
            return await self._json_error(int(exc.status), str(exc.reason or "request failed"))
        try:
            acked = await asyncio.to_thread(service.ack_drift, server_id, drift_id, by=by)
        except Exception as exc:
            logger.exception(
                "miniapp admin_autonomy_drift_ack failed session_uid=%s server_id=%s drift_id=%s",
                session_uid,
                server_id,
                drift_id,
            )
            return self._autonomy_handle_facade_error(exc)
        return web.json_response({"ok": True, "acknowledged": bool(acked)})

    async def admin_autonomy_snapshots(self, request: web.Request) -> web.Response:
        session_uid = self._autonomy_session_uid_from_request(request)
        server_id = str(request.match_info.get("server_id", "") or "").strip()
        check_id = str(request.query.get("check_id", "") or "").strip()
        if not server_id:
            return await self._json_error(400, "server_id is required")
        if not check_id:
            return await self._json_error(400, "check_id query param is required")
        try:
            limit = int(request.query.get("limit", "100") or "100")
        except (TypeError, ValueError):
            limit = 100
        since_ts_raw = str(request.query.get("since_ts", "") or "").strip()
        since_ts: Optional[int] = None
        if since_ts_raw:
            try:
                since_ts = int(since_ts_raw)
            except (TypeError, ValueError):
                since_ts = None
        try:
            service, _user = await self._autonomy_resolve_service(request, session_uid=session_uid)
        except web.HTTPException as exc:
            return await self._json_error(int(exc.status), str(exc.reason or "request failed"))
        try:
            snapshots = await asyncio.to_thread(
                service.get_snapshots,
                server_id,
                check_id,
                limit=limit,
                since_ts=since_ts,
            )
        except Exception as exc:
            logger.exception(
                "miniapp admin_autonomy_snapshots failed session_uid=%s server_id=%s check_id=%s",
                session_uid,
                server_id,
                check_id,
            )
            return self._autonomy_handle_facade_error(exc)
        return web.json_response({"ok": True, "snapshots": list(snapshots or [])})

    async def admin_autonomy_snapshot_checks(self, request: web.Request) -> web.Response:
        session_uid = self._autonomy_session_uid_from_request(request)
        server_id = str(request.match_info.get("server_id", "") or "").strip()
        if not server_id:
            return await self._json_error(400, "server_id is required")
        try:
            service, _user = await self._autonomy_resolve_service(request, session_uid=session_uid)
        except web.HTTPException as exc:
            return await self._json_error(int(exc.status), str(exc.reason or "request failed"))
        try:
            checks = await asyncio.to_thread(service.list_snapshot_checks, server_id)
        except Exception as exc:
            logger.exception(
                "miniapp admin_autonomy_snapshot_checks failed session_uid=%s server_id=%s",
                session_uid,
                server_id,
            )
            return self._autonomy_handle_facade_error(exc)
        return web.json_response({"ok": True, "checks": list(checks or [])})

    async def admin_autonomy_memory_get(self, request: web.Request) -> web.Response:
        session_uid = self._autonomy_session_uid_from_request(request)
        server_id = str(request.match_info.get("server_id", "") or "").strip()
        if not server_id:
            return await self._json_error(400, "server_id is required")
        try:
            service, _user = await self._autonomy_resolve_service(request, session_uid=session_uid)
        except web.HTTPException as exc:
            return await self._json_error(int(exc.status), str(exc.reason or "request failed"))
        try:
            memory = await asyncio.to_thread(service.get_memory, server_id)
        except Exception as exc:
            logger.exception(
                "miniapp admin_autonomy_memory_get failed session_uid=%s server_id=%s",
                session_uid,
                server_id,
            )
            return self._autonomy_handle_facade_error(exc)
        return web.json_response({"ok": True, "memory": memory})

    async def admin_autonomy_memory_fact_put(self, request: web.Request) -> web.Response:
        server_id = str(request.match_info.get("server_id", "") or "").strip()
        if not server_id:
            return await self._json_error(400, "server_id is required")
        try:
            body = await self._read_json_object(request)
        except web.HTTPException as exc:
            return await self._json_error(int(exc.status), str(exc.reason or "invalid request"))
        session_uid = str(body.get("session_uid", "") or request.query.get("session_uid", "") or "").strip()
        key = str(body.get("key") or "").strip()
        if not key:
            return await self._json_error(400, "fact key is required")
        value = body.get("value")
        by = str(body.get("by") or "").strip() or None
        try:
            service, _user = await self._autonomy_resolve_service(request, session_uid=session_uid)
        except web.HTTPException as exc:
            return await self._json_error(int(exc.status), str(exc.reason or "request failed"))
        try:
            facts = await asyncio.to_thread(
                service.update_memory_fact, server_id, key=key, value=value, by=by
            )
        except Exception as exc:
            logger.exception(
                "miniapp admin_autonomy_memory_fact_put failed session_uid=%s server_id=%s key=%s",
                session_uid,
                server_id,
                key,
            )
            return self._autonomy_handle_facade_error(exc)
        return web.json_response({"ok": True, "facts": facts})

    async def admin_autonomy_memory_fact_delete(self, request: web.Request) -> web.Response:
        server_id = str(request.match_info.get("server_id", "") or "").strip()
        key = str(request.match_info.get("key", "") or "").strip()
        if not server_id or not key:
            return await self._json_error(400, "server_id and key are required")
        session_uid = str(request.query.get("session_uid", "") or "").strip()
        if not session_uid:
            try:
                body = await self._read_json_object(request)
            except web.HTTPException:
                body = {}
            session_uid = str(body.get("session_uid", "") or "").strip()
        try:
            service, _user = await self._autonomy_resolve_service(request, session_uid=session_uid)
        except web.HTTPException as exc:
            return await self._json_error(int(exc.status), str(exc.reason or "request failed"))
        try:
            removed = await asyncio.to_thread(service.delete_memory_fact, server_id, key)
        except Exception as exc:
            logger.exception(
                "miniapp admin_autonomy_memory_fact_delete failed session_uid=%s server_id=%s key=%s",
                session_uid,
                server_id,
                key,
            )
            return self._autonomy_handle_facade_error(exc)
        return web.json_response({"ok": True, "removed": bool(removed)})

    async def admin_autonomy_memory_note(self, request: web.Request) -> web.Response:
        server_id = str(request.match_info.get("server_id", "") or "").strip()
        if not server_id:
            return await self._json_error(400, "server_id is required")
        try:
            body = await self._read_json_object(request)
        except web.HTTPException as exc:
            return await self._json_error(int(exc.status), str(exc.reason or "invalid request"))
        session_uid = str(body.get("session_uid", "") or request.query.get("session_uid", "") or "").strip()
        text = str(body.get("text") or "").strip()
        if not text:
            return await self._json_error(400, "text is required")
        source = str(body.get("source") or "manual").strip() or "manual"
        tags_raw = body.get("tags") or []
        if isinstance(tags_raw, list):
            tags = [str(t) for t in tags_raw if str(t or "").strip()]
        else:
            tags = []
        try:
            service, _user = await self._autonomy_resolve_service(request, session_uid=session_uid)
        except web.HTTPException as exc:
            return await self._json_error(int(exc.status), str(exc.reason or "request failed"))
        try:
            entry = await asyncio.to_thread(
                service.append_memory_note, server_id, text, source=source, tags=tags
            )
        except Exception as exc:
            logger.exception(
                "miniapp admin_autonomy_memory_note failed session_uid=%s server_id=%s",
                session_uid,
                server_id,
            )
            return self._autonomy_handle_facade_error(exc)
        return web.json_response(
            {
                "ok": True,
                "entry": {
                    "ts": str(getattr(entry, "ts", "") or ""),
                    "source": str(getattr(entry, "source", "") or ""),
                    "text": str(getattr(entry, "text", "") or ""),
                },
            }
        )

    async def admin_autonomy_memory_compact(self, request: web.Request) -> web.Response:
        server_id = str(request.match_info.get("server_id", "") or "").strip()
        if not server_id:
            return await self._json_error(400, "server_id is required")
        try:
            body = await self._read_json_object(request)
        except web.HTTPException:
            body = {}
        session_uid = str(body.get("session_uid", "") or request.query.get("session_uid", "") or "").strip()
        force = bool(body.get("force", False))
        try:
            service, _user = await self._autonomy_resolve_service(request, session_uid=session_uid)
        except web.HTTPException as exc:
            return await self._json_error(int(exc.status), str(exc.reason or "request failed"))
        try:
            result = await asyncio.to_thread(service.compact_memory, server_id, force=force)
        except Exception as exc:
            logger.exception(
                "miniapp admin_autonomy_memory_compact failed session_uid=%s server_id=%s",
                session_uid,
                server_id,
            )
            return self._autonomy_handle_facade_error(exc)
        return web.json_response({"ok": True, "result": result})

    async def admin_autonomy_runbooks_list(self, request: web.Request) -> web.Response:
        session_uid = self._autonomy_session_uid_from_request(request)
        filter_server_id = str(request.query.get("server_id", "") or "").strip() or None
        tags_raw = str(request.query.get("tags", "") or "").strip()
        tags = [t.strip() for t in tags_raw.split(",") if t.strip()] if tags_raw else []
        try:
            limit = int(request.query.get("limit", "20") or "20")
        except (TypeError, ValueError):
            limit = 20
        try:
            service, _user = await self._autonomy_resolve_service(request, session_uid=session_uid)
        except web.HTTPException as exc:
            return await self._json_error(int(exc.status), str(exc.reason or "request failed"))
        try:
            runbooks = await asyncio.to_thread(
                service.list_runbook_summary,
                server_id=filter_server_id,
                tags=tags,
                limit=limit,
            )
        except Exception as exc:
            logger.exception(
                "miniapp admin_autonomy_runbooks_list failed session_uid=%s server_id=%s",
                session_uid,
                filter_server_id,
            )
            return self._autonomy_handle_facade_error(exc)
        return web.json_response({"ok": True, "runbooks": list(runbooks or [])})

    async def admin_autonomy_runbook_get(self, request: web.Request) -> web.Response:
        session_uid = self._autonomy_session_uid_from_request(request)
        runbook_id = str(request.match_info.get("runbook_id", "") or "").strip()
        if not runbook_id:
            return await self._json_error(400, "runbook_id is required")
        filter_server_id = str(request.query.get("server_id", "") or "").strip() or None
        try:
            service, _user = await self._autonomy_resolve_service(request, session_uid=session_uid)
        except web.HTTPException as exc:
            return await self._json_error(int(exc.status), str(exc.reason or "request failed"))
        try:
            runbook = await asyncio.to_thread(
                service.get_runbook, runbook_id, server_id=filter_server_id
            )
        except Exception as exc:
            logger.exception(
                "miniapp admin_autonomy_runbook_get failed session_uid=%s runbook_id=%s",
                session_uid,
                runbook_id,
            )
            return self._autonomy_handle_facade_error(exc)
        if runbook is None:
            return await self._json_error(404, f"runbook {runbook_id!r} not found")
        return web.json_response({"ok": True, "runbook": runbook.as_dict()})

    async def admin_autonomy_scripts_scan(self, request: web.Request) -> web.Response:
        try:
            body = await self._read_json_object(request)
        except web.HTTPException:
            body = {}
        session_uid = await self._autonomy_session_uid_from_body(body)
        directory = str(body.get("directory", "") or "").strip()
        if not directory:
            return await self._json_error(400, "directory is required")
        try:
            service, _user = await self._autonomy_resolve_service(request, session_uid=session_uid)
        except web.HTTPException as exc:
            return await self._json_error(int(exc.status), str(exc.reason or "request failed"))
        try:
            files = await asyncio.to_thread(service.scan_script_sources, directory)
        except Exception as exc:
            logger.exception(
                "miniapp admin_autonomy_scripts_scan failed session_uid=%s dir=%s",
                session_uid,
                directory,
            )
            return self._autonomy_handle_facade_error(exc)
        return web.json_response({
            "ok": True,
            "files": [
                {
                    "path": str(f.path),
                    "name": f.name,
                    "size_bytes": int(f.size_bytes),
                    "sha1": f.sha1,
                }
                for f in files
            ],
        })

    async def admin_autonomy_scripts_read(self, request: web.Request) -> web.Response:
        try:
            body = await self._read_json_object(request)
        except web.HTTPException:
            body = {}
        session_uid = await self._autonomy_session_uid_from_body(body)
        path = str(body.get("path", "") or "").strip()
        if not path:
            return await self._json_error(400, "path is required")
        try:
            service, _user = await self._autonomy_resolve_service(request, session_uid=session_uid)
        except web.HTTPException as exc:
            return await self._json_error(int(exc.status), str(exc.reason or "request failed"))
        try:
            text = await asyncio.to_thread(service.read_script_from_source, path)
        except Exception as exc:
            logger.exception(
                "miniapp admin_autonomy_scripts_read failed session_uid=%s path=%s",
                session_uid,
                path,
            )
            return self._autonomy_handle_facade_error(exc)
        return web.json_response({"ok": True, "text": text})

    async def admin_autonomy_runbook_build(self, request: web.Request) -> web.Response:
        try:
            body = await self._read_json_object(request)
        except web.HTTPException:
            body = {}
        session_uid = await self._autonomy_session_uid_from_body(body)
        title = str(body.get("title", "") or "").strip()
        dev_server_id = str(body.get("dev_server_id", "") or "").strip()
        rb_id = str(body.get("rb_id", "") or "").strip() or None
        force = bool(body.get("force", False))
        scripts_raw = body.get("scripts") or []
        if not isinstance(scripts_raw, list) or not scripts_raw:
            return await self._json_error(400, "scripts is required")
        for entry in scripts_raw:
            if not isinstance(entry, dict) or not str(entry.get("source_path") or "").strip():
                return await self._json_error(400, "each script entry must include source_path")
        tags = body.get("tags") or []
        triggers = body.get("triggers") or []
        description = str(body.get("description", "") or "").strip()
        try:
            service, _user = await self._autonomy_resolve_service(request, session_uid=session_uid)
        except web.HTTPException as exc:
            return await self._json_error(int(exc.status), str(exc.reason or "request failed"))
        try:
            runbook = await asyncio.to_thread(
                service.create_runbook_from_scripts,
                title=title,
                dev_server_id=dev_server_id,
                scripts=scripts_raw,
                rb_id=rb_id,
                tags=tags,
                triggers=triggers,
                description=description,
                force=force,
            )
        except Exception as exc:
            logger.exception(
                "miniapp admin_autonomy_runbook_build failed session_uid=%s title=%s",
                session_uid,
                title,
            )
            return self._autonomy_handle_facade_error(exc)
        return web.json_response({"ok": True, "runbook": runbook.as_dict()})

    async def admin_autonomy_runbook_validate(self, request: web.Request) -> web.Response:
        try:
            body = await self._read_json_object(request)
        except web.HTTPException:
            body = {}
        session_uid = await self._autonomy_session_uid_from_body(body)
        runbook_id = str(request.match_info.get("runbook_id", "") or "").strip()
        if not runbook_id:
            return await self._json_error(400, "runbook_id is required")
        try:
            service, _user = await self._autonomy_resolve_service(request, session_uid=session_uid)
        except web.HTTPException as exc:
            return await self._json_error(int(exc.status), str(exc.reason or "request failed"))
        try:
            report = await service.validate_runbook(runbook_id)
        except Exception as exc:
            logger.exception(
                "miniapp admin_autonomy_runbook_validate failed session_uid=%s runbook_id=%s",
                session_uid,
                runbook_id,
            )
            return self._autonomy_handle_facade_error(exc)
        return web.json_response({"ok": True, "report": report.to_dict()})

    async def admin_autonomy_runbook_promote(self, request: web.Request) -> web.Response:
        try:
            body = await self._read_json_object(request)
        except web.HTTPException:
            body = {}
        session_uid = await self._autonomy_session_uid_from_body(body)
        runbook_id = str(request.match_info.get("runbook_id", "") or "").strip()
        if not runbook_id:
            return await self._json_error(400, "runbook_id is required")
        add_servers = body.get("add_servers") or []
        if not isinstance(add_servers, list) or not add_servers:
            return await self._json_error(400, "add_servers is required")
        confidence_raw = body.get("confidence")
        confidence: Optional[float]
        if confidence_raw is None or confidence_raw == "":
            confidence = None
        else:
            try:
                confidence = float(confidence_raw)
            except (TypeError, ValueError):
                return await self._json_error(400, "confidence must be numeric")
        run_validation = bool(body.get("run_validation", True))
        try:
            service, _user = await self._autonomy_resolve_service(request, session_uid=session_uid)
        except web.HTTPException as exc:
            return await self._json_error(int(exc.status), str(exc.reason or "request failed"))
        try:
            result = await service.promote_runbook(
                runbook_id,
                add_servers=add_servers,
                confidence=confidence,
                run_validation=run_validation,
            )
        except Exception as exc:
            logger.exception(
                "miniapp admin_autonomy_runbook_promote failed session_uid=%s runbook_id=%s",
                session_uid,
                runbook_id,
            )
            return self._autonomy_handle_facade_error(exc)
        return web.json_response({"ok": True, "result": result.to_dict()})

    async def admin_autonomy_runbook_run_step(self, request: web.Request) -> web.Response:
        try:
            body = await self._read_json_object(request)
        except web.HTTPException:
            body = {}
        session_uid = await self._autonomy_session_uid_from_body(body)
        runbook_id = str(request.match_info.get("runbook_id", "") or "").strip()
        step_name = str(body.get("step_name", "") or "").strip()
        server_id = str(body.get("server_id", "") or "").strip()
        if not runbook_id or not step_name or not server_id:
            return await self._json_error(400, "runbook_id, step_name and server_id are required")
        dry_run = bool(body.get("dry_run", True))
        verify_checksum = bool(body.get("verify_checksum", True))
        timeout_raw = body.get("timeout_sec")
        timeout_sec: Optional[float] = None
        if timeout_raw not in (None, ""):
            try:
                timeout_sec = float(timeout_raw)
            except (TypeError, ValueError):
                return await self._json_error(400, "timeout_sec must be numeric")
        try:
            service, _user = await self._autonomy_resolve_service(request, session_uid=session_uid)
        except web.HTTPException as exc:
            return await self._json_error(int(exc.status), str(exc.reason or "request failed"))
        try:
            result = await service.run_runbook_step(
                rb_id=runbook_id,
                step_name=step_name,
                server_id=server_id,
                dry_run=dry_run,
                verify_checksum=verify_checksum,
                timeout_sec=timeout_sec,
            )
        except Exception as exc:
            logger.exception(
                "miniapp admin_autonomy_runbook_run_step failed session_uid=%s runbook_id=%s step=%s",
                session_uid,
                runbook_id,
                step_name,
            )
            return self._autonomy_handle_facade_error(exc)
        return web.json_response({"ok": True, "result": result.to_dict()})

    async def admin_autonomy_maintenance_daily(self, request: web.Request) -> web.Response:
        try:
            body = await self._read_json_object(request)
        except web.HTTPException:
            body = {}
        session_uid = str(body.get("session_uid", "") or request.query.get("session_uid", "") or "").strip()
        try:
            service, _user = await self._autonomy_resolve_service(request, session_uid=session_uid)
        except web.HTTPException as exc:
            return await self._json_error(int(exc.status), str(exc.reason or "request failed"))
        try:
            report = await asyncio.to_thread(service.run_daily_maintenance)
        except Exception as exc:
            logger.exception("miniapp admin_autonomy_maintenance_daily failed session_uid=%s", session_uid)
            return self._autonomy_handle_facade_error(exc)
        return web.json_response({"ok": True, "report": report})

    async def admin_autonomy_prereqs_get(self, request: web.Request) -> web.Response:
        session_uid = self._autonomy_session_uid_from_request(request)
        server_id = str(request.match_info.get("server_id", "") or "").strip()
        if not server_id:
            return await self._json_error(400, "server_id is required")
        try:
            service, _user = await self._autonomy_resolve_service(request, session_uid=session_uid)
        except web.HTTPException as exc:
            return await self._json_error(int(exc.status), str(exc.reason or "request failed"))
        try:
            report = await asyncio.to_thread(service.check_server_prereqs, server_id)
        except Exception as exc:
            logger.exception(
                "miniapp admin_autonomy_prereqs_get failed session_uid=%s server_id=%s",
                session_uid,
                server_id,
            )
            return self._autonomy_handle_facade_error(exc)
        payload = report.to_dict() if hasattr(report, "to_dict") else {}
        return web.json_response({"ok": True, "report": payload})

    async def admin_autonomy_prereqs_bootstrap(self, request: web.Request) -> web.Response:
        try:
            body = await self._read_json_object(request)
        except web.HTTPException:
            body = {}
        session_uid = await self._autonomy_session_uid_from_body(body)
        server_id = str(request.match_info.get("server_id", "") or "").strip()
        if not server_id:
            return await self._json_error(400, "server_id is required")
        force = bool(body.get("force", False))
        try:
            service, _user = await self._autonomy_resolve_service(request, session_uid=session_uid)
        except web.HTTPException as exc:
            return await self._json_error(int(exc.status), str(exc.reason or "request failed"))
        try:
            result = await asyncio.to_thread(
                service.generate_bootstrap_runbook, server_id, force=force,
            )
        except Exception as exc:
            logger.exception(
                "miniapp admin_autonomy_prereqs_bootstrap failed session_uid=%s server_id=%s",
                session_uid,
                server_id,
            )
            return self._autonomy_handle_facade_error(exc)
        return web.json_response({"ok": True, **(result or {})})

    async def session_settings_get(self, request: web.Request) -> web.Response:
        user = await self._require_access(request)
        session_uid = str(request.match_info.get("uid", "") or "").strip()
        if not session_uid:
            return await self._json_error(400, "session uid is required in path")

        _canonical_uid, session = self._resolve_visible_session(
            user_id=int(user["user_id"]),
            is_admin=bool(user.get("is_admin", False)),
            session_uid=session_uid,
        )
        if session is None:
            return await self._json_error(404, "session not found")

        from app.services.ssh_config_loader import load_ssh_config, ssh_config_exists, ssh_remote_available
        from sessions.session_state_access import (
            get_active_mode,
            get_remote_control_host_alias,
            is_orchestrator_enabled,
            is_remote_control_enabled,
            is_ssh_remote_enabled,
        )

        workdir = str(getattr(session, "workdir", "") or "").strip()
        ssh_config_present = ssh_config_exists(workdir) if workdir else False
        ssh_available = ssh_remote_available(workdir) if workdir else False
        is_admin = bool(user.get("is_admin", False))
        chat_id = int(user["user_id"])
        mode_items = self._available_mode_items_for_user(
            self._list_mode_items(),
            chat_id=chat_id,
            is_admin=is_admin,
        )
        direct_cli_allowed = self._is_direct_cli_allowed_for_user(chat_id=chat_id, is_admin=is_admin)

        # Remote control fields
        rc_enabled = is_remote_control_enabled(session)
        rc_alias = get_remote_control_host_alias(session)
        # Filter hosts by ACL
        all_hosts = load_ssh_config(workdir) if workdir else {}
        rc_hosts = {}
        for alias, cfg in all_hosts.items():
            if is_admin or cfg.allowed_chat_ids is None or chat_id in cfg.allowed_chat_ids:
                rc_hosts[alias] = {
                    "host": cfg.host,
                    "user": cfg.user,
                    "remote_project_root": cfg.remote_project_root,
                    "description": cfg.description,
                }

        # Effective state
        rc_svc = getattr(self.bot_app, "remote_control_service", None)
        if rc_svc is not None:
            effective = rc_svc.compute_effective_state(session, all_hosts)
            effective_payload = {
                "execution_target": effective.execution_target.value,
                "host_alias": effective.host_alias,
                "remote_project_root": effective.remote_project_root,
                "git_available": effective.git_available,
            }
        else:
            effective_payload = {
                "execution_target": "local",
                "host_alias": None,
                "remote_project_root": None,
                "git_available": True,
            }

        from session import (
            available_execution_backends,
            get_session_execution_backend,
        )
        from app.services.cli_backends.tmux_backend import TmuxExecutionBackend

        execution_backend = get_session_execution_backend(session)
        available_backends = available_execution_backends(session)
        backend_blockers = ["configured in settings"]
        active_execution_backend = str(getattr(session, "_active_execution_backend", "") or "none")
        tmux_paths = TmuxExecutionBackend().paths(session)
        tmux_state = TmuxExecutionBackend._read_state(tmux_paths)
        tmux_status = (
            {
                "state": str(tmux_state.get("state") or "unknown"),
                "session_name": str(tmux_state.get("session_name") or tmux_paths["session_name"]),
                "pane_target": str(tmux_state.get("pane_target") or tmux_paths["pane_target"]),
                "last_activity_at": self._safe_status_value(tmux_state.get("last_activity_at")),
            }
            if tmux_state
            else None
        )

        return web.json_response({
            "ok": True,
            "settings": {
                "name": str(getattr(session, "name", "") or ""),
                "active_cli": str(getattr(session, "active_cli", "") or ""),
                "active_mode": str(get_active_mode(session, "") or ""),
                "execution_backend": execution_backend,
                "has_live_tmux": TmuxExecutionBackend().is_tmux_live(session),
                "active_execution_backend": active_execution_backend,
                "ssh_remote_enabled": bool(is_ssh_remote_enabled(session)),
                "orchestrator_enabled": bool(is_orchestrator_enabled(session)),
                "remote_control_enabled": rc_enabled,
                "remote_control_host_alias": rc_alias,
            },
            "available": {
                "ssh_config_exists": ssh_config_present,
                "ssh_available": ssh_available,
                "project_workdir": workdir,
                "remote_control_hosts": rc_hosts,
                "modes": mode_items,
                "direct_cli_allowed": direct_cli_allowed,
                "execution_backends": available_backends,
                "backend_switch_allowed": False,
                "backend_switch_blockers": backend_blockers,
            },
            "tmux_status": tmux_status,
            # Backward-compatible duplicate during contract migration.
            "remote_control_hosts": rc_hosts,
            "effective": effective_payload,
        })

    async def session_settings_update(self, request: web.Request) -> web.Response:
        user = await self._require_access(request)
        try:
            body = await self._read_json_object(request)
        except web.HTTPException as exc:
            return await self._json_error(int(exc.status), str(exc.reason or "invalid request"))

        session_uid = str(request.match_info.get("uid", "") or "").strip()
        if not session_uid:
            return await self._json_error(400, "session uid is required in path")

        _canonical_uid, session = self._resolve_visible_session(
            user_id=int(user["user_id"]),
            is_admin=bool(user.get("is_admin", False)),
            session_uid=session_uid,
        )
        if session is None:
            return await self._json_error(404, "session not found")

        changed: list[str] = []
        workdir = str(getattr(session, "workdir", "") or "").strip()
        actor = miniapp_actor_id(user["user_id"])
        actor_chat_id = int(user["user_id"])
        is_admin = bool(user.get("is_admin", False))
        owner_chat_id = self._session_owner_chat_id(session)
        admin_override = bool(
            is_admin and owner_chat_id is not None and int(owner_chat_id) != int(actor_chat_id)
        )

        # REQ-7: Logs and Config are always local — reject remote mode attempts
        for local_only_key in ("logs_remote_mode", "config_remote_mode"):
            if local_only_key in body and bool(body[local_only_key]):
                return await self._json_error(
                    400, f"{local_only_key} is not supported: logs and config are always local"
                )

        rc_svc = getattr(self.bot_app, "remote_control_service", None)
        before_enabled = False
        before_alias = None
        hosts = {}
        if rc_svc is not None:
            from app.services.ssh_config_loader import load_ssh_config
            from app.services.remote_control_service import TransitionRequest
            from sessions.session_state_access import (
                get_remote_control_host_alias,
                is_remote_control_enabled,
                is_ssh_remote_enabled,
            )

            hosts = load_ssh_config(workdir) if workdir else {}
            before_enabled = is_remote_control_enabled(session)
            before_alias = get_remote_control_host_alias(session)

            if any(
                key in body
                for key in ("ssh_remote_enabled", "remote_control_host_alias", "remote_control_enabled")
            ):
                idle_check = rc_svc.validate_idle(session)
                if not idle_check.ok:
                    return await self._json_error(409, idle_check.error or "session is busy")

        rc_transition_prevalidated = False
        if rc_svc is not None and "remote_control_enabled" in body:
            final_ssh_remote_enabled = (
                bool(body["ssh_remote_enabled"])
                if "ssh_remote_enabled" in body
                else bool(is_ssh_remote_enabled(session))
            )
            if "remote_control_host_alias" in body:
                final_alias = str(body.get("remote_control_host_alias") or "").strip() or None
            else:
                final_alias = get_remote_control_host_alias(session)
            want_enabled = bool(body["remote_control_enabled"])
            transition = TransitionRequest(enable=want_enabled, host_alias=final_alias)
            validation_session = self._remote_control_validation_session(
                session,
                ssh_remote_enabled=final_ssh_remote_enabled,
            )
            ssh_svc = getattr(self.bot_app, "ssh_service", None)
            if want_enabled and ssh_svc is not None:
                vr, pf = await rc_svc.validate_and_preflight(
                    validation_session,
                    transition,
                    hosts,
                    ssh_svc,
                    workdir,
                    chat_id=actor_chat_id,
                    is_admin=is_admin,
                )
            else:
                vr = rc_svc.validate_transition(
                    validation_session,
                    transition,
                    hosts,
                    chat_id=actor_chat_id,
                    is_admin=is_admin,
                )
                pf = None
            if not vr.ok:
                return await self._json_error(409, vr.error or "transition validation failed")
            if pf is not None and not pf.ok:
                return self._remote_control_preflight_failure_response(
                    session=session,
                    actor=actor,
                    admin_override=admin_override,
                    host_alias=str(final_alias or ""),
                    host_cfg=hosts.get(final_alias),
                    preflight=pf,
                    changed=[],
                )
            rc_transition_prevalidated = True

        settings_snapshot = self._snapshot_session_settings_state(session)
        if "execution_backend" in body:
            return await self._json_error(400, "execution backend is configured in settings")

        if "active_mode" in body:
            from sessions.session_state_access import get_active_mode

            mode_id = str(body.get("active_mode") or "").strip()
            before_active_mode = str(get_active_mode(session, "") or "").strip()
            error_response = await self._apply_active_mode_setting(
                session=session,
                mode_id=mode_id,
                actor_chat_id=actor_chat_id,
                is_admin=is_admin,
                allow_empty_noop=any(
                    key in body
                    for key in ("ssh_remote_enabled", "remote_control_enabled", "remote_control_host_alias")
                ),
            )
            if error_response is not None:
                self._rollback_session_settings_state(session, settings_snapshot)
                return error_response
            after_active_mode = str(get_active_mode(session, "") or "").strip()
            if before_active_mode != after_active_mode:
                changed.append("active_mode")

        if "ssh_remote_enabled" in body:
            from app.services.ssh_config_loader import ensure_ssh_config_template, load_ssh_config
            from sessions.session_state_access import set_ssh_remote_enabled

            try:
                value = bool(body["ssh_remote_enabled"])
                if value:
                    if workdir:
                        ensure_ssh_config_template(workdir)
                        hosts = load_ssh_config(workdir)
                set_ssh_remote_enabled(session, value)
                changed.append("ssh_remote_enabled")

                # Normalize dependent toggles via RemoteControlService
                if rc_svc is not None:
                    rc_svc.normalize_setting_change(session, "ssh_remote_enabled", value, hosts, workdir)
            except Exception:
                self._rollback_session_settings_state(session, settings_snapshot)
                raise

        has_rc_fields = "remote_control_enabled" in body or "remote_control_host_alias" in body
        if has_rc_fields:
            try:
                if rc_svc is not None:
                    from app.services.remote_control_service import TransitionRequest
                    from app.services.ssh_config_loader import load_ssh_config
                    from sessions.session_state_access import get_remote_control_host_alias

                    hosts = load_ssh_config(workdir) if workdir else hosts

                    # Apply host_alias change first (if present)
                    if "remote_control_host_alias" in body:
                        rc_svc.normalize_setting_change(
                            session, "remote_control_host_alias",
                            body["remote_control_host_alias"], hosts, workdir,
                        )
                        changed.append("remote_control_host_alias")

                    # Then apply enabled change with full validation + preflight
                    if "remote_control_enabled" in body:
                        want_enabled = bool(body["remote_control_enabled"])
                        alias = get_remote_control_host_alias(session)
                        tr = TransitionRequest(enable=want_enabled, host_alias=alias)

                        if not rc_transition_prevalidated:
                            vr = rc_svc.validate_transition(
                                session, tr, hosts, chat_id=actor_chat_id, is_admin=is_admin,
                            )
                            if not vr.ok:
                                self._rollback_session_settings_state(session, settings_snapshot)
                                return await self._json_error(409, vr.error or "transition validation failed")

                            # Run preflight when enabling
                            if want_enabled and alias and alias in hosts:
                                ssh_svc = getattr(self.bot_app, "ssh_service", None)
                                if ssh_svc is not None:
                                    pf = await rc_svc.run_preflight(ssh_svc, workdir, alias, hosts[alias])
                                    if not pf.ok:
                                        self._rollback_session_settings_state(session, settings_snapshot)
                                        return self._remote_control_preflight_failure_response(
                                            session=session,
                                            actor=actor,
                                            admin_override=admin_override,
                                            host_alias=alias,
                                            host_cfg=hosts.get(alias),
                                            preflight=pf,
                                            changed=[],
                                        )

                        rc_svc.normalize_setting_change(
                            session, "remote_control_enabled", want_enabled, hosts, workdir,
                        )
                        changed.append("remote_control_enabled")
            except Exception:
                self._rollback_session_settings_state(session, settings_snapshot)
                raise

        from sessions.session_state_access import (
            get_remote_control_host_alias,
            is_remote_control_enabled,
        )

        after_enabled = is_remote_control_enabled(session)
        after_alias = get_remote_control_host_alias(session)
        after_host_cfg = hosts.get(after_alias) if after_alias else None

        response_payload: Dict[str, Any] = {"ok": True, "changed": changed}

        if changed:
            manager = getattr(self.bot_app, "manager", None)
            if manager is not None and owner_chat_id is not None:
                manager.persist_session(int(owner_chat_id), str(session.id))

            if before_alias != after_alias:
                self._log_remote_control_audit(
                    session=session,
                    actor=actor,
                    surface="miniapp",
                    action="remote_control_host_changed",
                    host_alias=after_alias,
                    host_cfg=after_host_cfg,
                    result="ok",
                    reason=f"{before_alias or ''}->{after_alias or ''}",
                )
            if not before_enabled and after_enabled:
                self._log_remote_control_audit(
                    session=session,
                    actor=actor,
                    surface="miniapp",
                    action="remote_control_enabled",
                    host_alias=after_alias,
                    host_cfg=after_host_cfg,
                    result="ok",
                )
            if before_enabled and not after_enabled:
                self._log_remote_control_audit(
                    session=session,
                    actor=actor,
                    surface="miniapp",
                    action="remote_control_disabled",
                    host_alias=after_alias,
                    host_cfg=after_host_cfg,
                    result="ok",
                )
            if admin_override:
                self._log_remote_control_audit(
                    session=session,
                    actor=actor,
                    surface="miniapp",
                    action="admin_remote_override",
                    host_alias=after_alias,
                    host_cfg=after_host_cfg,
                    result="ok",
                    reason="admin override of foreign session",
                )

        return web.json_response(response_payload)

    async def remote_control_recheck(self, request: web.Request) -> web.Response:
        """POST /api/session/{uid}/remote-control/recheck — re-run preflight for current host."""
        user = await self._require_access(request)
        session_uid = str(request.match_info.get("uid", "") or "").strip()
        if not session_uid:
            return await self._json_error(400, "session uid is required in path")
        _canonical_uid, session = self._resolve_visible_session(
            user_id=int(user["user_id"]),
            is_admin=bool(user.get("is_admin", False)),
            session_uid=session_uid,
        )
        if session is None:
            return await self._json_error(404, "session not found")

        from app.services.ssh_config_loader import load_ssh_config
        from sessions.session_state_access import get_remote_control_host_alias

        workdir = str(getattr(session, "workdir", "") or "").strip()
        alias = get_remote_control_host_alias(session)
        if not alias:
            return await self._json_error(400, "no remote_control_host_alias configured")

        hosts = load_ssh_config(workdir) if workdir else {}
        host_cfg = hosts.get(alias)
        if host_cfg is None:
            return await self._json_error(404, f"host '{alias}' not found in SSH config")

        rc_svc = getattr(self.bot_app, "remote_control_service", None)
        ssh_svc = getattr(self.bot_app, "ssh_service", None)
        if rc_svc is None or ssh_svc is None:
            return await self._json_error(503, "remote control service unavailable")

        rc_svc.invalidate_preflight(workdir, alias)
        pf = await rc_svc.run_preflight(ssh_svc, workdir, alias, host_cfg)
        if not pf.ok:
            self._log_remote_control_audit(
                session=session,
                actor=miniapp_actor_id(user["user_id"]),
                surface="miniapp",
                action="remote_control_preflight_failed",
                host_alias=alias,
                host_cfg=host_cfg,
                result="error",
                reason=str(pf.error or ""),
            )
        return web.json_response({
            "ok": True,
            "preflight": {
                "ok": pf.ok,
                "host_alias": pf.host_alias,
                "remote_project_root": pf.remote_project_root,
                "checked_at": pf.checked_at,
                "error": pf.error,
            },
        })

    async def runs_list(self, request: web.Request) -> web.Response:
        user = await self._require_access(request)
        session_uid = self._validate_session_uid_input(request.query.get("session_uid", ""))
        mode_id = str(request.query.get("mode_id", "") or "").strip() or None
        try:
            limit = int(request.query.get("limit", "20") or "20")
        except Exception:
            return await self._json_error(400, "limit must be an integer")
        limit = max(1, min(limit, 100))

        try:
            canonical_uid, session = self._resolve_accessible_run_session(user=user, session_uid=session_uid)
            store = self._run_artifact_store()
            handles = await asyncio.to_thread(
                store.list_runs,
                session=session,
                mode_id=mode_id,
                limit=limit,
            )
            runs = await asyncio.to_thread(
                lambda: [self._serialize_run_listing(store=store, run=handle, session=session) for handle in handles]
            )
        except web.HTTPException as exc:
            return await self._json_error(int(exc.status), str(exc.reason or "request failed"))
        except Exception:
            logger.exception(
                "miniapp runs list failed",
                extra={
                    "chat_id": int(user["user_id"]),
                    "user_id": int(user["user_id"]),
                    "action": "runs_list",
                    "path": str(session_uid),
                    "status": "error",
                    "error": "",
                },
            )
            return await self._json_error(500, "runs list unavailable")

        return web.json_response(
            {
                "ok": True,
                "session_uid": canonical_uid,
                "mode_id": str(mode_id or ""),
                "runs": runs,
                "count": int(len(runs)),
            }
        )

    async def run_detail(self, request: web.Request) -> web.Response:
        user = await self._require_access(request)
        session_uid = self._validate_session_uid_input(request.query.get("session_uid", ""))
        mode_id = str(request.query.get("mode_id", "") or "").strip() or None
        run_id = str(request.match_info.get("run_id", "") or "").strip()

        try:
            canonical_uid, session, handle = self._resolve_run_handle(
                user=user,
                session_uid=session_uid,
                run_id=run_id,
                mode_id=mode_id,
            )
            store = self._run_artifact_store()
            run_payload = await asyncio.to_thread(self._serialize_run_detail, store=store, run=handle, session=session)
        except web.HTTPException as exc:
            return await self._json_error(int(exc.status), str(exc.reason or "request failed"))
        except Exception:
            logger.exception(
                "miniapp run detail failed",
                extra={
                    "chat_id": int(user["user_id"]),
                    "user_id": int(user["user_id"]),
                    "action": "run_detail",
                    "path": str(run_id),
                    "status": "error",
                    "error": "",
                },
            )
            return await self._json_error(500, "run detail unavailable")

        return web.json_response({"ok": True, "session_uid": canonical_uid, "run": run_payload})

    async def run_action(self, request: web.Request) -> web.Response:
        run_id = str(request.match_info.get("run_id", "") or "").strip()
        action = str(request.match_info.get("action", "") or "").strip().lower()
        if action not in {"doctor", "recover", "resume", "apply_recommendation", "promote_skills"}:
            return await self._json_error(400, "unsupported run action")
        user = await (self._require_admin(request) if action == "promote_skills" else self._require_access(request))
        try:
            body = await self._read_json_object(request)
        except web.HTTPException as exc:
            return await self._json_error(int(exc.status), str(exc.reason or "invalid request"))

        session_uid = self._validate_session_uid_input(body.get("session_uid") or "")
        mode_id = str(body.get("mode_id", "") or "").strip() or None
        try:
            from utils.lang import resolve_user_lang as _rsl_run
            _run_lang = _rsl_run(self.bot_app.config, chat_id=int(user["user_id"]))
        except Exception:
            _run_lang = "ru"
        try:
            canonical_uid, session, handle = self._resolve_run_handle(
                user=user,
                session_uid=session_uid,
                run_id=run_id,
                mode_id=mode_id,
            )
            decision = self.run_operations_policy.can_run_operation(
                operation=action,
                user_id=int(user["user_id"]),
                is_admin=bool(user.get("is_admin", False)),
                session=session,
                surface="miniapp",
            )
            if not decision.allowed:
                return await self._json_error(
                    403,
                    t("msg.run.policy_denied", _run_lang, reason=decision.reason),
                )
            execution_context, execution_dest = self._miniapp_run_execution_vector(user=user, session=session)
            store = self._run_artifact_store()
            if action == "promote_skills":
                skill_runtime = getattr(self.bot_app, "mode_skill_runtime", None)
                if skill_runtime is None or not hasattr(skill_runtime, "promote_run_skills"):
                    raise web.HTTPServiceUnavailable(reason="skill runtime unavailable")
                operation_result = skill_runtime.promote_run_skills(
                    session=session,
                    run_artifact_store=store,
                    mode_id=str(handle.mode_id or ""),
                    run_id=str(handle.run_id or ""),
                    is_admin=True,
                    context=execution_context,
                    dest=execution_dest,
                )
                result_payload = operation_result.to_dict()
            else:
                service = self._run_operations_service()
                method = getattr(service, f"{action}_run", None)
                if not callable(method):
                    raise web.HTTPServiceUnavailable(reason="run operation unavailable")
                operation_result = await method(
                    session=session,
                    mode_id=str(handle.mode_id or ""),
                    run_id=str(handle.run_id or ""),
                    context=execution_context,
                    dest=execution_dest,
                )
                result_payload = self._serialize_run_operation_result(operation_result)
            resolved_handle = store.get_run(session=session, mode_id=handle.mode_id, run_id=handle.run_id) or handle
            run_payload = await asyncio.to_thread(self._serialize_run_detail, store=store, run=resolved_handle, session=session)
        except web.HTTPException as exc:
            return await self._json_error(int(exc.status), str(exc.reason or "request failed"))
        except Exception:
            logger.exception(
                "miniapp run action failed",
                extra={
                    "chat_id": int(user["user_id"]),
                    "user_id": int(user["user_id"]),
                    "action": f"run_{action}",
                    "path": str(run_id),
                    "status": "error",
                    "error": "",
                },
            )
            return await self._json_error(500, "run action failed")

        return web.json_response(
            {
                "ok": str(result_payload.get("status") or "") == "ok",
                "session_uid": canonical_uid,
                "action": action,
                "result": result_payload,
                "run": run_payload,
            }
        )

    async def files_tree(self, request: web.Request) -> web.Response:
        user = await self._require_access(request)
        path = request.query.get("path", ".")
        requested_session_uid = self._validate_session_uid_input(request.query.get("session_uid", ""))
        if not requested_session_uid:
            return await self._json_error(400, "session_uid is required")
        try:
            session_uid, _session = self._resolve_accessible_files_session(
                user=user,
                session_uid=requested_session_uid,
            )
            result = self.files.tree(user["user_id"], session_uid, path)
            if inspect.isawaitable(result):
                result = await result
            return web.json_response(result)
        except web.HTTPException as exc:
            return await self._json_error(int(exc.status), str(exc.reason or "invalid request"))
        except FilesServiceError as exc:
            logger.warning(
                "miniapp files tree failed",
                extra={
                    "chat_id": user["user_id"],
                    "user_id": user["user_id"],
                    "action": "files_tree",
                    "path": path,
                    "status": "error",
                    "error": str(exc),
                },
            )
            return await self._json_error(exc.status, str(exc))

    async def files_read(self, request: web.Request) -> web.Response:
        user = await self._require_access(request)
        path = request.query.get("path", "")
        requested_session_uid = self._validate_session_uid_input(request.query.get("session_uid", ""))
        if not requested_session_uid:
            return await self._json_error(400, "session_uid is required")
        try:
            session_uid, _session = self._resolve_accessible_files_session(
                user=user,
                session_uid=requested_session_uid,
            )
            result = self.files.read(user["user_id"], session_uid, path)
            if inspect.isawaitable(result):
                result = await result
            return web.json_response(result)
        except web.HTTPException as exc:
            return await self._json_error(int(exc.status), str(exc.reason or "invalid request"))
        except FilesServiceError as exc:
            logger.warning(
                "miniapp files read failed",
                extra={
                    "chat_id": user["user_id"],
                    "user_id": user["user_id"],
                    "action": "files_read",
                    "path": path,
                    "status": "error",
                    "error": str(exc),
                },
            )
            return await self._json_error(exc.status, str(exc))

    async def files_write(self, request: web.Request) -> web.Response:
        user = await self._require_access(request)
        body: Dict[str, Any] = {}
        try:
            body = await self._read_json_object(request)
            requested_session_uid = self._validate_session_uid_input(body.get("session_uid") or "")
            if not requested_session_uid:
                return await self._json_error(400, "session_uid is required")
            session_uid, _session = self._resolve_accessible_files_session(
                user=user,
                session_uid=requested_session_uid,
            )
            rel_path = str(body.get("path") or "")
            force = bool(body.get("force", False))
            ctx = self.files.execution_context(session_uid)
            result = self.files.write(
                user["user_id"],
                session_uid,
                rel_path,
                str(body.get("content") or ""),
                body.get("expected_revision"),
                force=force,
            )
            if inspect.isawaitable(result):
                result = await result
            if force and result.get("forced"):
                from app.services.remote_control_service import build_remote_file_audit_extra

                logger.info(
                    "remote_file_force_saved",
                    extra=build_remote_file_audit_extra(
                        actor=user["user_id"],
                        session_uid=session_uid,
                        surface="miniapp",
                        action="remote_file_force_saved",
                        path=rel_path,
                        result="ok",
                        provider=str(ctx.get("execution_target") or "local"),
                        host=str(ctx.get("host_alias") or ""),
                        remote_project_root=str(ctx.get("remote_project_root") or ""),
                        old_revision=result.get("old_revision"),
                        new_revision=result.get("revision"),
                        chat_id=user["user_id"],
                        user_id=user["user_id"],
                    ),
                )
            return web.json_response(result)
        except web.HTTPException as exc:
            return await self._json_error(int(exc.status), str(exc.reason or "invalid request"))
        except RevisionConflictError as exc:
            from app.services.remote_control_service import build_remote_file_audit_extra

            ctx = self.files.execution_context(session_uid) if "session_uid" in locals() else {}
            logger.info(
                "remote_file_conflict_detected",
                extra=build_remote_file_audit_extra(
                    actor=user["user_id"],
                    session_uid=session_uid if "session_uid" in locals() else "",
                    surface="miniapp",
                    action="remote_file_conflict_detected",
                    path=str(body.get("path") or ""),
                    result="conflict",
                    provider=str(ctx.get("execution_target") or "local"),
                    host=str(ctx.get("host_alias") or ""),
                    remote_project_root=str(ctx.get("remote_project_root") or ""),
                    expected_revision=exc.expected_revision,
                    current_revision=exc.current_revision,
                    reason=str(exc),
                    chat_id=user["user_id"],
                    user_id=user["user_id"],
                ),
            )
            return web.json_response(
                {
                    "ok": False,
                    "error": str(exc),
                    "expected_revision": exc.expected_revision,
                    "current_revision": exc.current_revision,
                    "current_content": exc.current_content,
                    "diff_unified": exc.diff_unified,
                },
                status=409,
            )
        except FilesServiceError as exc:
            logger.warning(
                "miniapp files write failed",
                extra={
                    "chat_id": user["user_id"],
                    "user_id": user["user_id"],
                    "action": "files_write",
                    "path": str(body.get("path") or ""),
                    "status": "error",
                    "error": str(exc),
                },
            )
            return await self._json_error(exc.status, str(exc))

    async def files_create(self, request: web.Request) -> web.Response:
        user = await self._require_access(request)
        body: Dict[str, Any] = {}
        try:
            body = await self._read_json_object(request)
            requested_session_uid = self._validate_session_uid_input(body.get("session_uid") or "")
            if not requested_session_uid:
                return await self._json_error(400, "session_uid is required")
            session_uid, _session = self._resolve_accessible_files_session(user=user, session_uid=requested_session_uid)
            result = self.files.create(
                user["user_id"],
                session_uid,
                str(body.get("path") or ""),
                str(body.get("kind") or ""),
            )
            if inspect.isawaitable(result):
                result = await result
            return web.json_response(result)
        except web.HTTPException as exc:
            return await self._json_error(int(exc.status), str(exc.reason or "invalid request"))
        except FilesServiceError as exc:
            logger.warning(
                "miniapp files create failed",
                extra={
                    "chat_id": user["user_id"],
                    "user_id": user["user_id"],
                    "action": "files_create",
                    "path": str(body.get("path") or ""),
                    "status": "error",
                    "error": str(exc),
                },
            )
            return await self._json_error(exc.status, str(exc))

    async def files_delete(self, request: web.Request) -> web.Response:
        user = await self._require_access(request)
        body: Dict[str, Any] = {}
        try:
            body = await self._read_json_object(request)
            requested_session_uid = self._validate_session_uid_input(body.get("session_uid") or "")
            if not requested_session_uid:
                return await self._json_error(400, "session_uid is required")
            session_uid, _session = self._resolve_accessible_files_session(user=user, session_uid=requested_session_uid)
            result = self.files.delete(
                user["user_id"],
                session_uid,
                str(body.get("path") or ""),
            )
            if inspect.isawaitable(result):
                result = await result
            return web.json_response(result)
        except web.HTTPException as exc:
            return await self._json_error(int(exc.status), str(exc.reason or "invalid request"))
        except FilesServiceError as exc:
            logger.warning(
                "miniapp files delete failed",
                extra={
                    "chat_id": user["user_id"],
                    "user_id": user["user_id"],
                    "action": "files_delete",
                    "path": str(body.get("path") or ""),
                    "status": "error",
                    "error": str(exc),
                },
            )
            return await self._json_error(exc.status, str(exc))

    async def files_meta(self, request: web.Request) -> web.Response:
        user = await self._require_access(request)
        path = request.query.get("path", "")
        requested_session_uid = self._validate_session_uid_input(request.query.get("session_uid", ""))
        if not requested_session_uid:
            return await self._json_error(400, "session_uid is required")
        try:
            session_uid, _session = self._resolve_accessible_files_session(
                user=user,
                session_uid=requested_session_uid,
            )
            result = self.files.meta(user["user_id"], session_uid, path)
            if inspect.isawaitable(result):
                result = await result
            return web.json_response(result)
        except web.HTTPException as exc:
            return await self._json_error(int(exc.status), str(exc.reason or "invalid request"))
        except FilesServiceError as exc:
            logger.warning(
                "miniapp files meta failed",
                extra={
                    "chat_id": user["user_id"],
                    "user_id": user["user_id"],
                    "action": "files_meta",
                    "path": path,
                    "status": "error",
                    "error": str(exc),
                },
            )
            return await self._json_error(exc.status, str(exc))

    async def files_download(self, request: web.Request) -> web.StreamResponse:
        ticket = str(request.query.get("ticket", "") or "").strip()
        if ticket:
            try:
                user = self._consume_ws_ticket(ticket)
            except web.HTTPException as exc:
                return web.Response(status=int(exc.status), text=str(exc.reason or "unauthorized"))
        else:
            try:
                user = await self._require_access(request)
            except web.HTTPException as exc:
                return web.Response(status=int(exc.status), text=str(exc.reason or "unauthorized"))

        try:
            path = request.query.get("path", "")
            requested_session_uid = self._validate_session_uid_input(request.query.get("session_uid", ""))
            if not requested_session_uid:
                return web.Response(status=400, text="session_uid is required")
            session_uid, _session = self._resolve_accessible_files_session(
                user=user,
                session_uid=requested_session_uid,
            )
            payload = self.files.download(int(user["user_id"]), session_uid, path, allow_binary=True)
            if inspect.isawaitable(payload):
                payload = await payload
        except web.HTTPException as exc:
            return web.Response(status=int(exc.status), text=str(exc.reason or "bad request"))
        except FilesServiceError as exc:
            logger.warning(
                "miniapp files download failed",
                extra={
                    "chat_id": int(user["user_id"]),
                    "user_id": int(user["user_id"]),
                    "action": "files_download",
                    "path": path,
                    "status": "error",
                    "error": str(exc),
                },
            )
            return web.Response(status=exc.status, text=str(exc))

        _filename = str(payload.get("filename") or "")
        _body = bytes(payload.get("content") or b"")
        _mime, _ = mimetypes.guess_type(_filename)
        if not _mime:
            _mime = "application/octet-stream"
        return web.Response(
            body=_body,
            content_type=_mime,
            headers={
                "Content-Disposition": self._attachment_disposition(_filename),
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
            },
        )

    async def files_search(self, request: web.Request) -> web.Response:
        user = await self._require_access(request)
        requested_session_uid = self._validate_session_uid_input(request.query.get("session_uid", ""))
        if not requested_session_uid:
            return await self._json_error(400, "session_uid is required")
        pattern = str(request.query.get("pattern", "") or "").strip()
        if not pattern:
            return await self._json_error(400, "pattern query parameter is required")

        path = request.query.get("path", ".")
        case_sensitive = request.query.get("case_sensitive", "true").lower() != "false"
        try:
            max_results = max(1, min(int(request.query.get("max_results", "200") or "200"), 1000))
        except ValueError:
            return await self._json_error(400, "max_results must be an integer")

        try:
            session_uid, _session = self._resolve_accessible_files_session(
                user=user,
                session_uid=requested_session_uid,
            )
            result = await self.files.search(
                int(user["user_id"]),
                session_uid,
                pattern=pattern,
                rel_path=str(path or "."),
                case_sensitive=case_sensitive,
                max_results=max_results,
            )
            return web.json_response(result)
        except web.HTTPException as exc:
            return await self._json_error(int(exc.status), str(exc.reason or "invalid request"))
        except FilesServiceError as exc:
            return await self._json_error(exc.status, str(exc))

    async def modes_launch(self, request: web.Request) -> web.Response:
        user = await self._require_access(request)
        try:
            body = await self._read_json_object(request)
            project = self._require_owned_project(
                user_id=int(user["user_id"]),
                project_slug=str(body.get("project_slug") or ""),
            )
            mode_id = str(body.get("mode_id") or "").strip()
            if not mode_id:
                raise web.HTTPBadRequest(reason="mode_id is required")
            requested_session_uid = str(body.get("session_uid") or "").strip()
            if not requested_session_uid:
                raise web.HTTPBadRequest(reason="session_uid is required")
            session_uid, session = self._resolve_visible_session(
                user_id=int(user["user_id"]),
                is_admin=bool(user.get("is_admin", False)),
                session_uid=requested_session_uid,
            )
            if session is None or not session_uid:
                raise web.HTTPNotFound(reason="session not found")
            if not self._is_session_within_project(session=session, project_path=str(project.get("path") or "")):
                raise web.HTTPForbidden(reason="session does not belong to project")
            event_bus = getattr(self.bot_app, "system_event_bus", None)
            if event_bus is None or not hasattr(event_bus, "publish"):
                raise web.HTTPServiceUnavailable(reason="system event bus is unavailable")
            prompt = str(body.get("prompt") or body.get("text") or "").strip()
            correlation_id = (
                str(request.headers.get("X-Correlation-Id") or "").strip()
                or str(body.get("correlation_id") or "").strip()
                or secrets.token_hex(8)
            )
            dry_run = bool(body.get("dry_run", False))
            await event_bus.publish(
                MiniAppCommandEvent(
                    user_id=str(user["actor_id"]),
                    session_uid=session_uid,
                    project_slug=str(project.get("slug") or ""),
                    command=mode_id,
                    correlation_id=correlation_id,
                    payload={
                        "mode_id": mode_id,
                        "prompt": prompt,
                        "text": prompt,
                        "dry_run": dry_run,
                        "actor": {
                            "kind": "miniapp",
                            "user_id": int(user["user_id"]),
                            "actor_id": str(user["actor_id"]),
                        },
                    },
                )
            )
            return web.json_response(
                {
                    "ok": True,
                    "queued": True,
                    "mode_id": mode_id,
                    "project_slug": str(project.get("slug") or ""),
                    "session_uid": session_uid,
                    "correlation_id": correlation_id,
                },
                status=202,
            )
        except web.HTTPException as exc:
            return await self._json_error(int(exc.status), str(exc.reason or "request failed"))
        except Exception:
            logger.exception("miniapp modes launch failed")
            return await self._json_error(500, "mode launch failed")

    async def index(self, request: web.Request) -> web.Response:
        static_root = os.path.join(os.path.dirname(__file__), "static")
        return web.FileResponse(os.path.join(static_root, "index.html"))

    def register(self, app: web.Application) -> None:
        register_foundation_routes(app, self.route_context, self.foundation_route_services)
        register_json_routes(app, self.route_context, self.json_route_services)
        register_config_routes(app, self.route_context, self.config_route_services)
        register_admin_routes(app, self.route_context, self.admin_route_services)
        register_scheduler_routes(app, self.route_context, self.scheduler_route_services)
        register_logs_routes(app, self.route_context, self.logs_route_services)
        app.router.add_get("/api/auth/me", self.auth_me)
        app.router.add_get("/api/files/ws_ticket", self.files_ws_ticket)
        app.router.add_get("/api/files/download", self.files_download)
        app.router.add_get("/api/status/ws_ticket", self.status_ws_ticket)
        app.router.add_get("/api/status/ws", self.status_ws)
        app.router.add_get("/api/v1/admin/status", self.admin_status)
        app.router.add_post("/api/v1/admin/action", self.admin_action)
        app.router.add_get("/api/v1/admin/runs", self.admin_runs)
        app.router.add_get("/api/v1/admin/runs/{run_id}", self.admin_run_detail)
        app.router.add_get("/api/v1/admin/hosts", self.admin_hosts_list)
        app.router.add_get("/api/v1/admin/actions/ssh", self.admin_actions_ssh_get)
        app.router.add_put("/api/v1/admin/actions/ssh", self.admin_actions_ssh_put)
        app.router.add_get("/api/v1/admin/chat/messages", self.admin_chat_messages_get)
        app.router.add_post("/api/v1/admin/chat/messages", self.admin_chat_messages_post)
        app.router.add_get("/api/v1/admin/chat/pending", self.admin_chat_pending_get)
        app.router.add_post("/api/v1/admin/chat/pending/{approval_id}/approve", self.admin_chat_pending_approve)
        app.router.add_post("/api/v1/admin/chat/pending/{approval_id}/reject", self.admin_chat_pending_reject)
        app.router.add_get("/api/v1/admin/chat/memory", self.admin_chat_memory_get)
        app.router.add_put("/api/v1/admin/chat/memory", self.admin_chat_memory_put)
        app.router.add_get("/api/v1/admin/autonomy/dashboard", self.admin_autonomy_dashboard)
        app.router.add_get("/api/v1/admin/autonomy/servers", self.admin_autonomy_servers)
        app.router.add_get("/api/v1/admin/autonomy/servers/{server_id}", self.admin_autonomy_server_detail)
        app.router.add_post("/api/v1/admin/autonomy/servers/{server_id}/rescan", self.admin_autonomy_rescan_server)
        app.router.add_post("/api/v1/admin/autonomy/rescan_all", self.admin_autonomy_rescan_all)
        app.router.add_get("/api/v1/admin/autonomy/servers/{server_id}/baseline", self.admin_autonomy_baseline_get)
        app.router.add_post("/api/v1/admin/autonomy/servers/{server_id}/baseline/accept", self.admin_autonomy_baseline_accept)
        app.router.add_delete("/api/v1/admin/autonomy/servers/{server_id}/baseline/proposal", self.admin_autonomy_baseline_discard)
        app.router.add_get("/api/v1/admin/autonomy/servers/{server_id}/drifts", self.admin_autonomy_drifts_list)
        app.router.add_post("/api/v1/admin/autonomy/servers/{server_id}/drifts/{drift_id}/ack", self.admin_autonomy_drift_ack)
        app.router.add_get("/api/v1/admin/autonomy/servers/{server_id}/snapshots", self.admin_autonomy_snapshots)
        app.router.add_get("/api/v1/admin/autonomy/servers/{server_id}/snapshot-checks", self.admin_autonomy_snapshot_checks)
        app.router.add_get("/api/v1/admin/autonomy/servers/{server_id}/memory", self.admin_autonomy_memory_get)
        app.router.add_post("/api/v1/admin/autonomy/servers/{server_id}/memory/facts", self.admin_autonomy_memory_fact_put)
        app.router.add_delete("/api/v1/admin/autonomy/servers/{server_id}/memory/facts/{key}", self.admin_autonomy_memory_fact_delete)
        app.router.add_post("/api/v1/admin/autonomy/servers/{server_id}/memory/notes", self.admin_autonomy_memory_note)
        app.router.add_post("/api/v1/admin/autonomy/servers/{server_id}/memory/compact", self.admin_autonomy_memory_compact)
        app.router.add_get("/api/v1/admin/autonomy/runbooks", self.admin_autonomy_runbooks_list)
        app.router.add_get("/api/v1/admin/autonomy/runbooks/{runbook_id}", self.admin_autonomy_runbook_get)
        app.router.add_post("/api/v1/admin/autonomy/scripts/scan", self.admin_autonomy_scripts_scan)
        app.router.add_post("/api/v1/admin/autonomy/scripts/read", self.admin_autonomy_scripts_read)
        app.router.add_post("/api/v1/admin/autonomy/runbooks/build", self.admin_autonomy_runbook_build)
        app.router.add_post("/api/v1/admin/autonomy/runbooks/{runbook_id}/validate", self.admin_autonomy_runbook_validate)
        app.router.add_post("/api/v1/admin/autonomy/runbooks/{runbook_id}/promote", self.admin_autonomy_runbook_promote)
        app.router.add_post("/api/v1/admin/autonomy/runbooks/{runbook_id}/run-step", self.admin_autonomy_runbook_run_step)
        app.router.add_post("/api/v1/admin/autonomy/maintenance/daily", self.admin_autonomy_maintenance_daily)
        app.router.add_get("/api/v1/admin/autonomy/servers/{server_id}/prereqs", self.admin_autonomy_prereqs_get)
        app.router.add_post("/api/v1/admin/autonomy/servers/{server_id}/prereqs/bootstrap", self.admin_autonomy_prereqs_bootstrap)
        app.router.add_get("/api/runs", self.runs_list)
        app.router.add_get("/api/runs/{run_id}", self.run_detail)
        app.router.add_post("/api/runs/{run_id}/{action}", self.run_action)
        app.router.add_get("/api/v1/runs", self.runs_list)
        app.router.add_get("/api/v1/runs/{run_id}", self.run_detail)
        app.router.add_post("/api/v1/runs/{run_id}/{action}", self.run_action)

        app.router.add_get("/api/files/tree", self.files_tree)
        app.router.add_get("/api/files/read", self.files_read)
        app.router.add_post("/api/files/write", self.files_write)
        app.router.add_post("/api/files/create", self.files_create)
        app.router.add_post("/api/files/delete", self.files_delete)
        app.router.add_get("/api/files/meta", self.files_meta)
        app.router.add_get("/api/files/search", self.files_search)
        app.router.add_post("/api/v1/modes/launch", self.modes_launch)
        app.router.add_post("/api/modes/launch", self.modes_launch)

        app.router.add_get("/api/session/{uid}/settings", self.session_settings_get)
        app.router.add_put("/api/session/{uid}/settings", self.session_settings_update)
        app.router.add_post("/api/session/{uid}/remote-control/recheck", self.remote_control_recheck)

        register_ssh_routes(app, self.route_context, self.ssh_route_services)
        register_tasks_routes(app, self.route_context, self.tasks_route_services)
        register_reports_routes(app, self.route_context, self.reports_route_services)

        # i18n routes — user-lang literal routes BEFORE {lang} pattern
        app.router.add_get("/api/i18n/user-lang", self.i18n_user_lang_get)
        app.router.add_put("/api/i18n/user-lang", self.i18n_user_lang_put)
        app.router.add_get("/api/i18n/{lang}", self.i18n_catalog_get)

        app.router.add_get("/", self.index)
        static_root = os.path.join(os.path.dirname(__file__), "static")
        app.router.add_static("/", static_root, show_index=False)
