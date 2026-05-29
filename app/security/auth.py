from __future__ import annotations

import hmac
from typing import Any, Callable, Mapping, Sequence

from .errors import DenyReasonCode
from .interfaces import AuthenticationResult, AuthenticationStrategy, AuthDecision


class TokenAuthStrategy:
    strategy_name = "token"

    def __init__(self, *, expected_token: str, subject_claim: str = "subject") -> None:
        self._expected_token = str(expected_token or "").strip()
        self._subject_claim = str(subject_claim or "subject").strip() or "subject"

    def authenticate(self, credentials: Mapping[str, Any]) -> AuthenticationResult:
        provided_token = str(
            credentials.get("token")
            or credentials.get("access_token")
            or credentials.get("bearer_token")
            or ""
        ).strip()
        if not provided_token:
            return AuthenticationResult(
                strategy=self.strategy_name,
                authenticated=False,
                reason=DenyReasonCode.MISSING_TOKEN,
            )
        if not self._expected_token:
            return AuthenticationResult(
                strategy=self.strategy_name,
                authenticated=False,
                reason=DenyReasonCode.TOKEN_NOT_CONFIGURED,
            )
        if not hmac.compare_digest(provided_token, self._expected_token):
            return AuthenticationResult(
                strategy=self.strategy_name,
                authenticated=False,
                reason=DenyReasonCode.INVALID_TOKEN,
            )
        subject = str(credentials.get(self._subject_claim) or "token_subject").strip()
        return AuthenticationResult(
            strategy=self.strategy_name,
            authenticated=True,
            subject=subject,
            reason="",
            claims={self._subject_claim: subject},
        )


class OAuthStrategy:
    strategy_name = "oauth"

    def __init__(
        self,
        *,
        token_verifier: Callable[[str], Mapping[str, Any] | None],
        audience: str = "",
        required_scopes: Sequence[str] = (),
    ) -> None:
        self._token_verifier = token_verifier
        self._audience = str(audience or "").strip()
        self._required_scopes = tuple(str(scope or "").strip() for scope in required_scopes if str(scope or "").strip())

    def authenticate(self, credentials: Mapping[str, Any]) -> AuthenticationResult:
        access_token = str(
            credentials.get("access_token")
            or credentials.get("token")
            or credentials.get("bearer_token")
            or ""
        ).strip()
        if not access_token:
            return AuthenticationResult(
                strategy=self.strategy_name,
                authenticated=False,
                reason=DenyReasonCode.MISSING_ACCESS_TOKEN,
            )

        claims = self._token_verifier(access_token)
        if not isinstance(claims, Mapping):
            return AuthenticationResult(
                strategy=self.strategy_name,
                authenticated=False,
                reason=DenyReasonCode.INVALID_OAUTH_TOKEN,
            )

        if self._audience and not self._audience_matches(claims.get("aud")):
            return AuthenticationResult(
                strategy=self.strategy_name,
                authenticated=False,
                reason=DenyReasonCode.INVALID_AUDIENCE,
            )

        if self._required_scopes and not self._has_required_scopes(claims):
            return AuthenticationResult(
                strategy=self.strategy_name,
                authenticated=False,
                reason=DenyReasonCode.MISSING_SCOPE,
            )

        subject = str(claims.get("sub") or claims.get("subject") or "").strip()
        if not subject:
            return AuthenticationResult(
                strategy=self.strategy_name,
                authenticated=False,
                reason=DenyReasonCode.MISSING_SUBJECT,
            )

        return AuthenticationResult(
            strategy=self.strategy_name,
            authenticated=True,
            subject=subject,
            reason="",
            claims=dict(claims),
        )

    def _audience_matches(self, audience_claim: Any) -> bool:
        if isinstance(audience_claim, str):
            return audience_claim.strip() == self._audience
        if isinstance(audience_claim, Sequence):
            return any(str(item or "").strip() == self._audience for item in audience_claim)
        return False

    def _has_required_scopes(self, claims: Mapping[str, Any]) -> bool:
        raw_scope = claims.get("scope")
        if isinstance(raw_scope, str):
            token_scopes = {part for part in raw_scope.split() if part}
        elif isinstance(raw_scope, Sequence):
            token_scopes = {str(item or "").strip() for item in raw_scope if str(item or "").strip()}
        else:
            token_scopes = set()
        return set(self._required_scopes).issubset(token_scopes)


