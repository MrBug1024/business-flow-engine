"""全局配置。

通过 pydantic-settings 从 `.env` 读取配置；未配置 LLM 时自动降级为启发式推导，
保证项目在无大模型环境下也能完整运行。
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# 项目根目录（app/ 的上一级）
PROJECT_ROOT = Path(__file__).resolve().parent.parent

# 视为「占位符」的 API Key，出现这些值时认为未真正配置 LLM
_PLACEHOLDER_KEYS = {"", "your-api-key-here", "sk-xxx", "changeme"}


class Settings(BaseSettings):
    """应用配置项。环境变量大小写不敏感，对应 `.env` 中的同名键。"""

    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ===== LLM（OpenAI 兼容接口，可指向 MiniMax / 本地 / 代理） =====
    openai_api_key: str = ""
    openai_base_url: str = "https://api.minimaxi.com/v1"
    llm_model: str = "MiniMax-M2"
    llm_temperature: float = 0.0

    # ===== 数据存储 =====
    data_dir: str = "data"

    # ===== 验证通道执行保护 =====
    # 单轮对话的总时长上限（秒）：超过后强制终止本轮并明确告知用户，
    # 避免"长时间无反馈、也无结果"的静默挂死
    verify_turn_timeout: int = 600
    # 心跳间隔（秒）：Agent 超过该时长无任何事件产出时，向前端推送执行状态
    verify_heartbeat_interval: int = 60
    # Agent 工具调用循环的步数上限（langgraph recursion_limit）
    verify_recursion_limit: int = 100

    # ===== 服务监听 =====
    host: str = "127.0.0.1"
    port: int = 8000

    @property
    def data_path(self) -> Path:
        """数据根目录的绝对路径（不存在时自动创建）。"""
        path = Path(self.data_dir)
        if not path.is_absolute():
            path = PROJECT_ROOT / path
        path.mkdir(parents=True, exist_ok=True)
        return path

    @property
    def llm_enabled(self) -> bool:
        """是否已正确配置 LLM。未配置则走启发式降级路径。"""
        return self.openai_api_key.strip() not in _PLACEHOLDER_KEYS


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """获取全局唯一配置实例（带缓存）。"""
    return Settings()


settings = get_settings()
