"""回调接收端点:验签 → 解密 → 解析 → 幂等 → 后台编排。

- 无登录态(RPA 平台直接调用);验签失败 401 / 解密解析失败 400
- 成功恒返 code=2000(RPA 收到即停止重试)
- 回调快回 + 后台 task(LLM 秒级耗时不能阻塞响应触发重推)
- 幂等:(org,msg_id) TTL 24h 去重,防内存无限增长
"""
import asyncio
import logging
import time

from fastapi import APIRouter, Header, Request, Response
from pydantic import ValidationError

from ..config import config
from ..crypto import decrypt_rpa_body
from ..schemas import RpaCallbackRequest
from ..service import RpaMessage, handle_rpa_callback
from .shared import build_idempotent_key, is_idempotent, mark_idempotent

logger = logging.getLogger(__name__)
router = APIRouter()

TIME_WINDOW_S = 300       # 时间窗 ±5 分钟防重放
MAX_BODY_BYTES = 2 << 20  # 密文 body 上限


def _in_window(timestamp: str) -> bool:
    """时间窗校验。兼容毫秒级时间戳(企销宝实际发毫秒,MR215 2038717)"""
    try:
        ts = int(timestamp)
    except ValueError:
        return False
    if ts > 1e12:
        ts = ts // 1000  # 毫秒 → 秒
    now = int(time.time())
    return abs(now - ts) <= TIME_WINDOW_S


def _validate_team_id(team_id: str) -> bool:
    """团队标识白名单:小写字母数字中划线,1-32 位(防路径注入/目录穿越)"""
    return bool(team_id) and len(team_id) <= 32 and all(
        c.isalnum() or c in "-_" for c in team_id
    )


async def handle_encrypted(team_id: str, body: bytes, timestamp: str, nonce: str, sign: str,
                           aes_key: str, app_secret: str):
    """完整回调处理。team_id 从 URL 路径解析。"""
    try:
        plain = decrypt_rpa_body(body, aes_key, app_secret, timestamp, nonce, sign)
    except ValueError as e:
        # 决定性诊断: 对比"对方发来的签名"vs"我们用服务器当前密钥重算的签名",
        # 可判断 401 根因是密钥不匹配(两值不同)还是算法不匹配(相同却不通过)。
        from ..crypto import create_sign  # noqa: PLC0415 - 仅失败路径引用

        ours = create_sign(app_secret, body, timestamp, nonce)
        logger.warning(
            "verify failed team=%s ts=%s nonce=%s incoming_sign=%s ours=%s body_len=%d",
            team_id, timestamp, nonce, sign[:40], ours[:40], len(body),
        )
        return Response(status_code=401, content="verify failed")
    except Exception as e:  # noqa: BLE001 - 解密/解压异常统一 400
        # 解密/解压失败: 保存原始密文供离线分析(AES密钥/模式/zstd差异定位用)
        logger.warning(
            "decrypt error team=%s ts=%s nonce=%s incoming_sign=%s body_len=%d err=%s (saved body for analysis)",
            team_id, timestamp, nonce, sign[:40], len(body), e,
        )
        try:
            from pathlib import Path

            Path("/tmp/rpa_bodies").mkdir(exist_ok=True)
            Path(f"/tmp/rpa_bodies/body_{timestamp}_{nonce[:8]}.bin").write_bytes(body)
        except Exception as save_err:  # noqa: BLE001
            logger.warning("failed to save body: %s", save_err)
        return Response(status_code=400, content="decrypt failed")

    try:
        req = RpaCallbackRequest.model_validate_json(plain)
    except ValidationError as e:
        logger.warning("parse failed: %s", e)
        return Response(status_code=400, content="invalid payload")

    key = build_idempotent_key(team_id, req.payload.msg_id, req.uuid)
    if is_idempotent(key):
        return {"code": 2000, "message": "OK"}

    mark_idempotent(key)
    asyncio.create_task(handle_rpa_callback(RpaMessage(team_id, req.payload.model_dump())))
    return {"code": 2000, "message": "OK"}


@router.post("/callback/{team_id}")
async def rpa_callback(team_id: str,
                       request: Request,
                       x_sign: str = Header("", alias="X-Sign"),
                       x_nonce: str = Header("", alias="X-Nonce"),
                       x_timestamp: str = Header("", alias="X-Timestamp")):
    """团队隔离回调: URL /callback/{team_id},团队标识从路径解析"""
    if not _validate_team_id(team_id):
        return Response(status_code=400, content="invalid team_id")
    if not (x_sign and x_nonce and x_timestamp):
        return Response(status_code=401, content="missing headers")
    if not _in_window(x_timestamp):
        return Response(status_code=401, content="timestamp out of window")

    body = await request.body()
    if not body or len(body) > MAX_BODY_BYTES:
        return Response(status_code=400, content="invalid body")

    return await handle_encrypted(team_id, body, x_timestamp, x_nonce, x_sign,
                                  config.callback_aes_key, config.callback_app_secret)
