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
    # 系统数据（用户/会话等）目录：SQLite 落盘于此，随项目走、不依赖外部数据库
    system_dir: str = "system"

    # ===== 鉴权（JWT + 可插拔 OAuth） =====
    jwt_secret: str = "change-me-in-production-please"
    jwt_expire_hours: int = 168
    # 前端地址（OAuth 回跳目标）；后端对外基址（OAuth 回调拼接用）
    frontend_base_url: str = "http://127.0.0.1:5173"
    oauth_redirect_base: str = "http://127.0.0.1:8000"
    google_client_id: str = ""
    google_client_secret: str = ""
    github_client_id: str = ""
    github_client_secret: str = ""

    # ===== 验证通道执行保护 =====
    # 单轮对话的总时长上限（秒）：超过后强制终止本轮并明确告知用户，
    # 避免"长时间无反馈、也无结果"的静默挂死
    verify_turn_timeout: int = 600
    # 心跳间隔（秒）：Agent 超过该时长无任何事件产出时，向前端推送执行状态
    verify_heartbeat_interval: int = 60
    # Agent 工具调用循环的步数上限（langgraph recursion_limit）
    verify_recursion_limit: int = 200

    # ===== 服务监听 =====
    host: str = "127.0.0.1"
    port: int = 8000

    # ===== MCP 对外交付（第三方远程挂载）=====
    # 第三方宿主（Claude Desktop / Cursor / Cline 等）远程挂载本平台能力时使用的
    # 公网基址。开发/测试留空即可——此时后端按「请求所用的主机地址」（本机 IP / 域名）
    # 自动生成安装链接；正式环境在此填固定域名，如 https://mcp.example.com 。
    mcp_public_base_url: str = ""
    # 可选：远程 MCP 访问令牌。留空则端点公开（与旧版 stdio 交付一致）；
    # 填值后 /api/mcp/* 端点要求携带 `Authorization: Bearer <token>`（生成的配置会自动带上）。
    mcp_access_token: str = ""

    @property
    def mcp_base_url(self) -> str:
        """已配置的对外基址（去掉尾部斜杠）；未配置返回空串。"""
        return self.mcp_public_base_url.strip().rstrip("/")

    @property
    def data_path(self) -> Path:
        """数据根目录的绝对路径（不存在时自动创建）。"""
        path = Path(self.data_dir)
        if not path.is_absolute():
            path = PROJECT_ROOT / path
        path.mkdir(parents=True, exist_ok=True)
        return path

    @property
    def system_path(self) -> Path:
        """系统数据根目录（SQLite 等），不存在时自动创建。"""
        path = Path(self.system_dir)
        if not path.is_absolute():
            path = PROJECT_ROOT / path
        path.mkdir(parents=True, exist_ok=True)
        return path

    @property
    def llm_enabled(self) -> bool:
        """是否已正确配置 LLM。未配置则走启发式降级路径。"""
        return self.openai_api_key.strip() not in _PLACEHOLDER_KEYS

    @property
    def oauth_providers(self) -> dict[str, bool]:
        """哪些 OAuth 提供方已配置（前端据此显示按钮）。"""
        return {
            "google": bool(self.google_client_id and self.google_client_secret),
            "github": bool(self.github_client_id and self.github_client_secret),
        }


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """获取全局唯一配置实例（带缓存）。"""
    return Settings()


settings = get_settings()
