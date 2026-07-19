"""Stable facade for the durable LangGraph Agent runtime."""

from __future__ import annotations

from collections.abc import Iterator
from functools import lru_cache
from typing import Any

from app.studio.graph_runtime import StudioGraphRuntime
from app.studio.models import AIRun, BusinessRecord


def run_agent(
    record: BusinessRecord,
    run: AIRun,
    *,
    requested_model: str | None = None,
    user_prompt: str | None = None,
    include_history: bool = True,
    resume_payload: dict[str, Any] | None = None,
) -> Iterator[dict[str, Any]]:
    yield from graph_runtime().stream(
        record,
        run,
        requested_model=requested_model,
        user_prompt=user_prompt,
        include_history=include_history,
        resume_payload=resume_payload,
    )


def clear_runtime_thread(
    business_id: str,
    session_id: str,
    run_ids: tuple[str, ...] = (),
) -> None:
    graph_runtime().clear_thread(business_id, session_id, run_ids)


@lru_cache(maxsize=1)
def graph_runtime() -> StudioGraphRuntime:
    return StudioGraphRuntime()
