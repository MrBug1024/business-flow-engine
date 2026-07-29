"""Deterministic startup and first-run readiness for Studio capabilities."""

from __future__ import annotations

import threading
from time import time
from typing import Any

from app.studio.capabilities.registry import (
    clear_skill_registry_cache,
    list_skills,
    skill_discovery_issues,
)
from app.studio.capabilities.tools import tool_registry


_READINESS_LOCK = threading.RLock()
_BOOTSTRAPPED = False
_LAST_REFRESHED_AT = 0.0


def refresh_platform_capabilities() -> dict[str, Any]:
    """Refresh import-time registries after the application is fully imported."""

    global _BOOTSTRAPPED, _LAST_REFRESHED_AT
    with _READINESS_LOCK:
        tool_registry.refresh()
        clear_skill_registry_cache()
        list_skills(None)
        _BOOTSTRAPPED = True
        _LAST_REFRESHED_AT = time()
        return capability_readiness()


def ensure_capability_readiness(owner_id: str | None = None) -> dict[str, Any]:
    """Guarantee initialization even when code runs outside FastAPI lifespan."""

    with _READINESS_LOCK:
        if not _BOOTSTRAPPED:
            refresh_platform_capabilities()
        return capability_readiness(owner_id)


def capability_readiness(owner_id: str | None = None) -> dict[str, Any]:
    """Return discovery and single-responsibility status without refreshing I/O."""

    tools = tool_registry.list()
    skills = list_skills(owner_id)
    skill_errors = skill_discovery_issues(owner_id)
    tool_errors = [
        {"name": item.name, "source": item.source, "error": item.error or item.status}
        for item in tools
        if item.status != "ready"
    ]
    undeclared_tools = [
        item.name
        for item in tools
        if item.record_type == "tool" and item.mounted and item.contract_status != "declared"
    ]
    undeclared_skills = [
        {"name": item.name, "kind": item.kind}
        for item in skills
        if item.contract_status != "declared"
    ]
    undeclared_system_skills = [
        item["name"] for item in undeclared_skills if item["kind"] == "system"
    ]

    mcp = _mcp_readiness(owner_id)
    issues: list[dict[str, Any]] = [*tool_errors, *skill_errors]
    if undeclared_tools:
        issues.append(
            {
                "kind": "tool_contract",
                "names": undeclared_tools,
                "error": "Mounted project Tools must declare one capability responsibility.",
            }
        )
    if undeclared_skills:
        issues.append(
            {
                "kind": "skill_contract",
                "skills": undeclared_skills,
                "error": "Every available Skill should declare one capability responsibility.",
            }
        )
    issues.extend(mcp["issues"])
    return {
        "status": "ready" if not issues else "degraded",
        "bootstrapped": _BOOTSTRAPPED,
        "refreshed_at": _LAST_REFRESHED_AT,
        "tool_generation": tool_registry.generation,
        "tools": {
            "mounted": sum(1 for item in tools if item.mounted),
            "errors": len(tool_errors),
            "undeclared_contracts": undeclared_tools,
        },
        "skills": {
            "available": len(skills),
            "errors": len(skill_errors),
            "system": sum(1 for item in skills if item.kind == "system"),
            "user": sum(1 for item in skills if item.kind == "user"),
            "undeclared_contracts": undeclared_skills,
            "undeclared_system_contracts": undeclared_system_skills,
        },
        "mcp": {key: value for key, value in mcp.items() if key != "issues"},
        "issues": issues,
    }


def _mcp_readiness(owner_id: str | None) -> dict[str, Any]:
    if owner_id is None:
        return {"enabled_servers": 0, "ready_servers": 0, "tools": 0, "issues": []}

    from app.studio.settings import studio_settings

    enabled = [
        item
        for item in studio_settings.load(owner_id).mcp_configs
        if isinstance(item, dict) and item.get("enabled")
    ]
    ready = []
    issues = []
    tool_count = 0
    for entry in enabled:
        name = str(entry.get("name") or "mcp")
        config = entry.get("config") if isinstance(entry.get("config"), dict) else {}
        tools = config.get("tools") if isinstance(config.get("tools"), list) else []
        if config.get("tools_discovered") is True:
            ready.append(entry)
            tool_count += len(tools)
            continue
        issues.append(
            {
                "kind": "mcp_discovery",
                "name": name,
                "error": "Enabled MCP server has no validated tools snapshot; test and save it again.",
            }
        )
    return {
        "enabled_servers": len(enabled),
        "ready_servers": len(ready),
        "tools": tool_count,
        "issues": issues,
    }


__all__ = [
    "capability_readiness",
    "ensure_capability_readiness",
    "refresh_platform_capabilities",
]
