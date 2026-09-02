"""FastAPI 会话守卫依赖:读签名 cookie,verify 失败抛 WecomAuthError(401 统一信封)。

fail-closed:cookie_secret 为空/弱密钥时一律 401,不给伪造会话任何机会
(空串 HMAC 仍可运算——攻击者知道密钥为空即可自签合法签名)。
信封:WecomAuthError 经 app 级 exception_handler 转为 HTTP 401 + 顶层
{"code":4001, "message":..., "data":None},不再包 detail。
"""
from fastapi import Cookie

from .auth import verify_session
from .config import validate_cookie_secret

# 会话 cookie 名(router.py 再导出,测试共用)
SESSION_COOKIE = "wecom_sid"


class WecomAuthError(Exception):
    """会话无效/缺失/过期/服务端密钥不合规;由 app 级 handler 统一转 401 信封"""

    def __init__(self, code: int = 4001, message: str = "会话无效或已过期") -> None:
        self.code = code
        self.message = message
        super().__init__(message)


def wecom_auth_error_response(exc: "WecomAuthError") -> "JSONResponse":
    """WecomAuthError → HTTP 401 + 顶层 {"code","message","data"} 信封。

    供 app 级 add_exception_handler 注册(main.py);测试构建的最小 app 也必须注册,
    否则守卫端点的 401 会以未处理异常冒出。
    """
    from fastapi.responses import JSONResponse  # noqa: PLC0415
    from starlette.status import HTTP_401_UNAUTHORIZED

    return JSONResponse(
        status_code=HTTP_401_UNAUTHORIZED,
        content={"code": exc.code, "message": exc.message, "data": None},
    )


def _401() -> WecomAuthError:
    return WecomAuthError()


def get_current_staff(wecom_sid: str | None = Cookie(None, alias=SESSION_COOKIE)) -> str:
    """校验会话 cookie,返回 userid;缺失/无效/过期/服务端密钥不合规统一 401"""
    if not wecom_sid:
        raise _401()
    # 函数内引用避免与 router 循环导入;未注入时 _active_cfg() 即全局 wecom_config
    from .router import _active_cfg  # noqa: PLC0415 - 避免模块级循环导入

    cfg = _active_cfg()
    if validate_cookie_secret(cfg) is not None:
        # 密钥为空/过弱:任何 cookie 都不能通过(fail-closed,防伪造)
        raise _401()
    userid = verify_session(wecom_sid, secret=cfg.cookie_secret)
    if userid is None:
        raise _401()
    return userid
