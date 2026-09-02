"""共享 chat/completions 调用层单测:mock httpx(MockTransport),不访问真实 LLM

覆盖:
1. 正常 choices[0].message.content 返回 + 请求载荷(Authorization/model/temperature/max_tokens)
2. content=null 思维链兼容:str 与 list[dict] 两种形态 × provider_specific_fields / 消息顶层两级回退
3. HTTP 错误 / 网络错误 / 空回复 / 响应格式异常 → LlmError
4. API key 未配置 → LlmError 且不发起请求
5. generate_reply(RPA 路径)行为不变:消息构造/超参/key 检查/思维链兼容与提取前一致
"""
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx
import pytest

import app.llm as app_llm
import app.wecom.llm_shared as llm_shared
from app.config import RpaDemoConfig
from app.wecom.llm_shared import LlmError, call_chat_completion

OK_CONTENT = "好的,马上为您处理。"


@pytest.fixture
def llm_cfg(monkeypatch):
    """注入测试 LLM 配置(冻结 dataclass,整体替换 llm_shared 模块绑定)"""
    cfg = RpaDemoConfig(
        llm_api_key="sk-test",
        llm_base_url="http://llm.mock/v1",
        llm_model="test-model",
        llm_timeout_s=5,
    )
    monkeypatch.setattr(llm_shared, "config", cfg)
    return cfg


def _post_handler(box: dict, status: int = 200, body: dict | None = None):
    """记录请求并按 OpenAI 协议返回响应"""

    def handler(request: httpx.Request) -> httpx.Response:
        box["request"] = request
        box["json"] = json.loads(request.content)
        if body is None:
            return httpx.Response(200, json={"choices": [{"message": {"content": OK_CONTENT}}]})
        return httpx.Response(status, json=body)

    return handler


def _thinking_body(content, psf: dict | None = None, top: dict | None = None) -> dict:
    """构造思维链模型响应体:content 可为 null,思维链文本放 psf(一级)或消息顶层(二级)"""
    msg: dict = {"content": content}
    if psf:
        msg["provider_specific_fields"] = psf
    msg.update(top or {})
    return {"choices": [{"message": msg}]}


def _call(body_handler, **kwargs) -> str:
    return asyncio.run(call_chat_completion(
        kwargs.pop("messages", [{"role": "user", "content": "你好"}]),
        transport=httpx.MockTransport(body_handler),
        **kwargs,
    ))


# --- 1. 正常路径与请求载荷 ---

def test_normal_content_return(llm_cfg):
    box = {}
    reply = _call(_post_handler(box), max_tokens=200, temperature=0.7)
    assert reply == OK_CONTENT
    req = box["request"]
    assert str(req.url) == "http://llm.mock/v1/chat/completions"
    assert req.headers["Authorization"] == "Bearer sk-test"
    assert box["json"] == {
        "model": "test-model",
        "messages": [{"role": "user", "content": "你好"}],
        "temperature": 0.7,
        "max_tokens": 200,
    }


def test_params_passthrough(llm_cfg):
    box = {}
    _call(_post_handler(box), max_tokens=100, temperature=0.3)
    assert box["json"]["max_tokens"] == 100
    assert box["json"]["temperature"] == 0.3


# --- 2. 思维链兼容(content=null / 空白)---

@pytest.mark.parametrize("content", [None, "   "])
def test_content_null_psf_thinking_blocks_list_of_dicts(content, llm_cfg):
    """list[dict] 形态:各 block 的 text 拼接"""
    body = _thinking_body(content, psf={"thinking_blocks": [
        {"type": "thinking", "text": "用户问的是"}, {"text": "退款流程"},
    ]})
    assert _call(lambda req: httpx.Response(200, json=body)) == "用户问的是退款流程"


def test_content_null_top_level_blocks_list_with_raw_entries(llm_cfg):
    """顶层 list[dict],非 dict 项转 str"""
    body = _thinking_body(None, top={"reasoning_content": ["纯文本段", {"text": "加一段"}]})
    assert _call(lambda req: httpx.Response(200, json=body)) == "纯文本段加一段"


@pytest.mark.parametrize("key", ["thinking_blocks", "thinking", "reasoning_content", "reasoning"])
def test_content_null_psf_str_fallback(key, llm_cfg):
    """str 形态 × provider_specific_fields 一级回退(全 4 个兼容 key)"""
    body = _thinking_body(None, psf={key: "  推理文本  "})
    assert _call(lambda req: httpx.Response(200, json=body)) == "推理文本"


