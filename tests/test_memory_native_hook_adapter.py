from __future__ import annotations

import io
import json
import sys

from app.services.memory_event_store import MemoryEventStore
from app.services.memory_native_hook_adapter import build_native_memory_event, main, record_native_hook_payload
from config import AppConfig, DefaultsConfig, MCPConfig, MiniAppConfig, TelegramConfig, ToolConfig, save_config


def test_native_hook_adapter_builds_metadata_without_raw_prompt_or_paths() -> None:
    event = build_native_memory_event(
        {
            "hook_event_name": "UserPromptSubmit",
            "session_id": "session-1",
            "cwd": "/srv/git_projects/private-project",
            "prompt": "use OPENAI_API_KEY=sk-proj-secret and continue",
            "tool_input": {"command": "pytest", "token": "secret"},
            "tool_response": "x" * 80,
        },
        source="codex",
    )

    assert event["event_type"] == "native_cli_userpromptsubmit"
    assert event["source"] == "codex"
    assert event["session_uid"] == "session-1"
    assert event["prompt_hash"].startswith("sha256:")
    payload = event["payload"]
    assert payload["prompt_len"] == len("use OPENAI_API_KEY=sk-proj-secret and continue")
    assert payload["cwd_hash"].startswith("sha256:")
    assert payload["cwd_basename"] == "private-project"
    assert payload["tool_input_keys"] == ["command", "token"]
    dumped = json.dumps(payload, ensure_ascii=False)
    assert "sk-proj-secret" not in dumped
    assert "/srv/git_projects/private-project" not in dumped


def test_native_hook_adapter_records_to_memory_event_store(tmp_path) -> None:
    store = MemoryEventStore(str(tmp_path / "state.json"))

    inserted = record_native_hook_payload(
        store,
        {
            "hook_event_name": "Stop",
            "event_id": "evt-1",
            "session_id": "session-1",
            "prompt": "hello",
        },
        source="codex",
        created_at=1000.0,
    )
    duplicate = record_native_hook_payload(
        store,
        {
            "hook_event_name": "Stop",
            "event_id": "evt-1",
            "session_id": "session-1",
            "prompt": "changed but duplicate",
        },
        source="codex",
        created_at=1001.0,
    )

    assert inserted is True
    assert duplicate is False
    rows = store.list_events(session_uid="session-1")
    assert len(rows) == 1
    assert rows[0].event_type == "native_cli_stop"
    assert rows[0].source == "codex"
    assert rows[0].payload["hook_event_name"] == "Stop"


def test_native_hook_adapter_prunes_by_retention_after_record(tmp_path) -> None:
    store = MemoryEventStore(str(tmp_path / "state.json"))
    old, _ = store.record_event(
        event_type="old_native_hook",
        source="codex",
        session_uid="session-1",
        dedupe_key="old",
        created_at=1.0,
    )

    inserted = record_native_hook_payload(
        store,
        {
            "hook_event_name": "Stop",
            "event_id": "evt-1",
            "session_id": "session-1",
        },
        source="codex",
        retention_days=1,
    )

    assert inserted is True
    assert store.get_event(old.event_id) is None
    rows = store.list_events(session_uid="session-1")
    assert [row.event_type for row in rows] == ["native_cli_stop"]


def test_native_hook_adapter_dedupes_codex_turn_and_tool_ids_without_event_id(tmp_path) -> None:
    store = MemoryEventStore(str(tmp_path / "state.json"))
    payload = {
        "hook_event_name": "PostToolUse",
        "session_id": "session-1",
        "turn_id": "turn-1",
        "tool_use_id": "tool-1",
        "tool_name": "Bash",
        "tool_input": {"command": "pytest"},
    }

    first = record_native_hook_payload(store, payload, source="codex", created_at=1000.0)
    second = record_native_hook_payload(store, dict(payload), source="codex", created_at=1001.0)

    assert first is True
    assert second is False
    rows = store.list_events(session_uid="session-1")
    assert len(rows) == 1
    assert rows[0].payload["turn_id"] == "turn-1"
    assert rows[0].payload["tool_use_id"] == "tool-1"


