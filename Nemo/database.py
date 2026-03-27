"""
database.py — хранилище тезисов диалога на базе SQLite.

Схема:
  Для каждого пользователя создаётся отдельная таблица user_<user_id>.
  Каждая строка — один тезис, сохранённый после ответа бота.

  CREATE TABLE user_<id> (
      id        INTEGER PRIMARY KEY AUTOINCREMENT,
      thesis    TEXT    NOT NULL,
      created_at DATETIME DEFAULT CURRENT_TIMESTAMP
  )
"""

import logging
import os
import sqlite3
from pathlib import Path

# В Docker путь задаётся переменной DB_PATH (монтируется volume).
# Локально по умолчанию — рядом со скриптом.
DB_PATH = Path(os.getenv("DB_PATH", str(Path(__file__).parent / "nemo.db")))
logger = logging.getLogger(__name__)


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def _table(user_id: int) -> str:
    return f"user_{user_id}"


def init_user_table(user_id: int) -> None:
    """Создаёт таблицу для пользователя, если она ещё не существует."""
    table = _table(user_id)
    with _connect() as conn:
        conn.execute(f"""
            CREATE TABLE IF NOT EXISTS {table} (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                thesis     TEXT    NOT NULL,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
    logger.debug("БД | таблица %s готова", table)


def save_theses(user_id: int, theses: list[str]) -> None:
    """Сохраняет список тезисов в таблицу пользователя."""
    if not theses:
        return
    table = _table(user_id)
    with _connect() as conn:
        conn.executemany(
            f"INSERT INTO {table} (thesis) VALUES (?)",
            [(t,) for t in theses],
        )
    logger.info("БД | сохранено %d тезисов для user_%s", len(theses), user_id)


def get_all_theses(user_id: int) -> list[str]:
    """Возвращает все тезисы пользователя в хронологическом порядке."""
    table = _table(user_id)
    with _connect() as conn:
        rows = conn.execute(
            f"SELECT thesis FROM {table} ORDER BY id ASC"
        ).fetchall()
    theses = [row["thesis"] for row in rows]
    logger.debug("БД | загружено %d тезисов для user_%s", len(theses), user_id)
    return theses


def clear_theses(user_id: int) -> None:
    """Удаляет все тезисы пользователя (вызывается по команде /clear)."""
    table = _table(user_id)
    with _connect() as conn:
        conn.execute(f"DELETE FROM {table}")
    logger.info("БД | тезисы user_%s очищены", user_id)
