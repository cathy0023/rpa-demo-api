"""会话签名 cookie 单测:HMAC-SHA256 签名 payload(base64url(json{userid,exp}))

五场景:
1. 签发/校验往返成功
2. 篡改 payload 拒绝
3. 篡改签名拒绝
4. 过期拒绝(ttl_s/now 可注入,不真等)
5. 缺字段/垃圾 token 拒绝
"""
import base64
import hashlib
import hmac
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.wecom.auth import sign_session, verify_session

SECRET = "unit_test_cookie_secret"
USERID = "zhangsan"


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _forge_signed(payload_obj: dict, secret: str = SECRET) -> str:
    """按同一签名算法构造 payload,用于缺字段等边角用例"""
    payload = _b64e(json.dumps(payload_obj, separators=(",", ":")).encode())
    sig = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}.{sig}"


def test_roundtrip():
    token = sign_session(USERID, secret=SECRET)
    # token 形如 payload.sig 两段
    assert token.count(".") == 1
    assert verify_session(token, secret=SECRET) == USERID


def test_tampered_payload_rejected():
    """改 payload 里的 userid(保持签名不变)→ 校验失败"""
    token = sign_session(USERID, secret=SECRET)
    payload, sig = token.split(".", 1)
    data = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    data["userid"] = "attacker"
    forged = _b64e(json.dumps(data, separators=(",", ":")).encode())
    assert verify_session(f"{forged}.{sig}", secret=SECRET) is None


def test_tampered_signature_rejected():
    token = sign_session(USERID, secret=SECRET)
    payload, sig = token.split(".", 1)
    bad_sig = ("0" if sig[0] != "0" else "1") + sig[1:]
    assert verify_session(f"{payload}.{bad_sig}", secret=SECRET) is None


def test_expired_rejected():
    """ttl 注入为负 → 签发即已过期,拒绝"""
    token = sign_session(USERID, secret=SECRET, ttl_s=-10)
    assert verify_session(token, secret=SECRET) is None


def test_ttl_injected_now_shift():
    """短 TTL + 校验时刻前移 → 拒绝;未越过 exp 则仍有效"""
    token = sign_session(USERID, secret=SECRET, ttl_s=60)
    now = int(time.time())
    assert verify_session(token, secret=SECRET, now=now + 30) == USERID
    assert verify_session(token, secret=SECRET, now=now + 61) is None


def test_missing_exp_rejected():
    assert verify_session(_forge_signed({"userid": USERID}), secret=SECRET) is None


def test_missing_userid_rejected():
    assert verify_session(_forge_signed({"exp": int(time.time()) + 100}), secret=SECRET) is None


def test_wrong_secret_rejected():
    token = sign_session(USERID, secret=SECRET)
    assert verify_session(token, secret="another_secret") is None


def test_garbage_tokens_rejected():
    assert verify_session("", secret=SECRET) is None
    assert verify_session("not-a-token", secret=SECRET) is None
    assert verify_session("abc.def", secret=SECRET) is None
    assert verify_session(f"{USERID}.", secret=SECRET) is None
