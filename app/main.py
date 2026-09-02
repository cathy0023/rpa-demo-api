"""RPA demo 独立后端入口。

启动: uvicorn app.main:app --host 127.0.0.1 --port 8100 --reload
前端(rpa-demo-web, vite 5274)经 proxy 把 /api 转发到此。
"""
import asyncio
import logging
from contextlib import asynccontextmanager, suppress
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.status import HTTP_401_UNAUTHORIZED

from .config import config
from .routers import api_router

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")


def ensure_wecom_cookie_secret(wecom_cfg) -> None:
    """侧边栏启用时校验会话签名密钥;不合规直接拒绝启动(fail-fast)。

    corp_id 非空 = 要用企微侧边栏,空/弱 cookie_secret 可被伪造会话 → RuntimeError;
    corp_id 为空 = 纯 RPA 用法,不依赖侧边栏,不阻塞。
    """
    from .wecom.config import validate_cookie_secret

    if not wecom_cfg.corp_id:
        return
    err = validate_cookie_secret(wecom_cfg)
    if err:
        raise RuntimeError(err)


@asynccontextmanager
async def lifespan(app: FastAPI):
    missing = config.validate()
    if missing:
        logging.getLogger(__name__).warning(
            "缺少配置: %s —— 出站发送与回调验签将失败", ", ".join(missing))
    # 企微侧边栏会话密钥启动校验(corp_id 为空的纯 RPA 用法不阻塞)
    from .wecom.config import wecom_config

    ensure_wecom_cookie_secret(wecom_config)
    # 企微会话存档后台同步(WECOM_SID_ENABLED=false 时不启动,内部降级不抛)
    from .wecom.sync import start_sync_task

    sync_task = start_sync_task(app)  # 持强引用,防任务被 GC 中途回收
    yield
    # 优雅停机:取消后台同步任务并等待其处理 CancelledError,不残留运行中协程
    if sync_task is not None and not sync_task.done():
        sync_task.cancel()
        with suppress(asyncio.CancelledError):
            await sync_task
    from .rpa_client import close as close_rpa_client

    await close_rpa_client()


app = FastAPI(title="rpa-demo-api", version="0.2.0", lifespan=lifespan)

# 前端 dev server 直连场景(vite proxy 已同源,CORS 为直连兜底)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5273", "http://localhost:5274", "http://127.0.0.1:5273", "http://127.0.0.1:5274"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(api_router)


def mount_frontend_dist(app: FastAPI, dist_dir: str | Path = "dist") -> bool:
    """同源部署:./dist 存在时挂载 StaticFiles(html=True)托管前端构建产物。

    - 必须在 include_router 之后调用:FastAPI 按注册顺序匹配,API 前缀命中优先,
      静态兜底其余路径(已用 TestClient 验证 /api/v1/... 与 /sidebar.html 同时可达)
    - dist 不存在跳过(本地 dev 走 vite,不受影响)
    返回是否实际挂载。
    """
    from starlette.staticfiles import StaticFiles

    root = Path(dist_dir)
    if not root.is_dir():
        logging.getLogger(__name__).info("未发现前端构建目录 %s,跳过静态托管(本地 dev 模式)", root)
        return False
    app.mount("/", StaticFiles(directory=str(root), html=True), name="frontend")
    return True


mount_frontend_dist(app, "dist")


# 401 统一信封(app 级 handler,非 APIRouter):WecomAuthError → 顶层 code!=2000 信封,
# 与业务错误 {"code", "message", "data"} 结构一致,前端不再区分 detail 包裹
from .wecom.deps import WecomAuthError, wecom_auth_error_response


@app.exception_handler(WecomAuthError)
async def wecom_auth_error_handler(request: Request, exc: WecomAuthError) -> JSONResponse:
    return wecom_auth_error_response(exc)


@app.get("/healthz")
def healthz():
    return {"status": "ok"}
