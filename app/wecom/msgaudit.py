"""会话存档解密层:纯函数(RSA-PKCS1v15 解 random_key + AES-256-CBC 解消息体)。

约定(对齐企微会话存档文档):
- encrypt_random_key = base64(RSA-PKCS1v15(secret_key));私钥由管理端下发
- encrypt_chat_msg   = base64(AES-256-CBC(PKCS7(json(消息))));
  AES key = sha256(secret_key),iv = key[:16]
- 解密失败/非法消息体统一抛 MsgAuditError,由调用方决定单条跳过(T8)

SDK 拉取:T8 提供 CtypesChatArchiveClient(CDLL 加载 libWeWorkFinanceSdk_C,
Init/GetChatData/Destroy 三函数,密码/代理传 None)、FakeChatArchiveClient(测试/演示回放
加密批)与 DisabledChatArchiveClient 降级实现(WECOM_SID_ENABLED=false 语义)。
另含 /history 用的已落库消息查询(get_history_records,连接范式同 contact.py)。
"""
import base64
import ctypes
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
    """会话存档解密/解析失败(错误私钥、错误 secret_key、非法消息体、SDK 加载失败)"""


class ChatArchiveClient(Protocol):
    """会话存档拉取接口:ctypes 实现、Fake 实现与测试 mock 共同遵循"""

    def get_chat_data(self, seq: int, limit: int) -> list[dict]:
        """拉取 seq 之后的加密消息批,至多 limit 条,新消息 seq 递增

        返回条目与官方 GetChatData 的 chatdata 同构:
        {seq, msgid, publickey_ver, encrypt_random_key, encrypt_chat_msg}
        """
        ...


class DisabledChatArchiveClient:
    """降级实现(WECOM_SID_ENABLED=false):永远返回空批,不触碰 SDK"""

    def get_chat_data(self, seq: int, limit: int) -> list[dict]:
        return []


def parse_chat_data(raw_json: str) -> list[dict]:
    """解析 GetChatData 返回的 JSON 串,取 chatdata 数组(结构非法抛 MsgAuditError)"""
    try:
        data = json.loads(raw_json)
    except json.JSONDecodeError as e:
        raise MsgAuditError(f"GetChatData 返回非法 JSON: {e}") from e
    if not isinstance(data, dict) or not isinstance(data.get("chatdata"), list):
        raise MsgAuditError("GetChatData 返回缺少 chatdata 数组")
    return data["chatdata"]


class FakeChatArchiveClient:
    """测试/演示用:直接回放预先构造好的加密消息批。

    respect_seq=True(默认)时按起点 seq 过滤(模拟 SDK 游标语义);
    置 False 无视游标重复吐全量,用于验证 DB 层幂等。
    """

    def __init__(self, items: list[dict], respect_seq: bool = True):
        self.items = list(items)
        self.respect_seq = respect_seq
        self.calls: list[tuple[int, int]] = []

    def get_chat_data(self, seq: int, limit: int) -> list[dict]:
        self.calls.append((seq, limit))
        matched = [item for item in self.items if not self.respect_seq or item["seq"] > seq]
        return matched[:limit]


class CtypesChatArchiveClient:
    """官方 C SDK 拉取实现:CDLL 加载 libWeWorkFinanceSdk_C 后 Init/GetChatData/Destroy。

    - Init(corp_id, secret) 返回 SDK 句柄;密码(_rsa_private_key 密码)与代理官方允许传 None
    - GetChatData(seq, limit, proxy, passwd, timeout) 返回 JSON 串,解析后取 chatdata 数组
    - SDK 路径由 cfg.sdk_path 配置;load 失败/Init 失败抛 MsgAuditError,由调用方(sync)
      降级为 DisabledChatArchiveClient,不阻断应用启动
    """

    def __init__(self, sdk_path: str, corp_id: str, secret: str, timeout_s: int = 30):
        if not sdk_path:
            raise MsgAuditError("会话存档 SDK 路径未配置(WECOM_SID_SDK_PATH)")
        try:
            self._lib = ctypes.CDLL(sdk_path)
        except OSError as e:
            raise MsgAuditError(f"会话存档 SDK 加载失败: {sdk_path}: {e}") from e
        # 官方 C 接口签名:int Init(const char* corpid, const char* secret)
        self._lib.Init.argtypes = [ctypes.c_char_p, ctypes.c_char_p]
        self._lib.Init.restype = ctypes.c_void_p
        # ssize_t GetChatData(void* sdk, unsigned long long seq, unsigned int limit,
        #                     const char* proxy, const char* passwd, unsigned int timeout,
        #                     char** msg_data) — 官方签名,代理/密码允许传 None
        self._lib.GetChatData.argtypes = [
            ctypes.c_void_p, ctypes.c_ulonglong, ctypes.c_uint,
            ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint,
            ctypes.POINTER(ctypes.c_char_p),
        ]
        self._lib.GetChatData.restype = ctypes.c_ssize_t
        # int Destroy(void* sdk)
        self._lib.Destroy.argtypes = [ctypes.c_void_p]
        self._lib.Destroy.restype = ctypes.c_int
        # void FreeData(void* sdk, char* msg_data) — 释放 GetChatData 返回的 SDK 内部缓冲
        self._lib.FreeData.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
        self._lib.FreeData.restype = None
        self._handle = self._lib.Init(corp_id.encode("utf-8"), secret.encode("utf-8"))
        if not self._handle:
            raise MsgAuditError(f"会话存档 SDK Init 失败(corp_id={corp_id})")
        self._timeout_s = timeout_s

    def get_chat_data(self, seq: int, limit: int) -> list[dict]:
        """拉取加密批并解析;非 0 返回码/空指针/非法 JSON 抛 MsgAuditError。

        GetChatData 返回的缓冲由 SDK 内部分配,用完必须 FreeData 归还,
        否则每次轮询泄漏一块内存——释放放在 finally,解析失败也保证执行。
        """
        out = ctypes.c_char_p()
        ret = self._lib.GetChatData(self._handle, seq, limit, None, None,
                                    self._timeout_s, ctypes.byref(out))
        try:
            if ret != 0:
                raise MsgAuditError(f"GetChatData 失败 ret={ret} seq={seq}")
            raw_bytes = out.value
            if not raw_bytes:
                raise MsgAuditError(f"GetChatData 返回空数据 seq={seq}")
            return parse_chat_data(raw_bytes.decode("utf-8"))
        finally:
            self._lib.FreeData(self._handle, out)

    def destroy(self) -> None:
        """释放 SDK 句柄(进程退出时调用;幂等)"""
        if getattr(self, "_handle", None):
            self._lib.Destroy(self._handle)
            self._handle = None


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
