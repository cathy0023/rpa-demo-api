"""企微凭据客户端单测:mock httpx(MockTransport),不访问真实企微 API

三场景:
1. access_token / 企业 ticket / 应用 ticket 获取成功
2. 缓存命中不重复请求
3. 距过期 300s 内主动刷新
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import httpx
import pytest

from app.wecom.token import WecomTokenClient

CORP_ID = "corp_test_id"
SECRET = "test_app_secret"


def _ok_handler(calls: list[httpx.Request]):
    """记录请求并按企微协议返回成功响应"""

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if "gettoken" in str(request.url):
            return httpx.Response(200, json={"errcode": 0, "access_token": "AT-1", "expires_in": 7200})
        if request.url.params.get("type") == "consumer":
            return httpx.Response(200, json={"errcode": 0, "ticket": "CORP-TICKET-1", "expires_in": 7200})
        return httpx.Response(200, json={"errcode": 0, "ticket": "APP-TICKET-1", "expires_in": 7200})

    return handler


def test_get_access_token_success():
    calls: list[httpx.Request] = []
    client = WecomTokenClient(
        corp_id=CORP_ID, app_secret=SECRET,
        transport=httpx.MockTransport(_ok_handler(calls)),
    )
    token = client.get_access_token()
    assert token == "AT-1"
    assert len(calls) == 1
    assert calls[0].url.params["corpid"] == CORP_ID
    assert calls[0].url.params["corpsecret"] == SECRET


def test_corp_ticket_success():
    calls: list[httpx.Request] = []
    client = WecomTokenClient(
        corp_id=CORP_ID, app_secret=SECRET,
        transport=httpx.MockTransport(_ok_handler(calls)),
    )
    ticket = client.get_corp_jsapi_ticket()
    assert ticket == "CORP-TICKET-1"
    req = next(r for r in calls if "get_jsapi_ticket" in str(r.url))
    assert req.url.params["type"] == "consumer"
    # 企业 ticket 接口要求 access_token 查询参数
    assert req.url.params["access_token"] == "AT-1"


def test_app_ticket_success():
    calls: list[httpx.Request] = []
    client = WecomTokenClient(
        corp_id=CORP_ID, app_secret=SECRET,
        transport=httpx.MockTransport(_ok_handler(calls)),
    )
    ticket = client.get_app_jsapi_ticket()
    assert ticket == "APP-TICKET-1"
    req = next(r for r in calls if "get_jsapi_ticket" in str(r.url))
    assert "type" not in req.url.params  # 不带 type 即应用 ticket


def test_cache_hit_no_extra_request():
    calls: list[httpx.Request] = []
    client = WecomTokenClient(
        corp_id=CORP_ID, app_secret=SECRET,
        transport=httpx.MockTransport(_ok_handler(calls)),
    )
    for _ in range(3):
        assert client.get_access_token() == "AT-1"
        assert client.get_corp_jsapi_ticket() == "CORP-TICKET-1"
        assert client.get_app_jsapi_ticket() == "APP-TICKET-1"
    # 各接口只发一次网络请求
    assert len([r for r in calls if "gettoken" in str(r.url)]) == 1
    assert len([r for r in calls if "get_jsapi_ticket" in str(r.url)]) == 2


def test_refresh_before_expiry_300s():
    """距过期 <300s 时视为过期,主动刷新"""
    calls: list[httpx.Request] = []
    client = WecomTokenClient(
        corp_id=CORP_ID, app_secret=SECRET,
        transport=httpx.MockTransport(_ok_handler(calls)),
    )
    assert client.get_access_token() == "AT-1"
    assert len(calls) == 1

    # 人为把过期时间拨到"300s 内",缓存应判失效并重新拉取
    with client._lock:
        cached = client._access_token
        cached.expires_at = int(time.time()) + 100

    assert client.get_access_token() == "AT-1"
    assert len(calls) == 2


def test_api_error_raises():
    """errcode != 0 时抛 WecomApiError"""

    def err_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"errcode": 40013, "errmsg": "invalid corpid"})

    client = WecomTokenClient(
        corp_id=CORP_ID, app_secret=SECRET, transport=httpx.MockTransport(err_handler),
    )
    with pytest.raises(Exception):
        client.get_access_token()


def test_network_error_raises():
    def net_err_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    client = WecomTokenClient(
        corp_id=CORP_ID, app_secret=SECRET, transport=httpx.MockTransport(net_err_handler),
    )
    with pytest.raises(Exception):
        client.get_access_token()
