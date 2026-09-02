"""会话存档解密层单测:cryptography 自构造向量(不依赖企微 SDK)

约定(对齐企微会话存档文档的简化形态):
- encrypt_random_key = base64(RSA-PKCS1v15(secret_key))
- encrypt_chat_msg   = base64(AES-256-CBC(PKCS7(json(消息))));key=sha256(secret_key),iv=key[:16]

覆盖:
1. decrypt_random_key 往返;错误私钥 → MsgAuditError
2. decrypt_chat_msg 往返(含中文);错误 secret_key → MsgAuditError
3. parse_msg 校验:合法对象 / 非法 JSON / 缺 msgtype / 缺 tolist
4. decrypt_msg 全链路组合 + DisabledChatArchiveClient 空实现
"""
import base64
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from cryptography.hazmat.primitives import padding, serialization
from cryptography.hazmat.primitives.asymmetric import padding as asym_padding
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
import pytest

from app.wecom.msgaudit import (
    DisabledChatArchiveClient,
    MsgAuditError,
    decrypt_chat_msg,
    decrypt_msg,
    decrypt_random_key,
    parse_msg,
)

SECRET_KEY = "unit-aes-secret-key"


def _rsa_keypair_pem() -> tuple[bytes, bytes]:
    """生成测试 RSA 密钥对 → (private_pem, public_pem)"""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_pem = key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return private_pem, public_pem


def _rsa_encrypt_b64(public_pem: bytes, plaintext: bytes) -> str:
    """base64(RSA-PKCS1v15 加密),模拟企微下发的 encrypt_random_key"""
    public_key = serialization.load_pem_public_key(public_pem)
    return base64.b64encode(public_key.encrypt(plaintext, asym_padding.PKCS1v15())).decode("ascii")


def _aes_encrypt_b64(secret_key: str, plaintext_json: str) -> str:
    """base64(AES-256-CBC(PKCS7(json))),与实现约定一致:key=sha256(secret_key),iv=key[:16]"""
    key = hashlib.sha256(secret_key.encode("utf-8")).digest()
    padder = padding.PKCS7(128).padder()
    padded = padder.update(plaintext_json.encode("utf-8")) + padder.finalize()
    encryptor = Cipher(algorithms.AES(key), modes.CBC(key[:16])).encryptor()
    return base64.b64encode(encryptor.update(padded) + encryptor.finalize()).decode("ascii")


def test_decrypt_random_key_roundtrip():
    private_pem, public_pem = _rsa_keypair_pem()
    enc = _rsa_encrypt_b64(public_pem, SECRET_KEY.encode("utf-8"))
    assert decrypt_random_key(enc, private_pem) == SECRET_KEY


def test_decrypt_random_key_wrong_private_key_raises():
    """错误私钥 → MsgAuditError(非裸 ValueError/其他异常)"""
    _unused_priv, public_pem = _rsa_keypair_pem()
    other_priv, _ = _rsa_keypair_pem()
    enc = _rsa_encrypt_b64(public_pem, SECRET_KEY.encode("utf-8"))
    with pytest.raises(MsgAuditError):
        decrypt_random_key(enc, other_priv)


def test_decrypt_chat_msg_roundtrip():
    """AES-256-CBC 解密往返,key=sha256(secret_key)/iv=key[:16],返回消息 dict"""
    msg = {
        "msgid": 1001,
        "msgtype": "text",
        "tolist": ["cust_ext_1"],
        "from": "staff_zhang",
        "text": {"content": "您好,请问有什么可以帮到您?"},
    }
    enc = _aes_encrypt_b64(SECRET_KEY, json.dumps(msg, ensure_ascii=False))
    assert decrypt_chat_msg(enc, SECRET_KEY) == msg


def test_decrypt_chat_msg_wrong_secret_raises():
    msg = {"msgtype": "text", "tolist": ["cust_1"], "text": {"content": "hi"}}
    enc = _aes_encrypt_b64(SECRET_KEY, json.dumps(msg))
    with pytest.raises(MsgAuditError):
        decrypt_chat_msg(enc, "other-secret")


def test_parse_msg_valid():
    msg = {"msgtype": "text", "tolist": ["cust_1"], "text": {"content": "hello"}}
    assert parse_msg(json.dumps(msg)) == msg


def test_parse_msg_invalid_json_raises():
    with pytest.raises(MsgAuditError):
        parse_msg("not-json{")


def test_parse_msg_non_object_raises():
    with pytest.raises(MsgAuditError):
        parse_msg(json.dumps(["not", "an", "object"]))


def test_parse_msg_missing_msgtype_raises():
    with pytest.raises(MsgAuditError):
        parse_msg(json.dumps({"tolist": ["cust_1"]}))


def test_parse_msg_missing_tolist_raises():
    with pytest.raises(MsgAuditError):
        parse_msg(json.dumps({"msgtype": "text"}))


def test_decrypt_msg_full_pipeline():
    """RSA 解 random_key → AES 解消息体 全链路组合"""
    private_pem, public_pem = _rsa_keypair_pem()
    msg = {"msgtype": "text", "tolist": ["cust_1"], "text": {"content": "ok"}}
    enc_key = _rsa_encrypt_b64(public_pem, SECRET_KEY.encode("utf-8"))
    enc_msg = _aes_encrypt_b64(SECRET_KEY, json.dumps(msg))
    assert decrypt_msg(private_pem, enc_key, enc_msg) == msg


def test_disabled_chat_archive_client_returns_empty():
    """降级实现:任何游标/批量参数都返回空批(AC6)"""
    assert DisabledChatArchiveClient().get_chat_data(seq=0, limit=100) == []
    assert DisabledChatArchiveClient().get_chat_data(seq=999, limit=1) == []
