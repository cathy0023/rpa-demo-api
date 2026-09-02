"""统一写锁单测:db.write_lock 全局互斥 + 跨模块并发写 smoke(不炸库)

缺陷背景:wecom 各模块各自持有 threading.Lock(contact._mutex / sync._MUTEX /
context._MUTEX),同一 db.py 连接可能被多把锁同时放行的线程并发写。
修复后所有写操作统一走 db.write_lock() 返回的全局锁。
"""
import sqlite3
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app import db
from app.wecom import contact, context, msgaudit, sync


def test_write_lock_returns_same_global_lock():
    """所有模块拿到的必须是同一把锁(全局互斥的前提)"""
    assert db.write_lock() is db._mutex
    assert db._mutex is db.write_lock()


def test_write_lock_is_acquired_by_db_writers():
    """db.py 既有写函数持同一把锁(行为不变):持锁期间他人不可获锁"""
    acquired = []

    def probe():
        acquired.append(db._mutex.acquire(blocking=False))
        # probe 未获锁(acquire 返回 False),无需 release

    with db.write_lock():
        t = threading.Thread(target=probe)
        t.start()
        t.join()
    assert acquired == [False]  # 写锁被持有,探测线程拿不到


def test_concurrent_writes_different_tables_smoke(tmp_path):
    """两线程同时经统一写锁写不同表(sync 落库 + profile 缓存)→ 都成功不炸"""
    conn = sqlite3.connect(str(tmp_path / "conc.db"), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    from app.wecom.migrations import ensure_wecom_tables

    ensure_wecom_tables(conn)
    saved_conn = (msgaudit._conn, contact._conn, context._conn)
    msgaudit._conn = conn
    contact._conn = conn
    context._conn = conn
    errors: list[Exception] = []

    def writer_history():
        try:
            for i in range(50):
                with db.write_lock():
                    conn.execute(
                        "INSERT OR IGNORE INTO wecom_chat_history "
                        "(corp_id, external_userid, sender_userid, from_role, content, msg_ts, seq) "
                        "VALUES ('c', 'cust', 'staff', 'staff', ?, ?, ?)",
                        (f"m{i}", 1700000000 + i, i + 1),
                    )
                    conn.commit()
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    def writer_profile():
        try:
            for i in range(50):
                with db.write_lock():
                    conn.execute(
                        "INSERT INTO wecom_profile_cache (corp_id, external_userid, profile_json, updated_at) "
                        "VALUES ('c', 'cust2', ?, ?) "
                        "ON CONFLICT(corp_id, external_userid) DO UPDATE SET profile_json=excluded.profile_json",
                        (f'{{"n":{i}}}', 1700000000 + i),
                    )
                    conn.commit()
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    t1, t2 = threading.Thread(target=writer_history), threading.Thread(target=writer_profile)
    try:
        t1.start(); t2.start()
        t1.join(); t2.join()
        assert errors == []
        hist = conn.execute("SELECT COUNT(*) AS n FROM wecom_chat_history").fetchone()["n"]
        prof = conn.execute(
            "SELECT profile_json FROM wecom_profile_cache WHERE corp_id='c' AND external_userid='cust2'"
        ).fetchone()
        assert hist == 50
        assert prof["profile_json"] == '{"n":49}'  # 最后一次写入胜出
    finally:
        msgaudit._conn, contact._conn, context._conn = saved_conn
        conn.close()
