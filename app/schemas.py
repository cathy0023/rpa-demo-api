"""数据模型(与七小饱回调报文 / 发送请求对齐)"""
import json

from pydantic import BaseModel, Field, model_validator


class RpaCallbackJson(BaseModel):
    """回调消息正文(type=102000 文本消息字段)"""

    msg_id: int = Field(0, description="消息id,单条消息唯一标识")
    sender: int = Field(0, description="消息发送人;sender=vid 时代表企微号自己发的")
    receiver: int = Field(0, description="接收人")
    vid: int = Field(0, description="消息归属人(originalUserId,原始企微号id)")
    server_id: int = Field(0, description="服务会话ID,标记消息所属会话")
    content: str = Field("", description="消息内容")
    send_time: int = Field(0, description="消息发送时间戳")
    is_room: int = Field(0, description="是否群聊:0=非群聊 1=群聊")
    msgtype: int = Field(2, description="消息类型:0、2=文本")
    sender_name: str = Field("", description="发送人昵称")
    referid: int = Field(0, description="关联消息ID")


class RpaCallbackRequest(BaseModel):
    """回调报文(解密后的原始结构)。json 为报文保留字段名,别名 payload 规避遮蔽"""

    payload: RpaCallbackJson = Field(..., alias="json", description="消息正文")
    tenant_id: str = Field("", alias="tenantId", description="租户标识")
    type: int = Field(0, description="消息类型码,102000=文本消息")
    uuid: str = Field("", description="消息唯一标识")

    model_config = {"populate_by_name": True}

    @model_validator(mode="before")
    @classmethod
    def _compat_shapes(cls, data):  # noqa: ANN001 - 对齐 MR215 兼容形态
        """兼容企销宝真实回调的两种形态(MR215 278fa66):
        1. 顶层数组 [{...}] → 取第一条
        2. json 字段为字符串内嵌 JSON → 二次解析
        """
        if isinstance(data, list):
            if not data:
                raise ValueError("empty callback body")
            data = data[0]
        if isinstance(data, dict):
            inner = data.get("json")
            if isinstance(inner, str):
                try:
                    data = {**data, "json": json.loads(inner)}
                except json.JSONDecodeError as e:
                    raise ValueError(f"json field is invalid embedded JSON: {e}") from e
        return data


def ok(data=None) -> dict:
    """统一成功响应(code=2000 与平台约定一致)"""
    return {"code": 2000, "message": "OK", "data": data}