def test_native_hook_adapter_keeps_prompt_hooks_without_native_ids_distinct(tmp_path) -> None:
    store = MemoryEventStore(str(tmp_path / "state.json"))
    payload = {
        "hook_event_name": "UserPromptSubmit",
        "session_id": "session-1",
        "prompt": "same prompt",
    }

    first = record_native_hook_payload(store, payload, source="codex", created_at=1000.0)
    second = record_native_hook_payload(store, dict(payload), source="codex", created_at=1001.0)

    assert first is True
    assert second is True
    rows = store.list_events(session_uid="session-1")
    assert len(rows) == 2
    assert {row.event_type for row in rows} == {"native_cli_userpromptsubmit"}


def test_native_hook_adapter_main_is_opt_in_via_env(tmp_path, monkeypatch) -> None:
    state_path = str(tmp_path / "state.json")
    payload = {"hook_event_name": "UserPromptSubmit", "session_id": "session-1", "prompt": "hello"}
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    monkeypatch.setenv("CLI_PROXY_MEMORY_STATE_PATH", state_path)
    monkeypatch.delenv("CLI_PROXY_MEMORY_EVENTS_ENABLED", raising=False)
    monkeypatch.delenv("CLI_PROXY_MEMORY_NATIVE_CLI_HOOKS_ENABLED", raising=False)

    assert main(["--source", "codex"]) == 0
    assert not (tmp_path / "state.sqlite3").exists()

    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    monkeypatch.setenv("CLI_PROXY_MEMORY_EVENTS_ENABLED", "1")
    monkeypatch.setenv("CLI_PROXY_MEMORY_NATIVE_CLI_HOOKS_ENABLED", "1")

    assert main(["--source", "codex"]) == 0
    store = MemoryEventStore(state_path)
    rows = store.list_events(session_uid="session-1")
    assert len(rows) == 1
    assert rows[0].event_type == "native_cli_userpromptsubmit"


def test_native_hook_adapter_main_respects_config_flags(tmp_path, monkeypatch) -> None:
    cfg = AppConfig(
        telegram=TelegramConfig(token="token", whitelist_chat_ids=[1], admlist_chat_ids=[1]),
        tools={"dummy": ToolConfig(name="dummy", mode="headless", cmd=["cat"])},
        defaults=DefaultsConfig(
            workdir=str(tmp_path / "workdir"),
            state_path=str(tmp_path / "runtime" / "state.json"),
            memory_events_enabled=True,
            memory_native_cli_hooks_enabled=False,
        ),
        mcp=MCPConfig(enabled=False),
        mcp_clients=[],
        presets=[],
        path=str(tmp_path / "config.yaml"),
        miniapp=MiniAppConfig(),
    )
    save_config(cfg)
    payload = {"hook_event_name": "UserPromptSubmit", "session_id": "session-1", "prompt": "hello"}
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))

    assert main(["--config", cfg.path, "--source", "codex"]) == 0
    assert MemoryEventStore.from_config(cfg).list_events(limit=10) == []

    cfg.defaults.memory_native_cli_hooks_enabled = True
    save_config(cfg)
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))

    assert main(["--config", cfg.path, "--source", "codex"]) == 0
    rows = MemoryEventStore.from_config(cfg).list_events(session_uid="session-1")
    assert len(rows) == 1


def test_native_hook_adapter_main_fail_opens_on_malformed_stdin(monkeypatch, capsys) -> None:
    monkeypatch.setattr(sys, "stdin", io.StringIO("{not json"))
    monkeypatch.setenv("CLI_PROXY_MEMORY_EVENTS_ENABLED", "1")
    monkeypatch.setenv("CLI_PROXY_MEMORY_NATIVE_CLI_HOOKS_ENABLED", "1")
    monkeypatch.setenv("CLI_PROXY_MEMORY_STATE_PATH", "/definitely/missing/state.json")

    assert main(["--source", "codex"]) == 0
    assert "cli-proxy memory hook ignored error" in capsys.readouterr().err
