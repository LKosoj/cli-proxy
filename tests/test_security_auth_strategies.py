from __future__ import annotations

from app.security import SecurityFacade


def _oauth_verifier(token: str):
    if token == "oauth-good":
        return {
            "sub": "oauth:user-1",
            "aud": "cli-proxy",
            "scope": "openid profile security.read",
        }
    if token == "oauth-missing-scope":
        return {
            "sub": "oauth:user-2",
            "aud": "cli-proxy",
            "scope": "openid profile",
        }
    if token == "oauth-wrong-aud":
        return {
            "sub": "oauth:user-3",
            "aud": "other-service",
            "scope": "openid profile security.read",
        }
    return None


def test_token_strategy_authenticates_success_and_failure() -> None:
    facade = SecurityFacade.from_config(
        {
            "default_strategy": "token",
            "token": {"expected_token": "secret-token", "subject_claim": "subject"},
        }
    )

    success = facade.authenticate({"token": "secret-token", "subject": "svc:deploy"})
    failure = facade.authenticate({"token": "wrong-token", "subject": "svc:deploy"})

    assert success.authenticated is True
    assert success.strategy == "token"
    assert success.subject == "svc:deploy"
    assert failure.authenticated is False
    assert failure.reason == "invalid_token"


def test_oauth_strategy_authenticates_success_and_failure() -> None:
    facade = SecurityFacade.from_config(
        {
            "default_strategy": "oauth",
            "oauth": {
                "audience": "cli-proxy",
                "required_scopes": ["openid", "security.read"],
            },
        },
        oauth_token_verifier=_oauth_verifier,
    )

    success = facade.authenticate({"access_token": "oauth-good"})
    missing_scope = facade.authenticate({"access_token": "oauth-missing-scope"})
    wrong_audience = facade.authenticate({"access_token": "oauth-wrong-aud"})

    assert success.authenticated is True
    assert success.strategy == "oauth"
    assert success.subject == "oauth:user-1"
    assert missing_scope.authenticated is False
    assert missing_scope.reason == "missing_scope"
    assert wrong_audience.authenticated is False
    assert wrong_audience.reason == "invalid_audience"


def test_security_facade_supports_multiple_auth_strategies_with_explicit_override() -> None:
    facade = SecurityFacade.from_config(
        {
            "default_strategy": "token",
            "token": {"expected_token": "secret-token"},
            "oauth": {
                "audience": "cli-proxy",
                "required_scopes": ["openid", "security.read"],
            },
        },
        oauth_token_verifier=_oauth_verifier,
    )

    token_result = facade.authenticate({"token": "secret-token"})
    oauth_result = facade.authenticate({"access_token": "oauth-good"}, strategy="oauth")

    assert token_result.authenticated is True
    assert token_result.strategy == "token"
    assert oauth_result.authenticated is True
    assert oauth_result.strategy == "oauth"


def test_auth_backend_can_be_swapped_via_config_without_state_leak_between_facades() -> None:
    token_facade = SecurityFacade.from_config(
        {
            "default_strategy": "token",
            "token": {"expected_token": "secret-token"},
        }
    )
    oauth_facade = SecurityFacade.from_config(
        {
            "default_strategy": "oauth",
            "oauth": {
                "audience": "cli-proxy",
                "required_scopes": ["openid", "security.read"],
            },
        },
        oauth_token_verifier=_oauth_verifier,
    )

    token_ok = token_facade.authenticate({"token": "secret-token"})
    oauth_fail_on_token_facade = token_facade.authenticate({"access_token": "oauth-good"}, strategy="oauth")
    oauth_ok = oauth_facade.authenticate({"access_token": "oauth-good"})
    token_fail_on_oauth_facade = oauth_facade.authenticate({"token": "secret-token"}, strategy="token")

    assert token_ok.authenticated is True
    assert oauth_fail_on_token_facade.authenticated is False
    assert oauth_fail_on_token_facade.reason == "unknown_auth_strategy"
    assert oauth_ok.authenticated is True
    assert token_fail_on_oauth_facade.authenticated is False
    assert token_fail_on_oauth_facade.reason == "unknown_auth_strategy"
