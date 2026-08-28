"""加解密单测:验签算法对齐文档、AES-CTR/zstd 往返、错误拒绝、时间窗"""
import base64
import hashlib
import hmac
import json
import time

import pytest

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.crypto import (
    _ZSTD_AVAILABLE,
    ZstdUnavailableError,
    _zstd_compress,
    create_sign,
    decrypt_rpa_body,
    encrypt_rpa_body,
    verify_sign,
)

APP_SECRET = "test-app-secret"
AES_KEY = "test-aes-key"


def _reference_sign(app_secret: str, body: bytes, timestamp: str, nonce: str) -> str:
    """对照文档 Java createSign 的独立实现(交叉验证)"""
    str_to_sign = (timestamp + "\n" + nonce + "\n").encode() + body
    mac = hmac.new(app_secret.encode(), str_to_sign, hashlib.sha256)
    return base64.b64encode(mac.digest()).decode()


def test_create_sign_matches_reference():
    body = b"encrypted-body-bytes"
    assert create_sign(APP_SECRET, body, "1786948243", "abc123") == \
        _reference_sign(APP_SECRET, body, "1786948243", "abc123")


def test_verify_sign():
    body, ts, nonce = b"some-body", "1786948243", "nonce123"
    good = create_sign(APP_SECRET, body, ts, nonce)
    assert verify_sign(APP_SECRET, body, ts, nonce, good)
    assert not verify_sign(APP_SECRET, body, ts, nonce, "wrong-sign")


@pytest.mark.skipif(not _ZSTD_AVAILABLE, reason="zstandard 未安装")
def test_encrypt_decrypt_roundtrip():
    payload = {"json": {"msg_id": 1020009, "sender": 78812345, "receiver": 168855,
                        "vid": 168855, "server_id": 7600327, "content": "你好",
                        "send_time": 1786948243, "is_room": 0, "msgtype": 2},
               "tenantId": "1x1", "type": 102000, "uuid": "d411138"}
    ts, nonce = str(int(time.time())), "abc123"

    encrypted, sign = encrypt_rpa_body(payload, AES_KEY, APP_SECRET, ts, nonce)
    plain = decrypt_rpa_body(encrypted, AES_KEY, APP_SECRET, ts, nonce, sign)
    assert json.loads(plain) == payload


@pytest.mark.skipif(not _ZSTD_AVAILABLE, reason="zstandard 未安装")
def test_wrong_sign_rejected():
    ts = str(int(time.time()))
    body = encrypt_rpa_body({"a": 1}, AES_KEY, APP_SECRET, ts, "n1")[0]
    wrong = create_sign("wrong-secret", body, ts, "n1")
    with pytest.raises(ValueError):
        decrypt_rpa_body(body, AES_KEY, APP_SECRET, ts, "n1", wrong)


def test_in_window():
    from app.routers.callback import _in_window
    now = int(time.time())
    assert _in_window(str(now))
    assert _in_window(str(now - 60))
    assert not _in_window(str(now - 400))
    assert not _in_window("not-a-number")


def test_zstd_required_not_fallback():
    if not _ZSTD_AVAILABLE:
        with pytest.raises(ZstdUnavailableError):
            _zstd_compress(b"data")


# --- Java UTF-8 替换语义(对齐 MR215 rpaJavaUtf8Replace 边界回归) ---

from app.crypto import java_utf8_replace  # noqa: E402

R = b"\xef\xbf\xbd"  # U+FFFD


def test_java_utf8_valid_passthrough():
    assert java_utf8_replace("abc你好".encode()) == "abc你好".encode()


def test_java_utf8_overlong_2byte():
    # C0 80 overlong: 逐字节替换 → 2 个 FFFD(与 Go ToValidUTF8 不同!)
    assert java_utf8_replace(b"\xc0\x80") == R + R


def test_java_utf8_overlong_3byte():
    # E0 80 80 overlong: 逐字节替换 → 3 个 FFFD
    assert java_utf8_replace(b"\xe0\x80\x80") == R * 3


def test_java_utf8_surrogate_single_replacement():
    # ED A0 80 surrogate: 整个序列替换 1 个 FFFD
    assert java_utf8_replace(b"\xed\xa0\x80") == R


def test_java_utf8_out_of_range_4byte():
    # F4 90 80 80 超出 U+10FFFF: 逐字节替换 → 4 个 FFFD
    assert java_utf8_replace(b"\xf4\x90\x80\x80") == R * 4


def test_java_utf8_incomplete_leading():
    # C2 后无 continuation: 替换 leading 1 个
    assert java_utf8_replace(b"\xc2") == R
    # E4 B8 后缺: 合并替换 1 个(消费已有前缀)
    assert java_utf8_replace(b"\xe4\xb8") == R


def test_java_utf8_lone_continuation():
    assert java_utf8_replace(b"\x80\x41\xbf") == R + b"A" + R


def test_java_utf8_sign_uses_replaced_body():
    """签名输入是替换后的字节而非原始密文(核心坑)"""
    body = b"\xed\xa0\x80rest"  # 密文中常见 surrogate 序列
    ts, nonce = "1786948243", "n1"
    s1 = create_sign(APP_SECRET, body, ts, nonce)
    import base64 as b64

    str_to_sign = (ts + "\n" + nonce + "\n").encode() + (R + b"rest")
    expect = b64.b64encode(hmac.new(APP_SECRET.encode(), str_to_sign, hashlib.sha256).digest()).decode()
    assert s1 == expect


def test_ms_timestamp_window():
    """毫秒级时间戳兼容(企销宝实际发毫秒)"""
    from app.routers.callback import _in_window
    now_ms = int(time.time() * 1000)
    assert _in_window(str(now_ms))
    assert not _in_window(str(now_ms - 400_000))


def test_team_id_validation():
    """团队标识白名单: 合法通过, 非法拒绝"""
    from app.routers.callback import _validate_team_id
    assert _validate_team_id("team-a")
    assert _validate_team_id("T123_xyz")
    assert not _validate_team_id("")
    assert not _validate_team_id("a" * 33)
    assert not _validate_team_id("../etc")
    assert not _validate_team_id("a/b")


def test_idempotent_key_scoped_by_team():
    """幂等 key 按 team 隔离: 同 msg_id 不同 team 不冲突"""
    from app.routers.shared import build_idempotent_key
    assert build_idempotent_key("t1", 5, "u") != build_idempotent_key("t2", 5, "u")
    assert build_idempotent_key("t1", 5, "u") == build_idempotent_key("t1", 5, "u")
    assert build_idempotent_key("t1", 0, "u1") != build_idempotent_key("t1", 0, "u2")
