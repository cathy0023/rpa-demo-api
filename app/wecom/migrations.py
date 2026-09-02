"""企微侧边栏 SQLite 迁移:T4 先建画像缓存表;T6 统一补齐会话历史/同步状态两张表。"""
import sqlite3


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
    conn.commit()
