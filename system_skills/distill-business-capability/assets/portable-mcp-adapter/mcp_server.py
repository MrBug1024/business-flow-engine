#!/usr/bin/env python3
"""Dependency-free, ASCII-safe stdio MCP adapter for one capability package."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent
EXECUTOR_PATH = ROOT / "main_executor" / "scripts" / "execute_scenario.py"
_EXECUTOR: Any | None = None


def package_metadata() -> dict[str, Any]:
    manifest_path = ROOT / "manifest.json"
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


PACKAGE_METADATA = package_metadata()
NAMESPACE = str(PACKAGE_METADATA.get("namespace", "portable_capability")).strip() or "portable_capability"


def qualified_tool_name(action: str) -> str:
    return f"{NAMESPACE}__{action}"


def action_from_tool_name(name: str) -> str:
    prefix = f"{NAMESPACE}__"
    return name[len(prefix):] if name.startswith(prefix) else name


class AdapterError(ValueError):
    pass


def executor() -> Any:
    global _EXECUTOR
    if _EXECUTOR is not None:
        return _EXECUTOR
    if not EXECUTOR_PATH.is_file():
        raise AdapterError(f"Missing packaged executor: {EXECUTOR_PATH}")
    spec = importlib.util.spec_from_file_location("portable_capability_executor", EXECUTOR_PATH)
    if spec is None or spec.loader is None:
        raise AdapterError("Unable to load packaged executor")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    _EXECUTOR = module
    return module


def object_arg(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise AdapterError("Tool arguments must be a JSON object")
    return value


def required_text(arguments: dict[str, Any], key: str) -> str:
    value = str(arguments.get(key, "")).strip()
    if not value:
        raise AdapterError(f"{key} is required")
    return value


def required_data_root(arguments: dict[str, Any]) -> str:
    """Accept both the portable API and established package-host naming."""
    for key in ("data_root", "data_dir"):
        value = str(arguments.get(key, "")).strip()
        if value:
            return value
    raise AdapterError("data_root (or data_dir) is required")


def bindings(arguments: dict[str, Any]) -> list[str]:
    raw = arguments.get("bindings", arguments.get("bind", {}))
    if raw in (None, ""):
        return []
    if isinstance(raw, dict):
        result = []
        for source_id, relative_path in sorted(raw.items(), key=lambda item: str(item[0])):
            source = str(source_id).strip()
            path = str(relative_path).strip()
            if not source or not path:
                raise AdapterError("Each runtime binding needs a source id and relative file path")
            result.append(f"{source}={path}")
        return result
    if isinstance(raw, list) and all(isinstance(item, str) and "=" in item for item in raw):
        return [item.strip() for item in raw if item.strip()]
    raise AdapterError("bindings must be an object or a list of source_id=relative_path strings")


def repeat(flag: str, values: list[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        result.extend([flag, value])
    return result


def limited_rows(arguments: dict[str, Any], default: int) -> str:
    raw = arguments.get("max_rows", default)
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise AdapterError("max_rows must be an integer") from exc
    return str(max(1, min(value, 20_000)))


def describe_capability(_: dict[str, Any]) -> dict[str, Any]:
    module = executor()
    return module.run(["describe"])


def describe_schema(_: dict[str, Any]) -> dict[str, Any]:
    module = executor()
    contract = module.load_json(module.DEFAULT_CONTRACT)
    sources = []
    for source in contract.get("sources", []):
        if not isinstance(source, dict):
            continue
        sources.append({
            "source_id": source.get("source_id"),
            "view_name": source.get("view_name"),
            "path": source.get("path"),
            "lifecycle": source.get("lifecycle"),
            "runtime_required": source.get("runtime_required"),
            "tables": source.get("tables", []),
        })
    return {
        "status": "success",
        "sources": sources,
        "links": contract.get("links", []),
        "runtime_source_ids": contract.get("runtime_source_ids", []),
        "rule_source_ids": contract.get("rule_source_ids", []),
    }


def list_outputs(_: dict[str, Any]) -> dict[str, Any]:
    module = executor()
    flow = module.load_json(module.DEFAULT_FLOW)
    plan = flow.get("execution_plan", {}) if isinstance(flow.get("execution_plan"), dict) else {}
    return {
        "status": "success",
        "outputs": plan.get("output_specs", []),
        "result_contract": plan.get("result_contract", {}),
    }


def search_rules(arguments: dict[str, Any]) -> dict[str, Any]:
    module = executor()
    data_root = required_data_root(arguments)
    terms = arguments.get("terms", arguments.get("term", []))
    if isinstance(terms, str):
        terms = [terms]
    if not isinstance(terms, list) or not any(str(item).strip() for item in terms):
        raise AdapterError("terms must contain at least one search term")
    source_ids = arguments.get("source_ids", [])
    if isinstance(source_ids, str):
        source_ids = [source_ids]
    if not isinstance(source_ids, list):
        raise AdapterError("source_ids must be a list when provided")
    argv = ["search-rules", "--data-root", data_root, "--max-rows", limited_rows(arguments, 20)]
    argv += repeat("--source-id", [str(item) for item in source_ids if str(item).strip()])
    argv += repeat("--term", [str(item) for item in terms if str(item).strip()])
    argv += repeat("--bind", bindings(arguments))
    return module.run(argv)


def execute_business_request(arguments: dict[str, Any]) -> dict[str, Any]:
    module = executor()
    request = required_text(arguments, "request")
    data_root = required_data_root(arguments)
    argv = [
        "execute", "--request", request, "--data-root", data_root,
        "--max-rows", limited_rows(arguments, 50),
    ]
    output = str(arguments.get("output", "")).strip()
    if not output and str(arguments.get("out_dir", "")).strip():
        output = str(Path(str(arguments["out_dir"])).expanduser() / "scenario-evidence.json")
    if output:
        argv.extend(["--output", output])
    if arguments.get("validate_joins") is False:
        argv.append("--no-join-validation")
    argv += repeat("--bind", bindings(arguments))
    return module.run(argv)


def query_runtime_data(arguments: dict[str, Any]) -> dict[str, Any]:
    module = executor()
    data_root = required_data_root(arguments)
    sql = required_text(arguments, "sql")
    links = arguments.get("link_ids", arguments.get("link_id", []))
    if isinstance(links, str):
        links = [links]
    if not isinstance(links, list):
        raise AdapterError("link_ids must be a list when provided")
    argv = ["query", "--data-root", data_root, "--sql", sql, "--max-rows", limited_rows(arguments, 200)]
    argv += repeat("--link-id", [str(item) for item in links if str(item).strip()])
    argv += repeat("--bind", bindings(arguments))
    return module.run(argv)


def export_runtime_result(arguments: dict[str, Any]) -> dict[str, Any]:
    module = executor()
    data_root = required_data_root(arguments)
    sql = required_text(arguments, "sql")
    output = required_text(arguments, "output")
    links = arguments.get("link_ids", arguments.get("link_id", []))
    if isinstance(links, str):
        links = [links]
    if not isinstance(links, list):
        raise AdapterError("link_ids must be a list when provided")
    contract = module.load_json(module.DEFAULT_CONTRACT)
    reader = module.tabular_runtime()
    return reader.export_contract(
        contract,
        Path(data_root).expanduser().resolve(),
        sql,
        output,
        [str(item) for item in links if str(item).strip()],
        overwrite=bool(arguments.get("overwrite", False)),
        bindings=reader.parse_source_bindings(bindings(arguments)),
    )


def list_knowledge(arguments: dict[str, Any]) -> dict[str, Any]:
    module = executor()
    data_root = required_data_root(arguments)
    contract = module.load_json(module.DEFAULT_CONTRACT)
    sources = module.source_map(contract)
    source_id = next((str(item) for item in contract.get("rule_source_ids", []) if str(item) in sources), "")
    if not source_id:
        raise AdapterError("No structured knowledge source is declared")
    view_name = str(sources[source_id].get("view_name", ""))
    reader = module.tabular_runtime()
    return module.run([
        "query", "--data-root", data_root, "--max-rows", limited_rows(arguments, 50),
        "--sql", f"SELECT * FROM {reader.quote_identifier(view_name)}",
        *repeat("--bind", bindings(arguments)),
    ])


def search_knowledge(arguments: dict[str, Any]) -> dict[str, Any]:
    keyword = required_text(arguments, "keyword")
    mapped = dict(arguments)
    mapped["terms"] = [keyword]
    return search_rules(mapped)


def execute_legacy_request(arguments: dict[str, Any]) -> dict[str, Any]:
    request = str(arguments.get("request", "")).strip()
    if not request:
        raw = arguments.get("params", "")
        if isinstance(raw, dict):
            request = " ".join(str(value).strip() for value in raw.values() if str(value).strip())
        else:
            request = str(raw or "").strip()
    if not request:
        request = str(arguments.get("output_id", "")).strip()
    if not request:
        raise AdapterError("request, params, or output_id is required")
    mapped = dict(arguments)
    mapped["request"] = request
    return execute_business_request(mapped)


def query_legacy_data(arguments: dict[str, Any]) -> dict[str, Any]:
    return query_runtime_data(arguments)


TOOL_HANDLERS = {
    "describe_capability": describe_capability,
    "describe_schema": describe_schema,
    "list_outputs": list_outputs,
    "list_knowledge": list_knowledge,
    "search_knowledge": search_knowledge,
    "execute": execute_legacy_request,
    "query_data": query_legacy_data,
    "search_rules": search_rules,
    "execute_business_request": execute_business_request,
    "query_runtime_data": query_runtime_data,
    "export_runtime_result": export_runtime_result,
}

TOOLS = [
    {"name": "describe_capability", "description": "Describe the packaged capability and its required runtime inputs.", "inputSchema": {"type": "object", "properties": {}}},
    {"name": "describe_schema", "description": "Return declared sources, columns, lifecycle roles, and validated links.", "inputSchema": {"type": "object", "properties": {}}},
    {"name": "list_outputs", "description": "Return the declared business result contract.", "inputSchema": {"type": "object", "properties": {}}},
    {"name": "list_knowledge", "description": "List knowledge rows before selecting one governing record.", "inputSchema": {"type": "object", "properties": {"data_root": {"type": "string"}, "limit": {"type": "integer"}, "bindings": {"type": "object"}}, "required": ["data_root"]}},
    {"name": "search_knowledge", "description": "Search knowledge rows for a governing record.", "inputSchema": {"type": "object", "properties": {"data_root": {"type": "string"}, "keyword": {"type": "string"}, "limit": {"type": "integer"}, "bindings": {"type": "object"}}, "required": ["data_root", "keyword"]}},
    {"name": "execute", "description": "Resolve a business request into bounded rule and evidence results.", "inputSchema": {"type": "object", "properties": {"data_root": {"type": "string"}, "output_id": {"type": "string"}, "params": {"type": ["string", "object", "null"]}, "request": {"type": "string"}, "max_rows": {"type": "integer"}, "out_dir": {"type": "string"}, "bindings": {"type": "object"}}, "required": ["data_root"]}},
    {"name": "query_data", "description": "Run a bounded read-only SELECT against declared runtime sources.", "inputSchema": {"type": "object", "properties": {"data_root": {"type": "string"}, "sql": {"type": "string"}, "link_ids": {"type": "array", "items": {"type": "string"}}, "bindings": {"type": "object"}, "max_rows": {"type": "integer"}}, "required": ["data_root", "sql"]}},
]

for tool in TOOLS:
    schema = tool.get("inputSchema", {})
    properties = schema.get("properties", {}) if isinstance(schema, dict) else {}
    if "data_root" in properties:
        properties["data_dir"] = {
            "type": "string",
            "description": "Compatibility alias for data_root used by package-oriented MCP hosts.",
        }
        schema["required"] = [item for item in schema.get("required", []) if item != "data_root"]
        schema["anyOf"] = [{"required": ["data_root"]}, {"required": ["data_dir"]}]
    tool["name"] = qualified_tool_name(str(tool["name"]))


def tool_result(name: str, arguments: Any) -> dict[str, Any]:
    action = action_from_tool_name(name)
    handler = TOOL_HANDLERS.get(action)
    if handler is None:
        raise AdapterError(f"Unknown tool: {name}")
    return handler(object_arg(arguments))


def tool_response(name: str, arguments: Any) -> dict[str, Any]:
    try:
        result = tool_result(name, arguments)
        failed = str(result.get("status", "")).casefold() in {"error", "blocked"}
        return {
            "content": [{"type": "text", "text": json.dumps(result, ensure_ascii=True, indent=2)}],
            "structuredContent": result,
            "isError": failed,
        }
    except Exception as exc:
        detail = {"status": "error", "message": f"{type(exc).__name__}: {exc}"}
        return {"content": [{"type": "text", "text": json.dumps(detail, ensure_ascii=True)}], "isError": True}


def handle_message(message: dict[str, Any]) -> dict[str, Any] | None:
    identifier = message.get("id")
    method = str(message.get("method", ""))
    if not method:
        return {"jsonrpc": "2.0", "id": identifier, "error": {"code": -32600, "message": "Invalid Request"}}
    if method == "notifications/initialized":
        return None
    if method == "initialize":
        result = {
            "protocolVersion": "2024-11-05",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "portable-business-capability", "version": "1.0.0"},
        }
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": TOOLS}
    elif method == "tools/call":
        params = object_arg(message.get("params"))
        result = tool_response(str(params.get("name", "")), params.get("arguments", {}))
    else:
        return {"jsonrpc": "2.0", "id": identifier, "error": {"code": -32601, "message": "Method not found"}}
    if "id" not in message:
        return None
    return {"jsonrpc": "2.0", "id": identifier, "result": result}


def decode_request(raw: bytes) -> str:
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise AdapterError("Request is neither UTF-8 nor GB18030")


def emit(payload: dict[str, Any]) -> None:
    # JSON escapes make every wire byte ASCII, avoiding host-side GBK decoding
    # failures while preserving Unicode in parsed structuredContent.
    encoded = (json.dumps(payload, ensure_ascii=True, separators=(",", ":")) + "\n").encode("ascii")
    sys.stdout.buffer.write(encoded)
    sys.stdout.buffer.flush()


def main() -> int:
    for raw in sys.stdin.buffer:
        if not raw.strip():
            continue
        try:
            decoded = decode_request(raw)
            message = json.loads(decoded)
            if not isinstance(message, dict):
                raise AdapterError("Request must be a JSON object")
            response = handle_message(message)
            if response is not None:
                emit(response)
        except Exception as exc:
            emit({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": f"{type(exc).__name__}: {exc}"}})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
