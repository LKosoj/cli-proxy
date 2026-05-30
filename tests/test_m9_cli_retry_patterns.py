"""Tests for M9 fix #6: narrowed _RETRYABLE_TEXT_PATTERNS in cli_retry.

Verifies that fatal/non-retryable errors are not retried, while genuine
transient network/rate-limit errors are correctly identified.
"""
from __future__ import annotations

from modes.sdk.runtime.cli_retry import classify_retryable_cli_text


# --- Patterns that MUST still be retried (transient) ---

def test_quota_exceeded_is_retryable() -> None:
    assert classify_retryable_cli_text("[API Error: quota exceeded]")


def test_rate_limit_is_retryable() -> None:
    assert classify_retryable_cli_text("HTTP 429: rate limit hit")


def test_timed_out_is_retryable() -> None:
    assert classify_retryable_cli_text("connection timed out after 30s")


def test_request_timed_out_is_retryable() -> None:
    assert classify_retryable_cli_text("request timed out waiting for model response")


def test_connection_reset_is_retryable() -> None:
    assert classify_retryable_cli_text("connection reset by peer")


def test_overloaded_is_retryable() -> None:
    assert classify_retryable_cli_text("The model is currently overloaded")


def test_503_service_unavailable_is_retryable() -> None:
    assert classify_retryable_cli_text("503 service unavailable from upstream")


def test_temporarily_unavailable_is_retryable() -> None:
    assert classify_retryable_cli_text("service temporarily unavailable, please wait")


def test_try_again_later_is_retryable() -> None:
    assert classify_retryable_cli_text("Quota exhausted. Try again later.")


def test_api_quota_exhausted_is_retryable() -> None:
    assert classify_retryable_cli_text("[API Error: Qwen API quota has been exhausted]")


# --- Patterns that must NOT be retried (fatal / non-transient) ---

def test_bare_timeout_word_in_fatal_message_not_retried() -> None:
    # e.g. "TIMEOUT_EXCEEDED" in an auth/config error context should NOT trigger retry
    # With the old pattern "timeout" this would match; with the new patterns it won't.
    msg = "TIMEOUT_EXCEEDED: authentication token has expired and cannot be refreshed"
    reason = classify_retryable_cli_text(msg)
    assert not reason, f"Fatal message should not be retried, got: {reason!r}"


def test_internal_server_error_not_retried() -> None:
    # "internal server error" was removed as too broad; a 500 from the auth
    # system or a configuration error should not trigger a blind retry.
    msg = "internal server error: invalid API key format"
    reason = classify_retryable_cli_text(msg)
    assert not reason, f"Fatal message should not be retried, got: {reason!r}"


def test_generic_service_unavailable_not_retried() -> None:
    # "service unavailable" (without 503 prefix) was too broad — could match
    # permanent decommission notices. The pattern now requires "503 service unavailable".
    msg = "service unavailable: endpoint has been removed"
    reason = classify_retryable_cli_text(msg)
    assert not reason, f"Broad 'service unavailable' should not be retried, got: {reason!r}"


def test_empty_string_not_retried() -> None:
    assert classify_retryable_cli_text("") == ""


def test_unrelated_error_not_retried() -> None:
    assert classify_retryable_cli_text("SyntaxError: unexpected token at line 5") == ""
