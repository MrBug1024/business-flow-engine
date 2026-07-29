"""Strict loader for model-facing Markdown prompt templates."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from string import Formatter
from typing import Any

from app.core.config import PROJECT_ROOT


PROMPTS_ROOT = (PROJECT_ROOT / "prompts").resolve()


@lru_cache(maxsize=32)
def load_prompt(name: str) -> str:
    """Load one non-empty Markdown prompt below the project prompt root."""

    relative = Path(str(name or "").replace("\\", "/"))
    if relative.is_absolute() or relative.suffix.casefold() != ".md" or ".." in relative.parts:
        raise ValueError("Prompt name must be a relative .md path below prompts/.")
    path = (PROMPTS_ROOT / relative).resolve()
    if path == PROMPTS_ROOT or PROMPTS_ROOT not in path.parents:
        raise ValueError("Prompt path escapes prompts/.")
    try:
        content = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError as exc:
        raise RuntimeError(f"Prompt template not found: {relative.as_posix()}") from exc
    if not content:
        raise RuntimeError(f"Prompt template is empty: {relative.as_posix()}")
    return content


def prompt_fields(name: str) -> set[str]:
    """Return named replacement fields declared by a prompt template."""

    fields: set[str] = set()
    for _literal, field_name, _format_spec, _conversion in Formatter().parse(load_prompt(name)):
        if field_name:
            fields.add(field_name)
    return fields


def render_prompt(name: str, /, **values: Any) -> str:
    """Render a prompt only when its named placeholder contract is exact."""

    expected = prompt_fields(name)
    supplied = set(values)
    missing = sorted(expected - supplied)
    unexpected = sorted(supplied - expected)
    if missing or unexpected:
        details: list[str] = []
        if missing:
            details.append("missing=" + ",".join(missing))
        if unexpected:
            details.append("unexpected=" + ",".join(unexpected))
        raise ValueError(f"Prompt values for {name} do not match template: {'; '.join(details)}")
    return load_prompt(name).format_map({key: str(value) for key, value in values.items()})


__all__ = ["PROMPTS_ROOT", "load_prompt", "prompt_fields", "render_prompt"]
