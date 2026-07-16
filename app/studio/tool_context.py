"""Invocation context available to trusted dynamically discovered tools."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class StudioToolContext:
    """Runtime information for one tool call.

    Tools are trusted local Python plugins. They can use this context when they
    need the active workspace without coupling their function signature to the
    Studio's internal models.
    """

    business_id: str
    session_id: str
    run_id: str
    workspace_path: Path
    record: Any
    _save: Callable[[], Any]
    emitted_events: list[dict[str, Any]] = field(default_factory=list)

    def resolve_workspace_path(self, relative_path: str | Path) -> Path:
        """Resolve a path and reject access outside the active workspace."""

        candidate = (self.workspace_path / relative_path).resolve()
        root = self.workspace_path.resolve()
        if candidate != root and root not in candidate.parents:
            raise ValueError("Tool path must stay inside the active workspace.")
        return candidate

    def save(self) -> Any:
        """Persist mutations made to the active record."""

        return self._save()

    def emit(self, event: dict[str, Any]) -> None:
        """Queue a Studio event to publish after the tool call completes."""

        if isinstance(event, dict) and event.get("type"):
            self.emitted_events.append(dict(event))


_CURRENT_TOOL_CONTEXT: ContextVar[StudioToolContext | None] = ContextVar(
    "studio_tool_context",
    default=None,
)


def get_tool_context() -> StudioToolContext:
    """Return the active context or fail clearly outside a model tool call."""

    context = _CURRENT_TOOL_CONTEXT.get()
    if context is None:
        raise RuntimeError("No Studio tool invocation is active.")
    return context


@contextmanager
def bind_tool_context(context: StudioToolContext) -> Iterator[StudioToolContext]:
    token = _CURRENT_TOOL_CONTEXT.set(context)
    try:
        yield context
    finally:
        _CURRENT_TOOL_CONTEXT.reset(token)


__all__ = ["StudioToolContext", "bind_tool_context", "get_tool_context"]
