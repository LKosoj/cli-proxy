from __future__ import annotations

from typing import Any, Mapping


class DenyReasonCode:
    OK = "ok"
    SECURITY_DENIED = "security_denied"

    NOT_ALLOWED = "not_allowed"
    ADMIN_REQUIRED = "admin_required"
    MODE_NOT_ALLOWED = "mode_not_allowed"

    AUTH_STRATEGY_NOT_CONFIGURED = "auth_strategy_not_configured"
    UNKNOWN_AUTH_STRATEGY = "unknown_auth_strategy"
    MISSING_TOKEN = "missing_token"
    TOKEN_NOT_CONFIGURED = "token_not_configured"
    INVALID_TOKEN = "invalid_token"
    MISSING_ACCESS_TOKEN = "missing_access_token"
    INVALID_OAUTH_TOKEN = "invalid_oauth_token"
    INVALID_AUDIENCE = "invalid_audience"
    MISSING_SCOPE = "missing_scope"
    MISSING_SUBJECT = "missing_subject"
    MISSING_INIT_DATA = "missing_init_data"
    INVALID_INIT_DATA = "invalid_init_data"

    REQUIRED_FIELD = "required_field"
    REQUIRED_CONTEXT = "required_context"
    MAX_LENGTH_EXCEEDED = "max_length_exceeded"
    DENIED_SUBSTRING = "denied_substring"
    ROOT_REQUIRED = "root_required"
    PATH_ESCAPES_ROOT = "path_escapes_root"
    PROTECTED_PATH = "protected_path"
    PROTECTED_NAME = "protected_name"
    PROTECTED_EXTENSION = "protected_extension"
    VALIDATION_RULE_FAILED = "validation_rule_failed"
    VALIDATION_RULE_REJECTED = "validation_rule_rejected"
    UNKNOWN_TEXT_VALIDATION_RULES = "unknown_text_validation_rules"
    UNKNOWN_PATH_VALIDATION_RULES = "unknown_path_validation_rules"

    BURST_LIMIT_EXCEEDED = "burst_limit_exceeded"
    WINDOW_LIMIT_EXCEEDED = "window_limit_exceeded"


_USER_FACING_TEXTS = {
    DenyReasonCode.OK: "Request allowed.",
    DenyReasonCode.SECURITY_DENIED: "Request denied by security policy.",
    DenyReasonCode.NOT_ALLOWED: "You do not have access to this action.",
    DenyReasonCode.ADMIN_REQUIRED: "Administrator access is required.",
    DenyReasonCode.MODE_NOT_ALLOWED: "Requested mode is not available for this user.",
    DenyReasonCode.AUTH_STRATEGY_NOT_CONFIGURED: "Authentication is not configured for this action.",
    DenyReasonCode.UNKNOWN_AUTH_STRATEGY: "The requested authentication method is not available.",
    DenyReasonCode.MISSING_TOKEN: "Authentication token is required.",
    DenyReasonCode.TOKEN_NOT_CONFIGURED: "Token authentication is not configured.",
    DenyReasonCode.INVALID_TOKEN: "Authentication token is invalid.",
    DenyReasonCode.MISSING_ACCESS_TOKEN: "OAuth access token is required.",
    DenyReasonCode.INVALID_OAUTH_TOKEN: "OAuth access token is invalid.",
    DenyReasonCode.INVALID_AUDIENCE: "OAuth token audience is not accepted.",
    DenyReasonCode.MISSING_SCOPE: "OAuth token does not have the required scope.",
    DenyReasonCode.MISSING_SUBJECT: "Authenticated subject is missing.",
    DenyReasonCode.MISSING_INIT_DATA: "Telegram MiniApp initData is required.",
    DenyReasonCode.INVALID_INIT_DATA: "Telegram MiniApp initData is invalid.",
    DenyReasonCode.REQUIRED_FIELD: "Required input is missing.",
    DenyReasonCode.REQUIRED_CONTEXT: "Required security context is missing.",
    DenyReasonCode.MAX_LENGTH_EXCEEDED: "Input exceeds the allowed length.",
    DenyReasonCode.DENIED_SUBSTRING: "Input contains blocked content.",
    DenyReasonCode.ROOT_REQUIRED: "Security validation root is not configured.",
    DenyReasonCode.PATH_ESCAPES_ROOT: "Requested path is outside the allowed workspace.",
    DenyReasonCode.PROTECTED_PATH: "Requested path targets a protected file path.",
    DenyReasonCode.PROTECTED_NAME: "Requested path targets a protected file name.",
    DenyReasonCode.PROTECTED_EXTENSION: "Requested path targets a protected file type.",
    DenyReasonCode.VALIDATION_RULE_FAILED: "Security validation rule failed.",
    DenyReasonCode.VALIDATION_RULE_REJECTED: "Input was rejected by a security validation rule.",
    DenyReasonCode.UNKNOWN_TEXT_VALIDATION_RULES: "Text validation policy is misconfigured.",
    DenyReasonCode.UNKNOWN_PATH_VALIDATION_RULES: "Path validation policy is misconfigured.",
    DenyReasonCode.BURST_LIMIT_EXCEEDED: "Too many requests in a short period. Try again later.",
    DenyReasonCode.WINDOW_LIMIT_EXCEEDED: "Rate limit exceeded. Try again later.",
}


def normalize_deny_reason(code: str | None) -> str:
    token = str(code or "").strip()
    return token or DenyReasonCode.SECURITY_DENIED


def get_user_facing_error_text(code: str | None) -> str:
    return _USER_FACING_TEXTS.get(normalize_deny_reason(code), _USER_FACING_TEXTS[DenyReasonCode.SECURITY_DENIED])


class SecurityError(Exception):
    error_type = "security"

    def __init__(
        self,
        code: str,
        message: str = "",
        *,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        self.code = normalize_deny_reason(code)
        self.details = dict(details or {})
        self.user_message = get_user_facing_error_text(self.code)
        self.message = str(message or self.user_message).strip() or self.user_message
        super().__init__(self.message)

    def to_payload(self) -> dict[str, Any]:
        return {
            "type": self.error_type,
            "code": self.code,
            "message": self.message,
            "user_message": self.user_message,
            "details": dict(self.details),
        }

    def to_dict(self) -> dict[str, Any]:
        return self.to_payload()


class SecurityAuthenticationError(SecurityError, PermissionError):
    error_type = "authentication"


class SecurityAuthorizationError(SecurityError, PermissionError):
    error_type = "authorization"


class SecurityValidationError(SecurityError, ValueError):
    error_type = "validation"


class SecurityRateLimitError(SecurityError, RuntimeError):
    error_type = "rate_limit"


def serialize_security_error(
    error: BaseException,
    *,
    fallback_code: str = DenyReasonCode.SECURITY_DENIED,
) -> dict[str, Any]:
    if isinstance(error, SecurityError):
        return error.to_payload()
    normalized_code = normalize_deny_reason(fallback_code)
    message = str(error or "").strip() or get_user_facing_error_text(normalized_code)
    return {
        "type": "security",
        "code": normalized_code,
        "message": message,
        "user_message": get_user_facing_error_text(normalized_code),
        "details": {},
    }


__all__ = [
    "DenyReasonCode",
    "SecurityAuthenticationError",
    "SecurityAuthorizationError",
    "SecurityError",
    "SecurityRateLimitError",
    "SecurityValidationError",
    "get_user_facing_error_text",
    "normalize_deny_reason",
    "serialize_security_error",
]
