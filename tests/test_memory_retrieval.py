import json
import sqlite3

from modes.sdk.runtime.memory_retrieval import format_retrieved_context, retrieve_relevant_context


def test_memory_retrieval_finds_relevant_entries(tmp_path):
    cwd = str(tmp_path)
    (tmp_path / "MEMORY.md").write_text(
        "\n".join(
            [
                "- 2026-02-01 10:00: [CONFIG] Используем sqlite fts5 для поиска по памяти",
                "- 2026-02-02 09:30: [DECISION] Не используем векторную базу данных",
                "- 2026-02-03 08:15: случайная заметка без тега",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    session_payload = {
        "orchestrator_by_task": {
            "task-1": [
                {
                    "date": "2026-02-04",
                    "user": "Нужно ускорить поиск по памяти",
                    "final": "Добавили sqlite fts5 индекс и top-k retrieval",
                    "step_results": [
                        {"title": "Анализ", "summary": "Проверили MEMORY.md"},
                        {"title": "Решение", "summary": "Внедрили FTS"},
                    ],
                }
            ]
        }
    }
    (tmp_path / "SESSION.json").write_text(json.dumps(session_payload, ensure_ascii=False), encoding="utf-8")

    items = retrieve_relevant_context(cwd, "как работает fts5 поиск по памяти", limit=5)
    assert items
    text_blob = "\n".join(str(i.get("text") or "") for i in items).lower()
    assert "fts5" in text_blob

    rendered = format_retrieved_context(items, max_chars=500)
    assert rendered
    assert "fts5" in rendered.lower()


def test_memory_retrieval_sync_is_incremental(tmp_path):
    cwd = str(tmp_path)
    mem = tmp_path / "MEMORY.md"
    mem.write_text(
        "\n".join(
            [
                "- 2026-02-01 10:00: [CONFIG] sqlite fts5",
                "- 2026-02-02 09:30: [DECISION] no vector db",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    (tmp_path / "SESSION.json").write_text(json.dumps({"orchestrator_by_task": {}}, ensure_ascii=False), encoding="utf-8")

    first = retrieve_relevant_context(cwd, "sqlite", limit=5)
    assert first
    db_path = tmp_path / "MEMORY_FTS5.db"
    assert db_path.exists()
    with sqlite3.connect(str(db_path)) as conn:
        rows_before = conn.execute("SELECT COUNT(*) FROM memory_fts").fetchone()[0]

    second = retrieve_relevant_context(cwd, "sqlite", limit=5)
    assert second
    with sqlite3.connect(str(db_path)) as conn:
        rows_after_same = conn.execute("SELECT COUNT(*) FROM memory_fts").fetchone()[0]
    assert rows_after_same == rows_before

    mem.write_text(
        mem.read_text(encoding="utf-8") + "- 2026-02-03 08:15: [AGREEMENT] keep sqlite\n",
        encoding="utf-8",
    )
    third = retrieve_relevant_context(cwd, "keep sqlite", limit=5)
    assert third
    with sqlite3.connect(str(db_path)) as conn:
        rows_after_change = conn.execute("SELECT COUNT(*) FROM memory_fts").fetchone()[0]
    assert rows_after_change == rows_before + 1


def test_format_retrieved_context_preserves_head_and_tail_with_explicit_degraded_marker():
    items = [
        {
            "source": "session:1",
            "ts": "2026-02-01",
            "text": "HEAD-" + ("y" * 300) + "-TAIL",
        }
    ]
    rendered = format_retrieved_context(items, max_chars=90)
    assert "[degraded_retrieved_context_trimmed" in rendered
    assert "- [session:1]" in rendered
    assert "-TAIL" in rendered


def test_format_retrieved_context_keeps_degraded_marker_even_for_tiny_budget():
    items = [
        {
            "source": "session:1",
            "ts": "2026-02-01",
            "text": "HEAD-" + ("y" * 300) + "-TAIL",
        }
    ]
    rendered = format_retrieved_context(items, max_chars=32)
    assert rendered
    assert "degraded" in rendered.lower()
