"""路由聚合:RPA 挂 /api/v1/rpa 前缀(回调地址 https://host/api/v1/rpa/callback/{team_id});
企微侧边栏 router 自带 /api/v1/wecom/sidebar 前缀,与 RPA 并列挂顶层(避免前缀叠加)。"""
from fastapi import APIRouter

from ..wecom.router import api_router as wecom_api_router
from .callback import router as callback_router
from .monitor import router as monitor_router

rpa_router = APIRouter(prefix="/api/v1/rpa")
rpa_router.include_router(callback_router)
rpa_router.include_router(monitor_router)

# 顶层聚合(main.py include 后不加额外前缀,两组路径与既有约定一致)
api_router = APIRouter()
api_router.include_router(rpa_router)
api_router.include_router(wecom_api_router)
