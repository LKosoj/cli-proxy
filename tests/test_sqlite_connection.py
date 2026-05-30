from __future__ import annotations

import sqlite3
import threading

import pytest

from app.services.sqlite_connection import open_sqlite, sqlite_session


def test_journal_mode_wal(tmp_path):
    """journal_mode должен быть WAL после открытия соединения."""
    db = str(tmp_path / "test.db")
    with open_sqlite(db) as conn:
        row = conn.execute("PRAGMA journal_mode").fetchone()
        assert str(row[0]).lower() == "wal"


def test_foreign_keys_on(tmp_path):
    """foreign_keys должен быть включён (=1)."""
    db = str(tmp_path / "test.db")
    with open_sqlite(db) as conn:
        row = conn.execute("PRAGMA foreign_keys").fetchone()
        assert int(row[0]) == 1


def test_row_factory_column_name_access(tmp_path):
    """Доступ к колонкам по имени через sqlite3.Row."""
    db = str(tmp_path / "test.db")
    with open_sqlite(db) as conn:
        conn.execute("CREATE TABLE t (id INTEGER, val TEXT)")
        conn.execute("INSERT INTO t VALUES (42, 'hello')")
        row = conn.execute("SELECT id, val FROM t").fetchone()
        assert row["id"] == 42
        assert row["val"] == "hello"


def test_idempotent_reopen(tmp_path):
    """Повторное открытие той же БД не ломает WAL и foreign_keys."""
    db = str(tmp_path / "test.db")
    # первое открытие — создаём схему
    with open_sqlite(db) as conn:
        conn.execute("CREATE TABLE kv (k TEXT PRIMARY KEY, v TEXT)")
        conn.execute("INSERT INTO kv VALUES ('a', '1')")

    # второе открытие — читаем данные, проверяем PRAGMA
    with open_sqlite(db) as conn:
        row = conn.execute("PRAGMA journal_mode").fetchone()
        assert str(row[0]).lower() == "wal"
        fk = conn.execute("PRAGMA foreign_keys").fetchone()
        assert int(fk[0]) == 1
        kv = conn.execute("SELECT v FROM kv WHERE k='a'").fetchone()
        assert kv["v"] == "1"


def test_concurrent_writer_reader_no_lock(tmp_path):
    """WAL позволяет параллельному reader читать пока writer пишет без «database is locked»."""
    db = str(tmp_path / "concurrent.db")

    # создаём схему заранее
    with open_sqlite(db) as conn:
        conn.execute("CREATE TABLE nums (n INTEGER)")

    errors: list[Exception] = []
    results: list[int] = []

    def writer() -> None:
        try:
            with open_sqlite(db) as conn:
                for i in range(50):
                    conn.execute("INSERT INTO nums VALUES (?)", (i,))
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    def reader() -> None:
        try:
            with open_sqlite(db) as conn:
                row = conn.execute("SELECT COUNT(*) FROM nums").fetchone()
                results.append(int(row[0]))
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    t_write = threading.Thread(target=writer)
    t_read = threading.Thread(target=reader)
    t_write.start()
    t_read.start()
    t_write.join(timeout=10)
    t_read.join(timeout=10)

    assert not errors, f"ошибки в потоках: {errors}"
    # reader мог прочитать любое состояние — главное, что ошибок нет
    assert len(results) == 1


def test_sqlite_session_closes_connection(tmp_path):
    """sqlite_session должен закрывать соединение после выхода из контекста."""
    db = str(tmp_path / "session.db")
    with sqlite_session(db) as conn:
        conn.execute("CREATE TABLE t (id INTEGER)")
        conn.execute("INSERT INTO t VALUES (1)")
    # После выхода соединение закрыто — операции должны падать.
    with pytest.raises(sqlite3.ProgrammingError):
        conn.execute("SELECT 1")


def test_sqlite_session_commits_on_success(tmp_path):
    """sqlite_session коммитит транзакцию при успешном выходе (данные видны в новом соединении)."""
    db = str(tmp_path / "session_commit.db")
    with sqlite_session(db) as conn:
        conn.execute("CREATE TABLE kv (k TEXT, v TEXT)")
        conn.execute("INSERT INTO kv VALUES ('a', '1')")
    with sqlite_session(db) as conn:
        row = conn.execute("SELECT v FROM kv WHERE k='a'").fetchone()
        assert row["v"] == "1"


def test_sqlite_session_rolls_back_and_closes_on_error(tmp_path):
    """При исключении внутри контекста транзакция откатывается, соединение закрывается."""
    db = str(tmp_path / "session_rollback.db")
    with sqlite_session(db) as conn:
        conn.execute("CREATE TABLE kv (k TEXT, v TEXT)")
    # Вставка + исключение внутри одного контекста → откат вставки.
    with pytest.raises(RuntimeError):
        with sqlite_session(db) as conn:
            conn.execute("INSERT INTO kv VALUES ('a', '1')")
            raise RuntimeError("boom")
    with sqlite_session(db) as conn:
        count = conn.execute("SELECT COUNT(*) FROM kv").fetchone()[0]
        assert int(count) == 0
