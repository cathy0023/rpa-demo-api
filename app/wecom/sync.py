"""会话存档后台同步任务:sync_once 幂等落库 + run_sync_loop 轮询 + lifespan 挂载。

- sync_once:从 sync_state 读游标 → client.get_chat_data(seq, BATCH_LIMIT) →
  逐条 RSA/AES 解密 → 外部联系人会话(文本)INSERT OR IGNORE 落 wecom_chat_history(seq
  UNIQUE 支撑幂等,AC5)→ last_seq 推进写回 sync_state;解密失败单条跳过记日志不中断
- run_sync_loop:asyncio 循环,单轮异常捕获 logging.error 不退出(下个 poll 周期重试)
- start_sync_task:lifespan 钩子;WECOM_SID_ENABLED=false 不启动(AC6 降级);
  SDK/私钥缺失等启动异常降级 DisabledChatArchiveClient 并返回 None,不阻断应用启动

from_role 判定与 get_history_records 一致:sender==external_userid → customer,否则 staff。
入库判据(保守):roomid 空(非群聊)+ tolist 过滤后对端恰 1(非群发)+ 对端/sender
为外部联系人(wo/wm/wp 前缀启发式,有自定义 userid 误判局限,见 _is_external)。
"""
import asyncio
import logging
import sqlite3
import threading
import time

from .config import wecom_config
from .msgaudit import (
    ChatArchiveClient,
    DisabledChatArchiveClient,
    MsgAuditError,
    decrypt_msg,
)

logger = logging.getLogger(__name__)

# 官方建议单批 ≤1000;demo 取 100(侧边栏历史默认查 20 条,100 足够且降低解密延迟)
BATCH_LIMIT = 100

_MUTEX = threading.Lock()


def _get_conn() -> sqlite3.Connection:
    """复用 msgaudit 的连接源(测试注入连接优先,生产走 db.py 全局连接,建表幂等)"""
    from .msgaudit import _get_conn as msgaudit_conn  # noqa: PLC0415 - 避免模块级循环导入

    return msgaudit_conn()


def _read_last_seq(conn: sqlite3.Connection, corp_id: str) -> int:
    row = conn.execute("SELECT last_seq FROM sync_state WHERE corp_id=?", (corp_id,)).fetchone()
    return int(row["last_seq"]) if row else 0


def _is_external(userid: str) -> bool:
    """外部联系人 userid 启发式:约定以 wo/wm/wp 开头。

    局限:该前缀是企微默认分配规则,企业自定义 userid 时可能误判(内外部判定不绝对可靠)。
    因此仅作为「roomid 空 + 对端恰 1」单聊判据之上的附加条件,不单独作为入库依据。
    """
    return userid.startswith(("wo", "wm", "wp"))


def _pick_external(from_user: str, tolist_ids: list[str], roomid: str = "") -> str | None:
    """保守单聊判据:仅群外单聊且对端恰为 1 个外部联系人时返回该 external_userid。

    - roomid 非空 → 群聊,一律跳过(群聊非侧边栏场景,防泄漏)
    - tolist 过滤空后对端 != 1 → 群发/多对端,保守跳过
    - 单聊场景下 sender 与唯一对端任一为外部联系人(wo/wm/wp 前缀)才入库;
      前缀启发式有误判风险(见 _is_external 局限),但配合 roomid+单对端判据已收敛
    纯内部会话/群聊/多对端返回 None(不入库)。
    """
    if roomid:
        return None
    peers = [u for u in tolist_ids if u]
    if len(peers) != 1:
        return None
    if _is_external(from_user):
        return from_user
    if _is_external(peers[0]):
        return peers[0]
    return None


def _persist_msg(conn: sqlite3.Connection, corp_id: str, external: str,
                 sender: str, content: str, msg_ts: int, seq: int) -> int:
    """INSERT OR IGNORE 幂等落库(seq UNIQUE),返回实际落库条数(0/1)"""
    from_role = "customer" if sender == external else "staff"
    with _MUTEX:
        cur = conn.execute(
            "INSERT OR IGNORE INTO wecom_chat_history "
            "(corp_id, external_userid, sender_userid, from_role, content, msg_ts, seq) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (corp_id, external, sender, from_role, content, msg_ts, seq),
        )
        conn.commit()
        return cur.rowcount


def _advance_seq(conn: sqlite3.Connection, corp_id: str, last_seq: int) -> None:
    """游标推进写回 sync_state(UPSERT,持久化支撑重启续传)"""
    with _MUTEX:
        conn.execute(
            "INSERT INTO sync_state (corp_id, last_seq, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(corp_id) DO UPDATE SET last_seq=excluded.last_seq, "
            "updated_at=excluded.updated_at",
            (corp_id, last_seq, int(time.time())),
        )
        conn.commit()


