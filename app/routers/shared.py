"""路由共享常量与幂等工具"""
import time

IDEMPOTENT_TTL_S = 24 * 3600

_idempotent: dict[str, int] = {}


def build_idempotent_key(team_id: str, msg_id: int, uuid: str) -> str:
    """幂等 key:按 team 隔离,msg_id 优先,为 0 退化为 uuid"""
    if msg_id != 0:
        return f"{team_id}:{msg_id}"
    return f"{team_id}:u:{uuid}"


def is_idempotent(key: str) -> bool:
    expire_at = _idempotent.get(key)
    if expire_at is None:
        return False
    if expire_at <= time.time():
        del _idempotent[key]  # TTL 过期清理
        return False
    return True


def mark_idempotent(key: str) -> None:
    _idempotent[key] = int(time.time()) + IDEMPOTENT_TTL_S
