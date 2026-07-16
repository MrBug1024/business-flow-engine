"""Runtime configuration for AI Business Studio."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


PROJECT_ROOT = Path(__file__).resolve().parents[2]
_PLACEHOLDER_KEYS = {"", "your-api-key-here", "sk-xxx", "changeme"}


class Settings(BaseSettings):
    """Configuration loaded from `.env`."""

    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    openai_api_key: str = ""
    openai_base_url: str = "https://api.minimaxi.com/v1"
    llm_model: str = "MiniMax-M2"
    llm_temperature: float = 0.0
    llm_parallel_tool_calls: bool = False
    data_dir: str = "data"

    host: str = "127.0.0.1"
    port: int = 8000

    mcp_public_base_url: str = ""
    mcp_access_token: str = ""

    # Studio owns one shared Python environment outside business workspaces.
    sandbox_provider: str = "venv"
    sandbox_root: str = ""
    sandbox_command_timeout: int = 900
    sandbox_output_limit: int = 128_000
    sandbox_skill_environment_allowlist: dict[str, tuple[str, ...]] = {
        "ocr-parser": (
            "OCR_BASE_URL",
            "OCR_API_KEY",
            "OCR_TIMEOUT_SECONDS",
            "OCR_VERIFY_SSL",
        ),
        "vector-kb": (
            "VECTOR_KB_BASE_URL",
            "VECTOR_KB_LIBRARY_ID",
            "VECTOR_KB_API_KEY",
            "VECTOR_KB_TIMEOUT_SECONDS",
        ),
    }

    @property
    def data_path(self) -> Path:
        path = Path(self.data_dir)
        if not path.is_absolute():
            path = PROJECT_ROOT / path
        path.mkdir(parents=True, exist_ok=True)
        return path

    @property
    def llm_enabled(self) -> bool:
        return self.openai_api_key.strip() not in _PLACEHOLDER_KEYS

    @property
    def env_model_name(self) -> str:
        return self.llm_model.strip() or "local-context-builder"

    @property
    def mcp_base_url(self) -> str:
        return self.mcp_public_base_url.strip().rstrip("/")

    @property
    def sandbox_skill_environment_keys(self) -> dict[str, tuple[str, ...]]:
        """Return a normalized Skill-to-environment allowlist."""

        return {
            skill_name.strip(): tuple(
                sorted({key.strip() for key in keys if key.strip()})
            )
            for skill_name, keys in self.sandbox_skill_environment_allowlist.items()
            if skill_name.strip()
        }

    @property
    def sandbox_root_path(self) -> Path:
        if self.sandbox_root.strip():
            path = Path(self.sandbox_root.strip())
            if not path.is_absolute():
                path = PROJECT_ROOT / path
            return path.resolve()
        return (self.data_path / "business_studio" / "system_sandbox").resolve()


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
