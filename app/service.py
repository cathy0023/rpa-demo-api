"""核心业务编排:回调消息 → 过滤 → 落库 → SSE → LLM → 落库 → SSE → 回发 → SSE

对齐 mgvopen MR 215 rpa_message.go 的语义映射与跳过规则:
- sender==vid 企微号自己发的,跳过(避免"AI 回复 AI"死循环)
- 只处理文本(msgtype 0/2)
- 无话术不落库不发送;发送失败不影响已落库
"""
import logging
import time

from . import db, sse
from .llm import LlmError, generate_reply

logger = logging.getLogger(__name__)

_TEXT_MSGTYPES = {0, 2}


class RpaMessage:
    """一条待处理的回调消息"""

    def __init__(self, team_id: str, msg: dict):
        self.team_id = team_id
        self.msg_id = msg.get("msg_id", 0)
        self.sender = msg.get("sender", 0)
        self.vid = msg.get("vid", 0)
        self.server_id = msg.get("server_id", 0)
        self.content = msg.get("content", "")
        self.msgtype = msg.get("msgtype", 2)
        self.send_time = msg.get("send_time", 0)


def should_skip(msg: RpaMessage) -> str | None:
    """返回跳过原因;None 表示处理"""
    if msg.msgtype not in _TEXT_MSGTYPES:
        return f"非文本消息 msgtype={msg.msgtype}"
    if not msg.content:
        return "空内容"
    if msg.sender == msg.vid:
        return f"企微号自己发的消息 sender={msg.sender} vid={msg.vid}"
    return None


async def handle_rpa_callback(msg: RpaMessage) -> None:
    """处理一次回调消息(完整闭环)。异常内部收敛记日志,不外抛"""
    skip_reason = should_skip(msg)
    if skip_reason:
        logger.info("RpaMessage skip: %s", skip_reason)
        return

    team_id = msg.team_id
    customer_id = str(msg.sender)
    vid = str(msg.vid)

    # 会话聚合:同一客户复用已有会话,避免"发一条就新开一个会话"
    # 已有会话 → 沿用;无 → 用报文 server_id(真实回调用真实会话 ID)
    conv_id = db.find_conversation_by_customer(team_id, customer_id) or str(msg.server_id)

    # 1. 落库:客户 + 会话 + 客户消息(全部按 team_id 隔离)
    db.upsert_customer(team_id, customer_id, vid)
    db.upsert_conversation(team_id, conv_id, customer_id, vid, msg.content)
    db.insert_message(team_id, conv_id, "customer", msg.content, msg.send_time or db.now())

    # 2. SSE:客户消息(带 team_id,前端按团队过滤)
    sse.publish_event({"type": "customer_message", "team_id": team_id, "conversation_id": conv_id,
                       "role": "customer", "content": msg.content})

    # 3. LLM 话术
    try:
        s1 = await generate_reply(msg.content)
    except LlmError as e:
        logger.warning("LLM 生成失败,跳过: %s", e)
        sse.publish_event({"type": "error", "team_id": team_id, "conversation_id": conv_id,
                           "role": "assistant", "content": f"AI 生成失败: {e}"})
        return

    # 4. 落库 AI 话术 + SSE
    db.insert_message(team_id, conv_id, "assistant", s1)
    db.upsert_conversation(team_id, conv_id, customer_id, vid, s1)
    sse.publish_event({"type": "ai_reply", "team_id": team_id, "conversation_id": conv_id,
                       "role": "assistant", "content": s1})

    # 5. 回发客户(走真实 /open-api/message/send)
    try:
        from .rpa_client import send_text_message  # noqa: PLC0415 - 避免无谓启动开销

        await send_text_message(vid, customer_id, s1)
        sse.publish_event({"type": "message_sent", "team_id": team_id, "conversation_id": conv_id,
                           "role": "assistant", "content": s1})
    except Exception as e:  # noqa: BLE001 - 发送失败不影响已落库
        logger.error("RPA 回发失败(已落库): %s", e)
        sse.publish_event({"type": "error", "team_id": team_id, "conversation_id": conv_id,
                           "role": "assistant", "content": f"回发失败: {e}"})
