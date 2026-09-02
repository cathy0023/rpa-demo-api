"""企微侧边栏端点:/sign /login + 守卫占位(/profile /history /generate,T4/T7/T5 替换)。

会话:POST /login 兑换 code 后签 HMAC cookie(httponly/samesite=lax/max_age=7200);
除 /sign 外的端点经 get_current_staff 守卫(AC8:无/坏 cookie → HTTP 401 + 信封)。
测试注入:configure(cfg=..., token_client_factory=...);生产用全局 wecom_config 惰性单例。
"""
import logging
import secrets
import time
from urllib.parse import urlparse

import httpx
from fastapi import APIRouter, Depends, HTTPException, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from starlette.status import HTTP_501_NOT_IMPLEMENTED

from ..schemas import ok
from .auth import sign_session, verify_session
from .config import WecomConfig, wecom_config
from .deps import SESSION_COOKIE, get_current_staff
from .signature import jsapi_signature
from .token import WecomApiError, WecomTokenClient

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/wecom/sidebar")

# 聚合导出名(与 app/routers 范式一致)
api_router = router

_QYAPI_BASE = "https://qyapi.weixin.qq.com"
_COOKIE_MAX_AGE_S = 7200  # 与 auth.DEFAULT_TTL_S 一致

# 运行时句柄(测试经 configure 注入;生产首次请求时按 wecom_config 惰性构建)
_cfg: WecomConfig | None = None
_token_client: WecomTokenClient | None = None
_http_transport: httpx.BaseTransport | None = None  # login 直连 getuserinfo 用,测试注入 mock


def configure(cfg: WecomConfig, token_client_factory=None, http_transport: httpx.BaseTransport | None = None) -> None:
    """测试注入:自定义配置、token client(带 mock transport)与 login 用 http transport"""
    global _cfg, _token_client, _http_transport
    _cfg = cfg
    _token_client = token_client_factory() if token_client_factory else None
    _http_transport = http_transport


def _active_cfg() -> WecomConfig:
    return _cfg if _cfg is not None else wecom_config


def _client() -> WecomTokenClient:
    """生产惰性单例(进程内复用缓存);测试路径已由 configure 注入"""
    global _token_client
    if _token_client is None:
        cfg = _active_cfg()
        _token_client = WecomTokenClient(corp_id=cfg.corp_id, app_secret=cfg.app_secret)
    return _token_client


def _err(code: int, message: str) -> JSONResponse:
    """业务错误响应:HTTP 400 + 顶层 code!=2000 信封(端点直接 return)"""
    return JSONResponse(status_code=400, content={"code": code, "message": message, "data": None})


def _domain_allowed(url: str, trusted_domain: str) -> bool:
    """trusted_domain 为空 = 开发模式放行;否则要求 url 域名与配置一致"""
    if not trusted_domain:
        return True
    host = urlparse(url).hostname or ""
    allowed = urlparse(trusted_domain if "://" in trusted_domain else f"https://{trusted_domain}").hostname or ""
    return bool(host) and host == allowed


class LoginBody(BaseModel):
    """wx.qy.login 返回的一次性 code"""
    code: str


@router.get("/sign")
def sign(url: str):
    """JS-SDK 双签名(wx.config 用企业 ticket、wx.agentConfig 用应用 ticket)"""
    cfg = _active_cfg()
    if not _domain_allowed(url, cfg.trusted_domain):
        return _err(4003, f"url 域名不在 trusted_domain 白名单: {url}")
    try:
        client = _client()
        corp_ticket = client.get_corp_jsapi_ticket()
        app_ticket = client.get_app_jsapi_ticket()
    except WecomApiError as e:
        logger.warning("sign 取 ticket 失败: %s", e)
        return _err(4004, f"企微凭据获取失败: {e}")
    nonce_str = secrets.token_hex(8)  # 16 位小写十六进制
    timestamp = str(int(time.time()))
    return ok({
        "corp_id": cfg.corp_id,
        "agent_id": cfg.agent_id,
        "config_sig": jsapi_signature(corp_ticket, nonce_str, timestamp, url),
        "agent_config_sig": jsapi_signature(app_ticket, nonce_str, timestamp, url),
        "nonce_str": nonce_str,
        "timestamp": timestamp,
    })


@router.post("/login")
def login(body: LoginBody, response: Response):
    """wx.qy.login code 兑换 userid 并签会话 cookie"""
    cfg = _active_cfg()
    try:
        access_token = _client().get_access_token()
    except WecomApiError as e:
        logger.warning("login 取 access_token 失败: %s", e)
        return _err(4004, f"企微凭据获取失败: {e}")
    try:
        with httpx.Client(timeout=10.0, transport=_http_transport) as http:
            resp = http.get(
                f"{_QYAPI_BASE}/cgi-bin/auth/getuserinfo",
                params={"access_token": access_token, "code": body.code},
            )
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPError as e:
        logger.warning("login 调 getuserinfo 失败: %s", e)
        return _err(4005, f"企微登录失败: {e}")
    if data.get("errcode", 0) != 0:
        return _err(4006, f"企微登录被拒 errcode={data.get('errcode')} errmsg={data.get('errmsg', '')}")
    userid = data.get("userid")
    if not isinstance(userid, str) or not userid:
        return _err(4007, "企微登录响应缺少 userid")

    response.set_cookie(
        key=SESSION_COOKIE,
        value=sign_session(userid, secret=cfg.cookie_secret),
        max_age=_COOKIE_MAX_AGE_S,
        httponly=True,
        samesite="lax",
    )
    return ok({"userid": userid})


@router.get("/profile")
def profile(userid: str = Depends(get_current_staff)):
    """占位:T4 替换为客户画像"""
    raise HTTPException(status_code=HTTP_501_NOT_IMPLEMENTED,
                        detail={"code": 5001, "message": "profile 未实现", "data": {"userid": userid}})


@router.get("/history")
def history(userid: str = Depends(get_current_staff)):
    """占位:T7 替换为会话历史"""
    raise HTTPException(status_code=HTTP_501_NOT_IMPLEMENTED,
                        detail={"code": 5001, "message": "history 未实现", "data": {"userid": userid}})


@router.post("/generate")
def generate(userid: str = Depends(get_current_staff)):
    """占位:T5 替换为话术生成"""
    raise HTTPException(status_code=HTTP_501_NOT_IMPLEMENTED,
                        detail={"code": 5001, "message": "generate 未实现", "data": {"userid": userid}})
