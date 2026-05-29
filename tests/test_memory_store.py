import re

from modes.sdk.runtime.memory_store import (
    append_memory_structured,
    compact_memory_by_priority,
    parse_entries,
    read_memory,
    remove_expired_entries,
    trim_for_context,
)


def test_memory_store_task_state_ttl_is_pruned(tmp_path):
    cwd = str(tmp_path)
    ok = append_memory_structured(
        cwd,
        tag="TASK",
        content="временный шаг",
        layer="task_state",
        source="agent",
        confidence=0.5,
        ttl_days=1,
    )
    assert ok is True
    content = read_memory(cwd)
    assert "[EXP:" in content
    # Simulate expired entry and verify prune.
    expired = re.sub(r"\[EXP:[0-9]{4}-[0-9]{2}-[0-9]{2}\]", "[EXP:2000-01-01]", content, count=1)
    cleaned = remove_expired_entries(expired)
    assert "временный шаг" not in cleaned


def test_memory_store_layered_compaction_prioritizes_semantic(tmp_path):
    cwd = str(tmp_path)
    append_memory_structured(
        cwd,
        tag="PREF",
        content="использовать sqlite",
        layer="semantic",
        source="agent",
        confidence=0.9,
        ttl_days=None,
    )
    append_memory_structured(
        cwd,
        tag="TASK",
        content="временный заметный контекст",
        layer="task_state",
        source="agent",
        confidence=0.4,
        ttl_days=14,
    )
    raw = read_memory(cwd)
    compacted = compact_memory_by_priority(raw, max_bytes=220, priority=["PREF", "DECISION", "CONFIG", "AGREEMENT"])
    entries = parse_entries(compacted)
    assert entries
    assert entries[0]["tag"] == "PREF"


def test_trim_for_context_preserves_head_and_tail_with_explicit_degraded_marker():
    content = "HEAD-" + ("x" * 200) + "-TAIL"
    trimmed = trim_for_context(content, max_chars=80)
    assert "[degraded_context_trimmed" in trimmed
    assert trimmed.startswith("HEAD-")
    assert trimmed.endswith("-TAIL")


def test_trim_for_context_keeps_degraded_marker_even_for_tiny_budget():
    content = "HEAD-" + ("x" * 200) + "-TAIL"
    trimmed = trim_for_context(content, max_chars=24)
    assert trimmed
    assert "degraded" in trimmed.lower()
