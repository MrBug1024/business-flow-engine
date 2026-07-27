"""Shared model gateway and prompt assembly for the LangGraph Agent runtime."""

from __future__ import annotations

from typing import Any

from app.studio.prompts import load_prompt
from app.studio.runtime.llm import stream_model_turn
from app.studio.models import BusinessRecord


CORE_SYSTEM_PROMPT = load_prompt("core_system")
SKILLS_SYSTEM_PROMPT = load_prompt("skills")
CONTEXT_SUMMARY_PROMPT = load_prompt("context_summary")


def _system_prompt(_record: BusinessRecord | None = None) -> str:
    """Return the stable prompt kernel; dynamic state stays discoverable on demand."""

    return CORE_SYSTEM_PROMPT


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


__all__ = [
    "CONTEXT_SUMMARY_PROMPT",
    "CORE_SYSTEM_PROMPT",
    "SKILLS_SYSTEM_PROMPT",
    "_safe_arguments",
    "_system_prompt",
    "stream_model_turn",
]
