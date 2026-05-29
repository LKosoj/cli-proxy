from __future__ import annotations

import os

import pytest

from app.security import SecurityFacade, SecurityValidationError


def test_text_validation_supports_custom_rules_and_context_constraints() -> None:
    def deny_urls(value: str, *, field_name: str, context: dict[str, object]) -> str | None:
        del field_name, context
        if "http://" in value or "https://" in value:
            return "urls are not allowed"
        return None

    facade = SecurityFacade.from_config(
        validator_config={
            "text": {
                "max_length": 32,
                "deny_substrings": ["forbidden"],
                "required_context_keys": ["session_id"],
                "user_max_length": {"7": 5},
                "enabled_rules": ["deny_urls"],
            }
        },
        custom_text_rules={"deny_urls": deny_urls},
    )

    assert (
        facade.require_text(
            "  hello  ",
            field_name="message",
            context={"session_id": "sess-1"},
        )
        == "hello"
    )

    with pytest.raises(SecurityValidationError, match=r"context\.session_id is required"):
        facade.require_text("hello", field_name="message")

    with pytest.raises(SecurityValidationError, match="denied substring"):
        facade.require_text(
            "forbidden payload",
            field_name="message",
            context={"session_id": "sess-1"},
        )

    with pytest.raises(SecurityValidationError, match="max_length=5"):
        facade.require_text(
            "123456",
            field_name="message",
            context={"session_id": "sess-1", "user_id": "7"},
        )

    with pytest.raises(SecurityValidationError, match="urls are not allowed"):
        facade.require_text(
            "https://example.com",
            field_name="message",
            context={"session_id": "sess-1"},
        )


def test_path_validation_supports_context_roots_and_custom_rules(tmp_path) -> None:
    default_root = tmp_path / "default"
    user_root = tmp_path / "user"
    session_root = tmp_path / "session"
    default_root.mkdir()
    user_root.mkdir()
    session_root.mkdir()

    def deny_hidden(result, *, field_name: str, context: dict[str, object]) -> str | None:
        del field_name, context
        if result.relative_path.startswith("."):
            return "hidden paths are blocked"
        return None

    facade = SecurityFacade.from_config(
        validator_config={
            "path": {
                "deny_names": ["secrets.txt"],
                "deny_extensions": ["pem"],
                "roots_by_user": {"42": str(user_root)},
                "roots_by_session": {"sess-1": str(session_root)},
                "enabled_rules": ["deny_hidden"],
            }
        },
        custom_path_rules={"deny_hidden": deny_hidden},
    )

    user_path = facade.resolve_path(
        str(default_root),
        "docs/readme.md",
        context={"user_id": "42"},
    )
    assert user_path.root == os.path.realpath(str(user_root))
    assert user_path.relative_path == "docs/readme.md"

    session_path = facade.resolve_path(
        str(default_root),
        "notes.txt",
        context={"session_id": "sess-1", "user_id": "42"},
    )
    assert session_path.root == os.path.realpath(str(session_root))
    assert session_path.relative_path == "notes.txt"

    with pytest.raises(SecurityValidationError, match="protected name"):
        facade.resolve_path(
            str(default_root),
            "secrets.txt",
            context={"user_id": "42"},
        )

    with pytest.raises(SecurityValidationError, match="protected extension"):
        facade.resolve_path(
            str(default_root),
            "cert.pem",
            context={"user_id": "42"},
        )

    with pytest.raises(SecurityValidationError, match="hidden paths are blocked"):
        facade.resolve_path(
            str(default_root),
            ".env",
            context={"session_id": "sess-1"},
        )


def test_validator_config_rejects_unknown_custom_rules() -> None:
    with pytest.raises(SecurityValidationError, match="unknown text validation rules"):
        SecurityFacade.from_config(
            validator_config={
                "text": {
                    "enabled_rules": ["missing_rule"],
                }
            }
        )

    with pytest.raises(SecurityValidationError, match="unknown path validation rules"):
        SecurityFacade.from_config(
            validator_config={
                "path": {
                    "enabled_rules": ["missing_rule"],
                }
            }
        )


def test_validator_configuration_does_not_leak_between_facades() -> None:
    strict_facade = SecurityFacade.from_config(
        validator_config={
            "text": {
                "deny_substrings": ["secret"],
                "user_max_length": {"1": 4},
            }
        }
    )
    relaxed_facade = SecurityFacade.from_config(
        validator_config={
            "text": {
                "max_length": 64,
            }
        }
    )

    with pytest.raises(SecurityValidationError, match="denied substring"):
        strict_facade.require_text("secret payload", context={"user_id": "99"})

    assert relaxed_facade.require_text("secret payload", context={"user_id": "99"}) == "secret payload"

    with pytest.raises(SecurityValidationError, match="max_length=4"):
        strict_facade.require_text("12345", context={"user_id": "1"})

    assert relaxed_facade.require_text("12345", context={"user_id": "1"}) == "12345"
