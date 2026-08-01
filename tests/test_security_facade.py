from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from app.events.bus import SystemEventBus
from app.security import SecurityFacade, SecurityValidationError
from app.security.audit import EventBusAuditService


def test_security_facade_contract_covers_auth_validation_audit_and_rate_limits(tmp_path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    bus = SystemEventBus()
    events: list[tuple[str, dict]] = []

    async def _capture(event: str, payload: dict) -> None:
        events.append((event, dict(payload)))

    bus.subscribe(EventBusAuditService.EVENT_NAME, _capture)
    bot_app = SimpleNamespace(
        system_event_bus=bus,
        is_admin=lambda chat_id: int(chat_id) == 1,
        is_user=lambda chat_id: int(chat_id) in {1, 2},
    )
    facade = SecurityFacade.from_app_config(
        None,
        is_admin_fn=bot_app.is_admin,
        is_user_fn=bot_app.is_user,
        system_event_bus=bus,
    )

    admin = facade.authorize(1, scope="miniapp", require_admin=True)
    allowed_user = facade.authorize(2, scope="files")
    denied_user = facade.authorize(3, scope="files")
    denied_mode = asyncio.run(
        facade.authorize_mode_launch(
            2,
            mode_id="manager",
            is_mode_allowed=False,
            action="enable",
            session_id="sess-mode",
            context={"source": "test"},
        )
    )
    assert admin.allowed is True
    assert admin.is_admin is True
    assert allowed_user.allowed is True
    assert allowed_user.is_user is True
    assert denied_user.allowed is False
    assert denied_user.reason == "not_allowed"
    assert denied_mode.allowed is False
    assert denied_mode.reason == "mode_not_allowed"

    assert facade.require_text("  hello  ", field_name="message") == "hello"
    path_result = facade.resolve_path(
        str(root),
        "nested/file.txt",
        deny_names=("config.yaml",),
        deny_extensions=(".pem",),
    )
    assert path_result.relative_path == "nested/file.txt"
    assert path_result.resolved_path.endswith("nested/file.txt")

    first_limit = facade.consume_rate_limit("miniapp.auth", "user:2", limit=1, window_sec=60)
    second_limit = facade.consume_rate_limit("miniapp.auth", "user:2", limit=1, window_sec=60)
    assert first_limit.allowed is True
    assert second_limit.allowed is False
    assert second_limit.retry_after_sec > 0

    record = asyncio.run(
        facade.emit_audit(
            category="auth",
            action="authorize",
            status="denied",
            user_id=3,
            subject="user:3",
            scope="files",
            reason="not_allowed",
            timestamp=12345.0,
            context={"chat_id": 3, "session_id": "sess-1"},
            details={"chat_id": 3},
        )
    )
    assert record.action == "authorize"
    assert len(events) == 2
    mode_event_name, mode_payload = events[0]
    assert mode_event_name == EventBusAuditService.EVENT_NAME
    assert mode_payload["category"] == "mode_launch"
    assert mode_payload["action"] == "enable"
    assert mode_payload["status"] == "denied"
    assert mode_payload["user_id"] == "2"
    assert mode_payload["subject"] == "manager"
    assert mode_payload["reason"] == "mode_not_allowed"
    assert mode_payload["context"]["session_id"] == "sess-mode"
    assert mode_payload["context"]["source"] == "test"
    event_name, payload = events[1]
    assert event_name == EventBusAuditService.EVENT_NAME
    assert payload["category"] == "auth"
    assert payload["action"] == "authorize"
    assert payload["status"] == "denied"
    assert payload["user_id"] == "3"
    assert payload["subject"] == "user:3"
    assert payload["scope"] == "files"
    assert payload["reason"] == "not_allowed"
    assert payload["timestamp"] == 12345.0
    assert payload["context"] == {"chat_id": 3, "session_id": "sess-1"}
    assert payload["details"] == {"chat_id": 3}


def test_security_facade_path_validation_blocks_escape_and_protected_targets(tmp_path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    facade = SecurityFacade()

    with pytest.raises(SecurityValidationError, match="path escapes root"):
        facade.resolve_path(str(root), "../outside.txt")

    with pytest.raises(SecurityValidationError, match="protected name"):
        facade.resolve_path(str(root), "config.yaml", deny_names=("config.yaml",))

    with pytest.raises(SecurityValidationError, match="protected extension"):
        facade.resolve_path(str(root), "secret.pem", deny_extensions=(".pem",))


def test_security_rate_limit_state_is_scoped_by_subject_and_scope() -> None:
    facade = SecurityFacade()

    first = facade.consume_rate_limit("miniapp.auth", "user:1", limit=1, window_sec=60)
    blocked_same_key = facade.consume_rate_limit("miniapp.auth", "user:1", limit=1, window_sec=60)
    allowed_other_subject = facade.consume_rate_limit("miniapp.auth", "user:2", limit=1, window_sec=60)
    allowed_other_scope = facade.consume_rate_limit("miniapp.files", "user:1", limit=1, window_sec=60)

    assert first.allowed is True
    assert blocked_same_key.allowed is False
    assert allowed_other_subject.allowed is True
    assert allowed_other_scope.allowed is True


def test_security_facade_authorize_mode_launch_allows_desktop_actor_id_and_audits_it() -> None:
    bus = SystemEventBus()
    events: list[tuple[str, dict]] = []

    async def _capture(event: str, payload: dict) -> None:
        events.append((event, dict(payload)))

    bus.subscribe(EventBusAuditService.EVENT_NAME, _capture)
    bot_app = SimpleNamespace(
        system_event_bus=bus,
        is_admin=lambda chat_id: False,
        is_user=lambda chat_id: False,
    )
    facade = SecurityFacade.from_app_config(
        None,
        is_admin_fn=bot_app.is_admin,
        is_user_fn=bot_app.is_user,
        system_event_bus=bus,
    )

    decision = asyncio.run(
        facade.authorize_mode_launch(
            "desktop:default",
            mode_id="manager",
            action="event_launch",
            session_id="desktop:s1",
            context={
                "actor_id": "desktop:default",
                "origin": "desktop",
            },
        )
    )

    assert decision.allowed is True
    assert decision.chat_id == 0
    assert len(events) == 1
    event_name, payload = events[0]
    assert event_name == EventBusAuditService.EVENT_NAME
    assert payload["category"] == "mode_launch"
    assert payload["status"] == "allowed"
    assert payload["user_id"] == "desktop:default"
    assert payload["context"]["actor_id"] == "desktop:default"
    assert payload["context"]["origin"] == "desktop"
