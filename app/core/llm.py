"""LLM 工厂。

封装对 OpenAI 兼容接口（默认 MiniMax）的访问。未配置时返回 None，
上层据此自动切换到启发式降级路径。
"""

from __future__ import annotations

from functools import lru_cache
from typing import Optional

from langchain_openai import ChatOpenAI

from app.core.config import settings


@lru_cache(maxsize=1)
def get_llm() -> Optional[ChatOpenAI]:
    """获取「对话用」聊天模型实例（开启流式）；未配置 API Key 时返回 None。"""
    if not settings.llm_enabled:
        return None
    return ChatOpenAI(
        model=settings.llm_model,
        api_key=settings.openai_api_key,
        base_url=settings.openai_base_url,
        temperature=settings.llm_temperature,
        streaming=True,
    )


@lru_cache(maxsize=1)
def get_structured_llm() -> Optional[ChatOpenAI]:
    """获取「结构化生成用」模型实例（非流式）。

    用于在工具内部做关联/流程的结构化推导（`with_structured_output`），
    与对话流分离，结果更稳定。未配置时返回 None，调用方应回退到启发式推导。
    """
    if not settings.llm_enabled:
        return None
    return ChatOpenAI(
        model=settings.llm_model,
        api_key=settings.openai_api_key,
        base_url=settings.openai_base_url,
        temperature=settings.llm_temperature,
        streaming=False,
    )
