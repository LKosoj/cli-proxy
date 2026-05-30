from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from typing import Iterator


def open_sqlite(db_path: str) -> sqlite3.Connection:
    """Открывает SQLite-соединение с единой политикой PRAGMA для всех репозиториев.

    Политика совпадает с настройками SQLAlchemy-движка в JsonStateRepository._on_connect:
      - journal_mode=WAL  — параллельные reader/writer без блокировок
      - synchronous=NORMAL — баланс надёжности и скорости
      - foreign_keys=ON   — обязательная целостность ссылок
      - busy_timeout=5000 — ожидание 5 с при занятой БД вместо немедленной ошибки

    Все raw-sqlite3 репозитории, работающие с той же БД что и JsonStateRepository,
    обязаны открывать соединения через эту функцию.
    """
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


@contextmanager
def sqlite_session(db_path: str) -> Iterator[sqlite3.Connection]:
    """Контекст-менеджер: открыть соединение, обернуть тело в транзакцию и гарантированно закрыть.

    `sqlite3.Connection.__exit__` управляет только транзакцией (commit/rollback), но НЕ закрывает
    соединение. Без явного close() соединение живёт до сборки мусора. Этот менеджер коммитит/откатывает
    транзакцию (`with conn`) и закрывает соединение в `finally`.
    """
    conn = open_sqlite(db_path)
    try:
        with conn:
            yield conn
    finally:
        conn.close()
