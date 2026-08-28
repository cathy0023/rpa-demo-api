"""rpa_demo 配置:环境变量集中读取。密钥绝不入库(对齐 MR215: 真实值只走部署环境变量)。"""
import os
from dataclasses import dataclass, field


@dataclass(frozen=True)
class RpaDemoConfig:
    """RPA demo 全局配置(一次性读取,进程内不变)"""

    # --- 七小饱 RPA 平台 ---
    rpa_base_url: str = field(default_factory=lambda: os.getenv("RPA_PLATFORM_BASE_URL", "http://192.168.2.153:9080"))
    rpa_app_key: str = field(default_factory=lambda: os.getenv("RPA_PLATFORM_APP_KEY", ""))
    rpa_app_secret: str = field(default_factory=lambda: os.getenv("RPA_PLATFORM_APP_SECRET", ""))
    callback_app_secret: str = field(default_factory=lambda: os.getenv("RPA_PLATFORM_CALLBACK_SECRET", ""))
    callback_aes_key: str = field(default_factory=lambda: os.getenv("RPA_PLATFORM_CALLBACK_AESKEY", ""))

    # --- 通用 LLM(OpenAI 兼容接口) ---
    llm_base_url: str = field(default_factory=lambda: os.getenv("RPA_DEMO_LLM_BASE_URL", "https://api.openai.com/v1"))
    llm_api_key: str = field(default_factory=lambda: os.getenv("RPA_DEMO_LLM_API_KEY", ""))
    llm_model: str = field(default_factory=lambda: os.getenv("RPA_DEMO_LLM_MODEL", "gpt-4o-mini"))
    llm_timeout_s: float = field(default_factory=lambda: float(os.getenv("RPA_DEMO_LLM_TIMEOUT", "30")))

    # --- SQLite ---
    sqlite_path: str = field(default_factory=lambda: os.getenv("RPA_DEMO_SQLITE_PATH", "rpa_demo.db"))

    def validate(self) -> list[str]:
        """必需配置检查,返回缺失项列表"""
        missing = []
        if not self.rpa_base_url:
            missing.append("RPA_PLATFORM_BASE_URL")
        if not self.rpa_app_key:
            missing.append("RPA_PLATFORM_APP_KEY")
        if not self.rpa_app_secret:
            missing.append("RPA_PLATFORM_APP_SECRET")
        if not self.callback_app_secret:
            missing.append("RPA_PLATFORM_CALLBACK_SECRET")
        if not self.callback_aes_key:
            missing.append("RPA_PLATFORM_CALLBACK_AESKEY")
        return missing


config = RpaDemoConfig()
