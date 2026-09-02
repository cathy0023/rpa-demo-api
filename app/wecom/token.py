"""企微凭据客户端:access_token + 双 jsapi_ticket,内存缓存 + 提前 300s 主动刷新。

接口区别(官方文档口径,两个 ticket 是不同端点):
- access_token:   GET /cgi-bin/gettoken?corpid=&corpsecret=  (应用级,调用一切接口的凭证)
- 企业 jsapi_ticket: GET /cgi-bin/get_jsapi_ticket?access_token=
                  (企业级,wx.config 用;全企业共享一个;无 type 参数)
- 应用 jsapi_ticket: GET /cgi-bin/ticket/get?access_token=&type=agent_config
                  (应用级,wx.agentConfig 用;每个应用独立)
                  票面 TTL 7200s。
"""
import threading
import time
from dataclasses import dataclass

import httpx

_QYAPI_BASE = "https://qyapi.weixin.qq.com"
# 官方建议:缓存有效期取 expires_in 但提前刷新,避免边界时刻签名失败
_REFRESH_MARGIN_S = 300


class WecomApiError(Exception):
    """企微 API 返回 errcode != 0 或网络请求失败"""


@dataclass
class _CachedToken:
    """单条凭据缓存记录(可变,线程锁内读写)"""

    value: str
    expires_at: int  # unix 秒,原始过期时间(判新时扣除提前量)


class WecomTokenClient:
    """获取并缓存企微 access_token / 企业 ticket / 应用 ticket(线程安全)"""

    def __init__(
        self,
        corp_id: str,
        app_secret: str,
        http_timeout_s: float = 10.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._corp_id = corp_id
        self._app_secret = app_secret
        self._client = httpx.Client(timeout=http_timeout_s, transport=transport)
        # RLock:取 ticket 需先取 access_token(同线程嵌套加锁),跨线程仍互斥
        self._lock = threading.RLock()
        self._access_token: _CachedToken | None = None
        self._corp_ticket: _CachedToken | None = None
        self._app_ticket: _CachedToken | None = None

    # --- 对外接口 ---

    def get_access_token(self) -> str:
        """应用 access_token(gettoken 接口)"""
        return self._get_cached("_access_token", self._fetch_access_token)

    def get_corp_jsapi_ticket(self) -> str:
        """企业 jsapi_ticket(wx.config 签名用,type=consumer)"""
        return self._get_cached("_corp_ticket", self._fetch_corp_ticket)

    def get_app_jsapi_ticket(self) -> str:
        """应用 jsapi_ticket(wx.agentConfig 签名用,/cgi-bin/ticket/get?type=agent_config)"""
        return self._get_cached("_app_ticket", self._fetch_app_ticket)

    # --- 内部实现 ---

    @staticmethod
    def _is_fresh(cached: "_CachedToken | None") -> bool:
        """距过期时间 >300s 视为新鲜;否则视为过期,主动提前刷新"""
        return cached is not None and cached.expires_at - _REFRESH_MARGIN_S > time.time()

    def _get_cached(self, attr: str, fetch) -> str:
        """缓存新鲜直接返回,否则加锁拉取刷新"""
        cached: _CachedToken | None = getattr(self, attr)
        if self._is_fresh(cached):
            return cached.value
        with self._lock:
            # 双重检查:等锁期间可能已被其他线程刷新
            cached = getattr(self, attr)
            if self._is_fresh(cached):
                return cached.value
            value, expires_in = fetch()
            setattr(self, attr, _CachedToken(value=value, expires_at=int(time.time()) + expires_in))
            return value

    def _get(self, path: str, params: dict) -> dict:
        """GET 请求企微 API,校验 errcode 后返回 JSON"""
        try:
            resp = self._client.get(f"{_QYAPI_BASE}{path}", params=params)
            resp.raise_for_status()
            data = resp.json()
        except httpx.HTTPError as e:
            raise WecomApiError(f"企微 API 请求失败: {e}") from e
        if data.get("errcode", 0) != 0:
            raise WecomApiError(f"企微 API 错误 errcode={data.get('errcode')} errmsg={data.get('errmsg', '')}")
        return data

    def _fetch_access_token(self) -> tuple[str, int]:
        data = self._get("/cgi-bin/gettoken", {"corpid": self._corp_id, "corpsecret": self._app_secret})
        return data["access_token"], int(data.get("expires_in", 7200))

    def _fetch_corp_ticket(self) -> tuple[str, int]:
        # 企业 ticket:GET /cgi-bin/get_jsapi_ticket?access_token=(官方接口无 type 参数)
        token = self.get_access_token()
        data = self._get("/cgi-bin/get_jsapi_ticket", {"access_token": token})
        return data["ticket"], int(data.get("expires_in", 7200))

    def _fetch_app_ticket(self) -> tuple[str, int]:
        # 应用 ticket:GET /cgi-bin/ticket/get?access_token=&type=agent_config(官方独立端点)
        token = self.get_access_token()
        data = self._get("/cgi-bin/ticket/get", {"access_token": token, "type": "agent_config"})
        return data["ticket"], int(data.get("expires_in", 7200))
