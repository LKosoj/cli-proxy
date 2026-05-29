from __future__ import annotations

import dataclasses
import logging
import os
import time
from typing import Any, Mapping, Sequence

from .audit import EventBusAuditService, build_audit_service
from .auth import ConfigAuthService, build_auth_service
from .errors import DenyReasonCode
from .interfaces import (
    AuditRecord,
    AuditService,
    AuthenticationResult,
    AuthDecision,
    AuthService,
    PathValidationResult,
    RateLimitDecision,
    RateLimitService,
    ValidatorService,
)
from .rate_limits import InMemoryRateLimitService, build_rate_limit_service
from .validators import BasicValidatorService, build_validator_service


class SecurityFacade:
    """Unified entrypoint for auth, validation, audit and rate limits."""

    def __init__(
        self,
        *,
        auth: AuthService | None = None,
        validators: ValidatorService | None = None,
        audit: AuditService | None = None,
        rate_limits: RateLimitService | None = None,
    ) -> None:
        self.auth = auth or ConfigAuthService(
            is_admin_fn=lambda _chat_id: False,
            is_user_fn=lambda _chat_id: False,
        )
        self.validators = validators or BasicValidatorService()
        self.audit = audit or EventBusAuditService()
        self.rate_limits = rate_limits or InMemoryRateLimitService()

    @classmethod
    def from_config(
        cls,
        auth_config: Mapping[str, Any] | None = None,
        *,
        validator_config: Mapping[str, Any] | None = None,
        audit_config: Mapping[str, Any] | None = None,
        rate_limit_config: Mapping[str, Any] | None = None,
        custom_text_rules: Mapping[str, Any] | None = None,
        custom_path_rules: Mapping[str, Any] | None = None,
        is_admin_fn=None,
        is_user_fn=None,
        system_event_bus=None,
        oauth_token_verifier=None,
        telegram_init_data_verifier=None,
        default_audit_state_path: str | None = None,
        default_rate_limit_state_path: str | None = None,
        rate_limit_clock=None,
    ) -> "SecurityFacade":
        logger = logging.getLogger(__name__)
        return cls(
            auth=build_auth_service(
                auth_config,
                is_admin_fn=is_admin_fn,
                is_user_fn=is_user_fn,
                oauth_token_verifier=oauth_token_verifier,
                telegram_init_data_verifier=telegram_init_data_verifier,
            ),
            validators=build_validator_service(
                validator_config,
                custom_text_rules=custom_text_rules,
                custom_path_rules=custom_path_rules,
            ),
            audit=build_audit_service(
                audit_config,
                system_event_bus=system_event_bus,
                logger=logger,
                default_state_path=default_audit_state_path,
            ),
            rate_limits=build_rate_limit_service(
                rate_limit_config,
                default_state_path=default_rate_limit_state_path,
                clock=rate_limit_clock,
                logger=logger,
            ),
        )

    @classmethod
    def from_app_config(
        cls,
        config: Any,
        *,
        auth_config: Mapping[str, Any] | None = None,
        validator_config: Mapping[str, Any] | None = None,
        audit_config: Mapping[str, Any] | None = None,
        rate_limit_config: Mapping[str, Any] | None = None,
        custom_text_rules: Mapping[str, Any] | None = None,
        custom_path_rules: Mapping[str, Any] | None = None,
        is_admin_fn=None,
        is_user_fn=None,
        system_event_bus=None,
        oauth_token_verifier=None,
        telegram_init_data_verifier=None,
        rate_limit_clock=None,
    ) -> "SecurityFacade":
        defaults = getattr(config, "defaults", None)
        default_audit_state_path = getattr(defaults, "state_path", None)
        default_rate_limit_state_path = getattr(defaults, "state_path", None)
        resolved_rate_limit_config = rate_limit_config
        if resolved_rate_limit_config is None:
            security_cfg = getattr(config, "security", None)
            rate_limits_cfg = getattr(security_cfg, "rate_limits", None)
            if rate_limits_cfg is not None and dataclasses.is_dataclass(rate_limits_cfg):
                resolved_rate_limit_config = dataclasses.asdict(rate_limits_cfg)
        resolved_init_data_verifier = telegram_init_data_verifier
        if resolved_init_data_verifier is None:
            telegram_cfg = getattr(config, "telegram", None)
            bot_token = str(getattr(telegram_cfg, "token", "") or "").strip()
            if bot_token:
                from miniapp.auth import verify_telegram_init_data

                def _verify_init_data(init_data: str, _bot_token: str = bot_token) -> bool:
                    return verify_telegram_init_data(init_data, _bot_token)

                resolved_init_data_verifier = _verify_init_data

        return cls.from_config(
            auth_config,
            validator_config=validator_config,
            audit_config=audit_config,
            rate_limit_config=resolved_rate_limit_config,
            custom_text_rules=custom_text_rules,
            custom_path_rules=custom_path_rules,
            is_admin_fn=is_admin_fn or (lambda chat_id: cls._config_is_admin(config, int(chat_id))),
            is_user_fn=is_user_fn or (lambda chat_id: cls._config_is_user(config, int(chat_id))),
            system_event_bus=system_event_bus,
            oauth_token_verifier=oauth_token_verifier,
            telegram_init_data_verifier=resolved_init_data_verifier,
            default_audit_state_path=default_audit_state_path,
            default_rate_limit_state_path=default_rate_limit_state_path,
            rate_limit_clock=rate_limit_clock,
        )

    @staticmethod
    def _config_is_admin(config: Any, chat_id: int) -> bool:
        telegram_cfg = getattr(config, "telegram", None)
        return int(chat_id) in set(getattr(telegram_cfg, "admlist_chat_ids", []) or [])

    @classmethod
    def _config_is_user(cls, config: Any, chat_id: int) -> bool:
        chat_id = int(chat_id)
        if cls._config_is_admin(config, chat_id):
            return False
        telegram_cfg = getattr(config, "telegram", None)
        if chat_id not in (getattr(telegram_cfg, "whitelist_chat_ids", []) or []):
            return False
        return bool(cls._config_user_projects(config, chat_id))

    @staticmethod
    def _config_user_projects(config: Any, chat_id: int) -> list[str]:
        telegram_cfg = getattr(config, "telegram", None)
        raw = (getattr(telegram_cfg, "user_workdirs", {}) or {}).get(int(chat_id)) or []
        if isinstance(raw, str):
            raw = [raw]
        projects: list[str] = []
        seen: set[str] = set()
        for path in raw if isinstance(raw, list) else []:
            token = str(path or "").strip()
            if not token:
                continue
            real_path = os.path.realpath(token)
            if not os.path.isdir(real_path) or real_path in seen:
                continue
            seen.add(real_path)
            projects.append(real_path)
        return projects

    def authorize(self, chat_id: int, *, scope: str = "generic", require_admin: bool = False) -> AuthDecision:
        return self.auth.authorize(int(chat_id), scope=str(scope or "generic"), require_admin=bool(require_admin))

    async def authorize_mode_launch(
        self,
        chat_id: int | str,
        *,
        mode_id: str,
        is_mode_allowed: bool = True,
        action: str = "enable",
        session_id: str = "",
        context: Mapping[str, Any] | None = None,
    ) -> AuthDecision:
        token_mode = str(mode_id or "").strip()
        scope = f"mode.launch.{token_mode or 'unknown'}"
        payload_context = dict(context or {})
        actor_id = str(
            payload_context.get("actor_id")
            or chat_id
            or ""
        ).strip()
        numeric_actor_id: int | None = None
        try:
            numeric_actor_id = int(chat_id)
        except Exception:
            numeric_actor_id = None
        if numeric_actor_id is None and actor_id and ":" in actor_id:
            try:
                numeric_actor_id = int(actor_id.rsplit(":", 1)[-1])
            except Exception:
                numeric_actor_id = None

        if actor_id.startswith(("desktop:", "internal:")):
            decision = AuthDecision(
                chat_id=int(numeric_actor_id or 0),
                allowed=True,
                scope=scope,
                is_admin=True,
                is_user=True,
                reason="",
            )
        else:
            decision = self.authorize(int(numeric_actor_id or 0), scope=scope, require_admin=False)
        if decision.allowed and not bool(is_mode_allowed):
            decision = dataclasses.replace(
                decision,
                allowed=False,
                reason=DenyReasonCode.MODE_NOT_ALLOWED,
                scope=scope,
            )
        await self.emit_audit(
            category="mode_launch",
            action=str(action or "enable").strip() or "enable",
            status="allowed" if decision.allowed else "denied",
            user_id=actor_id or int(numeric_actor_id or 0),
            subject=token_mode,
            scope=scope,
            reason=str(decision.reason or ""),
            context={
                "actor_id": actor_id,
                "chat_id": int(numeric_actor_id or 0),
                "mode_id": token_mode,
                "session_id": str(session_id or ""),
                **payload_context,
            },
            details={
                "allowed": bool(decision.allowed),
                "mode_id": token_mode,
                "session_id": str(session_id or ""),
                "chat_id": int(numeric_actor_id or 0),
                "actor_id": actor_id,
                "action": str(action or "enable").strip() or "enable",
            },
        )
        return decision

    def authenticate(
        self,
        credentials: Mapping[str, Any],
        *,
        strategy: str | None = None,
    ) -> AuthenticationResult:
        return self.auth.authenticate(credentials, strategy=strategy)

    def require_text(
        self,
        value: str,
        *,
        field_name: str = "input",
        context: Mapping[str, Any] | None = None,
    ) -> str:
        return self.validators.require_text(value, field_name=field_name, context=context)

    def resolve_path(
        self,
        root: str,
        rel_path: str,
        *,
        deny_names: Sequence[str] = (),
        deny_extensions: Sequence[str] = (),
        context: Mapping[str, Any] | None = None,
    ) -> PathValidationResult:
        return self.validators.resolve_path(
            root,
            rel_path,
            deny_names=deny_names,
            deny_extensions=deny_extensions,
            context=context,
        )

    def consume_rate_limit(
        self,
        scope: str,
        subject: str | int,
        *,
        limit: int | None = None,
        window_sec: float | None = None,
        cost: int = 1,
        burst_limit: int | None = None,
        burst_window_sec: float | None = None,
    ) -> RateLimitDecision:
        return self.rate_limits.consume(
            scope,
            subject,
            limit=int(limit) if limit is not None else None,
            window_sec=float(window_sec) if window_sec is not None else None,
            cost=int(cost),
            burst_limit=int(burst_limit) if burst_limit is not None else None,
            burst_window_sec=float(burst_window_sec) if burst_window_sec is not None else None,
        )

    async def emit_audit(
        self,
        *,
        category: str,
        action: str,
        status: str = "ok",
        user_id: str | int | None = None,
        subject: str = "",
        scope: str = "",
        reason: str = "",
        timestamp: float | None = None,
        context: Mapping[str, Any] | None = None,
        details: dict[str, Any] | None = None,
    ) -> AuditRecord:
        payload_context = dict(context or {})
        record = AuditRecord(
            category=str(category or "").strip(),
            action=str(action or "").strip(),
            status=str(status or "ok").strip(),
            user_id=str(user_id if user_id is not None else payload_context.get("user_id", "")).strip(),
            subject=str(subject or "").strip(),
            scope=str(scope or "").strip(),
            reason=str(reason or "").strip(),
            timestamp=float(timestamp) if timestamp is not None else time.time(),
            context=payload_context,
            details=dict(details or {}),
        )
        await self.audit.emit(record)
        return record

    def list_audit_logs(
        self,
        *,
        limit: int = 100,
        action: str = "",
        user_id: str | int | None = None,
        status: str = "",
        subject: str = "",
    ) -> list[AuditRecord]:
        return self.audit.list_records(
            limit=limit,
            action=action,
            user_id=user_id,
            status=status,
            subject=subject,
        )


__all__ = ["SecurityFacade"]
