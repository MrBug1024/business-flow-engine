"""启发式推导。

当未配置 LLM 时，作为「降级路径」给出确定性的关联/流程/技能推导；
当配置了 LLM 时，这些函数也可作为 Agent 的辅助分析工具（如值重叠率计算）。

推导思路与需求一致：
* 关联关系：字段名语义相似 + 数据类型兼容 + 样本值重叠率。
* 业务流程：以结果表为终点逆向追溯（过滤 → 关联 → 聚合 → 计算）。
* 技能库：将每个流程步骤固化为一个可复用 Skill。
"""

from __future__ import annotations

import re
from difflib import SequenceMatcher

from . import table_io
from .models import (
    FlowResult,
    FlowStep,
    GraphData,
    GraphEdge,
    GraphNode,
    Relation,
    RelationResult,
    Scenario,
    Skill,
    TableMeta,
)

# 结果表 / 规则表的命名特征（用于在流程推导中定位终点与规则节点）
_RESULT_HINTS = ("result", "结果", "summary", "汇总", "output", "报表", "report")
_RULE_HINTS = ("rule", "规则", "config", "配置", "policy", "策略", "dict", "字典")
# 数值/可聚合类型
_NUMERIC_HINTS = ("int", "float", "decimal", "double", "number")


def _normalize(name: str) -> str:
    """归一化字段名，便于相似度比较。"""
    return re.sub(r"[\s_\-]+", "", name.strip().lower())


