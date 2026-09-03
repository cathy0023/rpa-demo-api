"""企微侧边栏 /profile 端点单测:TestClient + mock httpx(MockTransport 注入)+ 隔离 SQLite。

覆盖:
1. 有效 cookie GET /profile?userid=xx → code:2000 + 精简客户画像
2. 外部联系人不存在(errcode 84061)→ HTTP 400 + code!=2000 + 明确 message
"""
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.wecom import router as wecom_router
from app.wecom.auth import sign_session
from app.wecom.config import WecomConfig
from app.wecom.contact import set_conn
from app.wecom.token import WecomTokenClient

CORP_ID = "corp_test_id"
SECRET = "test_app_secret"
COOKIE_SECRET = "unit_cookie_secret"

_PROFILE_OK = {
    "errcode": 0,
    "external_contact": {
        "external_userid": "wo_customer1",
        "name": "张三丰",
        "remark": "王经理-大客户",
        "remark_company": "武当科技",
        "description": "意向A产品",
        "tags": [{"group_name": "意向度", "tag_name": "高意向", "type": 1}],
    },
}


def _wecom_handler(calls: list[httpx.Request], contact_json: dict):
    """token 三件套 + externalcontact/get(可注入返回体)"""

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if "gettoken" in str(request.url):
            return httpx.Response(200, json={"errcode": 0, "access_token": "AT-1", "expires_in": 7200})
        if "externalcontact/get" in str(request.url):
            return httpx.Response(200, json=contact_json)
        return httpx.Response(200, json={"errcode": 40029, "errmsg": "invalid code"})

    return handler


def _build_app(cfg: WecomConfig, handler) -> TestClient:
    app = FastAPI()
    from app.wecom.deps import WecomAuthError as _WAE, wecom_auth_error_response as _waer
    app.add_exception_handler(_WAE, lambda r, e: _waer(e))
    app.include_router(wecom_router.api_router)
    transport = httpx.MockTransport(handler)
    wecom_router.configure(
        cfg=cfg,
        token_client_factory=lambda: WecomTokenClient(
            corp_id=cfg.corp_id, app_secret=cfg.app_secret, transport=transport),
        http_transport=transport,
    )
    return TestClient(app)


@pytest.fixture()
def conn(tmp_path):
    """每个测试独立 SQLite 文件;结束还原模块级连接(check_same_thread=False 对齐 db.py:TestClient 端点跑在子线程)"""
    c = sqlite3.connect(str(tmp_path / "test_wecom_router.db"), check_same_thread=False)
    c.row_factory = sqlite3.Row
    set_conn(c)
    yield c
    set_conn(None)
    c.close()


def test_profile_with_valid_cookie_returns_condensed_profile(conn):
    calls: list[httpx.Request] = []
    cfg = WecomConfig(corp_id=CORP_ID, app_secret=SECRET, cookie_secret=COOKIE_SECRET)
    client = _build_app(cfg, _wecom_handler(calls, _PROFILE_OK))
    token = sign_session("zhangsan", secret=COOKIE_SECRET)
    resp = client.get("/api/v1/wecom/sidebar/profile", params={"userid": "wo_customer1"},
                      cookies={wecom_router.SESSION_COOKIE: token})
    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == 2000
    # 精简画像:external_contact → 6 字段 dict
    assert body["data"] == {
        "userid": "wo_customer1",
        "name": "张三丰",
        "remark": "王经理-大客户",
        "company": "武当科技",
        "tags": ["高意向"],
        "description": "意向A产品",
    }
    # 画像经 externalcontact/get 取回(带 token client 的 access_token)
    contact_reqs = [r for r in calls if "externalcontact/get" in str(r.url)]
    assert len(contact_reqs) == 1
    assert contact_reqs[0].url.params["access_token"] == "AT-1"
    assert contact_reqs[0].url.params["external_userid"] == "wo_customer1"


def test_profile_contact_not_exist_clear_error(conn):
    """errcode 84061(外部联系人不存在)→ code!=2000 + 明确 message"""
    calls: list[httpx.Request] = []
    cfg = WecomConfig(corp_id=CORP_ID, app_secret=SECRET, cookie_secret=COOKIE_SECRET)
    client = _build_app(cfg, _wecom_handler(
        calls, {"errcode": 84061, "errmsg": "no external contact relation"}))
    token = sign_session("zhangsan", secret=COOKIE_SECRET)
    resp = client.get("/api/v1/wecom/sidebar/profile", params={"userid": "wo_ghost"},
                      cookies={wecom_router.SESSION_COOKIE: token})
    assert resp.status_code == 400
    body = resp.json()
    assert body["code"] != 2000
    assert "不存在" in body["message"]
    assert "wo_ghost" in body["message"]
