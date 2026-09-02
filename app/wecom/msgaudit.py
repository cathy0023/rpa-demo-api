"""会话存档解密层:纯函数(RSA-PKCS1v15 解 random_key + AES-256-CBC 解消息体)。

约定(对齐企微会话存档文档):
- encrypt_random_key = base64(RSA-PKCS1v15(secret_key));私钥由管理端下发
- encrypt_chat_msg   = base64(AES-256-CBC(PKCS7(json(消息))));
  AES key = sha256(secret_key),iv = key[:16]
- 解密失败/非法消息体统一抛 MsgAuditError,由调用方决定单条跳过(T8)

SDK(ctypes 拉取加密批)留 TODO 由 T8 补;本任务仅提供 ChatArchiveClient Protocol
与 DisabledChatArchiveClient 降级实现(WECOM_SID_ENABLED=false 语义)。
另含 /history 用的已落库消息查询(get_history_records,连接范式同 contact.py)。
"""
import base64
import hashlib
import json
import sqlite3
import threading
from typing import Protocol

from cryptography.hazmat.primitives import padding, serialization
from cryptography.hazmat.primitives.asymmetric import padding as asym_padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from .migrations import ensure_wecom_tables


class MsgAuditError(Exception):
    """会话存档解密/解析失败(错误私钥、错误 secret_key、非法消息体)"""


# TODO(T8): ctypes 拉取实现——Init/GetChatData/Destroy(SDK .so 路径 cfg.sdk_path 可配,
# import/加载失败时降级 DisabledChatArchiveClient),与 sync.py 轮询任务配套。


class ChatArchiveClient(Protocol):
    """会话存档拉取接口:T8 ctypes 实现与测试 mock 共同遵循"""

    def get_chat_data(self, seq: int, limit: int) -> list[dict]:
        """拉取 seq 之后的加密消息批,至多 limit 条,新消息 seq 递增"""
        ...


class DisabledChatArchiveClient:
    """降级实现(WECOM_SID_ENABLED=false):永远返回空批,不触碰 SDK"""

    def get_chat_data(self, seq: int, limit: int) -> list[dict]:
        return []


def decrypt_random_key(encrypt_random_key_b64: str, private_key_pem: bytes) -> str:
    """RSA-PKCS1v15 解密出 secret_key(字符串形式,透传给 decrypt_chat_msg)"""
    try:
        private_key = serialization.load_pem_private_key(private_key_pem, password=None)
        key_bytes = private_key.decrypt(base64.b64decode(encrypt_random_key_b64), asym_padding.PKCS1v15())
        return key_bytes.decode("utf-8")
    except MsgAuditError:
        raise
    except Exception as e:  # noqa: BLE001 - 第三方库异常统一归一为 MsgAuditError
        raise MsgAuditError(f"decrypt_random_key 失败: {e}") from e


def decrypt_chat_msg(encrypt_chat_msg_b64: str, secret_key: str) -> dict:
    """AES-256-CBC 解密消息体并解析:key=sha256(secret_key),iv=key[:16],PKCS7 unpad"""
    key = hashlib.sha256(secret_key.encode("utf-8")).digest()
    try:
        decryptor = Cipher(algorithms.AES(key), modes.CBC(key[:16])).decryptor()
        padded = decryptor.update(base64.b64decode(encrypt_chat_msg_b64)) + decryptor.finalize()
        unpadder = padding.PKCS7(128).unpadder()
        plain = (unpadder.update(padded) + unpadder.finalize()).decode("utf-8")
    except Exception as e:  # noqa: BLE001 - 错误 secret_key/坏密文统一归一
        raise MsgAuditError(f"decrypt_chat_msg 失败: {e}") from e
    return parse_msg(plain)


def parse_msg(plain_json_str: str) -> dict:
    """解析消息 JSON 并校验基本字段(msgtype/tolist 必须存在)"""
    try:
        msg = json.loads(plain_json_str)
    except json.JSONDecodeError as e:
        raise MsgAuditError(f"消息 JSON 解析失败: {e}") from e
    if not isinstance(msg, dict):
        raise MsgAuditError("消息体不是 JSON 对象")
    if not msg.get("msgtype"):
        raise MsgAuditError("消息缺少 msgtype 字段")
    if not msg.get("tolist"):
        raise MsgAuditError("消息缺少 tolist 字段")
    return msg


def decrypt_msg(private_key_pem: bytes, encrypt_random_key: str, encrypt_chat_msg: str) -> dict:
    """全链路:RSA 解出 secret_key → AES 解消息体 → 校验并返回 dict"""
    secret_key = decrypt_random_key(encrypt_random_key, private_key_pem)
    return decrypt_chat_msg(encrypt_chat_msg, secret_key)


_mutex = threading.Lock()
_conn: sqlite3.Connection | None = None  # 测试注入的独立连接;None 时走 db.py
_db_migrated = False


def set_conn(conn: sqlite3.Connection | None) -> None:
    """测试注入独立 SQLite 连接(生产走 db.py 全局连接);换连接后重走一次建表"""
    global _conn, _db_migrated
    _conn = conn
    _db_migrated = False


def _get_conn() -> sqlite3.Connection:
    """注入连接优先;生产复用 db.py 的连接(WAL 范式)。首次使用确保表已建(幂等)"""
    global _db_migrated
    if _conn is not None:
        conn = _conn
    else:
        from .. import db  # noqa: PLC0415 - 函数内导入避免模块级循环依赖

        conn = db._get_conn()
    if not _db_migrated:
        with _mutex:
            if not _db_migrated:  # 双重检查:等锁期间可能已被其他线程建表
                ensure_wecom_tables(conn)
                _db_migrated = True
    return conn


def get_history_records(external_userid: str, limit: int = 20) -> list[dict]:
    """查某外部联系人最近 limit 条消息,新在前 [{role, content, ts}]。

    sender==external_userid → customer(客户发的),否则 staff(企微销售发的)。
    表不可查时返回 [](降级,同 context.get_recent_history 范式)。
    """
    try:
        conn = _get_conn()
        rows = conn.execute(
            "SELECT sender_userid, content, msg_ts FROM wecom_chat_history "
            "WHERE external_userid=? ORDER BY seq DESC LIMIT ?",
            (external_userid, limit),
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    return [
        {
            "role": "customer" if row["sender_userid"] == external_userid else "staff",
            "content": row["content"],
            "ts": int(row["msg_ts"]),
        }
        for row in rows
    ]
