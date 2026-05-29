from __future__ import annotations

import pytest

from app.security import (
    DenyReasonCode,
    SecurityAuthenticationError,
    SecurityFacade,
    SecurityRateLimitError,
    SecurityValidationError,
    get_user_facing_error_text,
    serialize_security_error,
)


def test_security_error_serialization_preserves_code_message_and_user_text() -> None:
    first_error = SecurityValidationError(
        DenyReasonCode.PROTECTED_EXTENSION,
        "path targets protected extension",
        details={"path": "secret.pem"},
    )
    second_error = SecurityRateLimitError(
        DenyReasonCode.WINDOW_LIMIT_EXCEEDED,
        details={"scope": "miniapp.auth", "subject": "user:7"},
    )

    first_payload = serialize_security_error(first_error)
    second_payload = serialize_security_error(second_error)

    assert first_payload == {
        "type": "validation",
        "code": DenyReasonCode.PROTECTED_EXTENSION,
        "message": "path targets protected extension",
        "user_message": get_user_facing_error_text(DenyReasonCode.PROTECTED_EXTENSION),
        "details": {"path": "secret.pem"},
    }
    assert second_payload["type"] == "rate_limit"
    assert second_payload["code"] == DenyReasonCode.WINDOW_LIMIT_EXCEEDED
    assert second_payload["message"] == get_user_facing_error_text(DenyReasonCode.WINDOW_LIMIT_EXCEEDED)
    assert second_payload["details"] == {"scope": "miniapp.auth", "subject": "user:7"}


def test_deny_reason_mapping_covers_auth_validation_and_rate_limit_codes() -> None:
    assert get_user_facing_error_text(DenyReasonCode.INVALID_TOKEN) == "Authentication token is invalid."
    assert (
        get_user_facing_error_text(DenyReasonCode.PATH_ESCAPES_ROOT)
        == "Requested path is outside the allowed workspace."
    )
    assert (
        get_user_facing_error_text(DenyReasonCode.BURST_LIMIT_EXCEEDED)
        == "Too many requests in a short period. Try again later."
    )


def test_security_layers_expose_standardized_reason_codes_and_typed_validation_error() -> None:
    facade = SecurityFacade.from_config(
        {
            "default_strategy": "token",
            "token": {"expected_token": "secret-token"},
        },
        validator_config={
            "text": {
                "deny_substrings": ["forbidden"],
            }
        },
    )

    auth_failure = facade.authenticate({"token": "wrong-token"})
    assert auth_failure.authenticated is False
    assert auth_failure.reason == DenyReasonCode.INVALID_TOKEN

    with pytest.raises(SecurityValidationError) as exc_info:
        facade.require_text("forbidden payload", field_name="message")

    assert exc_info.value.code == DenyReasonCode.DENIED_SUBSTRING
    assert exc_info.value.user_message == get_user_facing_error_text(DenyReasonCode.DENIED_SUBSTRING)

    first_limit = facade.consume_rate_limit("miniapp.auth", "user:1", limit=1, window_sec=60)
    blocked_limit = facade.consume_rate_limit("miniapp.auth", "user:1", limit=1, window_sec=60)

    assert first_limit.allowed is True
    assert first_limit.reason == DenyReasonCode.OK
    assert blocked_limit.allowed is False
    assert blocked_limit.reason == DenyReasonCode.WINDOW_LIMIT_EXCEEDED


def test_non_typed_security_error_serialization_uses_fallback_code() -> None:
    payload = serialize_security_error(
        SecurityAuthenticationError(DenyReasonCode.UNKNOWN_AUTH_STRATEGY),
    )
    fallback_payload = serialize_security_error(RuntimeError("unexpected denial"))

    assert payload["type"] == "authentication"
    assert payload["code"] == DenyReasonCode.UNKNOWN_AUTH_STRATEGY
    assert payload["user_message"] == get_user_facing_error_text(DenyReasonCode.UNKNOWN_AUTH_STRATEGY)
    assert fallback_payload == {
        "type": "security",
        "code": DenyReasonCode.SECURITY_DENIED,
        "message": "unexpected denial",
        "user_message": get_user_facing_error_text(DenyReasonCode.SECURITY_DENIED),
        "details": {},
    }