def _name_similarity(a: str, b: str) -> float:
    na, nb = _normalize(a), _normalize(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    # 一方为另一方的后缀（如 product_id 对 id），视为强信号
    if na.endswith(nb) or nb.endswith(na):
        return 0.9
    return SequenceMatcher(None, na, nb).ratio()


def _dtype_compatible(a: str, b: str) -> bool:
    a, b = a.lower(), b.lower()
    a_num = any(h in a for h in _NUMERIC_HINTS)
    b_num = any(h in b for h in _NUMERIC_HINTS)
    return a_num == b_num


def _is_keyish(col_name: str) -> bool:
    n = _normalize(col_name)
    return n == "id" or n.endswith("id") or n.endswith("code") or n.endswith("no")


def _classify(table: TableMeta) -> str:
    name = table.table_name.lower()
    if any(h in name for h in _RESULT_HINTS):
        return "result"
    if any(h in name for h in _RULE_HINTS):
        return "rule"
    return "table"


# ===========================================================================
# 关联关系推导
# ===========================================================================
def deduce_relations(scenario: Scenario) -> RelationResult:
    tables = scenario.tables_meta
    relations: list[Relation] = []
    questions: list[str] = []

    for i, left in enumerate(tables):
        for right in tables[i + 1:]:
            best = _best_column_match(left, right)
            if best is None:
                continue
            lcol, rcol, score, evidence, rel_type = best
            relations.append(
                Relation(
                    from_table=left.table_name,
                    from_column=lcol,
                    to_table=right.table_name,
                    to_column=rcol,
                    relation_type=rel_type,
                    confidence=round(score, 2),
                    evidence=evidence,
                )
            )
            if score < 0.8:
                questions.append(
                    f"{left.table_name}.{lcol} 与 {right.table_name}.{rcol} "
                    f"是否确为关联键？（置信度 {score:.0%}，请确认）"
                )

    graph = _build_relation_graph(tables, relations)
    confident = [r for r in relations if r.confidence >= 0.8]
    summary = f"共发现 {len(confident)} 条确定关联，{len(relations) - len(confident)} 条待确认关联。"
    return RelationResult(
        relations=relations,
        ambiguous_questions=questions,
        graph_data=graph,
        summary=summary,
    )


def _best_column_match(left: TableMeta, right: TableMeta):
    """在两张表之间寻找最佳关联列对。"""
    best = None
    for lc in left.columns:
        for rc in right.columns:
            if not _dtype_compatible(lc.dtype, rc.dtype):
                continue
            name_sim = _name_similarity(lc.name, rc.name)
            key_bonus = 0.1 if (_is_keyish(lc.name) or _is_keyish(rc.name)) else 0.0
            overlap = _value_overlap(left, lc.name, right, rc.name)
            score = min(0.6 * name_sim + 0.3 * overlap + key_bonus, 0.99)
            if score < 0.55:
                continue
            evidence = (
                f"字段名相似度 {name_sim:.0%}、类型兼容、样本值重叠率 {overlap:.0%}"
            )
            rel_type = "foreign_key" if score >= 0.8 else "possible_link"
            if best is None or score > best[2]:
                best = (lc.name, rc.name, score, evidence, rel_type)
    return best


def _value_overlap(left: TableMeta, lcol: str, right: TableMeta, rcol: str) -> float:
    """计算两列样本值的 Jaccard 重叠率（基于限量样本，避免整表读取）。"""
    try:
        lset = table_io.column_value_set(left.file_path, lcol)
        rset = table_io.column_value_set(right.file_path, rcol)
    except Exception:  # noqa: BLE001
        return 0.0
    if not lset or not rset:
        return 0.0
    inter = len(lset & rset)
    union = len(lset | rset)
    return inter / union if union else 0.0


def _build_relation_graph(tables: list[TableMeta], relations: list[Relation]) -> GraphData:
    nodes = [
        GraphNode(id=t.table_name, label=t.table_name, type=_classify(t)) for t in tables
    ]
    edges = [
        GraphEdge(
            source=r.from_table,
            target=r.to_table,
            label=f"{r.from_column}→{r.to_column}",
        )
        for r in relations
    ]
    return GraphData(nodes=nodes, edges=edges)


# ===========================================================================
# 业务流程推导
# ===========================================================================
def deduce_flow(scenario: Scenario) -> FlowResult:
    tables = scenario.tables_meta
    if not tables:
        return FlowResult(summary="尚无业务表，无法推导流程。")

    result_table = next((t for t in tables if _classify(t) == "result"), tables[-1])
    rule_tables = [t for t in tables if _classify(t) == "rule"]
    source_tables = [
        t for t in tables if t.table_name != result_table.table_name and t not in rule_tables
    ]
    primary = source_tables[0] if source_tables else tables[0]

    steps: list[FlowStep] = []
    step_id = 1

    # 步骤 1：从主业务表过滤有效数据
    steps.append(
        FlowStep(
            step_id=step_id,
            step_name="数据过滤",
            operation="FILTER",
            input_tables=[primary.table_name],
            output="valid_rows",
            logic="筛选有效状态/有效时间范围的记录",
            description=f"从主业务表 {primary.table_name} 中筛选参与计算的有效记录。",
            pseudo_sql=f"SELECT * FROM {primary.table_name} WHERE <有效条件>",
        )
    )
    step_id += 1

    # 步骤 2..n：逐一关联其他业务表
    current = "valid_rows"
    for other in source_tables[1:]:
        steps.append(
            FlowStep(
                step_id=step_id,
                step_name=f"关联 {other.table_name}",
                operation="JOIN",
                input_tables=[current, other.table_name],
                output=f"joined_{step_id}",
                logic=f"{current} JOIN {other.table_name} ON <关联键>",
                description=f"将 {current} 与 {other.table_name} 按关联键连接，补充维度字段。",
                pseudo_sql=f"SELECT * FROM {current} a JOIN {other.table_name} b ON a.<key> = b.<key>",
            )
        )
        current = f"joined_{step_id}"
        step_id += 1

    # 规则表应用
    if rule_tables:
        rule = rule_tables[0]
        steps.append(
            FlowStep(
                step_id=step_id,
                step_name="应用业务规则",
                operation="MAP",
                input_tables=[current, rule.table_name],
                output=f"ruled_{step_id}",
                logic=f"依据 {rule.table_name} 中的规则映射/赋值",
                description=f"按规则表 {rule.table_name} 对记录进行映射、赋值或校验。",
                pseudo_sql=f"SELECT *, r.<value> FROM {current} c JOIN {rule.table_name} r ON c.<key> = r.<key>",
            )
        )
        current = f"ruled_{step_id}"
        step_id += 1

    # 聚合
    steps.append(
        FlowStep(
            step_id=step_id,
            step_name="聚合汇总",
            operation="AGGREGATE",
            input_tables=[current],
            output="summary",
            logic="按维度分组并对数值字段求和/计数",
            description="按业务维度分组聚合，得到汇总指标。",
            pseudo_sql=f"SELECT <dims>, SUM(<metric>) FROM {current} GROUP BY <dims>",
        )
    )
    step_id += 1

    # 结果格式化
    steps.append(
        FlowStep(
            step_id=step_id,
            step_name="结果格式化",
            operation="CALCULATE",
            input_tables=["summary"],
            output=result_table.table_name,
            logic="计算占比/排名等衍生指标，对齐结果表结构",
            description=f"对汇总数据计算衍生指标，并对齐目标结果表 {result_table.table_name} 的结构。",
            pseudo_sql="SELECT *, metric / SUM(metric) OVER() AS ratio FROM summary ORDER BY metric DESC",
        )
    )

    graph = _build_flow_graph(primary, steps, result_table)
    summary = f"{len(steps)} 步业务流程：过滤 → 关联 → {'规则 → ' if rule_tables else ''}聚合 → 格式化。"
    questions = ["上述步骤的关联键与聚合维度是否符合实际业务口径？请确认或修正。"]
    return FlowResult(
        flow_steps=steps, flow_graph=graph, ambiguous_questions=questions, summary=summary
    )


def _build_flow_graph(primary: TableMeta, steps: list[FlowStep], result: TableMeta) -> GraphData:
    nodes = [GraphNode(id="input", label=primary.table_name, type="input")]
    edges: list[GraphEdge] = []
    prev = "input"
    for step in steps:
        nid = f"n{step.step_id}"
        nodes.append(GraphNode(id=nid, label=step.step_name, type="process"))
        edges.append(GraphEdge(source=prev, target=nid, label=step.operation))
        prev = nid
    nodes.append(GraphNode(id="output", label=result.table_name, type="output"))
    edges.append(GraphEdge(source=prev, target="output", label="result"))
    return GraphData(nodes=nodes, edges=edges)


# ===========================================================================
# 技能库生成（数据结构层面；落盘由 skill_builder 负责）
# ===========================================================================
def build_skill_specs(scenario: Scenario) -> list[Skill]:
    steps = scenario.flow.flow_steps if scenario.flow else []
    skills: list[Skill] = [
        Skill(
            skill_id="business_flow_executor",
            name="业务流程总执行器",
            operation="EXECUTE_ALL",
            description="统一入口：对新传入的同结构数据，按序调用各子技能复刻完整业务流程并产出结果。",
            is_main=True,
        )
    ]
    for step in steps:
        skills.append(
            Skill(
                skill_id=f"skill_{step.step_id}_{step.operation.lower()}",
                name=step.step_name,
                operation=step.operation,
                description=step.description,
                step_id=step.step_id,
            )
        )
    return skills
