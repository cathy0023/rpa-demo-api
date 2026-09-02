"""共享 chat/completions 调用层:OpenAI 兼容接口的 httpx 请求 + 响应解析 + 思维链兼容。

从 app/llm.py 提取,RPA 路径(generate_reply)与企微侧边栏共用;提取不改变原行为。
未配 key / 请求失败 / 空回复 / 格式异常统一抛 LlmError,由上游优雅降级。
"""
from __future__ import annotations

import httpx

from ..config import config

# 兼容思维链模型(如 zai/glm-5.2 路由到 muse-spark): content 可能为 null,
# 实际文本在 provider_specific_fields.thinking_blocks / thinking / reasoning_content / reasoning 里
_THINKING_KEYS = ("thinking_blocks", "thinking", "reasoning_content", "reasoning")


class LlmError(Exception):
    """LLM 调用失败"""


def _extract_reply(msg: dict) -> str:
    """取 content,为空时按思维链兼容顺序提取(两级来源 × 4 个 key,str/list 两形态)"""
    reply = (msg.get("content") or "").strip()
    if not reply:
        fields = msg.get("provider_specific_fields") or {}
        for key in _THINKING_KEYS:
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
    return reply


async def call_chat_completion(
    messages: list[dict],
    max_tokens: int = 200,
    temperature: float = 0.7,
    *,
    transport: httpx.BaseTransport | None = None,
    cfg=None,
) -> str:
    """调 chat/completions 并返回文本回复。失败/空回复抛 LlmError。

    transport/cfg 仅供测试注入(mock httpx / 配置),生产路径不传,默认用全局 config。
    """
    active_cfg = cfg if cfg is not None else config
    if not active_cfg.llm_api_key:
        raise LlmError("LLM API key 未配置(RPA_DEMO_LLM_API_KEY)")
    try:
        async with httpx.AsyncClient(timeout=active_cfg.llm_timeout_s, transport=transport) as client:
            resp = await client.post(
                f"{active_cfg.llm_base_url}/chat/completions",
                headers={"Authorization": f"Bearer {active_cfg.llm_api_key}"},
                json={
                    "model": active_cfg.llm_model,
                    "messages": messages,
                    "temperature": temperature,
                    "max_tokens": max_tokens,
                },
            )
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPError as e:
        raise LlmError(f"LLM 请求失败: {e}") from e

    try:
        reply = _extract_reply(data["choices"][0]["message"])
    except (KeyError, IndexError, AttributeError) as e:
        raise LlmError(f"LLM 响应格式异常: {e}") from e
    if not reply:
        raise LlmError("LLM 返回空回复")
    return reply
