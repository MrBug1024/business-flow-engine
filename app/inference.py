"""结构化推导。

把「关联关系推导」「业务流程推导」这类需要产出结构化结果的重活，放到工具内部用
`with_structured_output` 完成——而不是让对话模型在 tool 参数里手写大段 JSON
（实践表明那样模型经常「口头声称成功」却不真正落参）。

每个推导函数都遵循同一策略：
    1) 有 LLM → 用结构化输出生成结果；图谱由本地代码统一构建，保证一致性。
    2) 无 LLM 或调用失败 → 回退到 `heuristics` 的确定性推导。
无论走哪条路，都**一定**返回可持久化的结构化结果。
"""

from __future__ import annotations

import json
from typing import Optional

from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field

from . import heuristics
from .llm import get_structured_llm
from .models import (
    FlowResult,
    FlowStep,
    GraphData,
    Relation,
    RelationResult,
    Scenario,
)


# ===========================================================================
# 供 LLM 填充的「精简结构」（图谱不让模型生成，由本地构建）
# ===========================================================================
class _RelationLLM(BaseModel):
    relations: list[Relation] = Field(default_factory=list)
    ambiguous_questions: list[str] = Field(default_factory=list)
    summary: str = ""


class _FlowLLM(BaseModel):
    flow_steps: list[FlowStep] = Field(default_factory=list)
    ambiguous_questions: list[str] = Field(default_factory=list)
    summary: str = ""


# ===========================================================================
# 上下文描述（仅表头 + 少量样本，绝不含全量数据）
# ===========================================================================
def _describe_tables(scenario: Scenario) -> str:
    lines = []
    for t in scenario.tables_meta:
        cols = ", ".join(f"{c.name}({c.dtype})" for c in t.columns)
        samples = json.dumps(t.sample_rows[:2], ensure_ascii=False)
        lines.append(
            f"- 表「{t.table_name}」（约 {t.row_count} 行）\n"
            f"  字段：{cols}\n  样本：{samples}"
        )
    return "\n".join(lines)


def _describe_relations(scenario: Scenario) -> str:
    if not scenario.relations or not scenario.relations.relations:
        return "（暂无已确认的关联关系）"
    return "\n".join(
        f"- {r.from_table}.{r.from_column} → {r.to_table}.{r.to_column}"
        f"（{r.relation_type}，置信度 {r.confidence:.0%}）"
        for r in scenario.relations.relations
    )


# ===========================================================================
# 关联关系推导
# ===========================================================================
_REL_SYSTEM = """你是资深数据工程师。基于给定的多张业务表（仅表头与少量样本），
推导它们之间的关联关系。判断依据：字段名语义、数据类型兼容性、样本值的可关联性、
主外键命名习惯（如 id/code/no 结尾）。
- relation_type 取值：foreign_key（确定外键）/ possible_link（可能关联）/ rule_mapping（经规则表映射）。
- confidence 为 0~1 的小数。
- 对不确定之处，写入 ambiguous_questions，用于与用户对齐。
只输出结构化结果，不要编造不存在的字段。"""


def infer_relations(scenario: Scenario) -> RelationResult:
    """推导关联关系（LLM 结构化优先，失败回退启发式）。"""
    llm = get_structured_llm()
    if llm is None:
        return heuristics.deduce_relations(scenario)
    try:
        prompt = [
            SystemMessage(content=_REL_SYSTEM),
            HumanMessage(content=f"业务场景：{scenario.name}\n{scenario.description}\n\n"
                                 f"业务表如下：\n{_describe_tables(scenario)}\n\n"
                                 f"请推导所有表之间的关联关系。"),
        ]
        # 用 function_calling 模式：结构化结果经由工具调用返回，
        # 可避开 MiniMax 在正文前追加 <think> 导致 json_schema 解析失败的问题。
        out: _RelationLLM = llm.with_structured_output(
            _RelationLLM, method="function_calling"
        ).invoke(prompt)
        if not out.relations:
            return heuristics.deduce_relations(scenario)
        graph: GraphData = heuristics._build_relation_graph(scenario.tables_meta, out.relations)
        return RelationResult(
            relations=out.relations,
            ambiguous_questions=out.ambiguous_questions,
            graph_data=graph,
            summary=out.summary or f"共 {len(out.relations)} 条关联关系。",
        )
    except Exception:  # noqa: BLE001  任何失败都回退，保证一定有结果落盘
        return heuristics.deduce_relations(scenario)


# ===========================================================================
# 业务流程推导
# ===========================================================================
_FLOW_SYSTEM = """你是资深业务分析师。基于业务表结构与已确认的关联关系，
以结果表为终点，逆向推导出从原始数据到最终结果的完整处理流程。
- 每个步骤给出：step_id（从1递增）、step_name、operation（FILTER/JOIN/AGGREGATE/CALCULATE/MAP 等）、
  input_tables、output、logic（业务逻辑）、description（中文说明）、pseudo_sql（伪 SQL）。
- 步骤应当前后衔接：前一步的 output 作为后一步的 input。
- 对不确定的口径（关联键、过滤条件、聚合维度等）写入 ambiguous_questions。
只输出结构化结果。"""


def infer_flow(scenario: Scenario) -> FlowResult:
    """推导业务流程（LLM 结构化优先，失败回退启发式）。"""
    llm = get_structured_llm()
    if llm is None:
        return heuristics.deduce_flow(scenario)
    try:
        prompt = [
            SystemMessage(content=_FLOW_SYSTEM),
            HumanMessage(content=f"业务场景：{scenario.name}\n{scenario.description}\n\n"
                                 f"业务表：\n{_describe_tables(scenario)}\n\n"
                                 f"已确认关联关系：\n{_describe_relations(scenario)}\n\n"
                                 f"请逆向推导完整业务流程。"),
        ]
        out: _FlowLLM = llm.with_structured_output(
            _FlowLLM, method="function_calling"
        ).invoke(prompt)
        if not out.flow_steps:
            return heuristics.deduce_flow(scenario)
        # 规整 step_id，并构建流程图
        for i, step in enumerate(out.flow_steps, start=1):
            step.step_id = i
        tables = scenario.tables_meta
        if tables:
            graph = heuristics._build_flow_graph(tables[0], out.flow_steps, tables[-1])
        else:
            graph = GraphData()
        return FlowResult(
            flow_steps=out.flow_steps,
            flow_graph=graph,
            ambiguous_questions=out.ambiguous_questions,
            summary=out.summary or f"共 {len(out.flow_steps)} 个流程步骤。",
        )
    except Exception:  # noqa: BLE001
        return heuristics.deduce_flow(scenario)
