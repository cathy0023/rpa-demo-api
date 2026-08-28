"""LLM 话术生成:直连 OpenAI 兼容接口。未配 key 时抛 LlmError,由上游优雅降级。"""
import httpx

from .config import config

SYSTEM_PROMPT = """你是企业微信销售的 AI 助手,负责回复客户的咨询消息。
要求:
1. 用专业、礼貌、简短的中文回复
2. 回复要贴合客户的提问意图
3. 不要超过 80 字
4. 不要虚构事实,不确定的引导客户进一步说明"""


class LlmError(Exception):
    """LLM 调用失败"""


async def generate_reply(customer_message: str, customer_name: str = "") -> str:
    """根据客户消息生成话术。失败/空回复抛 LlmError"""
    if not config.llm_api_key:
        raise LlmError("LLM API key 未配置(RPA_DEMO_LLM_API_KEY)")

    prefix = f"客户「{customer_name}」" if customer_name else "客户"
    try:
        async with httpx.AsyncClient(timeout=config.llm_timeout_s) as client:
            resp = await client.post(
                f"{config.llm_base_url}/chat/completions",
                headers={"Authorization": f"Bearer {config.llm_api_key}"},
                json={
                    "model": config.llm_model,
                    "messages": [
                        {"role": "system", "content": SYSTEM_PROMPT},
                        {"role": "user", "content": f"{prefix}发来消息:{customer_message}"},
                    ],
                    "temperature": 0.7,
                    "max_tokens": 200,
                },
            )
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPError as e:
        raise LlmError(f"LLM 请求失败: {e}") from e

    try:
        msg = data["choices"][0]["message"]
        reply = (msg.get("content") or "").strip()
        # 兼容思维链模型(如 zai/glm-5.2 路由到 muse-spark): content 可能为 null,
        # 实际文本在 provider_specific_fields.thinking_blocks / thinking 里
        if not reply:
            fields = msg.get("provider_specific_fields") or {}
            for key in ("thinking_blocks", "thinking", "reasoning_content", "reasoning"):
                blocks = fields.get(key) or msg.get(key)
                if isinstance(blocks, str) and blocks.strip():
                    reply = blocks.strip()
                    break
                if isinstance(blocks, list):
                    parts = [b.get("text", "") if isinstance(b, dict) else str(b) for b in blocks if b]
                    joined = "".join(parts).strip()
                    if joined:
                        reply = joined
                        break
    except (KeyError, IndexError, AttributeError) as e:
        raise LlmError(f"LLM 响应格式异常: {e}") from e

    if not reply:
        raise LlmError("LLM 返回空回复")
    return reply
