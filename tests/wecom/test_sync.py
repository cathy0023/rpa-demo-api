"""会话存档同步任务单测:FakeChatArchiveClient 构造加密消息批(不依赖企微 SDK)

消息批条目与官方 GetChatData 返回的 chatdata 同构:
  {seq, msgid, publickey_ver, encrypt_random_key, encrypt_chat_msg}
消息体对齐企微 msgaudit 结构:{msgid, from, tolist:[{id, type}], msgtime, msgtype, text:{content}}

覆盖(T8):
1. 正常:2 条文本消息解密落库 wecom_chat_history(sender==external_userid → customer 否则 staff)
2. 幂等 AC5:同 seq 重复 sync → 0 条新落库(seq UNIQUE + INSERT OR IGNORE)
3. 解密失败单条跳过不中断(1 条坏密文 + 1 条好密文 → 只落 1 条)
4. last_seq 推进写入 sync_state 持久化;二次 sync 客户端收到正确 seq 参数
5. 非文本消息(msgtype:"image")跳过不落库
6. 同事间会话(from 和 tolist 都是内部 userid,无 external)不入库
7. CtypesChatArchiveClient:SDK load 失败/未配置 sdk_path 抛 MsgAuditError
8. run_sync_loop:单次异常捕获记日志不中断;start_sync_task:降级开关与降级路径
"""
import asyncio
import ctypes
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # 项目根 → app.*
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # tests/ → wecom.test_msgaudit_crypto

import pytest
from fastapi import FastAPI

from wecom.test_msgaudit_crypto import (
    SECRET_KEY,
    _aes_encrypt_b64,
    _rsa_encrypt_b64,
    _rsa_keypair_pem,
)

from app.wecom.config import WecomConfig
from app.wecom.migrations import ensure_wecom_tables
from app.wecom.msgaudit import (
    CtypesChatArchiveClient,
    FakeChatArchiveClient,
    MsgAuditError,
    set_conn as set_msgaudit_conn,
)
from app.wecom import sync as sync_mod

CORP_ID = "corp_sync_unit"
STAFF = "staff_zhang"
STAFF2 = "staff_li"
CUST = "wm_cust_001"
PRIVATE_PEM, PUBLIC_PEM = _rsa_keypair_pem()  # 全模块共用一对密钥,keygen 慢避免重复


def _text_msg(seq: int, sender: str, receiver: str, content: str,
              roomid: str = "", tolist: list[dict] | None = None) -> dict:
    """企微 msgaudit 文本消息结构:tolist 为对象数组 [{id, type}];群聊带 roomid"""
    receivers = tolist if tolist is not None else [{"id": receiver, "type": "single"}]
    msg = {
        "msgid": 1000 + seq,
        "from": sender,
        "tolist": receivers,
        "msgtime": 1700000000 + seq,
        "msgtype": "text",
        "text": {"content": content},
    }
    if roomid:
        msg["roomid"] = roomid
    return msg


def _envelope(seq: int, msg: dict) -> dict:
    """GetChatData chatdata 条目:seq + RSA 加密 random_key + AES 加密消息体"""
    return {
        "seq": seq,
        "msgid": str(msg["msgid"]),
        "publickey_ver": 1,
        "encrypt_random_key": _rsa_encrypt_b64(PUBLIC_PEM, SECRET_KEY.encode("utf-8")),
        "encrypt_chat_msg": _aes_encrypt_b64(SECRET_KEY, json.dumps(msg, ensure_ascii=False)),
    }


