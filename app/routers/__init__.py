"""路由聚合:统一挂载到 /api/v1/rpa 前缀(回调地址 https://host/api/v1/rpa/callback/{team_id})"""
from fastapi import APIRouter

from .callback import router as callback_router
from .monitor import router as monitor_router

api_router = APIRouter(prefix="/api/v1/rpa")
api_router.include_router(callback_router)
api_router.include_router(monitor_router)
