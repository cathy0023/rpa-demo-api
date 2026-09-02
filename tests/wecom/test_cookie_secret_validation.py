"""cookie_secret 防御单测:空/弱密钥 fail-closed(鉴权层)+ 启动期校验(入口层)

缺陷背景:cookie_secret 为空串时 HMAC 仍可运算,攻击者可自签 cookie 冒充任意 userid。
两层防御:
1. validate_cookie_secret:空/长度<16 返回错误说明,合法返回 None(config 层帮助函数)
2. deps.get_current_staff:校验前先查密钥合法性,非法一律 401(任何情况空 secret 不放行)
3. main.ensure_wecom_cookie_secret:corp_id 非空(要用侧边栏)而密钥不合规 → RuntimeError 拒绝启动;
   corp_id 为空(纯 RPA 用法)不阻塞
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import httpx
import pytest
from fastapi.testclient import TestClient

from app.main import ensure_wecom_cookie_secret
from app.wecom import router as wecom_router
from app.wecom.auth import sign_session
from app.wecom.config import WecomConfig, validate_cookie_secret
from app.wecom.token import WecomTokenClient

CORP_ID = "corp_test_id"
SECRET = "test_app_secret"
VALID_COOKIE_SECRET = "unit_cookie_secret"  # 18 字符 ≥16


def _build_client(cfg: WecomConfig) -> TestClient:
    from fastapi import FastAPI

    fast_app = FastAPI()
    fast_app.include_router(wecom_router.api_router)
    transport = httpx.MockTransport(lambda req: httpx.Response(
        200, json={"errcode": 0, "access_token": "AT-1", "expires_in": 7200}))
    wecom_router.configure(
        cfg=cfg,
        token_client_factory=lambda: WecomTokenClient(
            corp_id=cfg.corp_id, app_secret=cfg.app_secret, transport=transport),
        http_transport=transport,
    )
    return TestClient(fast_app)


# ---------- 1. validate_cookie_secret 帮助函数 ----------

def test_validate_cookie_secret_empty_returns_error():
    err = validate_cookie_secret(WecomConfig(corp_id=CORP_ID, cookie_secret=""))
    assert err is not None
    assert "WECOM_SID_COOKIE_SECRET" in err


def test_validate_cookie_secret_short_returns_error():
    err = validate_cookie_secret(WecomConfig(corp_id=CORP_ID, cookie_secret="short123"))
    assert err is not None


def test_validate_cookie_secret_valid_returns_none():
    assert validate_cookie_secret(WecomConfig(corp_id=CORP_ID, cookie_secret=VALID_COOKIE_SECRET)) is None


# ---------- 2. fail-closed:空密钥时守卫端点一律 401 ----------

def test_empty_secret_forged_cookie_rejected_401():
    """空 cookie_secret 下用空密钥自签的「合法」cookie 也必须 401(可伪造签名不放行)"""
    cfg = WecomConfig(corp_id=CORP_ID, app_secret=SECRET, cookie_secret="")
    client = _build_client(cfg)
    forged = sign_session("staff_zhang", secret="")  # 攻击者视角:空密钥人人可签
    resp = client.get("/api/v1/wecom/sidebar/profile", params={"userid": "cust_1"},
                      cookies={wecom_router.SESSION_COOKIE: forged})
    assert resp.status_code == 401


def test_missing_secret_no_cookie_rejected_401():
    """空 cookie_secret 下无 cookie 仍 401(基线行为保持)"""
    cfg = WecomConfig(corp_id=CORP_ID, app_secret=SECRET, cookie_secret="")
    client = _build_client(cfg)
    resp = client.get("/api/v1/wecom/sidebar/profile", params={"userid": "cust_1"})
    assert resp.status_code == 401


def test_valid_secret_valid_cookie_passes_guard():
    """合法密钥 + 合法 cookie 正常放行(修复不误伤正常路径)"""
    cfg = WecomConfig(corp_id=CORP_ID, app_secret=SECRET, cookie_secret=VALID_COOKIE_SECRET)
    client = _build_client(cfg)
    token = sign_session("staff_zhang", secret=VALID_COOKIE_SECRET)
    resp = client.get("/api/v1/wecom/sidebar/history", params={"userid": "cust_1"},
                      cookies={wecom_router.SESSION_COOKIE: token})
    assert resp.status_code == 200  # sid_enabled 默认 False → 降级空数组


# ---------- 3. 启动期校验 ----------

def test_startup_corp_id_set_without_secret_raises():
    with pytest.raises(RuntimeError, match="WECOM_SID_COOKIE_SECRET"):
        ensure_wecom_cookie_secret(WecomConfig(corp_id=CORP_ID, cookie_secret=""))


def test_startup_corp_id_set_with_short_secret_raises():
    with pytest.raises(RuntimeError, match="WECOM_SID_COOKIE_SECRET"):
        ensure_wecom_cookie_secret(WecomConfig(corp_id=CORP_ID, cookie_secret="short"))


def test_startup_empty_corp_id_pure_rpa_does_not_block():
    """corp_id 为空(纯 RPA 用法,不用侧边栏)→ 缺 cookie_secret 不阻塞启动"""
    ensure_wecom_cookie_secret(WecomConfig(corp_id="", cookie_secret=""))


def test_startup_valid_config_passes():
    ensure_wecom_cookie_secret(WecomConfig(corp_id=CORP_ID, cookie_secret=VALID_COOKIE_SECRET))
