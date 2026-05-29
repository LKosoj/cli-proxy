from __future__ import annotations

import importlib.util
import sqlite3
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
_STATE_STORE_PATH = REPO_ROOT / "modes" / "admin" / "state_store.py"
_SPEC = importlib.util.spec_from_file_location("modes_admin_state_store_test", _STATE_STORE_PATH)
if _SPEC is None or _SPEC.loader is None:
    raise RuntimeError(f"failed to load admin state_store module from {_STATE_STORE_PATH}")
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)
AdminStateStore = _MODULE.AdminStateStore
AdminStateStoreError = _MODULE.AdminStateStoreError


def _table_exists(db_path: Path, table_name: str) -> bool:
    with sqlite3.connect(str(db_path)) as conn:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (str(table_name),),
        ).fetchone()
    return bool(row and row[0] == table_name)


def test_admin_state_store_creates_required_tables(tmp_path) -> None:
    store = AdminStateStore(str(tmp_path / "state.json"))
    db_path = Path(store.db_path)
    required = {
        "admin_session_state",
        "admin_incidents",
        "admin_actions",
        "admin_alerts_state",
        "admin_acknowledgements",
        "admin_approved_overrides",
        "admin_digests",
    }
    for table_name in required:
        assert _table_exists(db_path, table_name)


def test_admin_session_state_has_required_schema_columns(tmp_path) -> None:
    store = AdminStateStore(str(tmp_path / "state.json"))
    with sqlite3.connect(str(store.db_path)) as conn:
        rows = conn.execute("PRAGMA table_info(admin_session_state)").fetchall()
    columns = {str(row[1]) for row in rows}
    required = {
        "id",
        "chat_id",
        "session_id",
        "enabled",
        "watch_enabled",
        "dry_run",
        "muted_until_ts",
        "updated_at",
        "updated_by",
        "last_error",
    }
    assert required.issubset(columns)


def test_admin_state_store_rejects_legacy_admin_session_state_schema(tmp_path) -> None:
    store = AdminStateStore(str(tmp_path / "state.json"))
    db_path = Path(store.db_path)

    with sqlite3.connect(str(db_path)) as conn:
        conn.execute("DROP TABLE IF EXISTS admin_session_state")
        conn.execute(
            """
            CREATE TABLE admin_session_state (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              session_id TEXT NOT NULL,
              status TEXT NOT NULL DEFAULT '',
              payload TEXT NOT NULL DEFAULT '{}',
              updated_at REAL NOT NULL DEFAULT 0
            )
            """
        )

    with pytest.raises(AdminStateStoreError, match=r"admin_session_state is missing required columns"):
        AdminStateStore(str(tmp_path / "state.json"))


def test_admin_session_state_keyed_by_chat_and_session(tmp_path) -> None:
    store = AdminStateStore(str(tmp_path / "state.json"))
    row_a = store.upsert_session_state("sess-1", chat_id=10, enabled=True, watch_enabled=True)
    row_b = store.upsert_session_state("sess-1", chat_id=20, enabled=False, watch_enabled=False)

    assert row_a["chat_id"] == 10
    assert row_a["session_id"] == "sess-1"
    assert row_a["enabled"] is True
    assert row_b["chat_id"] == 20
    assert row_b["enabled"] is False

    loaded_a = store.get_session_state("sess-1", chat_id=10)
    loaded_b = store.get_session_state("sess-1", chat_id=20)
    assert loaded_a is not None and loaded_b is not None
    assert loaded_a["enabled"] is True
    assert loaded_b["enabled"] is False


def test_admin_session_state_defaults_and_mute_unmute(tmp_path) -> None:
    store = AdminStateStore(str(tmp_path / "state.json"))
    row = store.upsert_session_state("sess-2", chat_id=1)
    assert row["dry_run"] is True
    assert row["watch_enabled"] is False
    assert row["muted_until_ts"] is None

    muted = store.mute_session("sess-2", 12345.0, chat_id=1)
    assert float(muted["muted_until_ts"]) == 12345.0
    unmuted = store.unmute_session("sess-2", chat_id=1)
    assert unmuted["muted_until_ts"] is None


def test_admin_entity_crud_scoped_by_chat_and_session(tmp_path) -> None:
    store = AdminStateStore(str(tmp_path / "state.json"))
    sid = "sess-entity"
    chat_a = 1
    chat_b = 2

    store.create_incident("inc-a", session_id=sid, chat_id=chat_a, payload={"n": 1})
    store.create_incident("inc-b", session_id=sid, chat_id=chat_b, payload={"n": 2})
    store.create_action("act-a", session_id=sid, chat_id=chat_a, payload={"kind": "x"})
    store.create_action("act-b", session_id=sid, chat_id=chat_b, payload={"kind": "y"})

    inc_a = store.list_incidents(sid, chat_id=chat_a, limit=10)
    inc_b = store.list_incidents(sid, chat_id=chat_b, limit=10)
    act_a = store.list_actions(sid, chat_id=chat_a, limit=10)
    act_b = store.list_actions(sid, chat_id=chat_b, limit=10)

    assert [item["incident_id"] for item in inc_a] == ["inc-a"]
    assert [item["incident_id"] for item in inc_b] == ["inc-b"]
    assert [item["action_id"] for item in act_a] == ["act-a"]
    assert [item["action_id"] for item in act_b] == ["act-b"]


def test_admin_approved_overrides_clear_is_chat_and_session_scoped(tmp_path) -> None:
    store = AdminStateStore(str(tmp_path / "state.json"))
    store.create_approved_override("ovr-a1", session_id="sess-a", chat_id=1, payload={"ok": True})
    store.create_approved_override("ovr-a2", session_id="sess-a", chat_id=1, payload={"ok": True})
    store.create_approved_override("ovr-b1", session_id="sess-a", chat_id=2, payload={"ok": True})
    store.create_approved_override("ovr-c1", session_id="sess-b", chat_id=1, payload={"ok": True})

    removed = store.clear_approved_overrides("sess-a", chat_id=1)
    assert removed == 2
    assert store.list_approved_overrides("sess-a", chat_id=1, limit=10) == []
    assert len(store.list_approved_overrides("sess-a", chat_id=2, limit=10)) == 1
    assert len(store.list_approved_overrides("sess-b", chat_id=1, limit=10)) == 1
