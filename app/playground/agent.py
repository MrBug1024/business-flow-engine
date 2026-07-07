"""通用第三方 Agent（沙盒）。

这是「验证通道改造为默认第三方平台」的核心：一个**不预置任何具体业务知识**的通用
Agent，模拟真实第三方宿主。它只被挂载了若干业务场景能力（命名空间化工具），必须靠
`list_business_capabilities` 自行发现这些能力的用途与触发条件，并自主判断何时调用——
从而真正验证「零改动挂载、自主发现、自主决策、多场景不冲突」。

与旧的 `verification_agent` 的关键区别：system_prompt 里**不含**任何具体场景名/技能/
执行模式；工具集来自「已挂载的能力包集合」，且每个场景的工具都带命名空间前缀。
"""

from __future__ import annotations

import json
from typing import Any

from deepagents import create_deep_agent
from langchain_core.tools import StructuredTool
from pydantic import Field, create_model

from app.core.agent_guard import ExcludeBuiltinToolsMiddleware
from app.core.llm import get_llm
from app.runtime import scenario_runtime as rt

_SYSTEM_PROMPT = """你是一个通用 AI 助理，为第三方业务提供服务。你**自身不预置任何具体业务领域知识**。

你被挂载了若干「业务场景能力」，它们以命名空间化工具的形式提供（工具名形如 `<命名空间>__动作`，
例如 `s_abcd1234__query_data`）。这些能力由外部业务平台蒸馏生成，你可以调用它们来完成对应业务。

工作准则：
1. **先发现，再决策**：收到用户诉求后，先判断它是否落入某个已挂载能力的适用范围。
   随时可调用 `list_business_capabilities` 查看每个能力的用途(summary)、何时应当使用
   (when_to_use)、何时不要使用(not_for)、以及它提供了哪些工具。
2. **匹配才调用**：仅当用户诉求明确匹配某能力的 when_to_use 时，才调用该能力对应命名空间
   （`<命名空间>__*`）的工具。若诉求属于某能力的 not_for、或与所有已挂载能力都无关，
   就用你自己的通用能力正常回答，**不要**硬套业务工具。
3. **不跨场景混用**：若挂载了多个能力，选择 when_to_use 最贴合的那一个处理，
   不要把不同命名空间（不同场景）的工具混在一起用。
4. **构造查询前先看结构**：调用某场景的 `__query_data` 前，若本轮还没看过它的表结构，
   先调用同命名空间的 `__describe_schema`，据此现场拼 SQL，不要凭猜测拼字段名。
5. **知识驱动执行的落地**：某些场景的 `__execute` 在知识驱动模式下只会返回命中的
   知识/规则行原文（而非最终结果），此时须按返回指引，逐条阅读规则原文，再用同命名空间的
   `__query_data` 针对每条规则构造并执行查询，得出最终结果。工具返回 0 行或报错 ≠ 任务失败，
   请如实向用户说明原因。
6. **文件由宿主平台处理**：附件上传、文件预览、结果文件下载是宿主 Agent 平台的公共能力。
   本业务 Skill 只负责说明需要哪些业务数据、如何理解字段/规则/流程，以及如何完成业务判断。
   若宿主已经把数据整理为可访问目录或表格内容，再按该能力包的 schema 和工具说明处理。
7. **一次一诉求**：每轮只完成用户当前这一条诉求，不要自作主张连续执行多步或合并历史任务。
"""


def _make_scenario_tools(pkg: rt.ScenarioPackage) -> list[StructuredTool]:
    """把一个能力包的 MCP 工具清单动态封成 StructuredTool。

    工具定义直接来自 ScenarioPackage.tools；验证沙盒与发布包暴露的业务工具清单保持一致。
    """
    tools: list[StructuredTool] = []
    for tdef in pkg.tools:
        name = tdef.get("name")
        action = tdef.get("action")
        if not name or not action:
            continue
        desc = tdef.get("description") or f"{pkg.display_name} 的 {action} 工具"
        args_schema = _args_model(name, tdef.get("inputSchema") or {})

        def _run(_action: str = action, **kwargs) -> str:
            return rt.call_action(pkg, _action, kwargs)

        tools.append(
            StructuredTool.from_function(
                _run,
                name=name,
                description=desc,
                args_schema=args_schema,
            )
        )
    return tools


def _args_model(tool_name: str, schema: dict) -> type:
    props = schema.get("properties") or {}
    required = set(schema.get("required") or [])
    fields = {}
    for prop, spec in props.items():
        if not isinstance(spec, dict):
            spec = {}
        typ = _schema_type(spec)
        default = ... if prop in required else spec.get("default", None)
        fields[prop] = (typ, Field(default, description=spec.get("description", "")))
    model_name = "Args_" + "".join(ch if ch.isalnum() else "_" for ch in tool_name)
    return create_model(model_name, **fields)


def _schema_type(spec: dict) -> type:
    typ = spec.get("type", "string")
    if isinstance(typ, list):
        typ = next((t for t in typ if t != "null"), "string")
    if typ == "integer":
        return int
    if typ == "number":
        return float
    if typ == "boolean":
        return bool
    if typ == "object":
        return dict[str, Any]
    if typ == "array":
        return list[Any]
    return str


def build_playground_agent(pkgs: list[rt.ScenarioPackage]):
    """构建通用沙盒 Agent。pkgs 为当前已挂载的能力包集合（可为空/单个/多个）。"""
    llm = get_llm()
    if llm is None:
        raise RuntimeError("LLM 未配置，无法构建沙盒 Agent。")

    # 发现工具：让 Agent 自己了解挂载了哪些能力、各自何时该/不该用
    def list_business_capabilities() -> str:
        """列出当前挂载的所有业务场景能力（用途/何时使用/何时不用/提供的工具名）。"""
        catalog = rt.capability_catalog(pkgs)
        if not catalog:
            return "当前没有挂载任何业务场景能力。请直接用通用能力回答用户。"
        return json.dumps(catalog, ensure_ascii=False, indent=2)

    tools: list[StructuredTool] = [
        StructuredTool.from_function(
            list_business_capabilities,
            name="list_business_capabilities",
            description=(
                "列出当前会话挂载的业务场景能力，而不是蒸馏平台数据库里的全部场景。"
                "返回每个能力的用途摘要、适用/不适用边界、必需业务数据、产出、工具名，"
                "以及首次接入时应该调用的 `<namespace>__describe_capability`。"
                "不清楚某个包做什么时，先调用本工具。"
            ),
        )
    ]
    for pkg in pkgs:
        if pkg.is_ready():
            tools.extend(_make_scenario_tools(pkg))

    mounted = "、".join(f"{p.display_name}({p.namespace})" for p in pkgs) or "（无）"
    context = f"\n\n# 本会话当前挂载的业务能力\n{mounted}\n（其余细节请调用 list_business_capabilities 获取。）"

    return create_deep_agent(
        model=llm,
        tools=tools,
        system_prompt=_SYSTEM_PROMPT + context,
        middleware=[ExcludeBuiltinToolsMiddleware()],
    )
