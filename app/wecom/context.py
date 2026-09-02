"""话术生成上下文:侧边栏用户 prompt 组装 + 会话历史查询(T8 前表缺失降级空列表)。

build_prompt 将画像/最近对话/使用场景/排除内容拼为中文用户消息;
get_recent_history 查 wecom_chat_history(T6 建,查不到表返回 [],见 get_recent_history)。
"""
import sqlite3
import threading

from .migrations import ensure_wecom_tables

_MUTEX = threading.Lock()
_conn: sqlite3.Connection | None = None  # 测试注入的独立连接;None 时走 db.py
_db_migrated = False


def set_conn(conn: sqlite3.Connection | None) -> None:
    """测试注入独立 SQLite 连接(生产走 db.py 全局连接);换连接后重走一次建表"""
    global _conn, _db_migrated
    _conn = conn
    _db_migrated = False


def _get_conn() -> sqlite3.Connection:
    """注入连接优先;生产复用 db.py 的连接(WAL 范式)。首次使用确保表已建(幂等)"""
    global _db_migrated
    if _conn is not None:
        conn = _conn
    else:
        from .. import db  # noqa: PLC0415 - 函数内导入避免模块级循环依赖

        conn = db._get_conn()
    if not _db_migrated:
        with _MUTEX:
            if not _db_migrated:  # 双重检查:等锁期间可能已被其他线程建表
                ensure_wecom_tables(conn)
                _db_migrated = True
    return conn


def get_recent_history(external_userid: str, limit: int = 20) -> list[dict]:
    """查最近 limit 条会话消息,翻回时间正序返回 [{role, content}]。

    wecom_chat_history 表 T6/T8 才建——表不存在(sqlite3.OperationalError)或无行时返回 [],
    /generate 在此之前正常降级为仅画像+scenario。sender==external_userid → 客户,否则销售。
    """
    conn = _get_conn()
    try:
        rows = conn.execute(
            "SELECT sender_userid, content FROM wecom_chat_history "
            "WHERE external_userid=? ORDER BY seq DESC LIMIT ?",
            (external_userid, limit),
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    return [
        {
            "role": "customer" if row["sender_userid"] == external_userid else "sales",
            "content": row["content"],
        }
        for row in reversed(rows)
    ]


def build_prompt(profile: dict, history: list[dict], scenario: str, exclude: str) -> str:
    """组装侧边栏话术生成的中文用户消息。

    profile 为 contact.get_contact_profile 的精简画像(可空 dict);
    history 为 [{role: "customer"|"sales", content}],按「客户:/销售:」行拼装;
    scenario/exclude 非空时才拼接对应段。全空时仍产出含生成指令的合法 prompt。
    """
    parts: list[str] = []
    if profile.get("name") or profile.get("company") or profile.get("tags") or profile.get("description"):
        lines = [f"客户名:{profile.get('name', '')}"]
        if profile.get("company"):
            lines.append(f"公司:{profile['company']}")
        if profile.get("tags"):
            lines.append(f"标签:{'、'.join(profile['tags'])}")
        if profile.get("description"):
            lines.append(f"备注:{profile['description']}")
        parts.append("【客户画像】\n" + "\n".join(lines))
    if history:
        lines = [
            f"{'客户' if m.get('role') == 'customer' else '销售'}:{m.get('content', '')}"
            for m in history
        ]
        parts.append("【最近对话】\n" + "\n".join(lines))
    if scenario.strip():
        parts.append(f"【使用场景】{scenario.strip()}")
    if exclude.strip():
        parts.append(f"不要与以下内容重复:\n{exclude.strip()}")

    parts.append("请根据以上信息生成 1 条中文销售话术,不超过 200 字。")
    return "\n\n".join(parts)
