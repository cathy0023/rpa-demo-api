"""本地端到端冒烟:app.main 完整应用(含 lifespan)+ TestClient,全部出站走 transport 注入。

与 router 单测的差异:这里用真实聚合 app(RPA router + wecom router + /healthz),
lifespan 真实执行(WECOM_SID_ENABLED=false → start_sync_task 降级不启动,AC6),
cookie 由 client jar 自然跨请求传递(非逐请求手工塞),验证 /sign → /login →
/profile → /generate 全链路 + 降级 + 401 守卫(AC8)。

绝不访问真实企微 API/LLM:wecom 出站与 LLM 出站均为 httpx.MockTransport。
"""
import os

os.environ["WECOM_SID_ENABLED"] = "false"  # 必须先于 app.main 导入:wecom_config 单例在 import 时读环境

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import httpx
import pytest
from fastapi.testclient import TestClient

from app.main import app as main_app
from app.config import RpaDemoConfig
from app.wecom import contact, context, llm_shared, router as wecom_router
from app.wecom.config import WecomConfig
from app.wecom.token import WecomTokenClient

CORP_ID = "corp_e2e_id"
AGENT_ID = "1000002"
SECRET = "e2e_app_secret"
COOKIE_SECRET = "e2e_cookie_secret"

STAFF_USERID = "staff_wang"
EXTERNAL_USERID = "woExt1"
PROFILE_NAME = "王客户"
SCRIPT_TEXT = "王总您好,根据您上周的咨询,为您整理了方案要点,方便时沟通。"


def _wecom_handler(calls: list[httpx.Request]) -> httpx.Response:
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        url = str(request.url)
        if "gettoken" in url:
            return httpx.Response(200, json={"errcode": 0, "access_token": "AT-E2E", "expires_in": 7200})
        if request.url.path.endswith("/get_jsapi_ticket"):
            return httpx.Response(200, json={"errcode": 0, "ticket": "CORP-TICKET-E2E", "expires_in": 7200})
        if request.url.path.endswith("/ticket/get"):
            return httpx.Response(200, json={"errcode": 0, "ticket": "APP-TICKET-E2E", "expires_in": 7200})
        if "getuserinfo" in url:
            return httpx.Response(200, json={"errcode": 0, "userid": STAFF_USERID})
        if "externalcontact/get" in url:
            return httpx.Response(200, json={
                "errcode": 0,
                "external_contact": {
                    "external_userid": EXTERNAL_USERID,
                    "name": PROFILE_NAME,
                    "remark_company": "示例科技",
                    "tags": [{"group_name": "意向度", "tag_name": "高意向", "type": 1}],
                },
            })
        return httpx.Response(200, json={"errcode": 40029, "errmsg": "invalid code"})

    return handler


def _llm_handler(calls: list[httpx.Request]) -> httpx.Response:
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"choices": [{"message": {"content": SCRIPT_TEXT}}]})

    return handler


@pytest.fixture()
def conn(tmp_path):
    """e2e 期间注入独立 SQLite(隔离真实 rpa_demo.db);结束还原模块级连接"""
    c = sqlite3.connect(str(tmp_path / "e2e_local.db"), check_same_thread=False)
    c.row_factory = sqlite3.Row
    contact.set_conn(c)
    context.set_conn(c)
    yield c
    contact.set_conn(None)
    context.set_conn(None)
    c.close()


@pytest.fixture()
def wired(conn, monkeypatch):
    """注入 wecom/LLM mock transport + LLM 配置;结束还原 router 全局句柄避免串扰"""
    monkeypatch.setattr(llm_shared, "config",
                        RpaDemoConfig(llm_api_key="sk-e2e", llm_base_url="http://llm.mock/v1",
                                      llm_model="e2e-model", llm_timeout_s=5))
    wecom_calls: list[httpx.Request] = []
    llm_calls: list[httpx.Request] = []
    wecom_transport = httpx.MockTransport(_wecom_handler(wecom_calls))
    llm_transport = httpx.MockTransport(_llm_handler(llm_calls))
    cfg = WecomConfig(corp_id=CORP_ID, agent_id=AGENT_ID, app_secret=SECRET,
                      cookie_secret=COOKIE_SECRET)  # trusted_domain 空=放行任意域名;sid_enabled 默认 False
    saved = (wecom_router._cfg, wecom_router._token_client,
             wecom_router._http_transport, wecom_router._llm_transport)
    wecom_router.configure(
        cfg=cfg,
        token_client_factory=lambda: WecomTokenClient(
            corp_id=cfg.corp_id, app_secret=cfg.app_secret, transport=wecom_transport),
        http_transport=wecom_transport,
    )
    wecom_router.configure_llm_transport(llm_transport)
    yield {"wecom_calls": wecom_calls, "llm_calls": llm_calls}
    wecom_router._cfg, wecom_router._token_client, wecom_router._http_transport, wecom_router._llm_transport = saved
    wecom_router.configure_llm_transport(None)


