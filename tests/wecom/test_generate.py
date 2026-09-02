"""企微侧边栏话术生成单测:prompt 组装 + LLM 调用(mock httpx)+ 历史查询 T8 前兼容。

覆盖:
1. build_prompt —— 画像(客户名/公司/标签)入 prompt;历史按「客户:/销售:」行拼装;
   scenario/exclude 非空才拼接;全空仍产出合法 prompt
2. get_recent_history —— wecom_chat_history 表未建(T8 前)返回 [];有表取最近 limit 条
   翻回时间正序并映射角色(sender==external_userid → 客户)
3. generate_script —— mock LLM 载荷(SIDEBAR_SYSTEM_PROMPT/max_tokens=300/temperature=0.7)
   与回复;LLM key 未配置抛 LlmError
"""
import asyncio
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import httpx
import pytest

from app.config import RpaDemoConfig
from app.wecom import llm_shared
from app.wecom.context import build_prompt, get_recent_history, set_conn as context_set_conn
from app.wecom.generate import SIDEBAR_SYSTEM_PROMPT, generate_script
from app.wecom.llm_shared import LlmError

PROFILE = {
    "userid": "wo_customer1",
    "name": "张三丰",
    "remark": "王经理-大客户",
    "company": "武当科技",
    "tags": ["高意向", "VIP"],
    "description": "意向A产品",
}

HISTORY = [
    {"role": "customer", "content": "在吗"},
    {"role": "sales", "content": "您好,请问有什么可以帮您?"},
]


# --- 1. build_prompt ---

def test_build_prompt_includes_profile_fields():
    prompt = build_prompt(PROFILE, [], "", "")
    assert "客户名:张三丰" in prompt
    assert "公司:武当科技" in prompt
    assert "高意向" in prompt and "VIP" in prompt


def test_build_prompt_history_as_customer_sales_lines():
    prompt = build_prompt({}, HISTORY, "", "")
    assert "客户:在吗" in prompt
    assert "销售:您好,请问有什么可以帮您?" in prompt


def test_build_prompt_scenario_appended_only_when_non_empty():
    assert "【使用场景】初次跟进" in build_prompt({}, [], "初次跟进", "")
    assert "【使用场景】" not in build_prompt({}, [], "", "")


def test_build_prompt_exclude_injected_only_when_non_empty():
    prompt = build_prompt({}, [], "", "昨天那条开场白")
    assert "不要与以下内容重复" in prompt
    assert "昨天那条开场白" in prompt
    assert "不要与以下内容重复" not in build_prompt({}, [], "", "")


def test_build_prompt_all_empty_still_valid():
    """画像/历史/场景/排除全空:不报错,仍产出含生成指令的合法 prompt"""
    prompt = build_prompt({}, [], "", "")
    assert isinstance(prompt, str) and prompt.strip()
    assert "话术" in prompt  # 仍含生成指令
    assert "【客户画像】" not in prompt and "【最近对话】" not in prompt


# --- 2. get_recent_history(T8 前表不存在 → 降级空列表)---

def test_get_recent_history_missing_table_returns_empty(tmp_path):
    c = sqlite3.connect(str(tmp_path / "no_history.db"))
    c.row_factory = sqlite3.Row
    context_set_conn(c)
    try:
        assert get_recent_history("wo_customer1", limit=20) == []
    finally:
        context_set_conn(None)
        c.close()


def test_get_recent_history_rows_chronological_with_limit(tmp_path):
    c = sqlite3.connect(str(tmp_path / "with_history.db"))
    c.row_factory = sqlite3.Row
    c.execute(
        "CREATE TABLE wecom_chat_history ("
        " seq INTEGER PRIMARY KEY, msgid TEXT UNIQUE, external_userid TEXT,"
        " sender_userid TEXT, content TEXT, msgtime INTEGER)"
    )
    for i in range(5):
        sender = "wo_customer1" if i % 2 == 0 else "zhangsan"
        c.execute(
            "INSERT INTO wecom_chat_history (seq, msgid, external_userid, sender_userid, content, msgtime)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (i, f"m{i}", "wo_customer1", sender, f"msg{i}", 1000 + i),
        )
    c.commit()
    context_set_conn(c)
    try:
        msgs = get_recent_history("wo_customer1", limit=4)
        # 取 seq 最大的 4 条,翻回时间正序
        assert [m["content"] for m in msgs] == ["msg1", "msg2", "msg3", "msg4"]
        # 角色映射:sender==external_userid → 客户,否则销售
        assert [m["role"] for m in msgs] == ["sales", "customer", "sales", "customer"]
    finally:
        context_set_conn(None)
        c.close()


# --- 3. generate_script ---

@pytest.fixture
def llm_cfg(monkeypatch):
    """注入测试 LLM 配置(冻结 dataclass,整体替换 llm_shared 模块绑定)"""
    cfg = RpaDemoConfig(llm_api_key="sk-test", llm_base_url="http://llm.mock/v1",
                        llm_model="test-model", llm_timeout_s=5)
    monkeypatch.setattr(llm_shared, "config", cfg)
    return cfg


def test_generate_script_calls_llm_with_sidebar_prompt(llm_cfg):
    box = {}

    def handler(request: httpx.Request) -> httpx.Response:
        box["request"] = request
        box["json"] = json.loads(request.content)
        return httpx.Response(200, json={"choices": [{"message": {"content": "张总您好,很高兴为您介绍…"}}]})

    script = asyncio.run(generate_script(
        PROFILE, HISTORY, "初次跟进", "", transport=httpx.MockTransport(handler)))
    assert script == "张总您好,很高兴为您介绍…"
    payload = box["json"]
    assert payload["max_tokens"] == 300
    assert payload["temperature"] == 0.7
    assert payload["messages"][0] == {"role": "system", "content": SIDEBAR_SYSTEM_PROMPT}
    user_content = payload["messages"][1]["content"]
    assert "客户名:张三丰" in user_content
    assert "客户:在吗" in user_content
    assert "【使用场景】初次跟进" in user_content


def test_generate_script_missing_key_raises_llm_error(monkeypatch):
    monkeypatch.setattr(llm_shared, "config", RpaDemoConfig(llm_api_key=""))
    with pytest.raises(LlmError, match="未配置"):
        asyncio.run(generate_script(PROFILE, [], "", ""))
