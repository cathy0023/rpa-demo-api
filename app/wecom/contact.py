"""企微外部联系人画像代理:GET /cgi-bin/externalcontact/get + SQLite 惰性缓存(TTL 600s)。

精简映射:external_contact 的 name/remark/remark_company/tags[].tag_name/description → dict,
供侧边栏画像展示与 T5 话术生成 prompt 注入。缓存键 (corp_id, external_userid)。
"""
import json
import sqlite3
import threading
import time

import httpx

from .migrations import ensure_wecom_tables

_QYAPI_BASE = "https://qyapi.weixin.qq.com"
_CACHE_TTL_S = 600  # 10min:画像字段(标签/备注)低频变更,惰性过期


class WecomContactError(Exception):
    """企微外部联系人接口返回 errcode != 0 或网络请求失败"""

    def __init__(self, errcode: int, errmsg: str = "") -> None:
        self.errcode = errcode
        self.errmsg = errmsg
        super().__init__(f"企微外部联系人接口错误 errcode={errcode} errmsg={errmsg}")


_mutex = threading.Lock()
_conn: sqlite3.Connection | None = None  # 测试注入的独立连接;None 时走 db.py
_db_migrated = False  # db.py 连接上的迁移只执行一次


def set_conn(conn: sqlite3.Connection | None) -> None:
    """测试注入独立 SQLite 连接(生产走 db.py 全局连接);换连接后重走一次建表"""
    global _conn, _db_migrated
    _conn = conn
    _db_migrated = False


def _get_conn() -> sqlite3.Connection:
    """注入连接优先;生产复用 db.py 的连接(WAL 范式)。首次使用确保缓存表已建(幂等)"""
    global _db_migrated
    if _conn is not None:
        conn = _conn
    else:
        from .. import db  # noqa: PLC0415 - 函数内导入避免模块级循环依赖

        conn = db._get_conn()
    if not _db_migrated:
        with _mutex:
            if not _db_migrated:  # 双重检查:等锁期间可能已被其他线程建表
                ensure_wecom_tables(conn)
                _db_migrated = True
    return conn


def _fetch_profile(access_token: str, userid: str, transport: httpx.BaseTransport | None) -> dict:
    """调企微 externalcontact/get 并精简映射;errcode!=0 / HTTP 错误 → WecomContactError"""
    try:
        with httpx.Client(timeout=10.0, transport=transport) as http:
            resp = http.get(
                f"{_QYAPI_BASE}/cgi-bin/externalcontact/get",
                # 官方参数名为 external_userid(不是 userid,写错会报 40058 missing field)
                params={"access_token": access_token, "external_userid": userid},
            )
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPError as e:
        raise WecomContactError(-1, f"网络请求失败: {e}") from e
    errcode = data.get("errcode", 0)
    if errcode != 0:
        raise WecomContactError(errcode, data.get("errmsg", ""))
    ext = data.get("external_contact") or {}
    return {
        "userid": userid,
        "name": ext.get("name", ""),
        "remark": ext.get("remark", ""),
        "company": ext.get("remark_company", ""),
        "tags": [t.get("tag_name", "") for t in ext.get("tags", [])],
        "description": ext.get("description", ""),
    }


def _read_cache(conn: sqlite3.Connection, corp_id: str, userid: str) -> dict | None:
    """TTL 内命中返回缓存画像;无记录或 updated_at+600<now(惰性过期)返回 None"""
    row = conn.execute(
        "SELECT profile_json, updated_at FROM wecom_profile_cache "
        "WHERE corp_id=? AND external_userid=?",
        (corp_id, userid),
    ).fetchone()
    if row is None:
        return None
    if int(row["updated_at"]) + _CACHE_TTL_S < int(time.time()):
        return None
    return json.loads(row["profile_json"])


def _write_cache(conn: sqlite3.Connection, corp_id: str, userid: str, profile: dict) -> None:
    from .. import db  # noqa: PLC0415 - 避免模块级循环导入

    with db.write_lock():  # 全局统一写锁:与 sync/context 等模块的 sqlite 写互斥
        conn.execute(
            "INSERT INTO wecom_profile_cache (corp_id, external_userid, profile_json, updated_at) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(corp_id, external_userid) DO UPDATE SET "
            "profile_json=excluded.profile_json, updated_at=excluded.updated_at",
            (corp_id, userid, json.dumps(profile, ensure_ascii=False), int(time.time())),
        )
        conn.commit()


def get_contact_profile(
    access_token: str,
    userid: str,
    transport: httpx.BaseTransport | None = None,
    corp_id: str = "",
) -> dict:
    """外部联系人精简画像:缓存 TTL 内直接读表,过期/缺失则拉企微并写缓存"""
    conn = _get_conn()
    cached = _read_cache(conn, corp_id, userid)
    if cached is not None:
        return cached
    profile = _fetch_profile(access_token, userid, transport)
    _write_cache(conn, corp_id, userid, profile)
    return profile
