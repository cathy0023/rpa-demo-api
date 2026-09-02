"""企微侧边栏 SQLite 迁移单测:三表 DDL、关键约束、幂等、与 rpa_demo 旧库共存。

覆盖:
1. ensure_wecom_tables 后 wecom_chat_history / sync_state / wecom_profile_cache 三表存在且字段齐全
2. wecom_chat_history 关键约束:id 主键、sender_userid NOT NULL、seq UNIQUE(重复 seq 插入 IntegrityError)
3. sync_state:corp_id 主键、last_seq NOT NULL 默认 0
4. idx_wecom_hist_user 索引存在且列序为 (corp_id, external_userid, msg_ts)
5. 重复执行幂等:二次迁移不报错、已入库数据保留
6. 旧库模拟:先建 rpa_demo 的 conversations/messages/customers 再迁移,新旧表共存且旧数据不破坏
"""
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import pytest

from app.wecom.migrations import ensure_wecom_tables


@pytest.fixture()
def conn(tmp_path):
    """每个测试独立 SQLite 文件,迁移函数直接接收连接(生产路径由 contact/sync 惰性触发)"""
    c = sqlite3.connect(str(tmp_path / "test_migrations.db"))
    c.row_factory = sqlite3.Row
    yield c
    c.close()


def _columns(db: sqlite3.Connection, table: str) -> dict:
    """PRAGMA table_info → {列名: 行 dict(cid/name/type/notnull/dflt_value/pk)}"""
    return {r["name"]: dict(r) for r in db.execute(f"PRAGMA table_info({table})").fetchall()}


def _insert_hist(conn: sqlite3.Connection, seq: int) -> None:
    conn.execute(
        "INSERT INTO wecom_chat_history "
        "(corp_id, external_userid, sender_userid, from_role, content, msg_ts, seq) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("corp_a", "wo_ext1", "wo_sender1", "external", "您好,想了解下产品", 1700000000, seq),
    )


def test_chat_history_table_columns(conn):
    ensure_wecom_tables(conn)
    cols = _columns(conn, "wecom_chat_history")
    assert list(cols) == [
        "id", "corp_id", "external_userid", "sender_userid",
        "from_role", "content", "msg_ts", "seq",
    ]
    assert cols["id"]["pk"] == 1
    assert cols["corp_id"]["notnull"] == 1
    assert cols["external_userid"]["notnull"] == 1
    assert cols["sender_userid"]["notnull"] == 1
    assert cols["from_role"]["notnull"] == 1
    assert cols["content"]["notnull"] == 1
    assert cols["msg_ts"]["notnull"] == 1
    assert cols["seq"]["notnull"] == 1


def test_chat_history_seq_unique_enforced(conn):
    ensure_wecom_tables(conn)
    _insert_hist(conn, seq=100)
    conn.commit()
    with pytest.raises(sqlite3.IntegrityError):
        _insert_hist(conn, seq=100)  # 同 seq 重复入库必须被 UNIQUE 约束拒绝(幂等落库前提)


def test_sync_state_pk_and_default(conn):
    ensure_wecom_tables(conn)
    cols = _columns(conn, "sync_state")
    assert set(cols) == {"corp_id", "last_seq", "updated_at"}
    assert cols["corp_id"]["pk"] == 1
    assert cols["last_seq"]["notnull"] == 1
    assert cols["last_seq"]["dflt_value"] == "0"
    assert cols["updated_at"]["notnull"] == 1
    conn.execute("INSERT INTO sync_state (corp_id, updated_at) VALUES (?, ?)", ("corp_a", 1700000000))
    row = conn.execute("SELECT last_seq FROM sync_state WHERE corp_id=?", ("corp_a",)).fetchone()
    assert row["last_seq"] == 0  # 省略 last_seq 时默认 0


def test_profile_cache_table_intact(conn):
    ensure_wecom_tables(conn)
    cols = _columns(conn, "wecom_profile_cache")
    assert set(cols) == {"corp_id", "external_userid", "profile_json", "updated_at"}
    assert cols["corp_id"]["pk"] == 1
    assert cols["external_userid"]["pk"] == 2
    assert cols["profile_json"]["notnull"] == 1
    assert cols["updated_at"]["notnull"] == 1


def test_hist_user_index_exists(conn):
    ensure_wecom_tables(conn)
    indexes = {r["name"] for r in conn.execute("PRAGMA index_list(wecom_chat_history)").fetchall()}
    assert "idx_wecom_hist_user" in indexes
    idx_cols = [r["name"] for r in conn.execute("PRAGMA index_info(idx_wecom_hist_user)").fetchall()]
    assert idx_cols == ["corp_id", "external_userid", "msg_ts"]


def test_repeated_execution_idempotent(conn):
    ensure_wecom_tables(conn)
    _insert_hist(conn, seq=1)
    conn.commit()
    ensure_wecom_tables(conn)  # 二次执行:不报错、已入库数据保留
    n = conn.execute("SELECT COUNT(*) AS n FROM wecom_chat_history").fetchone()["n"]
    assert n == 1
    ensure_wecom_tables(conn)  # 三次执行同样安全
    n = conn.execute("SELECT COUNT(*) AS n FROM sync_state").fetchone()["n"]
    assert n == 0


_LEGACY_SCHEMA = """
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
CREATE TABLE IF NOT EXISTS customers (
    team_id     TEXT NOT NULL,
    customer_id TEXT NOT NULL,
    vid         TEXT NOT NULL,
    created_at  INTEGER NOT NULL,
    PRIMARY KEY (team_id, customer_id)
);
"""


def test_legacy_rpa_tables_coexist(conn):
    """模拟 rpa_demo 旧库:先有 conversations/messages/customers,迁移后共存且旧数据不破坏"""
    conn.executescript(_LEGACY_SCHEMA)
    conn.execute(
        "INSERT INTO messages (team_id, conversation_id, role, content, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        ("team1", "conv1", "user", "旧消息一条", 1700000000),
    )
    conn.execute(
        "INSERT INTO conversations (team_id, conversation_id, customer_id, vid, last_message, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("team1", "conv1", "cust1", "vid1", "旧消息一条", 1700000000, 1700000000),
    )
    conn.commit()

    ensure_wecom_tables(conn)

    # 旧表结构与数据完好
    row = conn.execute(
        "SELECT content FROM messages WHERE team_id=? AND conversation_id=?",
        ("team1", "conv1"),
    ).fetchone()
    assert row["content"] == "旧消息一条"
    row = conn.execute(
        "SELECT last_message FROM conversations WHERE team_id=? AND conversation_id=?",
        ("team1", "conv1"),
    ).fetchone()
    assert row["last_message"] == "旧消息一条"
    # 三张 wecom 新表就绪,与旧表共存
    for table in ("wecom_chat_history", "sync_state", "wecom_profile_cache"):
        assert _columns(conn, table), f"{table} 未创建"
