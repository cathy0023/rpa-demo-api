"""七小饱 RPA 平台真实出站客户端(对齐 MR215 internal/manager/rpa_platform.go)

1. token: POST {base}/open-api/touxing/authorization  {appkey, appsecret}
   → {"authorization": "...", "code":0, "success":true}
   文档无过期字段,按 7 天计(604800s),提前 1 天刷新。
2. 发送: POST {base}/open-api/message/send
   body: {originalUserId(企微号), originalExternalUserId(客户), isRoom:false,
          msgType:"TEXT", msg:{content}}
   响应: {code(200成功), message, retryable}
3. 重试: 仅 429 或 retryable=true 时退避重试(2s*attempt),最多 3 次。

并发: token 缓存用 asyncio.Lock 双检(FastAPI 单事件循环,无线程锁必要)。
"""
import asyncio
import logging
import time

import httpx

from .config import config

logger = logging.getLogger(__name__)

TOKEN_TTL_S = 604800          # 文档无 expire 字段,按 7 天计(与 MR215 一致)
TOKEN_REFRESH_AHEAD_S = 86400 # 提前 1 天刷新
TOKEN_MAX_RETRIES = 3
TOKEN_RETRY_INTERVAL_S = 1.0
SEND_MAX_RETRIES = 3
SEND_RETRY_BASE_S = 2.0

_token_lock = asyncio.Lock()
_token_value: str = ""
_token_expire_at: float = 0.0

# 模块级共享 client(lifespan 关闭时 aclose;demo 进程生命周期一致)
_client = httpx.AsyncClient(timeout=30.0)


class RpaError(Exception):
    """RPA 平台调用失败"""


async def close() -> None:
    await _client.aclose()


def _require_real_config() -> None:
    missing = config.validate_real()
    if missing:
        raise RpaError(f"real 模式缺少配置: {', '.join(missing)}")


async def get_token() -> str:
    """获取 RPA token(缓存 + 提前刷新 + asyncio.Lock 双检 + 重试)"""
    global _token_value, _token_expire_at

    if _token_value and time.time() < _token_expire_at - TOKEN_REFRESH_AHEAD_S:
        return _token_value

    async with _token_lock:
        if _token_value and time.time() < _token_expire_at - TOKEN_REFRESH_AHEAD_S:
            return _token_value
        last_err: Exception | None = None
        for attempt in range(TOKEN_MAX_RETRIES):
            try:
                token = await _fetch_token()
                _token_value = token
                _token_expire_at = time.time() + TOKEN_TTL_S
                return token
            except Exception as e:  # noqa: BLE001
                last_err = e
                logger.warning("fetch token attempt %d failed: %s", attempt + 1, e)
                if attempt < TOKEN_MAX_RETRIES - 1:
                    await asyncio.sleep(TOKEN_RETRY_INTERVAL_S)
        raise RpaError(f"token 获取失败: {last_err}") from last_err


async def _fetch_token() -> str:
    _require_real_config()
    resp = await _client.post(
        f"{config.rpa_base_url}/open-api/touxing/authorization",
        json={"appkey": config.rpa_app_key, "appsecret": config.rpa_app_secret},
    )
    resp.raise_for_status()
    data = resp.json()
    authorization = data.get("authorization") or ""
    if not authorization:
        raise RpaError(f"token 响应缺少 authorization: {data}")
    return authorization


async def send_text_message(original_user_id: str, original_external_user_id: str, content: str) -> dict:
    """发送单聊文本消息给客户。仅 429/retryable 退避重试。"""
    _require_real_config()
    token = await get_token()
    url = f"{config.rpa_base_url}/open-api/message/send"
    body = {
        "originalUserId": original_user_id,
        "originalExternalUserId": original_external_user_id,
        "isRoom": False,
        "msgType": "TEXT",
        "msg": {"content": content},
    }

    last_err: Exception | None = None
    for attempt in range(1, SEND_MAX_RETRIES + 1):
        try:
            resp = await _client.post(url, json=body, headers={"Authorization": token})
            try:
                data = resp.json()
            except ValueError:
                data = {}
            if resp.status_code == 200 and data.get("code") == 200:
                return data
            if resp.status_code != 429 and not data.get("retryable"):
                # 业务明确失败且不可重试 → 直接抛
                raise RpaError(f"发送失败 http={resp.status_code} code={data.get('code')} msg={data.get('message')}")
            last_err = RpaError(f"发送失败 http={resp.status_code} code={data.get('code')} msg={data.get('message')}")
        except httpx.HTTPError as e:
            last_err = RpaError(f"发送网络错误: {e}")

        if attempt < SEND_MAX_RETRIES:
            await asyncio.sleep(SEND_RETRY_BASE_S * attempt)

    raise RpaError(f"发送重试耗尽: {last_err}") from last_err
