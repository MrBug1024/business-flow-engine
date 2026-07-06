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

from deepagents import create_deep_agent
from langchain_core.tools import StructuredTool

from .agent_guard import ExcludeBuiltinToolsMiddleware
from .llm import get_llm
from . import scenario_runtime as rt

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
6. **一次一诉求**：每轮只完成用户当前这一条诉求，不要自作主张连续执行多步或合并历史任务。
"""


def _make_scenario_tools(pkg: rt.ScenarioPackage) -> list[StructuredTool]:
    """把一个能力包封成 5 个命名空间化 StructuredTool，全部转调 scenario_runtime。"""
    ns = pkg.namespace
    # 用 mcp.json 里的工具描述（已含 when_to_use 触发提示），保证与交付给第三方的一致
    desc_by_action = {t.get("action"): t.get("description", "") for t in pkg.tools}

    def describe_capability() -> str:
        return rt.describe_capability(pkg)

    def describe_schema() -> str:
        return rt.describe_schema(pkg)

    def list_outputs() -> str:
        return rt.list_outputs(pkg)

    def list_knowledge(limit: int = 50) -> str:
        return rt.list_knowledge(pkg, limit=limit)

    def search_knowledge(keyword: str = "", limit: int = 20) -> str:
        return rt.search_knowledge(pkg, keyword=keyword, limit=limit)

    def execute(output_id: str, params: str = "", max_rows: int = 20000) -> str:
        return rt.execute(pkg, output_id=output_id, params=params, max_rows=max_rows)

    def query_data(sql: str) -> str:
        return rt.query_data(pkg, sql=sql)

    specs = [
        ("describe_capability", describe_capability),
        ("describe_schema", describe_schema),
        ("list_outputs", list_outputs),
        ("list_knowledge", list_knowledge),
        ("search_knowledge", search_knowledge),
        ("execute", execute),
        ("query_data", query_data),
    ]
    tools: list[StructuredTool] = []
    for action, fn in specs:
        desc = desc_by_action.get(action) or f"{pkg.display_name} 的 {action} 工具"
        tools.append(StructuredTool.from_function(fn, name=f"{ns}__{action}", description=desc))
    return tools


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