def sync_once(client: ChatArchiveClient, private_key_pem: bytes, corp_id: str) -> int:
    """单轮同步:解密落库并推进游标,返回本轮实际落库条数。

    幂等性双保险:客户端按游标增量拉取(respect_seq)+ DB 层 seq UNIQUE OR IGNORE;
    单条解密/解析失败跳过记日志不中断(坏密文只损失该条)。
    """
    conn = _get_conn()
    start_seq = _read_last_seq(conn, corp_id)
    batch = client.get_chat_data(start_seq, BATCH_LIMIT)
    inserted = 0
    last_seq = start_seq
    for item in batch:
        try:
            seq = int(item["seq"])
            msg = decrypt_msg(private_key_pem, item["encrypt_random_key"], item["encrypt_chat_msg"])
            if msg.get("msgtype") != "text":  # 非文本(图片/文件/语音…)暂不落库
                last_seq = max(last_seq, seq)
                continue
            content = msg.get("text", {}).get("content")
            sender = msg.get("from", "")
            tolist_ids = [t.get("id", "") for t in msg.get("tolist", [])]
            external = _pick_external(sender, tolist_ids, roomid=str(msg.get("roomid", "") or ""))
            if external is None:  # 群聊/多对端/纯内部会话,不入库
                last_seq = max(last_seq, seq)
                continue
            if not content:
                logger.warning("sync 跳过缺 content 的文本消息 seq=%s", seq)
            else:
                inserted += _persist_msg(conn, corp_id, external, sender,
                                         str(content), int(msg.get("msgtime", 0)), seq)
            last_seq = max(last_seq, seq)
        except (MsgAuditError, KeyError, ValueError, TypeError) as e:
            logger.error("sync 单条消息处理失败 seq=%s 跳过: %s", item.get("seq"), e)
    _advance_seq(conn, corp_id, last_seq)
    if inserted:
        logger.info("sync corp_id=%s 本轮落库 %d 条(游标 %d→%d)", corp_id, inserted,
                    start_seq, last_seq)
    return inserted


async def run_sync_loop(client: ChatArchiveClient, private_key_pem: bytes, corp_id: str,
                        poll_interval_s: int | None = None) -> None:
    """轮询循环:to_thread 执行 sync_once,sleep 后继续;单轮异常不退出。

    poll_interval_s=0 时仅连续轮询(测试用,落库即返回下轮)。
    """
    interval = wecom_config.poll_interval_s if poll_interval_s is None else poll_interval_s
    logger.info("会话存档同步任务启动 corp_id=%s poll=%ss", corp_id, interval)
    while True:
        try:
            await asyncio.to_thread(sync_once, client, private_key_pem, corp_id)
        except asyncio.CancelledError:
            logger.info("会话存档同步任务停止")
            raise
        except Exception:  # noqa: BLE001 - 单轮失败不中断循环,下个周期重试
            logger.exception("会话存档同步单轮失败,下个周期重试")
        if interval <= 0:
            await asyncio.sleep(0)  # 让出事件循环,紧接下一轮(测试场景)
        else:
            await asyncio.sleep(interval)


def start_sync_task(app, client: ChatArchiveClient | None = None,
                    private_key_pem: bytes | None = None):  # noqa: ANN201 - Task | None
    """lifespan 钩子:返回 asyncio.Task(启用且客户端就绪)或 None(降级不启动)。

    - WECOM_SID_ENABLED=false → None(AC6 降级语义,不触碰 SDK)
    - 启用但缺 SDK 路径/私钥 → MsgAuditError 捕获降级 Disabled,返回 None 不崩进程
    - 测试经 client/private_key_pem 注入 FakeChatArchiveClient;生产由 wecom_config 构建
    """
    cfg = wecom_config
    if not cfg.sid_enabled:
        logger.info("WECOM_SID_ENABLED=false,会话存档同步不启动(降级)")
        return None
    try:
        real_client = client if client is not None else _build_client()
        pem = private_key_pem if private_key_pem is not None else _load_private_key()
        corp_id = cfg.corp_id
    except MsgAuditError as e:
        logger.error("会话存档同步降级为 Disabled: %s", e)
        return None
    return asyncio.create_task(run_sync_loop(real_client, pem, corp_id))


def _build_client() -> ChatArchiveClient:
    """按配置构建拉取客户端;SDK 未配置/加载失败抛 MsgAuditError(由调用方降级)"""
    cfg = wecom_config
    if not cfg.sdk_path:
        return DisabledChatArchiveClient()
    from .msgaudit import CtypesChatArchiveClient  # noqa: PLC0415 - 仅启用时才 import ctypes 路径

    return CtypesChatArchiveClient(sdk_path=cfg.sdk_path, corp_id=cfg.corp_id,
                                   secret=cfg.app_secret)


def _load_private_key() -> bytes:
    """会话存档 RSA 私钥:环境变量注入(PEM 文本),绝不入库(AC7);缺失抛 MsgAuditError"""
    import os  # noqa: PLC0415

    pem = os.getenv("WECOM_SID_MSGAUDIT_PRIVATE_KEY", "").strip()
    if not pem:
        raise MsgAuditError("会话存档 RSA 私钥未配置(WECOM_SID_MSGAUDIT_PRIVATE_KEY)")
    return pem.encode("utf-8")