class TelegramMiniAppInitDataStrategy:
    strategy_name = "telegram_init_data"

    def __init__(self, *, init_data_verifier: Callable[[str], Any]) -> None:
        self._init_data_verifier = init_data_verifier

    def authenticate(self, credentials: Mapping[str, Any]) -> AuthenticationResult:
        init_data = str(
            credentials.get("init_data")
            or credentials.get("telegram_init_data")
            or ""
        ).strip()
        if not init_data:
            return AuthenticationResult(
                strategy=self.strategy_name,
                authenticated=False,
                reason=DenyReasonCode.MISSING_INIT_DATA,
            )
        try:
            verified_user = self._init_data_verifier(init_data)
        except Exception:
            return AuthenticationResult(
                strategy=self.strategy_name,
                authenticated=False,
                reason=DenyReasonCode.INVALID_INIT_DATA,
            )

        claims = self._claims_from_verified_user(verified_user)
        user_id = claims.get("user_id")
        if not isinstance(user_id, int):
            return AuthenticationResult(
                strategy=self.strategy_name,
                authenticated=False,
                reason=DenyReasonCode.INVALID_INIT_DATA,
            )

        return AuthenticationResult(
            strategy=self.strategy_name,
            authenticated=True,
            subject=str(user_id),
            reason="",
            claims=claims,
        )

    @staticmethod
    def _claims_from_verified_user(verified_user: Any) -> dict[str, Any]:
        if isinstance(verified_user, Mapping):
            raw = dict(verified_user)
        else:
            raw = {
                "user_id": getattr(verified_user, "user_id", None),
                "username": getattr(verified_user, "username", ""),
                "first_name": getattr(verified_user, "first_name", ""),
            }
        user_id = raw.get("user_id")
        try:
            normalized_user_id = int(user_id)
        except Exception:
            return {}
        return {
            "user_id": normalized_user_id,
            "username": str(raw.get("username") or ""),
            "first_name": str(raw.get("first_name") or ""),
        }


class ConfigAuthService:
    """Thin auth adapter over BotApp access checks plus pluggable auth strategies."""

    def __init__(
        self,
        *,
        is_admin_fn: Callable[[int], bool],
        is_user_fn: Callable[[int], bool],
        strategies: Mapping[str, AuthenticationStrategy] | None = None,
        default_strategy: str = "",
    ) -> None:
        self._is_admin = is_admin_fn
        self._is_user = is_user_fn
        self._strategies = {
            str(name or "").strip(): strategy
            for name, strategy in (strategies or {}).items()
            if str(name or "").strip()
        }
        self._default_strategy = str(default_strategy or "").strip()

    def authorize(self, chat_id: int, *, scope: str = "generic", require_admin: bool = False) -> AuthDecision:
        subject_chat_id = int(chat_id)
        is_admin = bool(self._is_admin(subject_chat_id))
        is_user = bool(self._is_user(subject_chat_id))
        if require_admin:
            return AuthDecision(
                chat_id=subject_chat_id,
                allowed=is_admin,
                scope=str(scope or "generic"),
                is_admin=is_admin,
                is_user=is_user,
                reason="" if is_admin else DenyReasonCode.ADMIN_REQUIRED,
            )
        allowed = is_admin or is_user
        return AuthDecision(
            chat_id=subject_chat_id,
            allowed=allowed,
            scope=str(scope or "generic"),
            is_admin=is_admin,
            is_user=is_user,
            reason="" if allowed else DenyReasonCode.NOT_ALLOWED,
        )

    def authenticate(
        self,
        credentials: Mapping[str, Any],
        *,
        strategy: str | None = None,
    ) -> AuthenticationResult:
        strategy_name = str(strategy or self._default_strategy or "").strip()
        if not strategy_name:
            return AuthenticationResult(
                strategy="",
                authenticated=False,
                reason=DenyReasonCode.AUTH_STRATEGY_NOT_CONFIGURED,
            )

        backend = self._strategies.get(strategy_name)
        if backend is None:
            return AuthenticationResult(
                strategy=strategy_name,
                authenticated=False,
                reason=DenyReasonCode.UNKNOWN_AUTH_STRATEGY,
            )
        return backend.authenticate(credentials)


def build_auth_service(
    auth_config: Mapping[str, Any] | None,
    *,
    is_admin_fn: Callable[[int], bool] | None = None,
    is_user_fn: Callable[[int], bool] | None = None,
    oauth_token_verifier: Callable[[str], Mapping[str, Any] | None] | None = None,
    telegram_init_data_verifier: Callable[[str], Any] | None = None,
) -> ConfigAuthService:
    config = dict(auth_config or {})
    strategies: dict[str, AuthenticationStrategy] = {}

    token_cfg = config.get("token")
    if isinstance(token_cfg, Mapping):
        expected_token = str(token_cfg.get("expected_token") or "").strip()
        if expected_token:
            strategies[TokenAuthStrategy.strategy_name] = TokenAuthStrategy(
                expected_token=expected_token,
                subject_claim=str(token_cfg.get("subject_claim") or "subject"),
            )

    oauth_cfg = config.get("oauth")
    if isinstance(oauth_cfg, Mapping) and oauth_token_verifier is not None:
        strategies[OAuthStrategy.strategy_name] = OAuthStrategy(
            token_verifier=oauth_token_verifier,
            audience=str(oauth_cfg.get("audience") or "").strip(),
            required_scopes=tuple(oauth_cfg.get("required_scopes") or ()),
        )

    if telegram_init_data_verifier is not None:
        strategies[TelegramMiniAppInitDataStrategy.strategy_name] = TelegramMiniAppInitDataStrategy(
            init_data_verifier=telegram_init_data_verifier,
        )

    default_strategy = str(config.get("default_strategy") or "").strip()
    return ConfigAuthService(
        is_admin_fn=is_admin_fn or (lambda _chat_id: False),
        is_user_fn=is_user_fn or (lambda _chat_id: False),
        strategies=strategies,
        default_strategy=default_strategy,
    )


__all__ = [
    "ConfigAuthService",
    "OAuthStrategy",
    "TelegramMiniAppInitDataStrategy",
    "TokenAuthStrategy",
    "build_auth_service",
]
