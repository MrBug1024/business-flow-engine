"""MCP configuration normalization, discovery, and client execution."""

from __future__ import annotations

import asyncio
import os
import re
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import Any, AsyncIterator
from urllib.parse import urlsplit


MASKED_SECRET = "********"
MAX_MCP_TOOLS = 256
MAX_LIST_TOOL_PAGES = 32
_ENV_PATTERN = re.compile(r"\$\{([A-Z_][A-Z0-9_]*)\}")
_SENSITIVE_KEY = re.compile(
    r"(?:authorization|api[_-]?key|access[_-]?token|token|secret|password|credential|bearer)",
    re.IGNORECASE,
)


def normalize_mcp_payload(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Convert Codex-style or internal MCP JSON into settings entries."""

    if not isinstance(payload, dict):
        raise ValueError("MCP config must be a JSON object.")

    wrapped = payload.get("mcpServers")
    if wrapped is not None:
        if not isinstance(wrapped, dict) or not wrapped:
            raise ValueError("mcpServers must contain at least one server.")
        items = list(wrapped.items())
    else:
        name = str(payload.get("name") or "").strip()
        config = payload.get("config") if isinstance(payload.get("config"), dict) else payload
        if not name:
            name = str(config.get("name") or "").strip()
        if not name:
            raise ValueError("A single MCP server config requires a name.")
        items = [(name, config)]

    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw_name, raw_config in items:
        name = str(raw_name).strip()
        if not name:
            raise ValueError("MCP server name cannot be empty.")
        if name in seen:
            raise ValueError(f"Duplicate MCP server: {name}")
        seen.add(name)
        if not isinstance(raw_config, dict):
            raise ValueError(f"MCP server {name} must be a JSON object.")
        normalized.append(
            {
                "name": name,
                "enabled": bool(raw_config.get("enabled", True)),
                "config": _normalize_server_config(name, raw_config),
            }
        )
    return normalized


def normalize_stored_mcp_configs(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Best-effort migration for previously persisted MCP configuration shapes."""

    normalized: list[dict[str, Any]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name") or "").strip()
        config = entry.get("config") if isinstance(entry.get("config"), dict) else {}
        enabled = bool(entry.get("enabled", False))
        try:
            if "mcpServers" in config:
                migrated = normalize_mcp_payload(config)
                for item in migrated:
                    item["enabled"] = enabled
                normalized.extend(migrated)
            elif name:
                migrated = normalize_mcp_payload({"name": name, "config": config})[0]
                migrated["enabled"] = enabled
                normalized.append(migrated)
        except ValueError:
            # Keep invalid drafts editable, but never make them Agent-callable.
            if name:
                normalized.append({"name": name, "enabled": False, "config": config})
    return _deduplicate_entries(normalized)


def public_mcp_configs(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [_redact_entry(entry) for entry in entries]


def merge_masked_mcp_configs(
    incoming: list[dict[str, Any]],
    existing: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Restore masked values only when the destination server is unchanged."""

    old_by_name = {
        str(item.get("name")): item
        for item in existing
        if isinstance(item, dict) and item.get("name")
    }
    merged: list[dict[str, Any]] = []
    for raw in incoming:
        if not isinstance(raw, dict):
            raise ValueError("MCP settings entries must be JSON objects.")
        name = str(raw.get("name") or "").strip()
        if not name:
            raise ValueError("MCP server name cannot be empty.")
        candidate = _clone(raw)
        old = old_by_name.get(name)
        if _contains_mask(candidate):
            if old is None or not _same_secret_destination(candidate, old):
                raise ValueError(
                    f"MCP server {name} changed its destination; enter its credentials again."
                )
            candidate = _restore_masks(candidate, old)
        merged.append(candidate)
    return normalize_stored_mcp_configs(merged)


def probe_mcp_configs(entries: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    return asyncio.run(_probe_mcp_configs(entries))


async def _probe_mcp_configs(
    entries: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    prepared: list[dict[str, Any]] = []
    results: list[dict[str, Any]] = []
    for entry in entries:
        name = str(entry.get("name") or "mcp")
        config = entry.get("config") if isinstance(entry.get("config"), dict) else {}
        try:
            info, tools = await _probe_server(config)
            updated = _clone(entry)
            updated["config"]["tools"] = tools
            updated["config"]["tools_discovered"] = True
            prepared.append(updated)
            results.append(
                {
                    "name": name,
                    "status": "connected",
                    "transport": config.get("transport"),
                    "server": info,
                    "tool_count": len(tools),
                    "tools": tools,
                }
            )
        except Exception as exc:  # noqa: BLE001 - connection errors are safe API results
            prepared.append(_clone(entry))
            results.append(
                {
                    "name": name,
                    "status": "failed",
                    "transport": config.get("transport"),
                    "tool_count": 0,
                    "tools": [],
                    "error": sanitize_mcp_error(exc, config),
                }
            )
    return prepared, results


async def _probe_server(config: dict[str, Any]) -> tuple[dict[str, str], list[dict[str, Any]]]:
    async with _open_mcp_session(config) as session:
        initialized = await session.initialize()
        tools: list[dict[str, Any]] = []
        tool_names: set[str] = set()
        cursor: str | None = None
        seen_cursors: set[str] = set()
        for _ in range(MAX_LIST_TOOL_PAGES):
            page = await session.list_tools(cursor)
            for tool in page.tools:
                snapshot = _tool_snapshot(tool)
                tool_name = str(snapshot.get("name") or "")
                if not tool_name:
                    raise ValueError("MCP tools/list returned a tool without a name.")
                if tool_name in tool_names:
                    raise ValueError(f"MCP tools/list returned duplicate tool {tool_name}.")
                tool_names.add(tool_name)
                tools.append(snapshot)
                if len(tools) > MAX_MCP_TOOLS:
                    raise ValueError(f"MCP server exposes more than {MAX_MCP_TOOLS} tools.")
            cursor = getattr(page, "nextCursor", None)
            if not cursor:
                break
            if cursor in seen_cursors:
                raise ValueError("MCP tools/list returned a repeated cursor.")
            seen_cursors.add(cursor)
        else:
            raise ValueError("MCP tools/list exceeded the pagination limit.")

        server_info = getattr(initialized, "serverInfo", None)
        return (
            {
                "name": str(getattr(server_info, "name", "") or ""),
                "version": str(getattr(server_info, "version", "") or ""),
            },
            tools,
        )


async def call_mcp_tool(
    config: dict[str, Any],
    tool_name: str,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    try:
        async with _open_mcp_session(config) as session:
            await session.initialize()
            result = await session.call_tool(
                tool_name,
                arguments,
                read_timeout_seconds=timedelta(seconds=_timeout_seconds(config)),
            )
        if bool(getattr(result, "isError", False)):
            raise RuntimeError(_mcp_result_error(result) or f"MCP tool {tool_name} failed.")
        if hasattr(result, "model_dump"):
            return result.model_dump(mode="json", by_alias=True)
        return {"result": str(result)}
    except Exception as exc:  # noqa: BLE001 - sanitize before reaching model/SSE
        raise RuntimeError(sanitize_mcp_error(exc, config)) from None


@asynccontextmanager
async def _open_mcp_session(config: dict[str, Any]) -> AsyncIterator[Any]:
    from mcp import ClientSession

    transport = config.get("transport")
    timeout = _timeout_seconds(config)
    if transport == "streamable_http":
        from mcp.client.streamable_http import streamablehttp_client

        url = _expand_secret(str(config.get("url") or ""))
        _validate_http_url(url)
        headers = {
            str(key): _expand_secret(str(value))
            for key, value in (config.get("headers") or {}).items()
        }
        async with streamablehttp_client(
            url,
            headers=headers or None,
            timeout=timeout,
            sse_read_timeout=timeout,
        ) as streams:
            async with ClientSession(
                streams[0],
                streams[1],
                read_timeout_seconds=timedelta(seconds=timeout),
            ) as session:
                yield session
        return

    if transport == "stdio":
        from mcp.client.stdio import StdioServerParameters, stdio_client

        command = _expand_secret(str(config.get("command") or ""))
        if not command:
            raise ValueError("MCP stdio config requires command.")
        params = StdioServerParameters(
            command=command,
            args=[_expand_secret(str(item)) for item in config.get("args") or []],
            env={
                str(key): _expand_secret(str(value))
                for key, value in (config.get("env") or {}).items()
            }
            or None,
            cwd=config.get("cwd"),
        )
        async with stdio_client(params) as streams:
            async with ClientSession(
                streams[0],
                streams[1],
                read_timeout_seconds=timedelta(seconds=timeout),
            ) as session:
                yield session
        return

    raise ValueError(f"Unsupported MCP transport: {transport}")


def sanitize_mcp_error(error: Exception, config: dict[str, Any] | None = None) -> str:
    message = "; ".join(_exception_messages(error))
    for secret in _secret_values(config or {}):
        if secret and secret != MASKED_SECRET:
            message = message.replace(secret, MASKED_SECRET)
    message = re.sub(r"(?i)(bearer\s+)[^\s,;\"']+", rf"\1{MASKED_SECRET}", message)
    message = re.sub(r"\bsk[_-][A-Za-z0-9_-]{8,}\b", MASKED_SECRET, message)
    return message[:1000]


def _exception_messages(error: BaseException) -> list[str]:
    nested = getattr(error, "exceptions", None)
    if isinstance(nested, (list, tuple)) and nested:
        messages: list[str] = []
        for item in nested:
            if isinstance(item, BaseException):
                messages.extend(_exception_messages(item))
        if messages:
            return messages
    detail = str(error).strip()
    return [detail or error.__class__.__name__]


def _normalize_server_config(name: str, raw: dict[str, Any]) -> dict[str, Any]:
    transport_raw = str(raw.get("transport") or raw.get("type") or "").strip().lower()
    if not transport_raw and raw.get("command"):
        transport_raw = "stdio"
    transport = {
        "http": "streamable_http",
        "streamable-http": "streamable_http",
        "streamable_http": "streamable_http",
        "stdio": "stdio",
    }.get(transport_raw)
    if transport is None:
        raise ValueError(f"MCP server {name} has unsupported type: {transport_raw or 'missing'}")

    timeout = raw.get("timeout_seconds", 60)
    try:
        timeout_seconds = max(1, min(int(timeout), 300))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"MCP server {name} has an invalid timeout.") from exc

    config: dict[str, Any] = {"transport": transport, "timeout_seconds": timeout_seconds}
    description = str(raw.get("description") or "").strip()
    if description:
        config["description"] = description[:2000]

    if transport == "streamable_http":
        url = str(raw.get("url") or "").strip()
        _validate_http_url(url, allow_env_placeholders=True)
        headers = raw.get("headers") or {}
        if not isinstance(headers, dict):
            raise ValueError(f"MCP server {name} headers must be an object.")
        config["url"] = url
        config["headers"] = {str(key): str(value) for key, value in headers.items()}
    else:
        command = str(raw.get("command") or "").strip()
        if not command:
            raise ValueError(f"MCP server {name} requires command.")
        args = raw.get("args") or []
        env = raw.get("env") or {}
        if not isinstance(args, list) or not isinstance(env, dict):
            raise ValueError(f"MCP server {name} args/env have invalid types.")
        config.update(
            {
                "command": command,
                "args": [str(item) for item in args],
                "env": {str(key): str(value) for key, value in env.items()},
            }
        )
        if raw.get("cwd"):
            config["cwd"] = str(raw["cwd"])

    tools = raw.get("tools")
    if isinstance(tools, list):
        config["tools"] = _clone(tools[:MAX_MCP_TOOLS])
    if raw.get("tools_discovered") is True:
        config["tools_discovered"] = True
    return config


def _validate_http_url(url: str, *, allow_env_placeholders: bool = False) -> None:
    candidate = url
    if allow_env_placeholders and _ENV_PATTERN.fullmatch(url):
        return
    if allow_env_placeholders:
        candidate = _ENV_PATTERN.sub("mcp-placeholder", url)
    parsed = urlsplit(candidate)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("MCP HTTP URL must use http:// or https://.")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("MCP HTTP URL cannot contain embedded credentials.")


def _tool_snapshot(tool: Any) -> dict[str, Any]:
    schema = getattr(tool, "inputSchema", None)
    if not isinstance(schema, dict):
        schema = {"type": "object", "properties": {}}
    output_schema = getattr(tool, "outputSchema", None)
    return {
        "name": str(getattr(tool, "name", "") or ""),
        "title": str(getattr(tool, "title", "") or ""),
        "description": str(getattr(tool, "description", "") or "")[:4000],
        "input_schema": _clone(schema),
        "output_schema": _clone(output_schema) if isinstance(output_schema, dict) else None,
    }


def _mcp_result_error(result: Any) -> str:
    parts: list[str] = []
    for item in getattr(result, "content", []) or []:
        text = getattr(item, "text", None)
        if text:
            parts.append(str(text))
    return "\n".join(parts)[:1000]


def _timeout_seconds(config: dict[str, Any]) -> int:
    try:
        return max(1, min(int(config.get("timeout_seconds", 60)), 300))
    except (TypeError, ValueError):
        return 60


def _expand_secret(value: str) -> str:
    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        resolved = os.getenv(name)
        if resolved is None:
            raise ValueError(f"MCP config requires environment variable {name}.")
        return resolved

    return _ENV_PATTERN.sub(replace, value)


def _redact_entry(entry: dict[str, Any]) -> dict[str, Any]:
    result = _clone(entry)
    config = result.get("config")
    if isinstance(config, dict):
        result["config"] = _redact_value(config)
    return result


def _redact_value(value: Any, parent_key: str = "") -> Any:
    if isinstance(value, dict):
        if parent_key.lower() == "headers":
            return {str(key): MASKED_SECRET for key in value}
        redacted: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            if (
                isinstance(item, str)
                and _SENSITIVE_KEY.fullmatch(key_text)
            ):
                redacted[key] = MASKED_SECRET
            elif (
                parent_key.lower() == "env"
                and isinstance(item, str)
                and _SENSITIVE_KEY.search(key_text)
            ):
                redacted[key] = MASKED_SECRET
            else:
                redacted[key] = _redact_value(item, key_text)
        return redacted
    if isinstance(value, list):
        return [_redact_value(item, parent_key) for item in value]
    return value


def _same_secret_destination(candidate: dict[str, Any], old: dict[str, Any]) -> bool:
    new_config = candidate.get("config") if isinstance(candidate.get("config"), dict) else {}
    old_config = old.get("config") if isinstance(old.get("config"), dict) else {}
    new_transport = _transport_alias(new_config)
    old_transport = _transport_alias(old_config)
    if new_transport != old_transport:
        return False
    if new_transport == "streamable_http":
        return str(new_config.get("url") or "") == str(old_config.get("url") or "")
    if new_transport == "stdio":
        return str(new_config.get("command") or "") == str(old_config.get("command") or "")
    return False


def _transport_alias(config: dict[str, Any]) -> str:
    value = str(config.get("transport") or config.get("type") or "").lower()
    return {
        "http": "streamable_http",
        "streamable-http": "streamable_http",
        "streamable_http": "streamable_http",
        "stdio": "stdio",
    }.get(value, value)


def _contains_mask(value: Any) -> bool:
    if value == MASKED_SECRET:
        return True
    if isinstance(value, dict):
        return any(_contains_mask(item) for item in value.values())
    if isinstance(value, list):
        return any(_contains_mask(item) for item in value)
    return False


def _restore_masks(candidate: Any, old: Any) -> Any:
    if candidate == MASKED_SECRET:
        if old is None or old == MASKED_SECRET:
            raise ValueError("Masked MCP credential has no stored value.")
        return _clone(old)
    if isinstance(candidate, dict):
        old_dict = old if isinstance(old, dict) else {}
        return {key: _restore_masks(value, old_dict.get(key)) for key, value in candidate.items()}
    if isinstance(candidate, list):
        old_list = old if isinstance(old, list) else []
        return [
            _restore_masks(value, old_list[index] if index < len(old_list) else None)
            for index, value in enumerate(candidate)
        ]
    return candidate


def _secret_values(config: dict[str, Any]) -> list[str]:
    values: list[str] = []
    headers = config.get("headers")
    if isinstance(headers, dict):
        values.extend(str(value) for value in headers.values())
    env = config.get("env")
    if isinstance(env, dict):
        values.extend(
            str(value)
            for key, value in env.items()
            if _SENSITIVE_KEY.search(str(key))
        )
    expanded: list[str] = []
    for value in values:
        if not value:
            continue
        expanded.append(value)
        try:
            resolved = _expand_secret(value)
        except ValueError:
            continue
        if resolved != value:
            expanded.append(resolved)
    return sorted(set(expanded), key=len, reverse=True)


def _deduplicate_entries(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_name: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for entry in entries:
        name = str(entry.get("name") or "")
        if name not in by_name:
            order.append(name)
        by_name[name] = entry
    return [by_name[name] for name in order]


def _clone(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _clone(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_clone(item) for item in value]
    return value
