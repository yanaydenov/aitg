"""SQLite-память: per-chat, global, user profiles, whitelist."""
from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

DB_PATH = Path(__file__).parent / "aitg.db"
_lock = threading.Lock()


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(DB_PATH)
    # chat память
    c.execute(
        """CREATE TABLE IF NOT EXISTS chat_memory (
            chat_id INTEGER NOT NULL,
            key     TEXT NOT NULL,
            value   TEXT NOT NULL,
            ts      INTEGER NOT NULL,
            PRIMARY KEY (chat_id, key)
        )"""
    )
    c.execute("CREATE INDEX IF NOT EXISTS idx_chat_ts ON chat_memory(chat_id, ts DESC)")
    # global память
    c.execute(
        """CREATE TABLE IF NOT EXISTS global_memory (
            key   TEXT NOT NULL PRIMARY KEY,
            value TEXT NOT NULL,
            ts    INTEGER NOT NULL
        )"""
    )
    c.execute("CREATE INDEX IF NOT EXISTS idx_global_ts ON global_memory(ts DESC)")
    # профили пользователей
    c.execute(
        """CREATE TABLE IF NOT EXISTS user_profiles (
            user_id INTEGER NOT NULL,
            key     TEXT NOT NULL,
            value   TEXT NOT NULL,
            ts      INTEGER NOT NULL,
            PRIMARY KEY (user_id, key)
        )"""
    )
    c.execute("CREATE INDEX IF NOT EXISTS idx_user_ts ON user_profiles(user_id, ts DESC)")
    # whitelist
    c.execute(
        """CREATE TABLE IF NOT EXISTS whitelist (
            user_id INTEGER NOT NULL PRIMARY KEY,
            ts      INTEGER NOT NULL
        )"""
    )
    # лог всех диалогов
    c.execute(
        """CREATE TABLE IF NOT EXISTS conversation_log (
            id       INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id  INTEGER NOT NULL,
            role     TEXT NOT NULL,
            content  TEXT NOT NULL,
            ts       INTEGER NOT NULL
        )"""
    )
    c.execute("CREATE INDEX IF NOT EXISTS idx_log_chat_ts ON conversation_log(chat_id, ts DESC)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_log_ts ON conversation_log(ts DESC)")
    # напоминания
    c.execute(
        """CREATE TABLE IF NOT EXISTS reminders (
            id       INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id  INTEGER NOT NULL,
            user_id  INTEGER NOT NULL,
            text     TEXT NOT NULL,
            fire_at  INTEGER NOT NULL,
            done     INTEGER NOT NULL DEFAULT 0
        )"""
    )
    c.execute("CREATE INDEX IF NOT EXISTS idx_reminders_fire ON reminders(fire_at) WHERE done=0")
    # миграция из старой таблицы если существует
    _migrate_old_data(c)
    return c


def _migrate_old_data(c: sqlite3.Connection) -> None:
    """Мигрирует данные из старой таблицы kv в новую схему."""
    try:
        # проверяем существует ли старая таблица
        cursor = c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='kv'")
        if not cursor.fetchone():
            return

        # мигрируем chat память
        c.execute(
            """INSERT OR IGNORE INTO chat_memory(chat_id,key,value,ts)
               SELECT CAST(substr(scope, 6) AS INTEGER), key, value, ts
               FROM kv WHERE scope LIKE 'chat:%'"""
        )
        # мигрируем global память
        c.execute(
            """INSERT OR IGNORE INTO global_memory(key,value,ts)
               SELECT key, value, ts FROM kv WHERE scope='global'"""
        )
        # мигрируем user profiles
        c.execute(
            """INSERT OR IGNORE INTO user_profiles(user_id,key,value,ts)
               SELECT CAST(substr(scope, 6) AS INTEGER), key, value, ts
               FROM kv WHERE scope LIKE 'user:%'"""
        )
        # мигрируем whitelist (старая схема с key='allowed_users')
        c.execute(
            """INSERT OR IGNORE INTO whitelist(user_id,ts)
               SELECT CAST(value AS INTEGER), ts FROM kv WHERE scope='global' AND key='allowed_users'"""
        )
        # удаляем старую таблицу после успешной миграции
        c.execute("DROP TABLE IF EXISTS kv")
        c.commit()
    except Exception as e:
        import logging
        logging.getLogger("aitg.memory").warning("migration failed: %s", e)


def remember(key: str, value: str, *, chat_id: int | None = None, glob: bool = False) -> None:
    with _lock, _conn() as c:
        if glob:
            c.execute(
                "INSERT OR REPLACE INTO global_memory(key,value,ts) VALUES(?,?,?)",
                (key, value, int(time.time())),
            )
        else:
            c.execute(
                "INSERT OR REPLACE INTO chat_memory(chat_id,key,value,ts) VALUES(?,?,?,?)",
                (chat_id, key, value, int(time.time())),
            )


def recall(key: str, *, chat_id: int | None = None, glob: bool = False) -> str | None:
    with _lock, _conn() as c:
        if glob:
            row = c.execute("SELECT value FROM global_memory WHERE key=?", (key,)).fetchone()
        else:
            row = c.execute("SELECT value FROM chat_memory WHERE chat_id=? AND key=?", (chat_id, key)).fetchone()
    return row[0] if row else None


def list_keys(*, chat_id: int | None = None, glob: bool = False) -> list[tuple[str, str]]:
    with _lock, _conn() as c:
        if glob:
            rows = c.execute("SELECT key,value FROM global_memory ORDER BY ts DESC").fetchall()
        else:
            rows = c.execute("SELECT key,value FROM chat_memory WHERE chat_id=? ORDER BY ts DESC", (chat_id,)).fetchall()
    return [(k, v) for k, v in rows]


