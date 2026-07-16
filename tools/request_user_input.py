"""Human-in-the-loop control Tool mounted through normal directory discovery."""

from __future__ import annotations

from typing import Any

from langchain_core.tools import tool


@tool(
    description=(
        "Pause the current Agent run when a decision, missing fact, or explicit user "
        "authorization is required. Provide question-specific options when useful; "
        "mark at most one option as recommended."
    ),
)
def request_user_input(
    question: str,
    reason: str = "",
    category: str = "agent_clarification",
    options: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Return a portable request when invoked outside the Studio interrupt adapter."""

    return {
        "question": question,
        "reason": reason,
        "category": category,
        "options": options or [],
    }


request_user_input.metadata = {
    "studio": {
        "protocol": "user_input",
        "retry_safe": True,
    }
}
