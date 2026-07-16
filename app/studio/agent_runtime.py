"""Shared model gateway and prompt assembly for the LangGraph Agent runtime."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.studio.capability_runtime import Capability
from app.studio.llm import stream_model_turn
from app.studio.models import BusinessRecord
from app.studio.registry import list_skills
from app.studio.storage import store


def _system_prompt(record: BusinessRecord, capabilities: list[Capability]) -> str:
    """Describe the runtime contract without prescribing a business workflow."""

    capability_lines = "\n".join(
        f"- {item.kind.upper()} `{item.function_name}` ({item.display_name}): {item.description}"
        for item in capabilities
    ) or "- No callable capabilities are currently mounted."
    workspace_root = store.workspace_dir(record.id).resolve()
    workspace_files = []
    for item in record.files:
        try:
            relative = Path(item.storage_path).resolve().relative_to(workspace_root)
            runtime_path = f"/workspace/{relative.as_posix()}"
        except (OSError, ValueError):
            runtime_path = item.storage_path
        workspace_files.append(
            {
                "id": item.id,
                "name": item.filename,
                "path": runtime_path,
                "size": item.size,
                "status": item.parse_status,
            }
        )
    context = record.context.model_dump(mode="json")
    context["versions"] = context.get("versions", [])[-3:]
    return f"""You are the Agent inside AI Business Studio. Work like an AI coding editor:
understand the user's current objective, inspect the active workspace, and decide
which mounted capabilities are needed. Match the language used by the user.

Runtime contract:
- The platform supplies only execution, storage, streaming, Tool discovery, Skill
  discovery, and MCP connectivity. It does not encode a business workflow for you.
- Tools come only from Python modules discovered under the project `tools/` directory.
- Skills are mounted independently by DeepAgents SkillsMiddleware from standard
  `system_skills/<name>/SKILL.md` packages. Follow the middleware's progressive
  disclosure instructions; a Skill is not a Tool and has no `skill__*` function.
- Every enabled Skill is available as one complete, read-only directory at
  `/skills/<name>/`, including its scripts, references, assets, and dependency files.
- Filesystem and command execution are Studio sandbox capabilities supplied through
  DeepAgents. The writable project is `/workspace`; `python` and `python -m pip`
  always resolve to Studio's system-level managed venv outside the business workspace,
  never to the ambient system Python environment.
- After activating a Skill, resolve all relative resource paths from that Skill's
  `/skills/<name>/` directory. Use absolute Skill paths or explicitly `cd` into that
  directory before running its documented commands.
- MCP tools come only from enabled, successfully probed MCP server configurations.
- Never claim that a Tool, Skill, or MCP call happened unless its call returned success.
- Reading SKILL.md only loads guidance. Report the actual filesystem, command, Tool, or
  MCP result that completed the work, and say explicitly when execution is unavailable.
- Do not invent unavailable capabilities or silently replace them with assumptions.
- Keep the final answer concise, use clear Markdown, and distinguish completed work
  from anything still requiring user input.

Active workspace:
- Business ID: {record.id}
- Name: {record.name}
- Goal: {record.goal or '(not set)'}
- Description: {record.description or '(not set)'}
- Writable workspace root: `/workspace`
- Read-only Skill packages root: `/skills`
- Files: {json.dumps(workspace_files, ensure_ascii=False, default=str)[:12000]}

Current persisted context:
```json
{json.dumps(context, ensure_ascii=False, default=str)[:20000]}
```

Mounted capabilities:
{capability_lines}
"""


def _skill_instructions() -> str:
    """Return a compact catalog; full instructions are loaded on demand."""

    return "\n".join(f"- {skill.name}: {skill.description}" for skill in list_skills())


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


__all__ = ["_safe_arguments", "_skill_instructions", "_system_prompt", "stream_model_turn"]
