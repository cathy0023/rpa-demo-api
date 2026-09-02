"""企微侧边栏配置:环境变量集中读取。密钥绝不入库(对齐 app/config.py 范式)。"""
import os
from dataclasses import dataclass, field


def _env_bool(key: str, default: str) -> bool:
    """读布尔环境变量("1"/"true"/"yes" 视为真,不区分大小写)"""
    return os.getenv(key, default).strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class WecomConfig:
    """企微侧边栏全局配置(一次性读取,进程内不变)"""

    # --- 企微基础(企业后台「我的企业」/「应用管理」) ---
    corp_id: str = field(default_factory=lambda: os.getenv("WECOM_CORP_ID", ""))
    agent_id: str = field(default_factory=lambda: os.getenv("WECOM_AGENT_ID", ""))
    app_secret: str = field(default_factory=lambda: os.getenv("WECOM_APP_SECRET", ""))

    # --- 侧边栏安全 ---
    trusted_domain: str = field(default_factory=lambda: os.getenv("WECOM_SID_TRUSTED_DOMAIN", ""))
    cookie_secret: str = field(default_factory=lambda: os.getenv("WECOM_SID_COOKIE_SECRET", ""))
    cookie_secure: bool = field(default_factory=lambda: _env_bool("WECOM_SID_COOKIE_SECURE", "false"))
    sid_enabled: bool = field(default_factory=lambda: _env_bool("WECOM_SID_ENABLED", "false"))

    # --- 会话存档同步 ---
    poll_interval_s: int = field(default_factory=lambda: int(os.getenv("WECOM_SID_POLL_INTERVAL", "5")))
    sdk_path: str = field(default_factory=lambda: os.getenv("WECOM_SID_SDK_PATH", ""))


wecom_config = WecomConfig()


def validate_cookie_secret(cfg: "WecomConfig") -> str | None:
    """校验会话签名密钥:空或长度 <16 返回错误说明,合法返回 None。

    空串密钥可被任何人自签 cookie 伪造会话,必须 fail-closed;16 为 HMAC 密钥的
    最低强度下限(demo 从宽,生产建议 ≥32 字节随机串)。
    """
    secret = cfg.cookie_secret
    if not secret or len(secret) < 16:
        return (
            "WECOM_SID_COOKIE_SECRET 未配置或长度不足 16 字符——空/弱密钥下会话 cookie 可被伪造,"
            "请配置 ≥16 字符随机串"
        )
    return None
