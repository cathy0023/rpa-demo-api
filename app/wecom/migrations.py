"""企微侧边栏 SQLite 迁移:统一幂等建三表(画像缓存/会话历史/同步状态)。

- wecom_profile_cache:外部联系人画像缓存(T4,TTL 600s 惰性过期)
- wecom_chat_history:会话存档落库,seq UNIQUE 支撑 T8 幂等(同 seq 重复跳过)
- sync_state:每个 corp_id 的同步游标 last_seq,重启续传
全部 IF NOT EXISTS,与 rpa_demo 既有 conversations/messages/customers 表共存无副作用;
预存在的异构 wecom_chat_history(缺 RFC 列)跳过索引创建,不破坏原表。
"""
import sqlite3

_INDEX_COLUMNS = {"corp_id", "external_userid", "msg_ts"}


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def ensure_wecom_tables(conn: sqlite3.Connection) -> None:
    """幂等建表(IF NOT EXISTS,重复执行无副作用)"""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS wecom_profile_cache (
            corp_id         TEXT NOT NULL,
            external_userid TEXT NOT NULL,
            profile_json    TEXT NOT NULL,
            updated_at      INTEGER NOT NULL,
            PRIMARY KEY (corp_id, external_userid)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS wecom_chat_history (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            corp_id         TEXT NOT NULL,
            external_userid TEXT NOT NULL,
            sender_userid   TEXT NOT NULL,
            from_role       TEXT NOT NULL,
            content         TEXT NOT NULL,
            msg_ts          INTEGER NOT NULL,
            seq             INTEGER NOT NULL UNIQUE
        )
        """
    )
    if _INDEX_COLUMNS <= _table_columns(conn, "wecom_chat_history"):
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_wecom_hist_user "
            "ON wecom_chat_history (corp_id, external_userid, msg_ts DESC)"
        )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS sync_state (
            corp_id    TEXT PRIMARY KEY,
            last_seq   INTEGER NOT NULL DEFAULT 0,
            updated_at INTEGER NOT NULL
        )
        """
    )
    conn.commit()
