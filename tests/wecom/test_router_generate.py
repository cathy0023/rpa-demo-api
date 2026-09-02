"""企微侧边栏 /generate 端点单测:TestClient + mock httpx(MockTransport 注入)+ 隔离 SQLite。

覆盖:
1. 有效 cookie POST /generate {userid, scenario?, exclude?} → code:2000 + {script}
2. 无/坏 cookie → HTTP 401(AC8)
3. LLM 未配 key → HTTP 400 + code!=2000 + message 明确(话术生成失败)
"""
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import RpaDemoConfig
from app.wecom import contact, llm_shared, router as wecom_router
from app.wecom.auth import sign_session
from app.wecom.config import WecomConfig
from app.wecom.context import set_conn as context_set_conn
from app.wecom.token import WecomTokenClient

CORP_ID = "corp_test_id"
SECRET = "test_app_secret"
COOKIE_SECRET = "unit_cookie_secret"


def _wecom_handler(calls: list[httpx.Request]):
    """token 三件套 + externalcontact/get 成功画像(generate 需画像注入 prompt)"""

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if "gettoken" in str(request.url):
            return httpx.Response(200, json={"errcode": 0, "access_token": "AT-1", "expires_in": 7200})
        if "externalcontact/get" in str(request.url):
            return httpx.Response(200, json={
                "errcode": 0,
                "external_contact": {
                    "external_userid": "wo_customer1",
                    "name": "张三丰",
                    "remark_company": "武当科技",
                    "tags": [{"group_name": "意向度", "tag_name": "高意向", "type": 1}],
                },
            })
        return httpx.Response(200, json={"errcode": 40029, "errmsg": "invalid code"})

    return handler


def _llm_handler(calls: list[httpx.Request], content: str = "张总您好,为您推荐我们的解决方案。"):
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

    return handler


def _build_app(cfg: WecomConfig, handler) -> TestClient:
    app = FastAPI()
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
    """每个测试独立 SQLite 文件;结束还原 contact/context 模块级连接(check_same_thread=False 对齐 db.py)"""
    c = sqlite3.connect(str(tmp_path / "test_wecom_generate.db"), check_same_thread=False)
    c.row_factory = sqlite3.Row
    contact.set_conn(c)
    context_set_conn(c)
    yield c
    contact.set_conn(None)
    context_set_conn(None)
    c.close()


def test_generate_with_valid_cookie_returns_script(conn, monkeypatch):
    cfg = RpaDemoConfig(llm_api_key="sk-test", llm_base_url="http://llm.mock/v1",
                        llm_model="test-model", llm_timeout_s=5)
    monkeypatch.setattr(llm_shared, "config", cfg)
    wecom_calls: list[httpx.Request] = []
    llm_calls: list[httpx.Request] = []
    wecom_transport = httpx.MockTransport(_wecom_handler(wecom_calls))
    llm_transport = httpx.MockTransport(_llm_handler(llm_calls))
    wecom_cfg = WecomConfig(corp_id=CORP_ID, app_secret=SECRET, cookie_secret=COOKIE_SECRET)
    app = FastAPI()
    app.include_router(wecom_router.api_router)
    wecom_router.configure(
        cfg=wecom_cfg,
        token_client_factory=lambda: WecomTokenClient(
            corp_id=CORP_ID, app_secret=SECRET, transport=wecom_transport),
        http_transport=wecom_transport,
    )
    # 注入 LLM mock transport(router 经 generate_script(transport=_llm_transport) 透传)
    wecom_router.configure_llm_transport(llm_transport)
    client = TestClient(app)
    token = sign_session("zhangsan", secret=COOKIE_SECRET)
    resp = client.post("/api/v1/wecom/sidebar/generate",
                       json={"userid": "wo_customer1", "scenario": "初次跟进"},
                       cookies={wecom_router.SESSION_COOKIE: token})
    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == 2000
    assert body["data"]["script"] == "张总您好,为您推荐我们的解决方案。"
    # 画像被拉取注入 prompt;LLM 收到一条请求
    assert any("externalcontact/get" in str(r.url) for r in wecom_calls)
    assert len(llm_calls) == 1


def test_generate_without_cookie_401(conn):
    wecom_cfg = WecomConfig(corp_id=CORP_ID, app_secret=SECRET, cookie_secret=COOKIE_SECRET)
    client = _build_app(wecom_cfg, _wecom_handler([]))
    resp = client.post("/api/v1/wecom/sidebar/generate", json={"userid": "wo_customer1"})
    assert resp.status_code == 401
    assert resp.json()["detail"]["code"] != 2000


def test_generate_invalid_cookie_401(conn):
    wecom_cfg = WecomConfig(corp_id=CORP_ID, app_secret=SECRET, cookie_secret=COOKIE_SECRET)
    client = _build_app(wecom_cfg, _wecom_handler([]))
    resp = client.post("/api/v1/wecom/sidebar/generate",
                       json={"userid": "wo_customer1"},
                       cookies={wecom_router.SESSION_COOKIE: "forged.token"})
    assert resp.status_code == 401


def test_generate_llm_key_missing_clear_error(conn, monkeypatch):
    """LLM 未配 key → HTTP 400 + code!=2000 + message 明确"""
    monkeypatch.setattr(llm_shared, "config", RpaDemoConfig(llm_api_key=""))
    wecom_calls: list[httpx.Request] = []
    wecom_cfg = WecomConfig(corp_id=CORP_ID, app_secret=SECRET, cookie_secret=COOKIE_SECRET)
    wecom_transport = httpx.MockTransport(_wecom_handler(wecom_calls))
    app = FastAPI()
    app.include_router(wecom_router.api_router)
    wecom_router.configure(
        cfg=wecom_cfg,
        token_client_factory=lambda: WecomTokenClient(
            corp_id=CORP_ID, app_secret=SECRET, transport=wecom_transport),
        http_transport=wecom_transport,
    )
    wecom_router.configure_llm_transport(None)
    client = TestClient(app)
    token = sign_session("zhangsan", secret=COOKIE_SECRET)
    resp = client.post("/api/v1/wecom/sidebar/generate",
                       json={"userid": "wo_customer1"},
                       cookies={wecom_router.SESSION_COOKIE: token})
    assert resp.status_code == 400
    body = resp.json()
    assert body["code"] != 2000
    assert "话术生成失败" in body["message"]
