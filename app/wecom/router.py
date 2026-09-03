"""企微侧边栏端点:/sign /login /profile /history /generate。

会话:POST /login 兑换 code 后签 HMAC cookie(httponly/samesite=lax/max_age=7200);
除 /sign 外的端点经 get_current_staff 守卫(AC8:无/坏 cookie → HTTP 401 + 信封)。
/history 受 WECOM_SID_ENABLED 降级开关控制(false → data:[],AC6)。
测试注入:configure(cfg=..., token_client_factory=...);生产用全局 wecom_config 惰性单例。

设计变更(2026-09-03):/login 接收的 code 来源由 wx.qy.login(小程序)
调整为 OAuth2 snsapi_base 网页授权(H5 侧边栏);两者最终都走
/cgi-bin/auth/getuserinfo 兑换 userid,本端点 code 兑换逻辑不变。
"""
import asyncio
import logging
import secrets
import time
from urllib.parse import urlparse

import httpx
from fastapi import APIRouter, Depends, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from ..schemas import ok
from .auth import sign_session, verify_session
from .config import WecomConfig, wecom_config
from .contact import WecomContactError, get_contact_profile
from .context import get_recent_history
from .deps import SESSION_COOKIE, get_current_staff
from .generate import generate_script
from .llm_shared import LlmError
from .msgaudit import get_history_records
from .signature import jsapi_signature
from .token import WecomApiError, WecomTokenClient

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/wecom/sidebar")

# 聚合导出名(与 app/routers 范式一致)
api_router = router

_QYAPI_BASE = "https://qyapi.weixin.qq.com"
_COOKIE_MAX_AGE_S = 7200  # 与 auth.DEFAULT_TTL_S 一致
# 外部联系人不存在类错误码:84061 非外部联系人/无好友关系;84060 不合法外部联系人 userid
_CONTACT_NOT_EXIST_CODES = {84060, 84061}

# 运行时句柄(测试经 configure 注入;生产首次请求时按 wecom_config 惰性构建)
_cfg: WecomConfig | None = None
_token_client: WecomTokenClient | None = None
_http_transport: httpx.BaseTransport | None = None  # login 直连 getuserinfo 用,测试注入 mock
_llm_transport: httpx.BaseTransport | None = None  # generate 调 LLM 用,测试注入 mock


def configure(cfg: WecomConfig, token_client_factory=None, http_transport: httpx.BaseTransport | None = None) -> None:
    """测试注入:自定义配置、token client(带 mock transport)与 login 用 http transport"""
    global _cfg, _token_client, _http_transport
    _cfg = cfg
    _token_client = token_client_factory() if token_client_factory else None
    _http_transport = http_transport


def configure_llm_transport(transport: httpx.BaseTransport | None) -> None:
    """测试注入 LLM mock transport;生产不调用(generate_script 走真实 httpx)"""
    global _llm_transport
    _llm_transport = transport


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
    """OAuth2 snsapi_base 网页授权回调的一次性 code

    侧边栏 H5 经 open.weixin.qq.com/connect/oauth2/authorize 重定向回
    redirect_uri 时,query 携带的 code 字段。语义上不同于 wx.qy.login 的
    code(小程序 JSAPI),但本端点对两者一视同仁——都调 getuserinfo 兑换
    当前员工 userid。
    """
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
    """OAuth2 snsapi_base code 兑换 userid 并签会话 cookie

    code 来源:企微侧边栏 H5 经 OAuth2 重定向回 sidebar URL 时,query
    `?code=xxx` 携带。前端 postLogin({code}) 把这个 code 送进本端点;
    本端点用 access_token + code 调 /cgi-bin/auth/getuserinfo 换 userid,
    签 HMAC cookie 写回浏览器。该逻辑与历史 wx.qy.login 路径完全一致。
    """
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
        secure=cfg.cookie_secure,  # WECOM_SID_COOKIE_SECURE=true 时仅 HTTPS 传输(生产建议开)
    )
    return ok({"userid": userid})


@router.get("/profile")
def profile(userid: str, staff_userid: str = Depends(get_current_staff)):  # noqa: ARG001 - staff_userid 仅守卫会话
    """外部联系人精简画像(缓存 TTL 600s,见 contact.get_contact_profile)。

    userid=外部联系人 ID(前端 getCurExternalContact);staff_userid=会话守卫身份
    """
    cfg = _active_cfg()
    try:
        access_token = _client().get_access_token()
    except WecomApiError as e:
        logger.warning("profile 取 access_token 失败: %s", e)
        return _err(4004, f"企微凭据获取失败: {e}")
    try:
        return ok(get_contact_profile(access_token, userid,
                                      transport=_http_transport, corp_id=cfg.corp_id))
    except WecomContactError as e:
        logger.warning("profile 取画像失败: %s", e)
        if e.errcode in _CONTACT_NOT_EXIST_CODES:
            return _err(4101, f"外部联系人不存在或无好友关系: {userid} (errcode={e.errcode})")
        return _err(4102, f"客户画像获取失败: {e}")


@router.get("/history")
def history(userid: str, limit: int = 20, staff_userid: str = Depends(get_current_staff)):  # noqa: ARG001 - staff_userid 仅守卫会话
    """会话历史:WECOM_SID_ENABLED=false 降级返回 data:[](AC6);true 查落库消息。

    userid=外部联系人 ID(前端 getCurExternalContact);staff_userid=会话守卫身份;
    返回最近 limit 条(默认 20,新在前) [{role: customer|staff, content, ts}];
    limit 夹取到 [1,100](负数/超大值安全化,防全表扫描)。
    """
    cfg = _active_cfg()
    if not cfg.sid_enabled:
        return ok([])
    limit = max(1, min(limit, 100))
    return ok(get_history_records(userid, limit=limit, corp_id=cfg.corp_id))


class GenerateBody(BaseModel):
    """外部联系人 ID + 可选场景/排除内容(「换一条」时前端回传上次话术)"""
    userid: str
    scenario: str = ""
    exclude: str = ""


@router.post("/generate")
def generate(body: GenerateBody, staff_userid: str = Depends(get_current_staff)):
    """生成 1 条销售话术:画像 + 最近对话(T8 前表缺失→空)+ 场景/排除注入 prompt。

    userid=外部联系人 ID(前端 getCurExternalContact);staff_userid=会话守卫身份
    """
    cfg = _active_cfg()
    try:
        access_token = _client().get_access_token()
    except WecomApiError as e:
        logger.warning("generate 取 access_token 失败: %s", e)
        return _err(4004, f"企微凭据获取失败: {e}")
    try:
        profile = get_contact_profile(access_token, body.userid,
                                      transport=_http_transport, corp_id=cfg.corp_id)
    except WecomContactError as e:
        logger.warning("generate 取画像失败: %s", e)
        if e.errcode in _CONTACT_NOT_EXIST_CODES:
            return _err(4101, f"外部联系人不存在或无好友关系: {body.userid} (errcode={e.errcode})")
        return _err(4102, f"客户画像获取失败: {e}")
    history = get_recent_history(body.userid, limit=20, corp_id=cfg.corp_id)
    try:
        script = asyncio.run(generate_script(
            profile, history, body.scenario, body.exclude, transport=_llm_transport))
    except LlmError as e:
        logger.warning("generate LLM 调用失败: %s", e)
        return _err(4201, f"话术生成失败: {e}")
    return ok({"script": script})
