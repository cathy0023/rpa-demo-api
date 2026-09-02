"""RPA demo 独立后端入口。

启动: uvicorn app.main:app --host 127.0.0.1 --port 8100 --reload
前端(rpa-demo-web, vite 5274)经 proxy 把 /api 转发到此。
"""
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .config import config
from .routers import api_router

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")


@asynccontextmanager
async def lifespan(app: FastAPI):
    missing = config.validate()
    if missing:
        logging.getLogger(__name__).warning(
            "缺少配置: %s —— 出站发送与回调验签将失败", ", ".join(missing))
    # 企微会话存档后台同步(WECOM_SID_ENABLED=false 时不启动,内部降级不抛)
    from .wecom.sync import start_sync_task

    start_sync_task(app)
    yield
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


@app.get("/healthz")
def healthz():
    return {"status": "ok"}