def forget(key: str, *, chat_id: int | None = None, glob: bool = False) -> bool:
    with _lock, _conn() as c:
        if glob:
            cur = c.execute("DELETE FROM global_memory WHERE key=?", (key,))
        else:
            cur = c.execute("DELETE FROM chat_memory WHERE chat_id=? AND key=?", (chat_id, key))
    return cur.rowcount > 0


def remember_user(user_id: int, key: str, value: str) -> None:
    """Сохраняет информацию о пользователе по его telegram ID."""
    with _lock, _conn() as c:
        c.execute(
            "INSERT OR REPLACE INTO user_profiles(user_id,key,value,ts) VALUES(?,?,?,?)",
            (user_id, key, value, int(time.time())),
        )


def recall_user(user_id: int, key: str) -> str | None:
    """Читает информацию о пользователе по его telegram ID."""
    with _lock, _conn() as c:
        row = c.execute(
            "SELECT value FROM user_profiles WHERE user_id=? AND key=?",
            (user_id, key),
        ).fetchone()
    return row[0] if row else None


def list_user_info(user_id: int) -> list[tuple[str, str]]:
    """Возвращает всю информацию о пользователе."""
    with _lock, _conn() as c:
        rows = c.execute(
            "SELECT key,value FROM user_profiles WHERE user_id=? ORDER BY ts DESC",
            (user_id,),
        ).fetchall()
    return [(k, v) for k, v in rows]


def forget_user(user_id: int, key: str) -> bool:
    """Удаляет конкретную информацию о пользователе."""
    with _lock, _conn() as c:
        cur = c.execute(
            "DELETE FROM user_profiles WHERE user_id=? AND key=?",
            (user_id, key),
        )
    return cur.rowcount > 0


# whitelist пользователей которые могут пользоваться ботом в ЛС
def whitelist_add(user_id: int) -> None:
    """Добавляет пользователя в whitelist."""
    with _lock, _conn() as c:
        c.execute(
            "INSERT OR REPLACE INTO whitelist(user_id,ts) VALUES(?,?)",
            (user_id, int(time.time())),
        )


def whitelist_remove(user_id: int) -> None:
    """Удаляет пользователя из whitelist."""
    with _lock, _conn() as c:
        c.execute("DELETE FROM whitelist WHERE user_id=?", (user_id,))


def whitelist_check(user_id: int) -> bool:
    """Проверяет есть ли пользователь в whitelist."""
    with _lock, _conn() as c:
        row = c.execute("SELECT 1 FROM whitelist WHERE user_id=?", (user_id,)).fetchone()
    return row is not None


def whitelist_list() -> list[int]:
    """Возвращает список всех пользователей в whitelist."""
    with _lock, _conn() as c:
        rows = c.execute("SELECT user_id FROM whitelist ORDER BY ts DESC").fetchall()
    return [r[0] for r in rows]


def add_reminder(chat_id: int, user_id: int, text: str, fire_at: int) -> int:
    """Добавляет напоминание. fire_at — unix timestamp UTC."""
    with _lock, _conn() as c:
        cur = c.execute(
            "INSERT INTO reminders(chat_id,user_id,text,fire_at,done) VALUES(?,?,?,?,0)",
            (chat_id, user_id, text, fire_at),
        )
        return cur.lastrowid


def get_due_reminders(now: int) -> list[dict]:
    """Возвращает напоминания которые пора отправить."""
    with _lock, _conn() as c:
        rows = c.execute(
            "SELECT id,chat_id,user_id,text,fire_at FROM reminders WHERE done=0 AND fire_at<=? ORDER BY fire_at",
            (now,),
        ).fetchall()
    return [{"id": r[0], "chat_id": r[1], "user_id": r[2], "text": r[3], "fire_at": r[4]} for r in rows]


def mark_reminder_done(reminder_id: int) -> None:
    with _lock, _conn() as c:
        c.execute("UPDATE reminders SET done=1 WHERE id=?", (reminder_id,))


def list_reminders(chat_id: int) -> list[dict]:
    with _lock, _conn() as c:
        rows = c.execute(
            "SELECT id,text,fire_at FROM reminders WHERE chat_id=? AND done=0 ORDER BY fire_at",
            (chat_id,),
        ).fetchall()
    return [{"id": r[0], "text": r[1], "fire_at": r[2]} for r in rows]


def cancel_reminder(reminder_id: int, chat_id: int) -> bool:
    with _lock, _conn() as c:
        cur = c.execute("UPDATE reminders SET done=1 WHERE id=? AND chat_id=?", (reminder_id, chat_id))
    return cur.rowcount > 0


def log_message(chat_id: int, role: str, content: str) -> None:
    """Записывает сообщение в постоянный лог диалогов."""
    with _lock, _conn() as c:
        c.execute(
            "INSERT INTO conversation_log(chat_id,role,content,ts) VALUES(?,?,?,?)",
            (chat_id, role, content, int(time.time())),
        )


def search_log(query: str, chat_id: int | None = None, limit: int = 20) -> list[dict]:
    """Ищет по логу диалогов. Возвращает [{chat_id,role,content,ts}]."""
    q = f"%{query}%"
    with _lock, _conn() as c:
        if chat_id is not None:
            rows = c.execute(
                "SELECT chat_id,role,content,ts FROM conversation_log "
                "WHERE chat_id=? AND content LIKE ? ORDER BY ts DESC LIMIT ?",
                (chat_id, q, limit),
            ).fetchall()
        else:
            rows = c.execute(
                "SELECT chat_id,role,content,ts FROM conversation_log "
                "WHERE content LIKE ? ORDER BY ts DESC LIMIT ?",
                (q, limit),
            ).fetchall()
    return [{"chat_id": r[0], "role": r[1], "content": r[2], "ts": r[3]} for r in rows]
