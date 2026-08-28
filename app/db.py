"""SQLite 落库:会话/消息/客户,按 team_id 隔离。WAL + 线程锁串行写。"""
import sqlite3
import threading
import time
from pathlib import Path

from .config import config

_mutex = threading.Lock()
_conn: sqlite3.Connection | None = None


def _get_conn() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        path = Path(config.sqlite_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        _conn = sqlite3.connect(str(path), check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA journal_mode=WAL")
        _init_schema(_conn)
    return _conn


def _init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS conversations (
            team_id         TEXT NOT NULL,
            conversation_id TEXT NOT NULL,
            customer_id     TEXT NOT NULL,
            vid             TEXT NOT NULL,
            last_message    TEXT DEFAULT '',
            created_at      INTEGER NOT NULL,
            updated_at      INTEGER NOT NULL,
            PRIMARY KEY (team_id, conversation_id)
        );
        CREATE TABLE IF NOT EXISTS messages (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            team_id         TEXT NOT NULL,
            conversation_id TEXT NOT NULL,
            role            TEXT NOT NULL,
            content         TEXT NOT NULL,
            created_at      INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_messages_team_conv ON messages(team_id, conversation_id, created_at);
        CREATE TABLE IF NOT EXISTS customers (
            team_id     TEXT NOT NULL,
            customer_id TEXT NOT NULL,
            vid         TEXT NOT NULL,
            created_at  INTEGER NOT NULL,
            PRIMARY KEY (team_id, customer_id)
        );
        """
    )
    conn.commit()


def now() -> int:
    return int(time.time())


def upsert_conversation(team_id: str, conversation_id: str, customer_id: str, vid: str, last_message: str) -> None:
    with _mutex:
        conn = _get_conn()
        conn.execute(
            "INSERT INTO conversations (team_id, conversation_id, customer_id, vid, last_message, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(team_id, conversation_id) DO UPDATE SET "
            "last_message=excluded.last_message, updated_at=excluded.updated_at",
            (team_id, conversation_id, customer_id, vid, last_message, now(), now()),
        )
        conn.commit()


def insert_message(team_id: str, conversation_id: str, role: str, content: str, created_at: int | None = None) -> None:
    with _mutex:
        conn = _get_conn()
        conn.execute(
            "INSERT INTO messages (team_id, conversation_id, role, content, created_at) VALUES (?, ?, ?, ?, ?)",
            (team_id, conversation_id, role, content, created_at or now()),
        )
        conn.commit()


def upsert_customer(team_id: str, customer_id: str, vid: str) -> None:
    with _mutex:
        conn = _get_conn()
        conn.execute(
            "INSERT INTO customers (team_id, customer_id, vid, created_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(team_id, customer_id) DO UPDATE SET vid=excluded.vid",
            (team_id, customer_id, vid, now()),
        )
        conn.commit()


def find_conversation_by_customer(team_id: str, customer_id: str) -> str | None:
    """按客户查该团队下已有会话 ID;无则 None(会话聚合:同一客户复用会话)"""
    row = _get_conn().execute(
        "SELECT conversation_id FROM conversations WHERE team_id=? AND customer_id=? "
        "ORDER BY updated_at DESC LIMIT 1",
        (team_id, customer_id),
    ).fetchone()
    return row["conversation_id"] if row else None


def list_conversations(team_id: str, limit: int = 50) -> list[dict]:
    rows = _get_conn().execute(
        "SELECT conversation_id, customer_id, vid, last_message, created_at, updated_at "
        "FROM conversations WHERE team_id=? ORDER BY updated_at DESC LIMIT ?",
        (team_id, limit),
    ).fetchall()
    return [dict(r) for r in rows]


def list_messages(team_id: str, conversation_id: str, limit: int = 100) -> list[dict]:
    rows = _get_conn().execute(
        "SELECT role, content, created_at FROM messages "
        "WHERE team_id=? AND conversation_id=? ORDER BY created_at ASC, id ASC LIMIT ?",
        (team_id, conversation_id, limit),
    ).fetchall()
    return [dict(r) for r in rows]


def list_customers(team_id: str, limit: int = 50) -> list[dict]:
    rows = _get_conn().execute(
        "SELECT customer_id, vid, created_at FROM customers "
        "WHERE team_id=? ORDER BY created_at DESC LIMIT ?",
        (team_id, limit),
    ).fetchall()
    return [dict(r) for r in rows]
