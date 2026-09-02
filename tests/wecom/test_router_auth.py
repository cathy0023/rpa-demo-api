"""企微侧边栏 router 鉴权链路单测:TestClient + mock httpx(MockTransport 注入)

覆盖:
1. GET /sign —— 成功返回 code:2000 + data 五字段;域名不在白名单报错;白名单为空放行
2. POST /login —— mock getuserinfo 成功 Set-Cookie + code:2000;企微 errcode!=0 拒绝
3. 401 守卫 —— 无效/缺失 cookie 访问 /profile 返回 HTTP 401 + code!=2000
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import httpx
import pytest
from fastapi.testclient import TestClient

from app.wecom import router as wecom_router
from app.wecom.auth import sign_session
from app.wecom.config import WecomConfig
from app.wecom.token import WecomTokenClient

CORP_ID = "corp_test_id"
SECRET = "test_app_secret"
COOKIE_SECRET = "unit_cookie_secret"


def _ok_token_handler(calls: list[httpx.Request]):
    """token client 用:access_token + 双 ticket 全成功"""

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if "gettoken" in str(request.url):
            return httpx.Response(200, json={"errcode": 0, "access_token": "AT-1", "expires_in": 7200})
        if request.url.params.get("type") == "consumer":
            return httpx.Response(200, json={"errcode": 0, "ticket": "CORP-TICKET-1", "expires_in": 7200})
        return httpx.Response(200, json={"errcode": 0, "ticket": "APP-TICKET-1", "expires_in": 7200})

    return handler


def _wecom_handler(calls: list[httpx.Request], login_json: dict | None = None):
    """企微 API 统一 mock:token 三件套 + getuserinfo(可注入返回体)"""

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if "gettoken" in str(request.url):
            return httpx.Response(200, json={"errcode": 0, "access_token": "AT-1", "expires_in": 7200})
        if "get_jsapi_ticket" in str(request.url):
            if request.url.params.get("type") == "consumer":
                return httpx.Response(200, json={"errcode": 0, "ticket": "CORP-TICKET-1", "expires_in": 7200})
            return httpx.Response(200, json={"errcode": 0, "ticket": "APP-TICKET-1", "expires_in": 7200})
        if "getuserinfo" in str(request.url):
            body = login_json or {"errcode": 0, "userid": "zhangsan"}
            return httpx.Response(200, json=body)
        return httpx.Response(200, json={"errcode": 40029, "errmsg": "invalid code"})

    return handler


def _build_app(cfg: WecomConfig, handler) -> TestClient:
    """构建仅挂 wecom router 的 app;router 依赖注入 cfg 与 mock transport"""
    from fastapi import FastAPI

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


def test_sign_returns_dual_signatures():
    calls: list[httpx.Request] = []
    cfg = WecomConfig(corp_id=CORP_ID, agent_id="1000002", app_secret=SECRET,
                      cookie_secret=COOKIE_SECRET)
    client = _build_app(cfg, _wecom_handler(calls))
    resp = client.get("/api/v1/wecom/sidebar/sign", params={"url": "https://example.com/path"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == 2000
    data = body["data"]
    assert set(data) >= {"corp_id", "agent_id", "config_sig", "agent_config_sig", "nonce_str", "timestamp"}
    assert data["corp_id"] == CORP_ID
    assert data["agent_id"] == "1000002"
    assert len(data["config_sig"]) == 40 and len(data["agent_config_sig"]) == 40
    assert len(data["nonce_str"]) == 16
    assert abs(int(data["timestamp"]) - time.time()) < 10
    # 双 ticket 均被取用(签名输入)
    assert len([r for r in calls if "get_jsapi_ticket" in str(r.url)]) == 2


def test_sign_url_not_in_trusted_domain_rejected():
    cfg = WecomConfig(corp_id=CORP_ID, app_secret=SECRET,
                      trusted_domain="https://allowed.example.com", cookie_secret=COOKIE_SECRET)
    client = _build_app(cfg, _wecom_handler([]))
    resp = client.get("/api/v1/wecom/sidebar/sign", params={"url": "https://evil.com/path"})
    assert resp.json()["code"] != 2000


def test_sign_empty_trusted_domain_allows_any():
    """白名单为空 = 开发模式,放行任意域名"""
    cfg = WecomConfig(corp_id=CORP_ID, app_secret=SECRET, trusted_domain="", cookie_secret=COOKIE_SECRET)
    client = _build_app(cfg, _wecom_handler([]))
    resp = client.get("/api/v1/wecom/sidebar/sign", params={"url": "https://anything.dev/x"})
    assert resp.json()["code"] == 2000


def test_login_success_sets_cookie():
    calls: list[httpx.Request] = []
    cfg = WecomConfig(corp_id=CORP_ID, app_secret=SECRET, cookie_secret=COOKIE_SECRET)
    client = _build_app(cfg, _wecom_handler(calls))
    resp = client.post("/api/v1/wecom/sidebar/login", json={"code": "auth-code-123"})
    assert resp.status_code == 200
    assert resp.json()["code"] == 2000
    assert resp.json()["data"]["userid"] == "zhangsan"
    # getuserinfo 被调用且带 code 参数
    userinfo_reqs = [r for r in calls if "getuserinfo" in str(r.url)]
    assert len(userinfo_reqs) == 1
    assert userinfo_reqs[0].url.params["code"] == "auth-code-123"
    assert userinfo_reqs[0].url.params["access_token"] == "AT-1"
    # Set-Cookie 属性:httponly + samesite=lax + max_age=7200
    set_cookie = resp.headers["set-cookie"]
    assert "httponly" in set_cookie.lower()
    assert "samesite=lax" in set_cookie.lower()
    assert "max-age=7200" in set_cookie.lower()
    # cookie 值可被 verify_session 校验
    cookie_token = resp.cookies.get(wecom_router.SESSION_COOKIE)
    assert cookie_token
    assert wecom_router.verify_session(cookie_token, secret=COOKIE_SECRET) == "zhangsan"


def test_login_wecom_errcode_rejected():
    calls: list[httpx.Request] = []
    cfg = WecomConfig(corp_id=CORP_ID, app_secret=SECRET, cookie_secret=COOKIE_SECRET)
    client = _build_app(cfg, _wecom_handler(calls, login_json={"errcode": 40029, "errmsg": "invalid code"}))
    resp = client.post("/api/v1/wecom/sidebar/login", json={"code": "bad-code"})
    assert resp.json()["code"] != 2000
    assert "set-cookie" not in {k.lower() for k in resp.headers}


def test_login_missing_userid_rejected():
    """getuserinfo 成功但无 userid(如外部联系人场景缺字段)→ 拒绝"""
    cfg = WecomConfig(corp_id=CORP_ID, app_secret=SECRET, cookie_secret=COOKIE_SECRET)
    client = _build_app(cfg, _wecom_handler([], login_json={"errcode": 0}))
    resp = client.post("/api/v1/wecom/sidebar/login", json={"code": "c1"})
    assert resp.json()["code"] != 2000


def test_invalid_cookie_profile_401():
    """无效 cookie 访问守卫端点 → HTTP 401 + code!=2000 信封"""
    cfg = WecomConfig(corp_id=CORP_ID, app_secret=SECRET, cookie_secret=COOKIE_SECRET)
    client = _build_app(cfg, _wecom_handler([]))
    resp = client.get("/api/v1/wecom/sidebar/profile", cookies={wecom_router.SESSION_COOKIE: "forged.token"})
    assert resp.status_code == 401
    # HTTPException(401, detail=统一信封) → body["detail"] 为信封结构
    assert resp.json()["detail"]["code"] != 2000


def test_missing_cookie_profile_401():
    cfg = WecomConfig(corp_id=CORP_ID, app_secret=SECRET, cookie_secret=COOKIE_SECRET)
    client = _build_app(cfg, _wecom_handler([]))
    for path, method in (("/profile", "get"), ("/history", "get"), ("/generate", "post")):
        resp = getattr(client, method)(f"/api/v1/wecom/sidebar{path}")
        assert resp.status_code == 401, path
        assert resp.json()["detail"]["code"] != 2000, path


def test_valid_cookie_profile_passes_guard():
    """有效 cookie 过守卫,占位端点返回 501(T4/T7/T5 替换)"""
    cfg = WecomConfig(corp_id=CORP_ID, app_secret=SECRET, cookie_secret=COOKIE_SECRET)
    client = _build_app(cfg, _wecom_handler([]))
    token = sign_session("zhangsan", secret=COOKIE_SECRET)
    resp = client.get("/api/v1/wecom/sidebar/profile", cookies={wecom_router.SESSION_COOKIE: token})
    assert resp.status_code == 501
