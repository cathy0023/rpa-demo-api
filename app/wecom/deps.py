"""FastAPI 会话守卫依赖:读签名 cookie,verify 失败抛 HTTP 401(统一信封体)。"""
from fastapi import Cookie, HTTPException
from starlette.status import HTTP_401_UNAUTHORIZED

from .auth import verify_session

# 会话 cookie 名(router.py 再导出,测试共用)
SESSION_COOKIE = "wecom_sid"


def _401() -> HTTPException:
    return HTTPException(
        status_code=HTTP_401_UNAUTHORIZED,
        detail={"code": 4001, "message": "未登录或会话已过期", "data": None},
    )


def get_current_staff(wecom_sid: str | None = Cookie(None, alias=SESSION_COOKIE)) -> str:
    """校验会话 cookie,返回 userid;缺失/无效/过期统一 401"""
    if not wecom_sid:
        raise _401()
    # 函数内引用避免与 router 循环导入;未注入时 _active_cfg() 即全局 wecom_config
    from .router import _active_cfg  # noqa: PLC0415 - 避免模块级循环导入

    userid = verify_session(wecom_sid, secret=_active_cfg().cookie_secret)
    if userid is None:
        raise _401()
    return userid
