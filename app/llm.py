"""LLM 话术生成:直连 OpenAI 兼容接口。未配 key 时抛 LlmError,由上游优雅降级。

HTTP 调用 / 响应解析 / 思维链兼容已提取到 app/wecom/llm_shared.py(共享给企微侧边栏),
本模块保留 RPA 路径的 prompt 组装,行为与提取前完全一致。
"""
import httpx

from .config import config
from .wecom.llm_shared import LlmError, call_chat_completion

SYSTEM_PROMPT = """你是企业微信销售的 AI 助手,负责回复客户的咨询消息。
要求:
1. 用专业、礼貌、简短的中文回复
2. 回复要贴合客户的提问意图
3. 不要超过 80 字
4. 不要虚构事实,不确定的引导客户进一步说明"""

__all__ = ["LlmError", "SYSTEM_PROMPT", "generate_reply"]


async def generate_reply(
    customer_message: str,
    customer_name: str = "",
    *,
    transport: httpx.BaseTransport | None = None,
) -> str:
    """根据客户消息生成话术。失败/空回复抛 LlmError(transport 仅供测试注入)"""
    if not config.llm_api_key:
        raise LlmError("LLM API key 未配置(RPA_DEMO_LLM_API_KEY)")

    prefix = f"客户「{customer_name}」" if customer_name else "客户"
    return await call_chat_completion(
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": f"{prefix}发来消息:{customer_message}"},
        ],
        max_tokens=200,
        temperature=0.7,
        transport=transport,
    )
