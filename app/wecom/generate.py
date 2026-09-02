"""侧边栏话术生成:SIDEBAR_SYSTEM_PROMPT + generate_script(共享 LLM 调用层)。

内部走 llm_shared.call_chat_completion(max_tokens=300, temperature=0.7),
key 未配置/请求失败/空回复统一抛 LlmError,由 /generate 端点转业务错误信封。
"""
from __future__ import annotations

import httpx

from .context import build_prompt
from .llm_shared import call_chat_completion

# 侧边栏系统提示:生成 1 条 ≤200 字、贴合场景、不虚构的中文销售话术
SIDEBAR_SYSTEM_PROMPT = (
    "你是企业微信侧边栏的销售话术助手。根据提供的客户画像与最近对话,生成 1 条中文销售话术:"
    "不超过 200 字,贴合给定使用场景,贴合客户标签与沟通进度;"
    "只基于已知信息,不虚构产品参数、价格或承诺;"
    "直接输出话术正文,不要解释,不要前后缀。"
)


async def generate_script(
    profile: dict,
    history: list[dict],
    scenario: str,
    exclude: str,
    *,
    transport: httpx.BaseTransport | None = None,
) -> str:
    """生成 1 条销售话术。失败/未配 key 抛 LlmError(transport 仅供测试注入)"""
    messages = [
        {"role": "system", "content": SIDEBAR_SYSTEM_PROMPT},
        {"role": "user", "content": build_prompt(profile, history, scenario, exclude)},
    ]
    return await call_chat_completion(messages, max_tokens=300, temperature=0.7, transport=transport)
