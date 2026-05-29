import pytest

from modes.admin.memory import (
    DEFAULT_COMPACT_THRESHOLD,
    ServerMemory,
    ServerMemoryError,
    facts_path,
    memory_dir,
    notes_path,
)


def _memory(tmp_path, server_id="web-01") -> ServerMemory:
    return ServerMemory(str(tmp_path), server_id)


def test_layout_paths(tmp_path):
    assert memory_dir(str(tmp_path), "web-01").as_posix().endswith("/web-01/memory")
    assert facts_path(str(tmp_path), "web-01").name == "facts.yaml"
    assert notes_path(str(tmp_path), "web-01").name == "notes.md"


def test_get_facts_empty(tmp_path):
    mem = _memory(tmp_path)
    assert mem.get_facts() == {}


def test_update_fact_persists_and_records_meta(tmp_path):
    mem = _memory(tmp_path)
    res = mem.update_fact("service_manager", "systemd", by="executor")
    assert res["key"] == "service_manager"
    assert res["value"] == "systemd"
    assert res["prev"] is None
    facts = mem.get_facts()
    assert facts["service_manager"] == "systemd"
    assert "_meta" in facts
    assert facts["_meta"]["updated_by"]["service_manager"] == "executor"


def test_update_fact_tracks_previous_value(tmp_path):
    mem = _memory(tmp_path)
    mem.update_fact("python_version", "3.11")
    res = mem.update_fact("python_version", "3.12")
    assert res["prev"] == "3.11"
    assert res["value"] == "3.12"
    assert mem.get_facts()["python_version"] == "3.12"


def test_delete_fact_removes_key(tmp_path):
    mem = _memory(tmp_path)
    mem.update_fact("a", 1)
    mem.update_fact("b", 2)
    assert mem.delete_fact("a") is True
    assert mem.delete_fact("a") is False
    facts = mem.get_facts()
    assert "a" not in facts
    assert facts["b"] == 2


def test_reserved_meta_key_rejected(tmp_path):
    mem = _memory(tmp_path)
    with pytest.raises(ServerMemoryError):
        mem.update_fact("_meta", "whatever")


def test_empty_key_rejected(tmp_path):
    mem = _memory(tmp_path)
    with pytest.raises(ServerMemoryError):
        mem.update_fact("   ", "x")


def test_append_note_and_read_back(tmp_path):
    mem = _memory(tmp_path)
    entry = mem.append_note("nginx reload ok", source="executor", tags=["nginx"])
    assert entry.source == "executor"
    assert "nginx reload ok" in entry.text
    raw = mem.get_notes()
    assert "[executor]" in raw
    assert "#nginx" in raw


def test_iter_note_entries_parses_multiple_blocks(tmp_path):
    mem = _memory(tmp_path)
    mem.append_note("one line", source="a", ts="2026-04-22T10:00:00Z")
    mem.append_note("two\nline", source="b", ts="2026-04-22T10:05:00Z")
    entries = list(mem.iter_note_entries())
    assert len(entries) == 2
    assert entries[0].source == "a"
    assert entries[0].text == "one line"
    assert entries[1].source == "b"
    assert entries[1].text == "two\nline"


def test_append_note_rejects_empty(tmp_path):
    mem = _memory(tmp_path)
    with pytest.raises(ServerMemoryError):
        mem.append_note("   ")


def test_notes_stats_counts_entries(tmp_path):
    mem = _memory(tmp_path)
    for i in range(3):
        mem.append_note(f"note {i}", ts=f"2026-04-22T10:0{i}:00Z")
    stats = mem.notes_stats()
    assert stats["entries"] == 3
    assert stats["bytes"] > 0


def test_should_compact_threshold(tmp_path):
    mem = _memory(tmp_path)
    for i in range(5):
        mem.append_note(f"n{i}", ts=f"2026-04-22T10:{i:02d}:00Z")
    assert mem.should_compact(threshold_entries=3) is True
    assert mem.should_compact(threshold_entries=100) is False


def test_compact_notes_noop_when_below_threshold(tmp_path):
    mem = _memory(tmp_path)
    mem.append_note("hello", ts="2026-04-22T10:00:00Z")
    result = mem.compact_notes(threshold_entries=10)
    assert result["compacted"] is False


def test_compact_notes_collapses_head_with_naive_summarizer(tmp_path):
    mem = _memory(tmp_path)
    for i in range(10):
        mem.append_note(f"entry {i}", source="auto", ts=f"2026-04-22T10:{i:02d}:00Z")
    result = mem.compact_notes(threshold_entries=5, keep_tail=3)
    assert result["compacted"] is True
    assert result["collapsed"] == 7
    remaining = list(mem.iter_note_entries())
    # один summary-entry + последние 3
    assert len(remaining) == 4
    assert remaining[0].source == "compactor"
    raw = mem.get_notes()
    assert "summary until" in raw
    assert "live entries" in raw


def test_compact_notes_uses_llm_summarizer(tmp_path):
    mem = _memory(tmp_path)
    for i in range(6):
        mem.append_note(f"e{i}", source="auto", ts=f"2026-04-22T10:{i:02d}:00Z")

    def fake_llm(entries):
        return f"LLM-summary of {len(entries)} entries"

    result = mem.compact_notes(threshold_entries=2, keep_tail=2, llm_summarizer=fake_llm)
    assert result["compacted"] is True
    raw = mem.get_notes()
    assert "LLM-summary of 4 entries" in raw


def test_compact_notes_llm_failure_fallback(tmp_path):
    mem = _memory(tmp_path)
    for i in range(5):
        mem.append_note(f"e{i}", ts=f"2026-04-22T10:{i:02d}:00Z")

    def bad_llm(_):
        raise RuntimeError("llm down")

    result = mem.compact_notes(threshold_entries=2, keep_tail=2, llm_summarizer=bad_llm)
    assert result["compacted"] is True
    assert "Схлопнуто" in mem.get_notes()


def test_compact_force_even_below_threshold(tmp_path):
    mem = _memory(tmp_path)
    for i in range(5):
        mem.append_note(f"e{i}", ts=f"2026-04-22T10:{i:02d}:00Z")
    result = mem.compact_notes(threshold_entries=100, keep_tail=1, force=True)
    assert result["compacted"] is True


def test_two_servers_memory_isolated(tmp_path):
    m1 = _memory(tmp_path, "web-01")
    m2 = _memory(tmp_path, "db-02")
    m1.update_fact("pg_version", "16")
    m1.append_note("web note")
    m2.update_fact("pg_version", "17")
    m2.append_note("db note")
    assert m1.get_facts()["pg_version"] == "16"
    assert m2.get_facts()["pg_version"] == "17"
    assert "web note" in m1.get_notes()
    assert "db note" in m2.get_notes()
    assert "db note" not in m1.get_notes()


def test_default_threshold_is_sensible():
    assert DEFAULT_COMPACT_THRESHOLD >= 100
