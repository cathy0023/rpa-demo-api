"""GET /history 端点单测:AC6 降级 + 落库消息查询

覆盖:
1. WECOM_SID_ENABLED=false(默认)+ 有效 cookie → code:2000 data:[](降级语义 AC6,即使表有数据)
2. sid_enabled=true + wecom_chat_history 种子数据 → 最近 limit 条 [{role, content, ts}] 新在前;
   sender==userid → customer,否则 staff
3. limit 截断与默认 20;其他客户消息不串
"""
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import httpx
from fastapi.testclient import TestClient

from app.wecom import router as wecom_router
from app.wecom.auth import sign_session
from app.wecom.config import WecomConfig
from app.wecom.migrations import ensure_wecom_tables
from app.wecom.msgaudit import set_conn as set_msgaudit_conn
from app.wecom.token import WecomTokenClient

CORP_ID = "corp_test_id"
SECRET = "test_app_secret"
COOKIE_SECRET = "unit_cookie_secret"
STAFF = "staff_zhang"
HIST_URL = "/api/v1/wecom/sidebar/history"


def _token_handler(calls: list[httpx.Request]):
    """history 端点不调企微 API,仅满足 configure 的 token client 构建"""

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"errcode": 0, "access_token": "AT-1", "expires_in": 7200})

    return handler


def _build_client(cfg: WecomConfig) -> TestClient:
    from fastapi import FastAPI

    app = FastAPI()
    app.include_router(wecom_router.api_router)
    transport = httpx.MockTransport(_token_handler([]))
    wecom_router.configure(
        cfg=cfg,
        token_client_factory=lambda: WecomTokenClient(
            corp_id=cfg.corp_id, app_secret=cfg.app_secret, transport=transport),
        http_transport=transport,
    )
    return TestClient(app)


def _seed_conn(tmp_path, rows: list[tuple]) -> sqlite3.Connection:
    """建表 + 手工 insert 种子数据,注入 msgaudit 连接"""
    conn = sqlite3.connect(str(tmp_path / "hist.db"), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    ensure_wecom_tables(conn)
    conn.executemany(
        "INSERT INTO wecom_chat_history "
        "(corp_id, external_userid, sender_userid, from_role, content, msg_ts, seq) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    conn.commit()
    set_msgaudit_conn(conn)
    return conn


def _get(client: TestClient, userid: str, **params) -> dict:
    token = sign_session(STAFF, secret=COOKIE_SECRET)
    resp = client.get(HIST_URL, params={"userid": userid, **params},
                      cookies={wecom_router.SESSION_COOKIE: token})
    assert resp.status_code == 200
    return resp.json()


def test_history_disabled_degrades_empty_even_with_data(tmp_path):
    """AC6:sid_enabled=false(默认)→ code:2000 data:[],表有数据也不查"""
    conn = _seed_conn(tmp_path, [
        (CORP_ID, "cust_1", "cust_1", "customer", "已落库但降级不显示", 1700000000, 1),
    ])
    try:
        cfg = WecomConfig(corp_id=CORP_ID, app_secret=SECRET, cookie_secret=COOKIE_SECRET)  # sid_enabled 默认 False
        client = _build_client(cfg)
        body = _get(client, "cust_1")
        assert body["code"] == 2000
        assert body["data"] == []
    finally:
        set_msgaudit_conn(None)
        conn.close()


def test_history_returns_messages_newest_first_with_roles(tmp_path):
    conn = _seed_conn(tmp_path, [
        (CORP_ID, "cust_1", "cust_1", "customer", "多少钱?", 1700000000, 1),
        (CORP_ID, "cust_1", STAFF, "staff", "您好,100 元一套", 1700000060, 2),
        (CORP_ID, "cust_1", "cust_1", "customer", "能便宜点吗?", 1700000120, 3),
    ])
    try:
        cfg = WecomConfig(corp_id=CORP_ID, app_secret=SECRET, cookie_secret=COOKIE_SECRET, sid_enabled=True)
        client = _build_client(cfg)
        body = _get(client, "cust_1")
        assert body["code"] == 2000
        data = body["data"]
        assert [m["content"] for m in data] == ["能便宜点吗?", "您好,100 元一套", "多少钱?"]
        assert [m["ts"] for m in data] == [1700000120, 1700000060, 1700000000]
        assert [m["role"] for m in data] == ["customer", "staff", "customer"]
        assert set(data[0]) == {"role", "content", "ts"}
    finally:
        set_msgaudit_conn(None)
        conn.close()


def test_history_filters_by_external_userid(tmp_path):
    """其他客户的留言不串入当前客户历史"""
    conn = _seed_conn(tmp_path, [
        (CORP_ID, "cust_1", "cust_1", "customer", "msg-for-1", 1700000001, 1),
        (CORP_ID, "cust_2", "cust_2", "customer", "msg-for-2", 1700000002, 2),
    ])
    try:
        cfg = WecomConfig(corp_id=CORP_ID, app_secret=SECRET, cookie_secret=COOKIE_SECRET, sid_enabled=True)
        client = _build_client(cfg)
        assert [m["content"] for m in _get(client, "cust_2")["data"]] == ["msg-for-2"]
    finally:
        set_msgaudit_conn(None)
        conn.close()


def test_history_empty_table_returns_empty(tmp_path):
    conn = _seed_conn(tmp_path, [])
    try:
        cfg = WecomConfig(corp_id=CORP_ID, app_secret=SECRET, cookie_secret=COOKIE_SECRET, sid_enabled=True)
        client = _build_client(cfg)
        body = _get(client, "cust_nobody")
        assert body["code"] == 2000
        assert body["data"] == []
    finally:
        set_msgaudit_conn(None)
        conn.close()


def test_history_limit_truncates_to_newest(tmp_path):
    conn = _seed_conn(tmp_path, [
        (CORP_ID, "cust_1", "cust_1", "customer", f"m{i}", 1700000000 + i, i + 1)
        for i in range(5)
    ])
    try:
        cfg = WecomConfig(corp_id=CORP_ID, app_secret=SECRET, cookie_secret=COOKIE_SECRET, sid_enabled=True)
        client = _build_client(cfg)
        data = _get(client, "cust_1", limit=2)["data"]
        assert [m["content"] for m in data] == ["m4", "m3"]
    finally:
        set_msgaudit_conn(None)
        conn.close()


def test_history_limit_defaults_to_20(tmp_path):
    conn = _seed_conn(tmp_path, [
        (CORP_ID, "cust_1", "cust_1", "customer", f"m{i}", 1700000000 + i, i + 1)
        for i in range(25)
    ])
    try:
        cfg = WecomConfig(corp_id=CORP_ID, app_secret=SECRET, cookie_secret=COOKIE_SECRET, sid_enabled=True)
        client = _build_client(cfg)
        data = _get(client, "cust_1")["data"]
        assert len(data) == 20
        assert data[0]["ts"] == 1700000024  # 最新一条(25 条中 ts 最大)
    finally:
        set_msgaudit_conn(None)
        conn.close()