@pytest.mark.parametrize("key", ["thinking_blocks", "thinking", "reasoning_content", "reasoning"])
def test_content_null_top_level_str_fallback(key, llm_cfg):
    """str 形态 × 消息顶层二级回退(provider_specific_fields 缺失时)"""
    body = _thinking_body(None, top={key: "顶层推理"})
    assert _call(lambda req: httpx.Response(200, json=body)) == "顶层推理"


def test_content_takes_precedence_over_thinking(llm_cfg):
    """content 非空时不读思维链字段"""
    body = _thinking_body("  正式回复  ", psf={"thinking": "思维链不应被采用"})
    assert _call(lambda req: httpx.Response(200, json=body)) == "正式回复"


# --- 3. 错误路径 ---

def test_http_status_error_raises_llm_error(llm_cfg):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "boom"})

    with pytest.raises(LlmError, match="LLM 请求失败"):
        _call(handler)


def test_network_error_raises_llm_error(llm_cfg):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    with pytest.raises(LlmError, match="LLM 请求失败"):
        _call(handler)


def test_empty_reply_raises_llm_error(llm_cfg):
    body = {"choices": [{"message": {"content": None}}]}
    with pytest.raises(LlmError, match="空回复"):
        _call(lambda req: httpx.Response(200, json=body))


@pytest.mark.parametrize("body", [{}, {"choices": []}, {"choices": [{"message": "not-a-dict"}]}])
def test_malformed_response_raises_llm_error(body, llm_cfg):
    with pytest.raises(LlmError, match="格式异常"):
        _call(lambda req: httpx.Response(200, json=body))


# --- 4. key 未配置 ---

def test_missing_api_key_raises_and_no_request(monkeypatch):
    monkeypatch.setattr(llm_shared, "config", RpaDemoConfig(llm_api_key=""))
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={})

    with pytest.raises(LlmError, match="未配置"):
        _call(handler)
    assert calls == []


# --- 5. RPA 路径(generate_reply)行为不变 ---

def test_llm_error_reexport_is_same_class():
    """service.py `from .llm import LlmError` 的 except 捕获必须命中同一异常类"""
    assert app_llm.LlmError is LlmError


def test_generate_reply_messages_and_params_unchanged(monkeypatch, llm_cfg):
    monkeypatch.setattr(app_llm, "config", llm_cfg)
    box = {}
    reply = asyncio.run(app_llm.generate_reply(
        "这个能开发票吗", "张三", transport=httpx.MockTransport(_post_handler(box)),
    ))
    assert reply == OK_CONTENT
    req = box["request"]
    assert str(req.url) == "http://llm.mock/v1/chat/completions"
    assert req.headers["Authorization"] == "Bearer sk-test"
    assert box["json"] == {
        "model": "test-model",
        "messages": [
            {"role": "system", "content": app_llm.SYSTEM_PROMPT},
            {"role": "user", "content": "客户「张三」发来消息:这个能开发票吗"},
        ],
        "temperature": 0.7,
        "max_tokens": 200,
    }


def test_generate_reply_prefix_without_name(monkeypatch, llm_cfg):
    monkeypatch.setattr(app_llm, "config", llm_cfg)
    box = {}
    asyncio.run(app_llm.generate_reply("在吗", transport=httpx.MockTransport(_post_handler(box))))
    assert box["json"]["messages"][1] == {"role": "user", "content": "客户发来消息:在吗"}


def test_generate_reply_thinking_model_compat_kept(monkeypatch, llm_cfg):
    """RPA 路径的思维链兼容行为保持(原 llm.py 场景)"""
    monkeypatch.setattr(app_llm, "config", llm_cfg)
    body = _thinking_body(None, psf={"thinking_blocks": [{"text": "想一下"}, {"text": "再回复"}]})

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=body)

    reply = asyncio.run(app_llm.generate_reply("hi", "李四", transport=httpx.MockTransport(handler)))
    assert reply == "想一下再回复"


def test_generate_reply_missing_key_raises(monkeypatch):
    empty_cfg = RpaDemoConfig(llm_api_key="")
    monkeypatch.setattr(app_llm, "config", empty_cfg)
    monkeypatch.setattr(llm_shared, "config", empty_cfg)
    with pytest.raises(LlmError, match="RPA_DEMO_LLM_API_KEY"):
        asyncio.run(app_llm.generate_reply("hi"))
