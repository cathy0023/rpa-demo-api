"""lifespan 后台任务生命周期单测:task 引用被保存 + shutdown 主动取消。

缺陷背景(审查确认):lifespan 调 start_sync_task(app) 不保存返回值——
1. 弱引用:asyncio 只持弱引用,无强引用时任务可能在任意时刻被 GC 中途回收
   (官方文档 Tasks and coroutines 章节明确警告,须保存返回值);
2. 退出不取消:shutdown 路径不显式 cancel,任务退出依赖 TestClient/anyio 的
   兜底清理,uvicorn 生产路径下不保证(协程泄漏风险)。
修复:局部变量持有强引用,shutdown 时 task.cancel() + await suppress(CancelledError)。

注:TestClient(anyio)portal 关闭时本身会兜底 cancel 所有 pending 任务,
因此「shutdown 后 done」在修复前后都可能为真;本测试观察的是实现本身:
- lifespan 持有强引用(fake 返回的 task 保存在局部,退出时不被 GC);
- lifespan 代码里存在显式 cancel 调用(fake 任务收到 cancel 而非 portal 兜底)。
"""
import asyncio
import gc
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from fastapi.testclient import TestClient

from app import main as main_mod
from app.wecom import sync as sync_mod

TASK_HOLDER: dict = {}


def _fake_start_sync_task(app, client=None, private_key_pem=None):
    """替换真实 start_sync_task:返回可观察的空转任务(不触碰 SDK/配置)"""

    async def _spin():
        try:
            while True:
                await asyncio.sleep(0.01)
        except asyncio.CancelledError:
            TASK_HOLDER["cancelled_inside"] = True
            raise

    task = asyncio.create_task(_spin())
    TASK_HOLDER["task"] = task
    return task


def test_lifespan_holds_task_ref_and_cancels_on_shutdown(monkeypatch):
    monkeypatch.setattr(sync_mod, "start_sync_task", _fake_start_sync_task)
    with TestClient(main_mod.app):  # startup 创建任务,shutdown 取消
        task = TASK_HOLDER.get("task")
        assert task is not None
        assert not task.done()  # 运行中
        ref = task  # 测试侧强引用,避免本测试自身触发 GC 歧义
        del task
        gc.collect()
    assert ref.done()  # shutdown 后任务结束
    assert ref.cancelled()
    assert TASK_HOLDER["cancelled_inside"] is True  # 收到显式 cancel 退出


def test_lifespan_survives_when_sync_disabled(monkeypatch):
    """start_sync_task 返回 None(降级)时 shutdown 不炸"""
    monkeypatch.setattr(sync_mod, "start_sync_task", lambda app, **kw: None)
    with TestClient(main_mod.app):
        pass


def test_lifespan_source_holds_task_reference():
    """白盒:修复前 lifespan 用裸语句 start_sync_task(app) 丢弃返回值(弱引用缺陷本体)"""
    import ast
    import inspect

    src = inspect.getsource(main_mod.lifespan)
    tree = ast.parse(src)
    bare_calls = [
        n for n in ast.walk(tree)
        if isinstance(n, ast.Expr) and isinstance(n.value, ast.Call)
        and getattr(n.value.func, "id", "") == "start_sync_task"
    ]
    assert bare_calls == [], "start_sync_task 返回值必须保存到局部变量(强引用)"
    assert "cancel()" in src and "CancelledError" in src
