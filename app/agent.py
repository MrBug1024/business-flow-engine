"""业务流蒸馏 Agent（v1.0.5：蒸馏专属，与验证执行彻底分离）。

职责：仅负责逆向推导（关联→流程→技能生成），不参与执行/验证。
提示词从 prompts/distillation/system.md 加载。
"""

from __future__ import annotations

from pathlib import Path

from deepagents import create_deep_agent

from .agent_guard import ExcludeBuiltinToolsMiddleware
from .llm import get_llm
from .models import Scenario, TableRole
from .tools import build_distillation_tools

_PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"


def _load_system_prompt() -> str:
    p = _PROMPTS_DIR / "distillation" / "system.md"
    return p.read_text(encoding="utf-8") if p.exists() else ""


def build_agent(scenario: Scenario):
    """构建蒸馏 Agent（仅负责追踪→推关联→推流程→生成技能）。"""
    llm = get_llm()
    if llm is None:
        raise RuntimeError("LLM 未配置，无法构建 Agent；请改用启发式降级路径。")

    tools = build_distillation_tools(scenario.id)

    roles = {t.table_name: t.role for t in scenario.tables_meta}
    roles_line = "表角色：" + ("；".join(f"{n}={r}" for n, r in roles.items()) if roles else "无表")
    n_input = sum(1 for r in roles.values() if r == TableRole.INPUT.value)
    n_knowledge = sum(1 for r in roles.values() if r in (TableRole.RULE.value, TableRole.KNOWLEDGE.value))
    n_result = sum(1 for r in roles.values() if r == TableRole.RESULT.value)
    role_summary = f"输入表 {n_input}、知识表 {n_knowledge}、结果表 {n_result}"

    trace_line = "已追踪" if scenario.trace_chain else "未追踪"
    rel_line = "已推导（含字段语义）" if scenario.relations else "未推导"
    flow_line = ("已推导" if scenario.flow else "未推导") + (
        f"（{len(scenario.flow.flow_steps)} 节点）" if scenario.flow else ""
    )

    skills_line = (
        f"已落盘技能：{len(scenario.skills)} 个（含主技能与节点子技能）✅ 蒸馏完成，验证请切换验证通道"
        if scenario.skills else "已落盘技能：无"
    )

    context = (
        f"\n\n# 当前业务场景（蒸馏视图）\n名称：{scenario.name}\n"
        f"描述：{scenario.description or '（无）'}\n"
        f"已上传 {len(scenario.tables_meta)} 张表（{role_summary}）\n{roles_line}\n"
        f"数据链路追踪：{trace_line}\n关联+字段语义：{rel_line}\n业务流程：{flow_line}\n"
        f"{skills_line}\n当前状态：{scenario.status.value}\n\n"
        "⚠️ 本通道仅用于蒸馏（推导+生成技能），不提供执行/验证功能。"
        "执行验证请切换到「验证通道」。"
    )
    return create_deep_agent(
        model=llm,
        tools=tools,
        system_prompt=_load_system_prompt() + context,
        middleware=[ExcludeBuiltinToolsMiddleware()],
    )
