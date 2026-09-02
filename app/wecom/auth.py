"""会话签名 cookie:HMAC-SHA256 自实现(不引入 itsdangerous)。

token 形如 `payload.sig`:
- payload = base64url(json{userid, exp}) 无填充
- sig     = hex(hmac_sha256(cookie_secret, payload))
verify 校验签名(恒定时间比较)+ exp;失败一律返回 None,不区分原因。
"""
import base64
import hashlib
import hmac
import json
import time

DEFAULT_TTL_S = 7200  # 与登录 cookie max_age 一致


def sign_session(userid: str, secret: str, ttl_s: int = DEFAULT_TTL_S) -> str:
    """为 userid 签发会话 token,ttl_s 可注入(测试短 TTL 用)"""
    payload = base64.urlsafe_b64encode(
        json.dumps({"userid": userid, "exp": int(time.time()) + ttl_s}, separators=(",", ":")).encode()
    ).decode().rstrip("=")
    sig = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}.{sig}"


def verify_session(token: str, secret: str, now: int | None = None) -> str | None:
    """校验 token 返回 userid;签名/过期/结构异常均返回 None。now 可注入(测过期)"""
    try:
        payload, sig = token.rsplit(".", 1)
        expected = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected):
            return None
        padded = payload + "=" * (-len(payload) % 4)
        data = json.loads(base64.urlsafe_b64decode(padded))
        userid = data["userid"]
        exp = data["exp"]
        if not isinstance(userid, str) or not userid or not isinstance(exp, int):
            return None
        if exp <= (now if now is not None else int(time.time())):
            return None
        return userid
    except (ValueError, KeyError, TypeError):
        return None
