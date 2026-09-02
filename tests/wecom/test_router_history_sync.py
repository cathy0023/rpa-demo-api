"""GET /history 端点 × sync 联动测试(AC3 本地部分):sync 落库后端点能查到消息

与 test_router_history.py(手工 INSERT 种子)不同,本文件走真实 sync 链路:
FakeChatArchiveClient 回放加密批 → sync_once 解密落库 → GET /history 返回消息;
并验证 sid_enabled=false 时即使已落库仍降级空列表(AC6 与 AC3 的开关边界)。
"""
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # 项目根 → app.*
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # tests/ → wecom.test_msgaudit_crypto

import httpx
import json
from fastapi import FastAPI
from fastapi.testclient import TestClient

from wecom.test_msgaudit_crypto import (
    SECRET_KEY,
    _aes_encrypt_b64,
    _rsa_encrypt_b64,
    _rsa_keypair_pem,
)

from app.wecom import router as wecom_router
from app.wecom.auth import sign_session
from app.wecom.config import WecomConfig
from app.wecom.migrations import ensure_wecom_tables
from app.wecom.msgaudit import FakeChatArchiveClient, set_conn as set_msgaudit_conn
from app.wecom.sync import sync_once
from app.wecom.token import WecomTokenClient

CORP_ID = "corp_router_sync"
STAFF = "staff_wang"
CUST = "wm_cust_router"
COOKIE_SECRET = "unit_cookie_secret"
PRIVATE_PEM, PUBLIC_PEM = _rsa_keypair_pem()
HIST_URL = "/api/v1/wecom/sidebar/history"


def _text_msg(seq: int, sender: str, receiver: str, content: str) -> dict:
    return {
        "msgid": 3000 + seq,
        "from": sender,
        "tolist": [{"id": receiver, "type": "single"}],
        "msgtime": 1700000000 + seq,
        "msgtype": "text",
        "text": {"content": content},
    }


def _envelope(seq: int, msg: dict) -> dict:
    return {
        "seq": seq,
        "msgid": str(msg["msgid"]),
        "publickey_ver": 1,
        "encrypt_random_key": _rsa_encrypt_b64(PUBLIC_PEM, SECRET_KEY.encode("utf-8")),
        "encrypt_chat_msg": _aes_encrypt_b64(SECRET_KEY, json.dumps(msg, ensure_ascii=False)),
    }


def _build_client(cfg: WecomConfig) -> TestClient:
    app = FastAPI()
    app.include_router(wecom_router.api_router)
    transport = httpx.MockTransport(lambda req: httpx.Response(
        200, json={"errcode": 0, "access_token": "AT-1", "expires_in": 7200}))
    wecom_router.configure(
        cfg=cfg,
        token_client_factory=lambda: WecomTokenClient(
            corp_id=cfg.corp_id, app_secret=cfg.app_secret, transport=transport),
        http_transport=transport,
    )
    return TestClient(app)


def _get(client: TestClient, userid: str, **params) -> dict:
    token = sign_session(STAFF, secret=COOKIE_SECRET)
    resp = client.get(HIST_URL, params={"userid": userid, **params},
                      cookies={wecom_router.SESSION_COOKIE: token})
    assert resp.status_code == 200
    return resp.json()


def test_history_after_sync_returns_messages(tmp_path):
    """AC3 本地:sync 解密落库 → /history 返回该客户消息,role 判定正确"""
    conn = sqlite3.connect(str(tmp_path / "rs.db"), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    ensure_wecom_tables(conn)
    set_msgaudit_conn(conn)
    client = FakeChatArchiveClient([
        _envelope(1, _text_msg(1, STAFF, CUST, "您好,这边是 RPA 助手")),
        _envelope(2, _text_msg(2, CUST, STAFF, "帮我看看报表怎么配")),
        _envelope(3, _text_msg(3, STAFF, CUST, "稍等,马上给您方案")),
    ])
    try:
        assert sync_once(client, PRIVATE_PEM, CORP_ID) == 3
        cfg = WecomConfig(corp_id=CORP_ID, app_secret="s",
                          cookie_secret=COOKIE_SECRET, sid_enabled=True)
        body = _get(_build_client(cfg), CUST)
        assert body["code"] == 2000
        data = body["data"]
        assert [m["content"] for m in data] == ["稍等,马上给您方案", "帮我看看报表怎么配", "您好,这边是 RPA 助手"]
        assert [m["role"] for m in data] == ["staff", "customer", "staff"]
    finally:
        set_msgaudit_conn(None)
        conn.close()


def test_history_disabled_after_sync_still_empty(tmp_path):
    """AC6 边界:已落库但 sid_enabled=false → 仍降级空列表"""
    conn = sqlite3.connect(str(tmp_path / "rs2.db"), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    ensure_wecom_tables(conn)
    set_msgaudit_conn(conn)
    client = FakeChatArchiveClient([_envelope(1, _text_msg(1, STAFF, CUST, "已落库但降级"))])
    try:
        assert sync_once(client, PRIVATE_PEM, CORP_ID) == 1
        cfg = WecomConfig(corp_id=CORP_ID, app_secret="s",
                          cookie_secret=COOKIE_SECRET)  # sid_enabled 默认 False
        body = _get(_build_client(cfg), CUST)
        assert body["code"] == 2000
        assert body["data"] == []
    finally:
        set_msgaudit_conn(None)
        conn.close()


def test_history_after_sync_other_customer_not_leaked(tmp_path):
    """sync 落多客户消息 → /history 只返回当前 userid 的消息"""
    conn = sqlite3.connect(str(tmp_path / "rs3.db"), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    ensure_wecom_tables(conn)
    set_msgaudit_conn(conn)
    other = "wm_cust_other"
    client = FakeChatArchiveClient([
        _envelope(1, _text_msg(1, STAFF, CUST, "给客户的")),
        _envelope(2, _text_msg(2, STAFF, other, "给别的客户的")),
    ])
    try:
        assert sync_once(client, PRIVATE_PEM, CORP_ID) == 2
        cfg = WecomConfig(corp_id=CORP_ID, app_secret="s",
                          cookie_secret=COOKIE_SECRET, sid_enabled=True)
        assert [m["content"] for m in _get(_build_client(cfg), CUST)["data"]] == ["给客户的"]
        assert [m["content"] for m in _get(_build_client(cfg), other)["data"]] == ["给别的客户的"]
    finally:
        set_msgaudit_conn(None)
        conn.close()
