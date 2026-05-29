from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from types import SimpleNamespace

from app.events.bus import SystemEventBus
from app.security import SecurityFacade
from app.security.audit import EventBusAuditService, SqliteAuditLogStore


def test_audit_event_payload_contains_user_action_timestamp_and_context(tmp_path) -> None:
    bus = SystemEventBus()
    events: list[tuple[str, dict]] = []

    async def _capture(event: str, payload: dict) -> None:
        events.append((event, dict(payload)))

    bus.subscribe(EventBusAuditService.EVENT_NAME, _capture)
    facade = SecurityFacade.from_config(
        audit_config={"state_path": str(tmp_path / "state.json")},
        system_event_bus=bus,
    )

    asyncio.run(
        facade.emit_audit(
            category="auth",
            action="token.login",
            status="ok",
            user_id=101,
            subject="user:101",
            scope="miniapp",
            reason="",
            timestamp=1700000000.0,
            context={"chat_id": 55, "session_id": "sess-a"},
            details={"ip": "127.0.0.1"},
        )
    )

    assert len(events) == 1
    event_name, payload = events[0]
    assert event_name == EventBusAuditService.EVENT_NAME
    assert payload["user_id"] == "101"
    assert payload["action"] == "token.login"
    assert payload["timestamp"] == 1700000000.0
    assert payload["context"] == {"chat_id": 55, "session_id": "sess-a"}
    assert payload["details"] == {"ip": "127.0.0.1"}


def test_audit_logs_are_persisted_and_restored_from_sqlite(tmp_path) -> None:
    state_path = tmp_path / "state.json"
    facade = SecurityFacade.from_config(audit_config={"state_path": str(state_path)})

    first = asyncio.run(
        facade.emit_audit(
            category="auth",
            action="token.login",
            status="ok",
            user_id=7,
            subject="user:7",
            scope="miniapp",
            timestamp=1700000001.0,
            context={"chat_id": 7},
            details={"provider": "token"},
        )
    )
    second = asyncio.run(
        facade.emit_audit(
            category="auth",
            action="oauth.login",
            status="denied",
            user_id=8,
            subject="user:8",
            scope="miniapp",
            reason="invalid_scope",
            timestamp=1700000002.0,
            context={"chat_id": 8},
            details={"provider": "oauth"},
        )
    )

    assert first.action == "token.login"
    assert second.action == "oauth.login"

    store = SqliteAuditLogStore(state_path=str(state_path))
    db_path = Path(store.db_path)
    with sqlite3.connect(str(db_path)) as conn:
        row = conn.execute(
            f"SELECT COUNT(*) FROM {SqliteAuditLogStore.TABLE_NAME}"
        ).fetchone()
    assert row is not None
    assert int(row[0]) == 2

    restored_facade = SecurityFacade.from_config(audit_config={"state_path": str(state_path)})
    restored = restored_facade.list_audit_logs(limit=10)
    assert [record.action for record in restored] == ["oauth.login", "token.login"]
    assert restored[0].user_id == "8"
    assert restored[0].timestamp == 1700000002.0
    assert restored[0].context == {"chat_id": 8}
    assert restored[1].details == {"provider": "token"}


def test_audit_log_storage_is_isolated_between_paths_and_app_config_restores(tmp_path) -> None:
    first_state = tmp_path / "state-a.json"
    second_state = tmp_path / "state-b.json"
    bot_app = SimpleNamespace(
        config=SimpleNamespace(defaults=SimpleNamespace(state_path=str(first_state))),
        system_event_bus=SystemEventBus(),
        is_admin=lambda chat_id: int(chat_id) == 1,
        is_user=lambda chat_id: int(chat_id) in {1, 2},
    )

    first_facade = SecurityFacade.from_app_config(
        bot_app.config,
        is_admin_fn=bot_app.is_admin,
        is_user_fn=bot_app.is_user,
        system_event_bus=bot_app.system_event_bus,
    )
    second_facade = SecurityFacade.from_config(audit_config={"state_path": str(second_state)})

    asyncio.run(
        first_facade.emit_audit(
            category="validation",
            action="text.accepted",
            status="ok",
            user_id=1,
            timestamp=1700000003.0,
            context={"chat_id": 1, "session_id": "sess-one"},
        )
    )
    asyncio.run(
        second_facade.emit_audit(
            category="validation",
            action="text.rejected",
            status="denied",
            user_id=2,
            reason="too_long",
            timestamp=1700000004.0,
            context={"chat_id": 2, "session_id": "sess-two"},
        )
    )

    restored_first = SecurityFacade.from_app_config(
        bot_app.config,
        is_admin_fn=bot_app.is_admin,
        is_user_fn=bot_app.is_user,
        system_event_bus=bot_app.system_event_bus,
    ).list_audit_logs(limit=10)
    restored_second = SecurityFacade.from_config(audit_config={"state_path": str(second_state)}).list_audit_logs(limit=10)

    assert [record.action for record in restored_first] == ["text.accepted"]
    assert [record.action for record in restored_second] == ["text.rejected"]
    assert restored_first[0].context["session_id"] == "sess-one"
    assert restored_second[0].reason == "too_long"
