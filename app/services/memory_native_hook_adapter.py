from __future__ import annotations

import argparse
import hashlib
import logging
import os
import re
import sys
import time
from typing import Any, Sequence

from app.services.memory_event_store import MemoryEventStore
from config import load_config
from modes.sdk.runtime.json_normalizer import loads_safe


logger = logging.getLogger(__name__)


def _truthy(value: Any) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _sha256_text(value: Any) -> str:
    text = str(value or "")
    if not text:
        return ""
    return f"sha256:{hashlib.sha256(text.encode('utf-8', errors='ignore')).hexdigest()}"


def _snake(value: str) -> str:
    text = re.sub(r"[^0-9A-Za-z]+", "_", str(value or "").strip()).strip("_").lower()
    return text or "unknown"


def _read_payload(stdin: Any = None) -> dict[str, Any]:
    handle = stdin if stdin is not None else sys.stdin
    raw = handle.read()
    if not str(raw or "").strip():
        return {}
    parsed = loads_safe(str(raw), strict_first=False)
    return dict(parsed) if isinstance(parsed, dict) else {}


def _first_text(payload: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = payload.get(key)
        if value is None:
            continue
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _mapping_keys(value: Any) -> list[str]:
    if not isinstance(value, dict):
        return []
    return sorted(str(key) for key in value.keys())


def build_native_memory_event(
    payload: dict[str, Any],
    *,
    source: str = "native_cli",
    event_name: str = "",
) -> dict[str, Any]:
    hook_name = event_name or _first_text(
        payload,
        "hook_event_name",
        "hookEventName",
        "event",
        "event_name",
        "type",
    )
    hook_name = hook_name or "unknown"
    prompt = _first_text(payload, "prompt", "user_prompt", "userPrompt")
    transcript_path = _first_text(payload, "transcript_path", "transcriptPath")
    cwd = _first_text(payload, "cwd", "workdir", "workspace")
    tool_name = _first_text(payload, "tool_name", "toolName", "tool")
    turn_id = _first_text(payload, "turn_id", "turnId")
    tool_use_id = _first_text(payload, "tool_use_id", "toolUseId")
    tool_input = payload.get("tool_input") or payload.get("toolInput")
    tool_response = payload.get("tool_response") or payload.get("toolResponse") or payload.get("response")
    session_uid = _first_text(payload, "session_id", "sessionId", "session_uid", "sessionUid")
    run_id = _first_text(payload, "run_id", "runId")
    prompt_hash = _sha256_text(prompt)
    metadata = {
        "hook_event_name": hook_name,
        "payload_keys": sorted(str(key) for key in payload.keys()),
        "prompt_len": len(prompt),
        "transcript_path_hash": _sha256_text(transcript_path),
        "cwd_hash": _sha256_text(cwd),
        "cwd_basename": os.path.basename(os.path.normpath(cwd)) if cwd else "",
        "turn_id": turn_id,
        "tool_use_id": tool_use_id,
        "tool_name": tool_name,
        "tool_input_keys": _mapping_keys(tool_input),
        "tool_response_len": len(str(tool_response or "")),
    }
    explicit_dedupe = _first_text(payload, "event_id", "eventId", "id", "hook_id", "hookId")
    fallback_dedupe = ""
    if turn_id or tool_use_id:
        fallback_dedupe = ":".join(token for token in (session_uid, hook_name, turn_id, tool_use_id) if token)
    return {
        "event_type": f"native_cli_{_snake(hook_name)}",
        "source": str(source or "native_cli").strip() or "native_cli",
        "session_uid": session_uid,
        "run_id": run_id,
        "mode_id": "cli",
        "phase": "native_hook",
        "unit_id": f"native:{_snake(hook_name)}",
        "prompt_hash": prompt_hash,
        "payload": metadata,
        "dedupe_key": explicit_dedupe or fallback_dedupe,
    }


def record_native_hook_payload(
    store: MemoryEventStore,
    payload: dict[str, Any],
    *,
    source: str = "native_cli",
    event_name: str = "",
    created_at: float | None = None,
    retention_days: int | None = None,
) -> bool:
    event = build_native_memory_event(payload, source=source, event_name=event_name)
    _record, inserted = store.record_event(
        event_type=event["event_type"],
        source=event["source"],
        session_uid=event["session_uid"],
        run_id=event["run_id"],
        mode_id=event["mode_id"],
        phase=event["phase"],
        unit_id=event["unit_id"],
        prompt_hash=event["prompt_hash"],
        payload=event["payload"],
        dedupe_key=event["dedupe_key"],
        created_at=created_at,
    )
    if retention_days is not None:
        store.prune_older_than(retention_days=int(retention_days or 1))
    return bool(inserted)


def _store_and_retention_from_args(args: argparse.Namespace) -> tuple[MemoryEventStore | None, int]:
    config_path = str(args.config or "").strip()
    if config_path:
        cfg = load_config(config_path)
        defaults = getattr(cfg, "defaults", None)
        if not bool(getattr(defaults, "memory_events_enabled", False)):
            return None, 30
        if not bool(getattr(defaults, "memory_native_cli_hooks_enabled", False)):
            return None, 30
        retention_days = int(getattr(defaults, "memory_events_retention_days", 30) or 30)
        return MemoryEventStore.from_config(cfg), retention_days

    if not _truthy(os.environ.get("CLI_PROXY_MEMORY_EVENTS_ENABLED")):
        return None, 30
    if not _truthy(os.environ.get("CLI_PROXY_MEMORY_NATIVE_CLI_HOOKS_ENABLED")):
        return None, 30
    state_path = str(args.state_path or os.environ.get("CLI_PROXY_MEMORY_STATE_PATH") or "").strip()
    if not state_path:
        return None, 30
    max_payload_chars = int(os.environ.get("CLI_PROXY_MEMORY_EVENTS_MAX_PAYLOAD_CHARS") or 6000)
    redaction_enabled = not str(os.environ.get("CLI_PROXY_MEMORY_EVENTS_REDACTION_ENABLED") or "").strip().lower() == "false"
    retention_days = int(os.environ.get("CLI_PROXY_MEMORY_EVENTS_RETENTION_DAYS") or 30)
    return (
        MemoryEventStore(
            state_path,
            max_payload_chars=max_payload_chars,
            redaction_enabled=redaction_enabled,
        ),
        retention_days,
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="CLI Proxy native hook adapter for shadow memory events.")
    parser.add_argument("--config", default="", help="Path to config.yaml. Both memory flags must be enabled.")
    parser.add_argument("--state-path", default="", help="Fallback state path when --config is not used.")
    parser.add_argument("--source", default=os.environ.get("CLI_PROXY_MEMORY_HOOK_SOURCE", "native_cli"))
    parser.add_argument("--event-name", default=os.environ.get("CLI_PROXY_MEMORY_HOOK_EVENT", ""))
    args = parser.parse_args(argv)

    try:
        payload = _read_payload()
        store, retention_days = _store_and_retention_from_args(args)
        if store is None:
            return 0
        record_native_hook_payload(
            store,
            payload,
            source=args.source,
            event_name=args.event_name,
            created_at=time.time(),
            retention_days=retention_days,
        )
        return 0
    except Exception as exc:
        logger.exception("native memory hook adapter failed")
        print(f"cli-proxy memory hook ignored error: {type(exc).__name__}", file=sys.stderr)
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
