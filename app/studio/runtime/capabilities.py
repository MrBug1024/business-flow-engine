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
from app.studio.models import BusinessRecord
from app.studio.settings import studio_settings
from app.studio.storage import new_id, store
from app.studio.runtime.tool_context import StudioToolContext, bind_tool_context
from app.studio.capabilities.tools import tool_registry


CapabilityKind = Literal["tool", "mcp"]
MAX_TOOL_OUTPUT = 32_000


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
    if capability.protocol in {"task_progress", "user_input"}:
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


def _function_slug(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_-]+", "_", value).strip("_") or "capability"


def _unique_function_name(value: str, existing: set[str], identity: str | None = None) -> str:
    normalized = _function_slug(value)
    digest = hashlib.sha1((identity or value).encode("utf-8")).hexdigest()[:8]
    candidate = normalized if len(normalized) <= 64 else f"{normalized[:55]}_{digest}"
    if candidate not in existing:
        return candidate
    return f"{candidate[:55]}_{digest}"
