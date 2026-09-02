"""JS-SDK 签名单测:已知向量 + url 含 # 截断(官方算法 sha1 逐字段 & 拼接)"""
import hashlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import pytest

from app.wecom.signature import jsapi_signature


def _reference(ticket: str, nonce_str: str, timestamp: str, url: str) -> str:
    """独立参照实现(交叉验证)"""
    raw = f"jsapi_ticket={ticket}&noncestr={nonce_str}&timestamp={timestamp}&url={url}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def test_known_vector():
    """自算已知向量:字段按 key 排序拼接后 sha1 hex"""
    sig = jsapi_signature("jsapi_ticket_demo_value", "nonce_abc123", "1700000000",
                          "https://sidebar.example.com/page?x=1")
    assert sig == "23974bb9e0f80804c5d572c116685c293bb4076c"


def test_matches_reference_implementation():
    assert jsapi_signature("t1", "n1", "100", "https://a.com/b") == _reference("t1", "n1", "100", "https://a.com/b")
    # 参数顺序不影响结果(签名固定 ticket/noncestr/timestamp/url 顺序)
    assert jsapi_signature("t2", "n2", "200", "https://a.com/c?y=2") == \
        _reference("t2", "n2", "200", "https://a.com/c?y=2")


def test_url_with_fragment_truncated():
    """url 含 # 时截断 # 及其后内容(官方要求前端 location.href.split('#')[0])"""
    base = "https://sidebar.example.com/page?x=1"
    without = jsapi_signature("tk", "n", "1700000000", base)
    with_frag = jsapi_signature("tk", "n", "1700000000", base + "#/chat?from=sidebar")
    assert with_frag == without


def test_different_ticket_different_signature():
    sig1 = jsapi_signature("tk-a", "n", "1700000000", "https://a.com")
    sig2 = jsapi_signature("tk-b", "n", "1700000000", "https://a.com")
    assert sig1 != sig2


def test_signature_is_40_hex_chars():
    sig = jsapi_signature("tk", "n", "1700000000", "https://a.com")
    assert len(sig) == 40
    assert all(c in "0123456789abcdef" for c in sig)
