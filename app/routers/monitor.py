"""监控页端点:SSE 实时事件流 + 会话/消息/客户历史查询,按 team_id 隔离"""
from fastapi import APIRouter, Query
from fastapi.responses import StreamingResponse

from .. import db, sse
from ..schemas import ok

router = APIRouter()


@router.get("/monitor/{team_id}/events")
async def subscribe_monitor_stream(team_id: str):
    """SSE:实时推送 customer_message / ai_reply / message_sent / error(按 team 过滤)"""
    return StreamingResponse(
        sse.event_stream(team_id=team_id),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
    )


@router.get("/monitor/{team_id}/conversations")
async def list_conversations(team_id: str, limit: int = Query(50, ge=1, le=200)):
    return ok(db.list_conversations(team_id, limit))


@router.get("/monitor/{team_id}/conversations/{conversation_id}/messages")
async def list_conversation_messages(team_id: str, conversation_id: str, limit: int = Query(100, ge=1, le=500)):
    return ok(db.list_messages(team_id, conversation_id, limit))


@router.get("/monitor/{team_id}/customers")
async def list_customers(team_id: str, limit: int = Query(50, ge=1, le=200)):
    return ok(db.list_customers(team_id, limit))
