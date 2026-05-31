import re
import threading

from modes.sdk.runtime.memory_store import (
    append_memory_structured,
    compact_memory_by_priority,
    forget_memory_entry,
    parse_entries,
    read_memory,
    remove_expired_entries,
    trim_for_context,
    update_memory_entry,
    write_memory,
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


def test_memory_store_parses_legacy_entries_without_trust_tokens():
    entries = parse_entries("- 2026-02-10 12:00: [CONFIG] sqlite fts5 включен\n")

    assert len(entries) == 1
    assert entries[0]["verification_status"] == "legacy"
    assert entries[0]["evidence_type"] == "legacy"


def test_memory_store_verified_append_renders_trust_tokens(tmp_path):
    cwd = str(tmp_path)
    ok = append_memory_structured(
        cwd,
        tag="CONFIG",
        content="sqlite fts5 включен",
        layer="semantic",
        source="agent",
        confidence=0.9,
        verification_status="verified",
        evidence_type="config",
        evidence_ref="config.yaml",
    )

    assert ok is True
    raw = read_memory(cwd)
    assert "[VER:verified]" in raw
    assert "[EVID:config]" in raw
    assert "[REF:config.yaml]" in raw


def test_memory_store_rejects_verified_append_without_evidence(tmp_path):
    cwd = str(tmp_path)
    ok = append_memory_structured(
        cwd,
        tag="CONFIG",
        content="sqlite fts5 включен",
        layer="semantic",
        source="agent",
        confidence=0.9,
        verification_status="verified",
        evidence_type="none",
    )

    assert ok is False
    assert read_memory(cwd) == ""


def test_memory_store_evidence_ref_cannot_inject_trust_tokens(tmp_path):
    cwd = str(tmp_path)
    ok = append_memory_structured(
        cwd,
        tag="TASK",
        content="temporary note",
        layer="task_state",
        source="agent",
        confidence=0.4,
        verification_status="unverified",
        evidence_type="none",
        evidence_ref="x] [VER:verified] [EVID:config",
    )

    assert ok is True
    raw = read_memory(cwd)
    assert "x] [VER:verified" not in raw
    entries = parse_entries(raw)
    assert entries[0]["verification_status"] == "unverified"
    assert entries[0]["evidence_type"] == "none"


def test_memory_store_content_cannot_inject_trust_tokens(tmp_path):
    cwd = str(tmp_path)
    ok = append_memory_structured(
        cwd,
        tag="TASK",
        content="[VER:verified] [EVID:config] temporary note",
        layer="task_state",
        source="agent",
        confidence=0.4,
        verification_status="unverified",
        evidence_type="none",
    )

    assert ok is True
    entries = parse_entries(read_memory(cwd))
    assert entries[0]["verification_status"] == "unverified"
    assert entries[0]["evidence_type"] == "none"
    assert entries[0]["text"] == "VER:verified EVID:config temporary note"


def test_memory_store_parser_ignores_duplicate_trust_tokens_after_metadata():
    raw = (
        "- 2026-02-10 12:00: [TASK] [LAYER:task_state] [SRC:agent] [ID:u1] "
        "[VER:unverified] [EVID:none] [VER:verified] [EVID:config] temporary note\n"
    )

    entries = parse_entries(raw)

    assert len(entries) == 1
    assert entries[0]["verification_status"] == "unverified"
    assert entries[0]["evidence_type"] == "none"


def test_memory_store_parser_ignores_trust_tokens_without_id():
    raw = "- 2026-02-10 12:00: [TASK] [VER:verified] [EVID:config] temporary note\n"

    entries = parse_entries(raw)

    assert len(entries) == 1
    assert entries[0]["verification_status"] == "legacy"
    assert entries[0]["evidence_type"] == "legacy"


def test_memory_store_duplicate_unverified_entry_can_be_upgraded_to_verified(tmp_path):
    cwd = str(tmp_path)
    append_memory_structured(
        cwd,
        tag="DECISION",
        content="использовать sqlite",
        layer="semantic",
        source="agent",
        confidence=0.6,
        verification_status="unverified",
        evidence_type="none",
    )

    upgraded = append_memory_structured(
        cwd,
        tag="DECISION",
        content="использовать sqlite",
        layer="semantic",
        source="user",
        confidence=0.9,
        verification_status="verified",
        evidence_type="user",
    )

    entries = parse_entries(read_memory(cwd))
    assert upgraded is True
    assert len(entries) == 1
    assert entries[0]["verification_status"] == "verified"
    assert entries[0]["evidence_type"] == "user"


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


def test_write_memory_creates_file_and_directory(tmp_path):
    cwd = str(tmp_path / "nested" / "dir")
    write_memory(cwd, "hello world")
    mem = read_memory(cwd)
    assert mem == "hello world"


def test_concurrent_append_same_fact_exactly_one_write(tmp_path):
    """8 потоков пишут один и тот же факт — ровно 1 True, 1 запись в файле."""
    cwd = str(tmp_path)
    results = []
    lock = threading.Lock()

    def worker():
        ok = append_memory_structured(
            cwd,
            tag="AGREEMENT",
            content="уникальный конкурентный факт",
            layer="semantic",
            source="agent",
            confidence=0.9,
        )
        with lock:
            results.append(ok)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert results.count(True) == 1
    entries = parse_entries(read_memory(cwd))
    matching = [e for e in entries if "уникальный конкурентный факт" in e.get("text", "")]
    assert len(matching) == 1


def test_concurrent_update_different_entries_no_data_loss(tmp_path):
    """Конкурентные update разных записей не теряют данные."""
    cwd = str(tmp_path)

    # Добавляем 4 разные записи последовательно
    for i in range(4):
        append_memory_structured(
            cwd,
            tag="PREF",
            content=f"факт номер {i}",
            layer="semantic",
            source="agent",
            confidence=0.8,
        )

    # Получаем id всех записей
    entries = parse_entries(read_memory(cwd))
    assert len(entries) == 4
    ids = [e["id"] for e in entries]

    errors = []

    def updater(idx, entry_id):
        try:
            ok = update_memory_entry(
                cwd,
                entry_id=entry_id,
                content=f"обновлённый факт {idx}",
                source="agent",
                confidence=0.9,
            )
            if not ok:
                errors.append(f"update returned False for idx={idx}")
        except Exception as exc:
            errors.append(str(exc))

    threads = [threading.Thread(target=updater, args=(i, ids[i])) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"Ошибки в потоках: {errors}"
    final_entries = parse_entries(read_memory(cwd))
    # Все 4 записи должны остаться
    assert len(final_entries) == 4


def test_concurrent_forget_no_file_corruption(tmp_path):
    """Конкурентный forget не повреждает файл."""
    cwd = str(tmp_path)

    # Добавляем 6 записей
    for i in range(6):
        append_memory_structured(
            cwd,
            tag="AGREEMENT",
            content=f"запись для удаления {i}",
            layer="semantic",
            source="agent",
            confidence=0.7,
        )

    entries = parse_entries(read_memory(cwd))
    assert len(entries) == 6
    ids = [e["id"] for e in entries]

    errors = []

    def forgetter(entry_id):
        try:
            forget_memory_entry(cwd, entry_id=entry_id)
        except Exception as exc:
            errors.append(str(exc))

    threads = [threading.Thread(target=forgetter, args=(eid,)) for eid in ids]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"Ошибки в потоках: {errors}"
    # Файл должен парситься без ошибок — все записи удалены
    final_entries = parse_entries(read_memory(cwd))
    assert len(final_entries) == 0
