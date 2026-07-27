"""Markdown-managed prompt registry for the Studio Agent runtime.

All agent-facing prompts live as ``.md`` files in this package so they can be
reviewed and edited without touching Python code. Use :func:`load_prompt` to
read one; results are cached and the cache can be cleared in tests via
:func:`clear_prompt_cache`.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

PROMPTS_DIR = Path(__file__).resolve().parent


@lru_cache(maxsize=32)
def load_prompt(name: str) -> str:
    """Return the text of prompt ``name`` (with or without the ``.md`` suffix)."""

    filename = name if name.endswith(".md") else f"{name}.md"
    path = PROMPTS_DIR / filename
    return path.read_text(encoding="utf-8").strip()


def clear_prompt_cache() -> None:
    load_prompt.cache_clear()


__all__ = ["PROMPTS_DIR", "clear_prompt_cache", "load_prompt"]
