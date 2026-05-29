from __future__ import annotations

import sqlite3
import time
from types import SimpleNamespace

from app.services.memory_event_store import MemoryEventStore


def test_memory_event_store_records_redacted_bounded_event_and_dedupes(tmp_path) -> None:
    store = MemoryEventStore(
        str(tmp_path / "state.json"),
        max_payload_chars=220,
        redaction_enabled=True,
    )

    first, inserted_first = store.record_event(
        event_type="cli_execution_start",
        source="task_bearing_cli",
        session_uid="session-1",
        run_id="run-1",
        mode_id="agent",
        phase="execute",
        unit_id="cli:telegram_direct",
        prompt_hash="sha256:prompt",
        dedupe_key="run-1:start",
        payload={
            "prompt": "use OPENAI_API_KEY=sk-secret and continue",
            "headers": {"Authorization": "Bearer secret"},
            "nested": [{"github_token": "ghp-secret"}],
            "long": "x" * 500,
        },
        created_at=1000.0,
    )
    duplicate, inserted_duplicate = store.record_event(
        event_type="cli_execution_start",
        source="task_bearing_cli",
        session_uid="session-1",
        run_id="run-1",
        mode_id="agent",
        phase="execute",
        unit_id="cli:telegram_direct",
        prompt_hash="sha256:prompt",
        dedupe_key="run-1:start",
        payload={"different": "ignored by dedupe"},
        created_at=1001.0,
    )

    assert inserted_first is True
    assert inserted_duplicate is False
    assert duplicate.event_id == first.event_id
    assert duplicate.created_at == 1000.0
    assert first.payload_truncated is True
    assert first.redacted is True
    assert first.payload["truncated"] is True
    assert len(store._dumps(first.payload)) <= store.max_payload_chars
    assert "sk-secret" not in first.payload["preview"]
    assert "ghp-secret" not in first.payload["preview"]
    assert "Bearer secret" not in first.payload["preview"]

    restarted = MemoryEventStore(str(tmp_path / "state.json"))
    rows = restarted.list_events(session_uid="session-1", run_id="run-1")
    assert [row.event_id for row in rows] == [first.event_id]

    with sqlite3.connect(restarted.db_path) as conn:
        count = conn.execute(
            f"SELECT COUNT(*) FROM {MemoryEventStore.TABLE_NAME} WHERE run_id = ?",
            ("run-1",),
        ).fetchone()[0]
    assert count == 1


def test_memory_event_store_redacts_free_text_secret_shapes(tmp_path) -> None:
    store = MemoryEventStore(str(tmp_path / "state.json"))

    record, inserted = store.record_event(
        event_type="cli_execution_start",
        source="task_bearing_cli",
        payload={
            "prompt": (
                "Authorization: Bearer abc.def.ghi "
                "then use sk-proj-abcdefghijklmnop and ghp_abcdefghijklmnopqrstuvwxyz "
                "with OPENAI_API_KEY=sk-envsecretvalue ANTHROPIC_API_KEY=anthropic-secret "
                "and GITHUB_TOKEN=ghp_envsecretvalue"
            ),
        },
        dedupe_key="free-text-secrets",
    )

    assert inserted is True
    assert record.redacted is True
    prompt = record.payload["prompt"]
    assert "abc.def.ghi" not in prompt
    assert "sk-proj-abcdefghijklmnop" not in prompt
    assert "ghp_abcdefghijklmnopqrstuvwxyz" not in prompt
    assert "sk-envsecretvalue" not in prompt
    assert "anthropic-secret" not in prompt
    assert "ghp_envsecretvalue" not in prompt
    assert "OPENAI_API_KEY=[REDACTED]" in prompt
    assert "ANTHROPIC_API_KEY=[REDACTED]" in prompt
    assert "GITHUB_TOKEN=[REDACTED]" in prompt
    assert prompt.count("[REDACTED]") >= 2


def test_memory_event_store_prunes_by_retention_days(tmp_path) -> None:
    store = MemoryEventStore(str(tmp_path / "state.json"))
    now = time.time()
    old, _ = store.record_event(
        event_type="cli_execution_end",
        source="task_bearing_cli",
        session_uid="session-1",
        run_id="old-run",
        dedupe_key="old",
        created_at=now - 10 * 86400,
    )
    fresh, _ = store.record_event(
        event_type="cli_execution_end",
        source="task_bearing_cli",
        session_uid="session-1",
        run_id="fresh-run",
        dedupe_key="fresh",
        created_at=now,
    )

    assert store.prune_older_than(retention_days=7, now=now) == 1
    assert store.get_event(old.event_id) is None
    assert store.get_event(fresh.event_id) is not None


def test_memory_event_store_uses_config_defaults(tmp_path) -> None:
    config = SimpleNamespace(
        defaults=SimpleNamespace(
            state_path=str(tmp_path / "runtime" / "state.json"),
            memory_events_max_payload_chars=512,
            memory_events_redaction_enabled=False,
        )
    )

    store = MemoryEventStore.from_config(config)
    record, inserted = store.record_event(
        event_type="native_hook",
        source="codex",
        payload={"api_key": "not-redacted-when-disabled"},
        dedupe_key="native-1",
    )

    assert inserted is True
    assert record.payload["api_key"] == "not-redacted-when-disabled"
    assert record.redacted is False
