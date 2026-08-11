"""Shared model gateway and prompt assembly for the LangGraph Agent runtime."""

from __future__ import annotations

from typing import Any

from app.studio.prompt_loader import render_prompt
from app.studio.runtime.llm import stream_model_turn
from app.studio.models import BusinessRecord
from app.studio.distillation_gates import distillation_runtime_context


_EMPTY_CAPABILITY_INDEX = "- Optional Tools: none\n- MCP capabilities: none"
CORE_SYSTEM_PROMPT = render_prompt(
    "agent/core-system.md",
    optional_capability_index=_EMPTY_CAPABILITY_INDEX,
)


def _system_prompt(
    _record: BusinessRecord | None = None,
    *,
    optional_capability_index: str = _EMPTY_CAPABILITY_INDEX,
) -> str:
    """Render the stable prompt policy with a bounded runtime routing index."""

    prompt = render_prompt(
        "agent/core-system.md",
        optional_capability_index=optional_capability_index,
    )
    return prompt + (distillation_runtime_context(_record) if _record is not None else "")


def _safe_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    return {str(key): _redact_value(str(key), value) for key, value in arguments.items()}


def _redact_value(key: str, value: Any) -> Any:
    lowered = key.casefold().replace("-", "_")
    if any(token in lowered for token in ("api_key", "token", "secret", "password", "authorization")):
        return "***"
    if isinstance(value, dict):
        return {str(child): _redact_value(str(child), item) for child, item in value.items()}
    if isinstance(value, list):
        return [_redact_value(key, item) for item in value]
    return value


__all__ = ["CORE_SYSTEM_PROMPT", "_safe_arguments", "_system_prompt", "stream_model_turn"]