def test_healthz_reachable():
    """/healthz 冒烟:完整 app 可启动路由可达"""
    with TestClient(main_app) as client:
        resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_full_chain_sign_login_profile_generate(wired):
    """全链路:sign 双签名 → login 兑 code 得 cookie(cookie jar 跨请求自然携带)
    → profile 拉画像 → generate 画像+LLM 出话术;每步 code:2000 且 data.script 非空"""
    wecom_calls, llm_calls = wired["wecom_calls"], wired["llm_calls"]
    with TestClient(main_app) as client:  # lifespan 真实执行;SID_ENABLED=false 同步任务不启动
        resp = client.get("/api/v1/wecom/sidebar/sign", params={"url": "https://example.com/chat"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["code"] == 2000
        assert set(body["data"]) >= {"corp_id", "agent_id", "config_sig", "agent_config_sig", "nonce_str", "timestamp"}
        assert body["data"]["corp_id"] == CORP_ID
        assert body["data"]["agent_id"] == AGENT_ID

        resp = client.post("/api/v1/wecom/sidebar/login", json={"code": "test"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["code"] == 2000
        assert body["data"]["userid"] == STAFF_USERID
        assert client.cookies.get(wecom_router.SESSION_COOKIE)  # Set-Cookie 已入 jar

        resp = client.get("/api/v1/wecom/sidebar/profile", params={"userid": EXTERNAL_USERID})
        assert resp.status_code == 200
        body = resp.json()
        assert body["code"] == 2000
        assert body["data"]["name"] == PROFILE_NAME
        assert body["data"]["company"] == "示例科技"
        assert "高意向" in body["data"]["tags"]

        resp = client.post("/api/v1/wecom/sidebar/generate", json={"userid": EXTERNAL_USERID})
        assert resp.status_code == 200
        body = resp.json()
        assert body["code"] == 2000
        assert isinstance(body["data"]["script"], str) and body["data"]["script"]

    # 出站全部落在 mock:getuserinfo 带 code=test;画像命中缓存后 generate 不重复拉企微;LLM 恰一次
    userinfo_reqs = [r for r in wecom_calls if "getuserinfo" in str(r.url)]
    assert len(userinfo_reqs) == 1 and userinfo_reqs[0].url.params["code"] == "test"
    assert any("externalcontact/get" in str(r.url) for r in wecom_calls)
    assert len(llm_calls) == 1


def test_history_degraded_empty(wired):
    """AC6 降级:WECOM_SID_ENABLED=false 时 /history 返回 code:2000 data:[](表有数据也不查)"""
    with TestClient(main_app) as client:
        resp = client.post("/api/v1/wecom/sidebar/login", json={"code": "test"})
        assert resp.json()["code"] == 2000
        resp = client.get("/api/v1/wecom/sidebar/history", params={"userid": EXTERNAL_USERID})
        assert resp.status_code == 200
        assert resp.json() == {"code": 2000, "message": "OK", "data": []}


def test_generate_without_cookie_401(wired):
    """AC8:无 cookie POST /generate → HTTP 401 + 顶层统一信封(code==4001)"""
    with TestClient(main_app) as client:
        resp = client.post("/api/v1/wecom/sidebar/generate", json={"userid": EXTERNAL_USERID})
    assert resp.status_code == 401
    body = resp.json()
    assert body["code"] == 4001
    assert body["data"] is None
    assert "detail" not in body
