"""业务场景能力包 —— MCP Server（stdio）。

这是交付给**第三方 Agent 宿主**（Claude Desktop / Cursor / Cline 等）的运行时：
第三方只需在自己的 MCP 配置里粘贴一段 `command`/`args`（见每个能力包的
`mcp_config.example.json`），无需改动任何自己的代码，即可挂载本平台蒸馏出的
业务场景能力。

运行方式：
    python -m app.runtime.mcp_server --pkg  <某场景 skills 目录>     # 只服务单个能力包
    python -m app.runtime.mcp_server --root <data/scenarios 目录>     # 服务该目录下全部能力包（多场景）

对外暴露的工具：
    list_business_capabilities()                 —— 发现：有哪些业务能力、各自何时该/不该调用
    <namespace>__describe_schema / __list_knowledge / __search_knowledge
    <namespace>__execute / __query_data          —— 每个场景一组，命名空间前缀保证多场景不撞名

所有工具最终都转调平台无关的 `scenario_runtime`，执行不依赖平台 `store`/数据库。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from . import scenario_runtime as rt

_DISCOVERY_TOOL = "list_business_capabilities"


def _load_packages(args) -> list[rt.ScenarioPackage]:
    pkgs: list[rt.ScenarioPackage] = []
    if args.pkg:
        p = rt.ScenarioPackage.load(args.pkg)
        if not p.is_ready():
            print(f"[mcp_server] 警告：{args.pkg} 不是有效能力包（缺 mcp.json/main_skill）", file=sys.stderr)
        pkgs.append(p)
    if args.root:
        pkgs.extend(rt.discover_packages(args.root))
    # 去重（按命名空间）
    seen: dict[str, rt.ScenarioPackage] = {}
    for p in pkgs:
        seen.setdefault(p.namespace, p)
    return list(seen.values())


def _tool_action_index(pkgs: list[rt.ScenarioPackage]) -> dict[str, tuple[rt.ScenarioPackage, str]]:
    """工具名 → (能力包, action) 的路由表。"""
    idx: dict[str, tuple[rt.ScenarioPackage, str]] = {}
    for p in pkgs:
        for tool in p.tools:
            name = tool.get("name")
            action = tool.get("action")
            if name and action:
                idx[name] = (p, action)
    return idx


def build_server(pkgs: list[rt.ScenarioPackage]):
    """构建一个 mcp.server.Server，注册发现工具 + 各能力包的命名空间工具。"""
    try:
        from mcp.server import Server
        import mcp.types as mcp_types
    except Exception as exc:  # pragma: no cover
        raise RuntimeError(
            "未安装 MCP SDK。请先 `pip install mcp`（或 pip install -r requirements.txt）。"
            f" 原始错误：{exc}"
        )

    server = Server("business-flow-engine")
    route = _tool_action_index(pkgs)

    # ---- 工具清单 ----
    @server.list_tools()
    async def list_tools() -> list:
        tools = [
            mcp_types.Tool(
                name=_DISCOVERY_TOOL,
                description=(
                    "列出当前 MCP Server 挂载的业务场景能力，而不是蒸馏平台数据库里的全部场景。"
                    "返回每个能力的用途摘要、适用/不适用边界、必需业务数据、产出、工具名，"
                    "以及首次接入时应该调用的 `<namespace>__describe_capability`。"
                    "第三方宿主不清楚这个包做什么时，先调用本工具。"
                ),
                inputSchema={"type": "object", "properties": {}, "required": []},
            )
        ]
        for p in pkgs:
            for tdef in p.tools:
                tools.append(
                    mcp_types.Tool(
                        name=tdef["name"],
                        description=tdef.get("description", ""),
                        inputSchema=tdef.get("inputSchema", {"type": "object", "properties": {}}),
                    )
                )
        return tools

    # ---- 工具调用 ----
    @server.call_tool()
    async def call_tool(name: str, arguments: dict) -> list:
        text = _dispatch(name, arguments or {}, pkgs, route)
        return [mcp_types.TextContent(type="text", text=text)]

    return server


def _dispatch(name: str, arguments: dict,
              pkgs: list[rt.ScenarioPackage],
              route: dict[str, tuple[rt.ScenarioPackage, str]]) -> str:
    """同步执行工具逻辑（供 async 包装调用；scenario_runtime 是同步的）。"""
    if name == _DISCOVERY_TOOL:
        return json.dumps(rt.capability_catalog(pkgs), ensure_ascii=False, indent=2)
    target = route.get(name)
    if not target:
        available = ", ".join([_DISCOVERY_TOOL] + list(route.keys()))
        return f"❌ 未知工具：{name}。可用工具：{available}"
    pkg, action = target
    try:
        return rt.call_action(pkg, action, arguments)
    except Exception as exc:  # pragma: no cover
        return f"❌ 工具执行异常：{type(exc).__name__}: {exc}"


async def _serve(pkgs: list[rt.ScenarioPackage]) -> None:
    from mcp.server.stdio import stdio_server
    server = build_server(pkgs)
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="业务场景能力包 MCP Server（stdio）")
    parser.add_argument("--pkg", help="单个能力包目录（某场景的 skills/ 目录）")
    parser.add_argument("--root", help="data/scenarios 根目录，服务其下全部能力包")
    args = parser.parse_args(argv)

    if not args.pkg and not args.root:
        parser.error("至少提供 --pkg 或 --root 之一")

    pkgs = _load_packages(args)
    if not pkgs:
        print("[mcp_server] 未发现任何能力包，退出。", file=sys.stderr)
        return 1

    print(f"[mcp_server] 挂载 {len(pkgs)} 个能力包："
          + "、".join(f"{p.display_name}({p.namespace})" for p in pkgs), file=sys.stderr)
    try:
        asyncio.run(_serve(pkgs))
    except KeyboardInterrupt:  # pragma: no cover
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