def _make_env(tmp_path, items: list[dict]) -> tuple[FakeChatArchiveClient, sqlite3.Connection]:
    """测试环境:注入独立 SQLite 连接 + 构造回放指定加密批的 FakeChatArchiveClient"""
    conn = sqlite3.connect(str(tmp_path / "sync.db"), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    ensure_wecom_tables(conn)
    set_msgaudit_conn(conn)
    return FakeChatArchiveClient(items), conn


def _rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM wecom_chat_history ORDER BY seq").fetchall()


# ---------- 1. 正常落库 ----------

def test_sync_once_persists_two_text_messages(tmp_path):
    items = [
        _envelope(1, _text_msg(1, STAFF, CUST, "您好,请问有什么可以帮到您?")),
        _envelope(2, _text_msg(2, CUST, STAFF, "想了解下价格")),
    ]
    client, conn = _make_env(tmp_path, items)
    try:
        inserted = sync_mod.sync_once(client, PRIVATE_PEM, CORP_ID)
        assert inserted == 2
        rows = _rows(conn)
        assert len(rows) == 2
        first, second = rows
        assert first["external_userid"] == CUST
        assert first["sender_userid"] == STAFF
        assert first["from_role"] == "staff"  # sender != external_userid
        assert first["content"] == "您好,请问有什么可以帮到您?"
        assert first["msg_ts"] == 1700000001
        assert first["corp_id"] == CORP_ID
        assert second["sender_userid"] == CUST
        assert second["from_role"] == "customer"  # sender == external_userid
        assert second["content"] == "想了解下价格"
    finally:
        set_msgaudit_conn(None)
        conn.close()


# ---------- 2. 幂等 AC5 ----------

def test_sync_once_idempotent_same_seq_no_duplicate(tmp_path):
    """客户端无视游标重复吐同批数据 → 第二次 0 条新落库(seq UNIQUE + INSERT OR IGNORE)"""
    items = [
        _envelope(1, _text_msg(1, STAFF, CUST, "第一条")),
        _envelope(2, _text_msg(2, CUST, STAFF, "第二条")),
    ]
    client, conn = _make_env(tmp_path, items)
    client.respect_seq = False  # 强制每次返回全量,验证 DB 层幂等
    try:
        assert sync_mod.sync_once(client, PRIVATE_PEM, CORP_ID) == 2
        assert sync_mod.sync_once(client, PRIVATE_PEM, CORP_ID) == 0
        assert len(_rows(conn)) == 2
    finally:
        set_msgaudit_conn(None)
        conn.close()


# ---------- 3. 解密失败单条跳过 ----------

def test_sync_once_skips_single_decrypt_failure(tmp_path):
    good = _envelope(2, _text_msg(2, STAFF, CUST, "好的消息"))
    bad = {"seq": 1, "msgid": "x", "encrypt_random_key": "!!!not-base64###",
           "encrypt_chat_msg": "???bad-cipher$$$", "publickey_ver": 1}
    client, conn = _make_env(tmp_path, [bad, good])
    try:
        inserted = sync_mod.sync_once(client, PRIVATE_PEM, CORP_ID)
        assert inserted == 1
        rows = _rows(conn)
        assert len(rows) == 1
        assert rows[0]["content"] == "好的消息"
    finally:
        set_msgaudit_conn(None)
        conn.close()


# ---------- 4. last_seq 推进持久化 + 二次 sync 游标 ----------

def test_sync_once_advances_last_seq_and_persists_cursor(tmp_path):
    items = [
        _envelope(100, _text_msg(100, STAFF, CUST, "m100")),
        _envelope(101, _text_msg(101, CUST, STAFF, "m101")),
        _envelope(102, _text_msg(102, STAFF, CUST, "m102")),
    ]
    client, conn = _make_env(tmp_path, items)
    try:
        assert sync_mod.sync_once(client, PRIVATE_PEM, CORP_ID) == 3
        # 游标推进写入 sync_state 并持久化
        row = conn.execute(
            "SELECT last_seq, updated_at FROM sync_state WHERE corp_id=?", (CORP_ID,)).fetchone()
        assert row["last_seq"] == 102
        assert row["updated_at"] > 0
        # 二次 sync:客户端收到 last_seq 作为拉取起点(seq 之后),无新数据
        assert sync_mod.sync_once(client, PRIVATE_PEM, CORP_ID) == 0
        assert client.calls == [(0, sync_mod.BATCH_LIMIT), (102, sync_mod.BATCH_LIMIT)]
    finally:
        set_msgaudit_conn(None)
        conn.close()


# ---------- 5. 非文本消息跳过 ----------

def test_sync_once_skips_non_text_messages(tmp_path):
    image_msg = {
        "msgid": 2001,
        "from": STAFF,
        "tolist": [{"id": CUST, "type": "single"}],
        "msgtime": 1700000005,
        "msgtype": "image",
        "image": {"sdkfileid": "sdk-file-id", "md5sum": "abc", "filesize": 100},
    }
    items = [
        _envelope(1, _text_msg(1, STAFF, CUST, "文字消息")),
        _envelope(2, image_msg),
    ]
    client, conn = _make_env(tmp_path, items)
    try:
        assert sync_mod.sync_once(client, PRIVATE_PEM, CORP_ID) == 1
        assert [r["content"] for r in _rows(conn)] == ["文字消息"]
    finally:
        set_msgaudit_conn(None)
        conn.close()


# ---------- 6. 同事间会话不入库 ----------

def test_sync_once_skips_internal_only_conversation(tmp_path):
    items = [_envelope(1, _text_msg(1, STAFF, STAFF2, "内部沟通,无客户"))]
    client, conn = _make_env(tmp_path, items)
    try:
        assert sync_mod.sync_once(client, PRIVATE_PEM, CORP_ID) == 0
        assert _rows(conn) == []
    finally:
        set_msgaudit_conn(None)
        conn.close()


# ---------- 6b. 群聊/多对端消息不入库(泄漏防御) ----------

def test_sync_once_skips_group_messages_with_roomid(tmp_path):
    """roomid 非空(群聊,即使含外部成员)→ 不入库,游标照常推进"""
    group_msg = _text_msg(1, STAFF, "", "群聊里的消息",
                          roomid="wr_group_001", tolist=[{"id": CUST, "type": "group"}])
    items = [
        _envelope(1, group_msg),
        _envelope(2, _text_msg(2, STAFF, CUST, "正常单聊")),
    ]
    client, conn = _make_env(tmp_path, items)
    try:
        assert sync_mod.sync_once(client, PRIVATE_PEM, CORP_ID) == 1
        rows = _rows(conn)
        assert [r["content"] for r in rows] == ["正常单聊"]
        assert all(not r["content"].startswith("群聊") for r in rows)
    finally:
        set_msgaudit_conn(None)
        conn.close()


def test_sync_once_skips_customer_initiated_group_message(tmp_path):
    """客户在群里发的消息(群聊)同样不入库"""
    group_msg = _text_msg(1, CUST, "", "客户在群里问", roomid="wr_group_002")
    client, conn = _make_env(tmp_path, [_envelope(1, group_msg)])
    try:
        assert sync_mod.sync_once(client, PRIVATE_PEM, CORP_ID) == 0
        assert _rows(conn) == []
    finally:
        set_msgaudit_conn(None)
        conn.close()


def test_sync_once_skips_multi_recipient_messages(tmp_path):
    """tolist 过滤后对端 != 1(群发/多对端)→ 保守跳过不入库"""
    multi = _text_msg(1, STAFF, "", "同时发给两个人",
                      tolist=[{"id": CUST, "type": "single"}, {"id": STAFF2, "type": "single"}])
    client, conn = _make_env(tmp_path, [_envelope(1, multi)])
    try:
        assert sync_mod.sync_once(client, PRIVATE_PEM, CORP_ID) == 0
        assert _rows(conn) == []
    finally:
        set_msgaudit_conn(None)
        conn.close()


def test_sync_once_single_chat_both_directions_still_persisted(tmp_path):
    """加固回归:1v1 单聊双向(员工→客户 / 客户→员工)仍正常入库"""
    items = [
        _envelope(1, _text_msg(1, STAFF, CUST, "员工发给客户")),
        _envelope(2, _text_msg(2, CUST, STAFF, "客户回给员工")),
    ]
    client, conn = _make_env(tmp_path, items)
    try:
        assert sync_mod.sync_once(client, PRIVATE_PEM, CORP_ID) == 2
        rows = _rows(conn)
        assert [r["from_role"] for r in rows] == ["staff", "customer"]
        assert all(r["external_userid"] == CUST for r in rows)
    finally:
        set_msgaudit_conn(None)
        conn.close()


# ---------- 7. CtypesChatArchiveClient load 失败 ----------

def test_ctypes_client_empty_sdk_path_raises():
    with pytest.raises(MsgAuditError):
        CtypesChatArchiveClient(sdk_path="", corp_id="c", secret="s")


def test_ctypes_client_bad_sdk_path_raises():
    with pytest.raises(MsgAuditError):
        CtypesChatArchiveClient(sdk_path="/nonexistent/libWeWorkFinanceSdk_C.so", corp_id="c", secret="s")


# ---------- 7b. CtypesChatArchiveClient FreeData 内存释放 ----------

class _FakeSdkFunc:
    """可赋 argtypes/restype 的 fake 函数对象(模拟 ctypes 函数指针)"""

    def __init__(self, fn):
        self._fn = fn
        self.argtypes = None
        self.restype = None

    def __call__(self, *args):
        return self._fn(*args)


class _FakeSdkLib:
    """fake CDLL:模拟官方 SDK 入口,记录调用次数验证 FreeData 配对"""

    def __init__(self, payload: bytes):
        self.payload = payload
        self.init_calls = 0
        self.get_calls = 0
        self.free_calls = 0
        self.Init = _FakeSdkFunc(self._init)
        self.GetChatData = _FakeSdkFunc(self._get)
        self.Destroy = _FakeSdkFunc(self._destroy)
        self.FreeData = _FakeSdkFunc(self._free)

    def _init(self, corp_id, secret):
        self.init_calls += 1
        return 0x1234  # 非 0 句柄

    def _get(self, sdk, seq, limit, proxy, passwd, timeout, out_ptr):
        self.get_calls += 1
        # out_ptr 是 byref(c_char_p) 包装;经 _obj 回写真实 c_char_p 字段
        # (真实 SDK 内部写指针,单测以等价方式写入以便实现读取)
        out_ptr._obj.value = self.payload
        return 0

    def _destroy(self, sdk):
        return 0

    def _free(self, sdk, ptr):
        self.free_calls += 1
        return 0


def test_ctypes_client_frees_data_after_each_get(monkeypatch):
    """FreeData 与 GetChatData 一一配对:每次成功拉取后必须释放 SDK 缓冲(防内存泄漏)"""
    import json as _json

    import app.wecom.msgaudit as ma

    lib = _FakeSdkLib(_json.dumps({"chatdata": []}).encode("utf-8"))
    monkeypatch.setattr(ma.ctypes, "CDLL", lambda path: lib)
    client = ma.CtypesChatArchiveClient(sdk_path="/fake/sdk.so", corp_id="c", secret="s")
    for _ in range(3):
        assert client.get_chat_data(0, 10) == []
    assert lib.get_calls == 3
    assert lib.free_calls == 3  # 每次成功 GetChatData 后恰一次 FreeData
    client.destroy()


def test_ctypes_client_frees_data_even_when_parse_fails(monkeypatch):
    """解析失败(JSON 非法)也必须 FreeData:释放逻辑在 finally,不因异常跳过"""
    import app.wecom.msgaudit as ma

    lib = _FakeSdkLib(b"not-json{")
    monkeypatch.setattr(ma.ctypes, "CDLL", lambda path: lib)
    client = ma.CtypesChatArchiveClient(sdk_path="/fake/sdk.so", corp_id="c", secret="s")
    with pytest.raises(MsgAuditError):
        client.get_chat_data(0, 10)
    assert lib.get_calls == 1
    assert lib.free_calls == 1
    client.destroy()


# ---------- 8. run_sync_loop / start_sync_task ----------

class _ExplodingClient:
    """每次拉取都抛异常,验证轮询循环不因单次异常退出"""

    def __init__(self):
        self.calls = 0

    def get_chat_data(self, seq: int, limit: int) -> list[dict]:
        self.calls += 1
        raise RuntimeError("sdk boom")


def test_run_sync_loop_survives_exceptions_and_repeats():
    client = _ExplodingClient()

    async def scenario():
        task = asyncio.create_task(
            sync_mod.run_sync_loop(client, PRIVATE_PEM, CORP_ID, poll_interval_s=0.01))
        await asyncio.sleep(0.08)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    assert client.calls >= 2  # 异常被捕获记日志,下个周期继续重试


def test_start_sync_task_disabled_returns_none(monkeypatch):
    """WECOM_SID_ENABLED=false(默认)→ 不启动同步任务(降级语义)"""
    monkeypatch.setattr(sync_mod, "wecom_config", WecomConfig())  # sid_enabled 默认 False
    assert sync_mod.start_sync_task(FastAPI()) is None


def test_start_sync_task_enabled_degrades_without_sdk(monkeypatch):
    """开关开启但缺 SDK/私钥 → MsgAuditError 由调用方降级,返回 None 不崩进程"""
    monkeypatch.setattr(sync_mod, "wecom_config", WecomConfig(sid_enabled=True))
    assert sync_mod.start_sync_task(FastAPI()) is None


def test_start_sync_task_enabled_starts_loop_with_injected_client(monkeypatch, tmp_path):
    """开关开启 + 注入 client/私钥 → 返回 asyncio.Task 并真实落库(验证 lifespan 挂载行为)"""
    conn = sqlite3.connect(str(tmp_path / "loop.db"), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    ensure_wecom_tables(conn)
    set_msgaudit_conn(conn)
    client = FakeChatArchiveClient([_envelope(1, _text_msg(1, STAFF, CUST, "loop 落库"))])
    monkeypatch.setattr(sync_mod, "wecom_config",
                        WecomConfig(sid_enabled=True, corp_id=CORP_ID, poll_interval_s=0))
    try:
        async def scenario():
            task = sync_mod.start_sync_task(FastAPI(), client=client, private_key_pem=PRIVATE_PEM)
            assert task is not None
            for _ in range(200):
                if len(_rows(conn)) == 1:
                    break
                await asyncio.sleep(0.01)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        asyncio.run(scenario())
        rows = _rows(conn)
        assert len(rows) == 1
        assert rows[0]["content"] == "loop 落库"
    finally:
        set_msgaudit_conn(None)
        conn.close()
