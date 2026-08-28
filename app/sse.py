"""SSE 事件源:按 team_id 隔离广播。订阅者登记 team,事件只推给同 team 的订阅者。"""
import asyncio
import json
import time
from typing import Any

# 订阅者集合: (team_id, asyncio.Queue)
_subscribers: set[tuple[str, asyncio.Queue]] = set()


def publish_event(event: dict[str, Any]) -> None:
    """广播事件给同 team 的订阅者(慢消费者丢弃,避免阻塞)"""
    event.setdefault("ts", int(time.time()))
    team_id = event.get("team_id", "default")
    payload = f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
    for t, q in list(_subscribers):
        if t == team_id:
            try:
                q.put_nowait(payload)
            except asyncio.QueueFull:
                pass


async def event_stream(team_id: str = "1"):
    """SSE 生成器:订阅指定 team 的事件 → 逐条 yield → 断开时清理"""
    q: asyncio.Queue = asyncio.Queue(maxsize=100)
    _subscribers.add((team_id, q))
    try:
        while True:
            try:
                yield await asyncio.wait_for(q.get(), timeout=15)
            except asyncio.TimeoutError:
                yield ": keep-alive\n\n"
    finally:
        _subscribers.discard((team_id, q))
