"""Dynamic discovery and generic execution for Tool, Skill and MCP capabilities."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from dataclasses import dataclass, field
from time import time
from typing import Any, Literal

from langchain_core.tools import BaseTool

from app.studio.capabilities.mcp import call_mcp_tool
from app.studio.models import BusinessRecord, SkillDefinition
from app.studio.settings import studio_settings
from app.studio.storage import new_id, store
from app.studio.runtime.tool_context import StudioToolContext, bind_tool_context
from app.studio.capabilities.tools import tool_registry


CapabilityKind = Literal["tool", "mcp"]
MAX_TOOL_OUTPUT = 32_000
MAX_DISCOVERY_DESCRIPTION = 500


@dataclass(slots=True)
class Capability:
    function_name: str
    display_name: str
    kind: CapabilityKind
    description: str
    input_schema: dict[str, Any]
    config: dict[str, Any] = field(default_factory=dict)
    retry_safe: bool = False
    handler: BaseTool | None = None
    protocol: str = ""
    source: str = ""

    def model_tool(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.function_name,
                "description": self.description,
                "parameters": self.input_schema,
            },
        }


@dataclass(slots=True)
class CapabilityResult:
    output: dict[str, Any]
    summary: str
    emitted_events: list[dict[str, Any]] = field(default_factory=list)


def discover_capabilities(_record: BusinessRecord | None = None) -> list[Capability]:
    """Build the model-visible catalog from the filesystem and saved MCP config."""

    capabilities: list[Capability] = []
    function_names: set[str] = set()
    tool_sources = {
        item.name: item.source
        for item in tool_registry.list()
        if item.status == "ready" and item.mounted
    }

    for installed_tool in tool_registry.get_tools():
        metadata = installed_tool.metadata if isinstance(installed_tool.metadata, dict) else {}
        studio_metadata = metadata.get("studio") if isinstance(metadata.get("studio"), dict) else metadata
        function_name = _unique_function_name(str(installed_tool.name), function_names)
        function_names.add(function_name)
        capabilities.append(
            Capability(
                function_name=function_name,
                display_name=str(installed_tool.name),
                kind="tool",
                description=str(installed_tool.description or installed_tool.name),
                input_schema=installed_tool.get_input_schema().model_json_schema(),
                retry_safe=bool(studio_metadata.get("retry_safe", False)),
                handler=installed_tool,
                protocol=str(studio_metadata.get("protocol") or ""),
                source=tool_sources.get(str(installed_tool.name), "tools"),
            )
        )

    for entry in studio_settings.load().mcp_configs:
        if not entry.get("enabled"):
            continue
        config = entry.get("config") if isinstance(entry.get("config"), dict) else {}
        if config.get("transport") not in {"streamable_http", "stdio"}:
            continue
        if config.get("tools_discovered") is not True:
            continue
        server_name = str(entry.get("name") or config.get("name") or "mcp")
        for remote_tool in config.get("tools") or []:
            if not isinstance(remote_tool, dict):
                continue
            remote_name = str(remote_tool.get("name") or "").strip()
            if not remote_name:
                continue
            input_schema = remote_tool.get("input_schema") or remote_tool.get("inputSchema")
            if not isinstance(input_schema, dict):
                input_schema = {"type": "object", "properties": {}}
            base_name = f"mcp__{_function_slug(server_name)}__{_function_slug(remote_name)}"
            function_name = _unique_function_name(base_name, function_names, f"{server_name}\0{remote_name}")
            function_names.add(function_name)
            capabilities.append(
                Capability(
                    function_name=function_name,
                    display_name=f"{server_name}.{remote_name}",
                    kind="mcp",
                    description=str(
                        remote_tool.get("description")
                        or config.get("description")
                        or f"Call {server_name}.{remote_name} through MCP."
                    ),
                    input_schema=input_schema,
                    config=dict(config) | {
                        "_server_name": server_name,
                        "_remote_tool": remote_name,
                    },
                    retry_safe=bool(remote_tool.get("idempotent", False)),
                    source=server_name,
                )
            )
    return capabilities


def execute_capability(
    capability: Capability,
    record: BusinessRecord,
    arguments: dict[str, Any],
    *,
    run_id: str = "",
    session_id: str = "",
) -> CapabilityResult:
    """Execute one discovered capability without any name-based dispatch."""

    context = StudioToolContext(
        business_id=record.id,
        session_id=session_id,
        run_id=run_id,
        workspace_path=store.workspace_dir(record.id),
        record=record,
        _save=lambda: store.save(record),
    )
    with bind_tool_context(context):
        if capability.kind == "tool":
            result = _execute_tool(capability, arguments)
        else:
            result = _execute_mcp(capability, arguments)
    result.emitted_events.extend(context.emitted_events)
    _record_usage(record, capability, arguments, result)
    store.save(record)
    return result


def result_for_model(result: CapabilityResult) -> str:
    raw = json.dumps(result.output, ensure_ascii=False, default=str)
    if len(raw) <= MAX_TOOL_OUTPUT:
        return raw
    return raw[:MAX_TOOL_OUTPUT] + "\n...[tool output truncated]"


def optional_capability_catalog(
    tool_capabilities: list[Capability],
    mcp_capabilities: list[Capability],
    skills: list[SkillDefinition],
    *,
    kind: str = "all",
    query: str = "",
    limit: int = 10,
    offset: int = 0,
    include_schema: bool = False,
) -> dict[str, Any]:
    """Return a bounded, on-demand Skill/MCP catalog for one model turn."""

    normalized_kind = kind.casefold().strip()
    if normalized_kind not in {"all", "skill", "tool", "mcp"}:
        raise ValueError("kind must be one of: all, skill, tool, mcp")
    safe_limit = max(1, min(int(limit), 20))
    safe_offset = max(0, int(offset))
    entries: list[dict[str, Any]] = []

    if normalized_kind in {"all", "skill"}:
        entries.extend(
            {
                "kind": "skill",
                "name": skill.name,
                "description": _bounded_description(skill.description),
                "instruction_path": skill.location,
            }
            for skill in skills
            if skill.enabled
        )
    if normalized_kind in {"all", "tool"}:
        entries.extend(
            {
                "kind": "tool",
                "name": capability.function_name,
                "display_name": capability.display_name,
                "source": capability.source,
                "description": _bounded_description(capability.description),
                "_capability": capability,
            }
            for capability in tool_capabilities
        )
    if normalized_kind in {"all", "mcp"}:
        entries.extend(
            {
                "kind": "mcp",
                "name": capability.function_name,
                "display_name": capability.display_name,
                "server": capability.source,
                "description": _bounded_description(capability.description),
                "_capability": capability,
            }
            for capability in mcp_capabilities
        )

    terms = [item for item in re.split(r"\s+", query.casefold().strip()) if item]
    if terms:
        ranked: list[tuple[int, dict[str, Any]]] = []
        for entry in entries:
            name_haystack = " ".join(
                str(entry.get(key) or "")
                for key in ("name", "display_name", "server")
            ).casefold()
            description_haystack = str(entry.get("description") or "").casefold()
            score = sum(
                30 if term in name_haystack else 10 if term in description_haystack else 0
                for term in terms
            )
            if not score:
                continue
            exact = query.casefold().strip() in {
                str(entry.get("name") or "").casefold(),
                str(entry.get("display_name") or "").casefold(),
            }
            ranked.append((score + int(exact) * 1_000, entry))
        entries = [entry for _score, entry in sorted(ranked, key=lambda item: -item[0])]
    else:
        entries.sort(key=lambda item: (str(item["kind"]), str(item["name"]).casefold()))

    total = len(entries)
    selected = entries[safe_offset : safe_offset + safe_limit]
    schema_added = False
    public_entries: list[dict[str, Any]] = []
    for entry in selected:
        capability = entry.pop("_capability", None)
        public_entry = dict(entry)
        if include_schema and not schema_added and isinstance(capability, Capability):
            public_entry["input_schema"] = capability.input_schema
            schema_added = True
        public_entries.append(public_entry)
    next_offset = safe_offset + len(public_entries)
    return {
        "items": public_entries,
        "total": total,
        "offset": safe_offset,
        "next_offset": next_offset if next_offset < total else None,
        "schema_included": schema_added,
        "guidance": (
            "Read a Skill's instruction_path before using it. For Tool or MCP, search "
            "narrowly with include_schema=true, then pass its exact name and arguments "
            "to call_tool or call_mcp respectively."
        ),
    }


def _execute_tool(capability: Capability, arguments: dict[str, Any]) -> CapabilityResult:
    if capability.handler is None:
        raise RuntimeError(f"Dynamic tool {capability.display_name!r} is not mounted.")
    raw = capability.handler.invoke(arguments)
    if isinstance(raw, CapabilityResult):
        return raw
    output = _normalize_output(raw)
    studio_result = output.pop("_studio", None)
    emitted: list[dict[str, Any]] = []
    summary = ""
    if isinstance(studio_result, dict):
        summary = str(studio_result.get("summary") or "").strip()
        emitted = [dict(item) for item in studio_result.get("events") or [] if isinstance(item, dict)]
    summary = summary or str(output.get("summary") or "").strip()
    return CapabilityResult(
        output=output,
        summary=summary or f"{capability.display_name} completed",
        emitted_events=emitted,
    )


def _execute_mcp(capability: Capability, arguments: dict[str, Any]) -> CapabilityResult:
    remote_tool = str(capability.config.get("_remote_tool") or "").strip()
    if not remote_tool:
        raise ValueError("MCP capability is missing its discovered remote tool name.")
    output = asyncio.run(call_mcp_tool(capability.config, remote_tool, arguments))
    return CapabilityResult(output=output, summary=f"{capability.display_name} completed")


def _record_usage(
    record: BusinessRecord,
    capability: Capability,
    arguments: dict[str, Any],
    result: CapabilityResult,
) -> None:
    if capability.protocol in {
        "task_progress",
        "user_input",
        "capability_discovery",
        "mcp_gateway",
        "tool_gateway",
    }:
        return
    usage_name = (
        str(capability.config.get("_server_name") or capability.display_name)
        if capability.kind == "mcp"
        else capability.display_name
    )
    usage = {
        "id": new_id("call"),
        "name": usage_name,
        "kind": capability.kind,
        "status": "succeeded",
        "summary": result.summary,
        "created_at": time(),
    }
    if capability.kind == "tool":
        record.context.tool_usages.append(usage | {"tool": capability.display_name})
        return
    remote_tool = str(capability.config.get("_remote_tool") or "")
    existing = next(
        (item for item in record.context.mcp_references if item.get("name") == usage_name),
        None,
    )
    payload = usage | {"tool": remote_tool, "reason": "Called by the Agent."}
    if existing is None:
        record.context.mcp_references.append(payload)
    else:
        existing.update(payload | {"last_tool": remote_tool, "last_called_at": time()})


def _normalize_output(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if hasattr(value, "model_dump"):
        dumped = value.model_dump(mode="json")
        return dumped if isinstance(dumped, dict) else {"data": dumped}
    if value is None:
        return {"status": "success"}
    if isinstance(value, str):
        return {"content": value}
    return {"data": value}


def _bounded_description(value: str) -> str:
    normalized = re.sub(r"\s+", " ", str(value)).strip()
    if len(normalized) <= MAX_DISCOVERY_DESCRIPTION:
        return normalized
    return normalized[: MAX_DISCOVERY_DESCRIPTION - 3].rstrip() + "..."


def _function_slug(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_-]+", "_", value).strip("_") or "capability"


def _unique_function_name(value: str, existing: set[str], identity: str | None = None) -> str:
    normalized = _function_slug(value)
    digest = hashlib.sha1((identity or value).encode("utf-8")).hexdigest()[:8]
    candidate = normalized if len(normalized) <= 64 else f"{normalized[:55]}_{digest}"
    if candidate not in existing:
        return candidate
    return f"{candidate[:55]}_{digest}"
