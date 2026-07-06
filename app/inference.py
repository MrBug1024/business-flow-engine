"""结构化推导（v1.0.4）。

三个核心推导步骤：
* `infer_field_semantics`   字段业务语义推断
* `infer_relations`         表关联（ER）推导，含字段语义
* `infer_flow`              业务流程节点反推

策略：LLM 优先（结构化输出），失败回退启发式。
提示词统一从 prompts/ 目录加载，不在代码中硬编码。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from time import perf_counter
from typing import Optional

_PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"


def _load_prompt(relative_path: str) -> str:
    """从 prompts/ 目录加载提示词文件。"""
    p = _PROMPTS_DIR / relative_path
    if p.exists():
        return p.read_text(encoding="utf-8")
    raise FileNotFoundError(f"提示词文件缺失：{p}")

from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field

from . import clarifications, heuristics, rule_schema, knowledge_schema, table_io, validators
from .llm import get_structured_llm
from .models import (
    ColumnMeta,
    FlowResult,
    FlowStep,
    GraphData,
    GraphEdge,
    GraphNode,
    KnowledgeSchemaMapping,
    Relation,
    RelationResult,
    RuleSchemaMapping,
    Scenario,
    SemanticRole,
    TableMeta,
    TableRole,
)


# ===========================================================================
# 上下文描述（仅表头 + 少量样本 + 角色）
# ===========================================================================
def _describe_tables(scenario: Scenario, include_semantic: bool = False) -> str:
    lines = []
    for t in scenario.tables_meta:
        col_strs = []
        for c in t.columns:
            if include_semantic and c.semantic:
                col_strs.append(f"{c.name}({c.dtype}|{c.semantic_role}|{c.semantic})")
            else:
                col_strs.append(f"{c.name}({c.dtype})")
        cols = ", ".join(col_strs)
        samples = json.dumps(t.sample_rows[:2], ensure_ascii=False)
        lines.append(
            f"- 表「{t.table_name}」[角色={t.role}, ≈{t.row_count}行]\n"
            f"  字段：{cols}\n  样本：{samples}"
        )
    return "\n".join(lines)


def _describe_tables_with_trace(
    scenario: Scenario,
    trace_report: "dict | None",
    include_semantic: bool = False,
) -> str:
    """用追踪采样结果替代随机样本，确保 LLM 看到跨表有因果关联的样本行。

    关键原则：发给 LLM 的样本必须是「哪几条业务行/知识行共同产生了结果行N」，
    而不是「各表独立随机取前2行」。这样 LLM 才能准确推导表间关联和业务流程。
    """
    if not trace_report or trace_report.get("degraded"):
        return _describe_tables(scenario, include_semantic)

    result_sample = trace_report.get("result_sample", [])
    trace_map = trace_report.get("trace_map", {})
    result_table_name = trace_report.get("result_table", "")
    trace_summary = trace_report.get("trace_summary", "")

    lines = []
    if trace_summary and not trace_report.get("degraded"):
        lines.append(f"【追踪采样：{trace_summary}】\n")

    for t in scenario.tables_meta:
        col_strs = []
        for c in t.columns:
            if include_semantic and c.semantic:
                col_strs.append(f"{c.name}({c.dtype}|{c.semantic_role}|{c.semantic})")
            else:
                col_strs.append(f"{c.name}({c.dtype})")
        cols = ", ".join(col_strs)

        if t.table_name == result_table_name and result_sample:
            samples_data = result_sample[:3]
            note = "结果表锚点行（追踪起点）"
        elif t.table_name in trace_map:
            info = trace_map[t.table_name]
            matched = info.get("matched_rows", [])
            by = info.get("matched_by", "?")
            conf = info.get("trace_confidence", "?")
            if matched and by != "random":
                samples_data = matched[:3]
                note = f"追踪样本（置信:{conf}，关联键:{by}）"
            else:
                samples_data = matched[:2] if matched else t.sample_rows[:2]
                note = "随机样本（未追踪到关联行）"
        else:
            samples_data = t.sample_rows[:2]
            note = "随机样本（无追踪路径）"

        samples = json.dumps(samples_data, ensure_ascii=False)
        lines.append(
            f"- 表「{t.table_name}」[角色={t.role}, ≈{t.row_count}行]\n"
            f"  字段：{cols}\n  样本【{note}】：{samples}"
        )
    return "\n".join(lines)


def _describe_trace_chain(trace_report: "dict | None") -> str:
    """将追踪链以 '结果行→业务行→知识行' 格式描述给 LLM，作为推导因果关系的核心依据。"""
    if not trace_report or trace_report.get("degraded"):
        return "（未建立追踪链，样本为随机采样，关联推导参考字段名相似度）"

    result_sample = trace_report.get("result_sample", [])
    result_table = trace_report.get("result_table", "")
    trace_map = trace_report.get("trace_map", {})

    if not result_sample:
        return "（结果表无数据，无法建立追踪链）"

    parts = [f"以下是以「结果表第1行」为锚点，逆向追踪到各表的关联行（因果样本链）：\n"]

    if result_table:
        row0 = result_sample[0]
        parts.append(f"**结果行（{result_table}）**：")
        parts.append(json.dumps(row0, ensure_ascii=False))
        parts.append("")

    for tbl, info in trace_map.items():
        matched = info.get("matched_rows", [])
        by = info.get("matched_by", "?")
        conf = info.get("trace_confidence", "?")
        if matched and by != "random":
            parts.append(f"**↳ 关联到「{tbl}」（关联键={by}，置信度={conf}）**：")
            parts.append(json.dumps(matched[0], ensure_ascii=False))
            if info.get("composite_suggested"):
                parts.append(f"⚠️ {info.get('warning', '')}")
            parts.append("")

    if len(parts) == 2:
        return "（追踪路径均为随机采样，无因果关联链）"

    return "\n".join(parts)


def _trace_quality_questions(trace_report: "dict | None") -> list[str]:
    """把追踪链路质量问题转成少量需要用户判断的业务问题。"""
    if not trace_report or trace_report.get("degraded"):
        return []
    affected: list[str] = []
    for tbl in trace_report.get("unmatched_tables", []) or []:
        if tbl not in affected:
            affected.append(str(tbl))
    for tbl, info in (trace_report.get("trace_map") or {}).items():
        if info.get("matched_by") == "random" and tbl not in affected:
            affected.append(str(tbl))
    if not affected:
        return []
    names = "、".join(affected[:6]) + (" 等" if len(affected) > 6 else "")
    return [
        f"追踪链路没有确认这些表在流程中的作用：{names}。"
        "请确认它们是否参与本次业务流程；如果参与，请补充它们应通过哪个业务编号/对象/单据与其它表连接。"
    ]


def _describe_value_overlap_evidence(candidates: list[Relation]) -> str:
    """把「字段名相似度 + 真实样本值包含率」候选证据格式化给 LLM。

    这是判断关联关系的**硬证据**：字段名像不像只是弱信号，两列的实际取值是否有
    真实交集才是强信号（如同一批业务单号/编码同时出现在两张表里，即便列名完全不同，
    也应判为高置信度关联；反之列名相似但取值域完全不重叠，不应仅凭名字断定关联）。
    """
    if not candidates:
        return "（未计算出候选关联证据——可能各表字段数据在样本窗口内无重叠，或表数不足两张）"
    ranked = sorted(candidates, key=lambda r: -r.confidence)[:40]
    lines = []
    for r in ranked:
        lines.append(
            f"- {r.from_table}.{r.from_column} ↔ {r.to_table}.{r.to_column}："
            f"{r.evidence}（综合置信度 {r.confidence:.0%}）"
        )
    return "\n".join(lines)


def _describe_relations(scenario: Scenario) -> str:
    if not scenario.relations or not scenario.relations.relations:
        return "（暂无已确认的关联关系）"
    lines = []
    for r in scenario.relations.relations:
        f_cols = r.from_columns or [r.from_column]
        t_cols = r.to_columns or [r.to_column]
        if len(f_cols) > 1:
            cols = (f"{r.from_table}.({'+'.join(f_cols)}) → {r.to_table}.({'+'.join(t_cols)})"
                    "【复合键：所有列同时相等才算对应】")
        else:
            cols = f"{r.from_table}.{r.from_column} → {r.to_table}.{r.to_column}"
        tag = "✅人工确认" if r.confirmed else f"置信度 {r.confidence:.0%}"
        lines.append(f"- {cols}（{r.relation_type}，{tag}）")
    return "\n".join(lines)


def _describe_knowledge_schema(
    ks: KnowledgeSchemaMapping | RuleSchemaMapping | None,
) -> str:
    """统一描述知识表/规则表结构映射（兼容新旧两种模型）。"""
    if ks is None:
        return "（本场景无知识表，或知识表结构尚未推导）"
    if isinstance(ks, KnowledgeSchemaMapping):
        parts = [
            f"知识表：{ks.knowledge_table}",
            f"分派键列：{ks.dispatch_key_column or '（未识别）'}",
            f"分派值→处理模式：{ks.dispatch_map}",
            f"条目编号列：{ks.item_id_column or '（未识别）'}",
            f"自然语言条件列：{ks.condition_columns}",
            f"参数列：{ks.parameter_columns}",
            f"字段角色映射：{ks.field_role_map}",
        ]
    else:
        # 向后兼容旧 RuleSchemaMapping
        parts = [
            f"知识表（旧称规则表）：{ks.rule_table}",
            f"分派键列（旧称分派列）：{ks.discriminator_column or '（未识别）'}",
            f"分派值→处理模式：{ks.discriminator_to_template}",
            f"条目编号列（旧称规则编号列）：{ks.rule_id_column or '（未识别）'}",
            f"自然语言条件列：{ks.nl_description_columns}",
            f"参数列：{ks.parameter_columns}",
            f"字段角色映射（旧称业务字段映射）：{ks.business_field_map}",
        ]
    return "\n".join(parts)


def _describe_rule_schema(rs: RuleSchemaMapping | None) -> str:
    """向后兼容的别名，委托给 _describe_knowledge_schema。"""
    return _describe_knowledge_schema(rs)


# ===========================================================================
# 字段语义推导（v1.0.3 新增）
# ===========================================================================
class _ColumnSemantic(BaseModel):
    table: str
    column: str
    semantic: str = ""                                    # 业务含义（中文）
    semantic_role: str = SemanticRole.UNKNOWN.value       # PK/FK/DIM/METRIC/TIME/NL_TEXT/CATEGORY/UNKNOWN


class _SemanticsLLM(BaseModel):
    columns: list[_ColumnSemantic] = Field(default_factory=list)


def _get_sem_system() -> str:
    return _load_prompt("inference/field_semantics.md")


def _validate_role(role: str) -> str:
    if role in {r.value for r in SemanticRole}:
        return role
    return SemanticRole.UNKNOWN.value


def _heuristic_semantics(table: TableMeta) -> list[_ColumnSemantic]:
    """无 LLM 时的兜底：基于字段名形态给出粗略的语义角色与含义。"""
    from . import strategies as st

    out: list[_ColumnSemantic] = []
    for c in table.columns:
        name = c.name
        dt = (c.dtype or "").lower()
        role = SemanticRole.UNKNOWN.value
        semantic = name  # 兜底含义=字段名

        if st.is_key_like(name):
            role = SemanticRole.PK.value if name.lower() in ("id",) else SemanticRole.FK.value
        elif any(t in dt for t in ("date", "time")):
            role = SemanticRole.TIME.value
        elif st.is_numeric_dtype(c.dtype):
            role = SemanticRole.METRIC.value
        else:
            # 文本：按名称长度与样本值粗判
            sample_avg_len = (sum(len(str(v)) for v in c.sample_values) / max(len(c.sample_values), 1)) if c.sample_values else 0
            if sample_avg_len > 30:
                role = SemanticRole.NL_TEXT.value
            elif any(h in name for h in ("类型", "类别", "等级", "状态", "category", "type", "level")):
                role = SemanticRole.CATEGORY.value
            else:
                role = SemanticRole.DIM.value
        out.append(_ColumnSemantic(
            table=table.table_name, column=name,
            semantic=semantic, semantic_role=role,
        ))
    return out


def infer_field_semantics(scenario: Scenario) -> dict[str, dict[str, tuple[str, str]]]:
    """推断每个字段的业务含义 + 语义角色。

    返回结构：{表名: {字段名: (semantic, semantic_role)}}
    同时**原地**更新 scenario.tables_meta 的 ColumnMeta.semantic / semantic_role。
    """
    llm = get_structured_llm()
    results: list[_ColumnSemantic] = []

    if llm is not None:
        try:
            prompt = [
                SystemMessage(content=_get_sem_system()),
                HumanMessage(content=f"业务场景：{scenario.name}\n{scenario.description}\n\n"
                                     f"业务表如下：\n{_describe_tables(scenario)}\n\n"
                                     f"请为每个字段给出业务含义与语义角色。"),
            ]
            out: _SemanticsLLM = llm.with_structured_output(
                _SemanticsLLM, method="function_calling"
            ).invoke(prompt)
            results = out.columns
        except Exception:  # noqa: BLE001
            results = []

    if not results:
        # 兜底：逐表启发式
        for t in scenario.tables_meta:
            results.extend(_heuristic_semantics(t))

    # 原地更新 + 收集字典
    index: dict[str, dict[str, tuple[str, str]]] = {}
    by_table: dict[str, dict[str, _ColumnSemantic]] = {}
    for cs in results:
        by_table.setdefault(cs.table, {})[cs.column] = cs

    for t in scenario.tables_meta:
        per_col = by_table.get(t.table_name, {})
        index[t.table_name] = {}
        for c in t.columns:
            cs = per_col.get(c.name)
            if cs is not None:
                c.semantic = cs.semantic or c.semantic or c.name
                c.semantic_role = _validate_role(cs.semantic_role)
            else:
                # LLM 漏掉的字段也补一份启发式
                hs = next((x for x in _heuristic_semantics(t) if x.column == c.name), None)
                if hs:
                    c.semantic = c.semantic or hs.semantic
                    c.semantic_role = c.semantic_role if c.semantic_role != SemanticRole.UNKNOWN.value else hs.semantic_role
            index[t.table_name][c.name] = (c.semantic, c.semantic_role)
    return index


# ===========================================================================
# 关联关系推导（叠加字段语义，结果合并返回）
# ===========================================================================
class _RelationLLM(BaseModel):
    relations: list[Relation] = Field(default_factory=list)
    ambiguous_questions: list[str] = Field(default_factory=list)
    summary: str = ""


def _get_rel_system() -> str:
    return _load_prompt("inference/relations.md")


def infer_relations(scenario: Scenario) -> RelationResult:
    """推导关联关系（**先**推字段语义，**再**推关联，结果合并返回）。

    v1.1 改进：只复用独立「数据链路追踪」阶段保存的样本，不在关联推导里隐式重跑大表追踪。
    """
    timings: dict[str, float] = {}
    t0 = perf_counter()
    semantics = infer_field_semantics(scenario)
    timings["字段语义"] = perf_counter() - t0

    # 关联推导只消费已保存的独立「数据链路追踪」阶段产物，不在这里隐式重跑大表追踪。
    trace_report = scenario.trace_chain or (
        scenario.relations.trace_chain if scenario.relations else {}
    )

    # 真实值重叠证据：字段名相似度 + 全字段对的样本值包含率（唯一权威证据来源）
    rel_stats: dict[str, int] = {}
    t0 = perf_counter()
    try:
        value_evidence_candidates = heuristics.candidate_relations(
            scenario,
            trace_report=trace_report,
            stats=rel_stats,
        )
    except Exception:  # noqa: BLE001
        value_evidence_candidates = []
    timings["值证据"] = perf_counter() - t0
    value_evidence = _describe_value_overlap_evidence(value_evidence_candidates)

    llm = get_structured_llm()
    relations: list[Relation] = []
    questions: list[str] = []
    summary: str = ""

    if llm is not None:
        t0 = perf_counter()
        try:
            tables_desc = _describe_tables_with_trace(scenario, trace_report, include_semantic=True)
            trace_chain = _describe_trace_chain(trace_report)
            prompt = [
                SystemMessage(content=_get_rel_system()),
                HumanMessage(content=(
                    f"业务场景：{scenario.name}\n{scenario.description}\n\n"
                    f"业务表（含字段语义角色，样本为追踪采样结果）：\n{tables_desc}\n\n"
                    f"因果追踪链（结果行→业务行→知识行，用于判断表间关联）：\n{trace_chain}\n\n"
                    f"真实值重叠证据（对全部字段对计算的名称相似度 + 样本值包含率——较小一侧的值有多大"
                    f"比例能在另一侧找到，例如结果表只有25行、业务表有几十万行时依然能正确反映"
                    f"'结果表这25个值是否都能在业务表里查到'，不会因为业务表本身很大就被稀释成0%；"
                    f"这是判断关联的硬证据，优先级高于仅凭字段名或个别样本的猜测）：\n{value_evidence}\n\n"
                    "请基于以上真实值重叠证据、追踪链和字段语义推导所有表之间的关联关系。"
                    "字段名相似但值域不重叠的，不要判定为关联；包含率高的，即使字段名不像也应重点考虑。"
                )),
            ]
            out: _RelationLLM = llm.with_structured_output(
                _RelationLLM, method="function_calling"
            ).invoke(prompt)
            relations = out.relations
            questions = out.ambiguous_questions
            summary = out.summary
        except Exception:  # noqa: BLE001
            relations = []
        timings["LLM"] = perf_counter() - t0

    if not relations:
        relations = list(value_evidence_candidates)
        questions = []
        summary = "已根据追踪样本和真实值重叠证据生成候选关联。"

    # 剔除引用了不存在表/字段的"幻觉"关联（如凭结果文件名联想出一个根本不存在的列）
    relations, bad_relation_questions = validators.sanitize_relations(relations, scenario.tables_meta)
    questions = list(questions) + bad_relation_questions

    # 弱关联候选不进入图，也不再追问普通用户。缺少足够值证据时先保守跳过。
    relations, weak_relation_count = validators.filter_low_confidence_relations(relations)
    if not relations and value_evidence_candidates:
        evidence_relations, evidence_dropped = validators.filter_low_confidence_relations(
            value_evidence_candidates
        )
        if evidence_relations:
            relations = evidence_relations
            weak_relation_count += evidence_dropped
            if not summary:
                summary = "已根据真实值重叠证据回退生成高置信关联。"

    # 人工确认过的关联必须原样保留，不能被这一轮新推导覆盖/丢弃
    old_relations = scenario.relations.relations if scenario.relations else None
    relations = validators.preserve_confirmed_relations(old_relations, relations)

    graph = heuristics._build_relation_graph(scenario.tables_meta, relations)

    # 复合键建议：追踪层交叉校验发现"单列匹配不唯一、需组合列才能收窄"时，
    # 一律作为待确认问题抛给用户（此前只有无 LLM 的启发式路径带出这个信号，
    # LLM 路径会把它悄悄吞掉）。
    for _tbl, _info in ((trace_report or {}).get("trace_map") or {}).items():
        if _info.get("composite_suggested") and _info.get("warning"):
            _q = f"【{_tbl}】{_info['warning']}"
            if _q not in questions:
                questions.append(_q)
    questions.extend(_trace_quality_questions(trace_report))

    field_sem_payload: dict[str, dict[str, str]] = {}
    for tname, cols in semantics.items():
        field_sem_payload[tname] = {
            name: f"{sem}（{role}）" for name, (sem, role) in cols.items()
        }

    if not summary:
        confident = sum(1 for r in relations if r.confidence >= 0.8)
        summary = (f"共发现 {confident} 条确定关联、{len(relations) - confident} 条待确认；"
                   f"字段语义已为 {sum(len(v) for v in semantics.values())} 个字段标注。")
    if weak_relation_count:
        summary += f" 已忽略 {weak_relation_count} 条缺少足够值证据的弱关联候选。"
    detail = (
        f" 执行明细：字段语义 {timings.get('字段语义', 0):.1f}s；"
        f"链路样本 {'已复用' if trace_report else '未提供'}；"
        f"值证据 {timings.get('值证据', 0):.1f}s"
        f"（实际比较 {rel_stats.get('value_compared', 0)} 对，"
        f"预筛跳过 {rel_stats.get('prefilter_skipped', 0)} 对，"
        f"类型跳过 {rel_stats.get('dtype_skipped', 0)} 对）；"
        f"LLM {timings.get('LLM', 0):.1f}s。"
    )
    summary = (summary or "") + detail

    clarification_items = clarifications.build_clarifications(
        questions, context="deduce_relations"
    )
    questions = clarifications.normalized_question_texts(clarification_items)

    return RelationResult(
        relations=relations,
        ambiguous_questions=questions,
        clarifications=clarification_items,
        graph_data=graph,
        summary=summary,
        field_semantics=field_sem_payload,
        # 保存这一轮算出的因果链，推流程时直接复用，不用重新对大表搜一遍
        trace_chain=trace_report or {},
    )


# ===========================================================================
# 业务流程推导（v1.0.3 重写：节点带可读能力描述 + 模板算子）
# ===========================================================================
class _FlowStepLLM(BaseModel):
    """供 LLM 填充的「节点结构」。字段名与 FlowStep 对齐，便于直接映射。"""
    step_id: int
    step_name: str
    operation: str = ""                                  # FILTER/JOIN/RULE_DRIVEN/AGGREGATE/CALCULATE/FORMAT
    purpose: str = ""                                    # 该做什么（业务目标）
    capability: str = ""                                 # 能做什么 / 输出什么数据
    data_in: list[str] = Field(default_factory=list)     # 输入数据的可读说明
    data_out: list[str] = Field(default_factory=list)    # 输出数据的可读说明
    template_kind: str = ""                              # passthrough/dedup/threshold/keyword/aggregate/join/column_select/lookup/formula/set_compare/knowledge_driven_join/sql（任意自定义逻辑写 sql 直接给 DuckDB 语句，不必迁就前面的枚举）
    input_tables: list[str] = Field(default_factory=list)
    output: str = ""
    output_columns: list[str] = Field(default_factory=list)
    description: str = ""
    pseudo_sql: str = ""
    strategy: str = ""
    params: dict = Field(default_factory=dict)


class _FlowLLM(BaseModel):
    flow_steps: list[_FlowStepLLM] = Field(default_factory=list)
    ambiguous_questions: list[str] = Field(default_factory=list)
    summary: str = ""


def _get_flow_system() -> str:
    return _load_prompt("inference/flow.md")


def _build_flow_mermaid(steps: list[FlowStep]) -> str:
    """从流程节点直接生成 Mermaid 业务流程图（含可读能力标签）。"""
    if not steps:
        return ""
    lines = ["flowchart LR"]
    for s in steps:
        clean_name = (s.step_name or "").replace('"', "'")
        kind = s.template_kind or s.operation
        label = f"步骤{s.step_id}<br/>{clean_name}<br/>[{kind}]"
        nid = f"n{s.step_id}"
        lines.append(f'  {nid}["{label}"]')
    for i in range(len(steps) - 1):
        lines.append(f"  n{steps[i].step_id} --> n{steps[i + 1].step_id}")
    return "\n".join(lines)


def _build_flow_graph(steps: list[FlowStep]) -> GraphData:
    """从流程节点生成 GraphData（用于 SVG 图谱）。"""
    nodes = [GraphNode(id=f"n{s.step_id}", label=f"{s.step_name}",
                       type="process") for s in steps]
    edges = [GraphEdge(source=f"n{steps[i].step_id}",
                       target=f"n{steps[i + 1].step_id}",
                       label=steps[i + 1].operation)
             for i in range(len(steps) - 1)]
    return GraphData(nodes=nodes, edges=edges)


def _llm_step_to_model(s: _FlowStepLLM) -> FlowStep:
    return FlowStep(
        step_id=s.step_id,
        step_name=s.step_name,
        operation=s.operation,
        purpose=s.purpose,
        capability=s.capability,
        data_in=s.data_in,
        data_out=s.data_out,
        template_kind=s.template_kind,
        input_tables=s.input_tables,
        output=s.output,
        output_columns=s.output_columns,
        description=s.description,
        pseudo_sql=s.pseudo_sql,
        strategy=s.strategy or s.template_kind,
        params=s.params,
        status="draft",
    )


def _heuristic_flow(scenario: Scenario, rs: RuleSchemaMapping | None) -> list[FlowStep]:
    """无 LLM 时的兜底流程节点（结构化的 4~5 步骨架，节点带能力描述）。"""
    inputs = [t for t in scenario.tables_meta if t.role == TableRole.INPUT.value]
    results = [t for t in scenario.tables_meta if t.role == TableRole.RESULT.value]

    if not inputs:
        return []
    primary = inputs[0]
    pk_cols = [c.name for c in primary.columns if c.semantic_role == SemanticRole.PK.value]
    fk_cols = [c.name for c in primary.columns if c.semantic_role == SemanticRole.FK.value]
    nl_cols_input = [c.name for c in primary.columns if c.semantic_role == SemanticRole.NL_TEXT.value]
    cat_cols_input = [c.name for c in primary.columns if c.semantic_role == SemanticRole.CATEGORY.value]
    metric_cols = [c.name for c in primary.columns if c.semantic_role == SemanticRole.METRIC.value]

    steps: list[FlowStep] = []
    sid = 1

    # 节点 1：业务数据进入（passthrough）
    steps.append(FlowStep(
        step_id=sid,
        step_name=f"读取业务数据「{primary.table_name}」",
        operation="FILTER",
        purpose=f"从业务输入表 {primary.table_name} 读取本次要处理的数据",
        capability="输出与原表同结构的有效数据子集，供下游加工",
        data_in=[f"{primary.table_name}（约 {primary.row_count} 行，{primary.col_count} 列）"],
        data_out=[f"{primary.table_name}（已过滤无效行）"],
        template_kind="passthrough",
        input_tables=[primary.table_name],
        output=f"step_{sid}_input",
        strategy="passthrough",
        params={"source": primary.table_name},
        status="draft",
    ))
    sid += 1

    # 节点 2：与其他业务表关联（若有多张）
    other_inputs = inputs[1:]
    if other_inputs:
        joins = [{"table": t.table_name, "left": "<待定>", "right": "<待定>"} for t in other_inputs]
        steps.append(FlowStep(
            step_id=sid,
            step_name=f"关联其他业务表（{'、'.join(t.table_name for t in other_inputs)}）",
            operation="JOIN",
            purpose="把维度表/补充表按业务键关联到主表，补全后续判定所需字段",
            capability="输出已补维度信息的明细行",
            data_in=[f"上一步输出 + {'、'.join(t.table_name for t in other_inputs)}"],
            data_out=["补充了维度字段的业务明细"],
            template_kind="join",
            input_tables=[f"step_{sid - 1}_input", *(t.table_name for t in other_inputs)],
            output=f"step_{sid}_joined",
            strategy="join",
            params={"base": f"step_{sid - 1}_input", "joins": joins},
            status="draft",
            external_data_needed=["待 AI/用户确认每个 JOIN 的关联键"],
        ))
        sid += 1

    # 节点 3：应用知识条目（若有知识表，且已识别知识结构）
    if rs and rs.rule_table:
        steps.append(FlowStep(
            step_id=sid,
            step_name="知识驱动判定",
            operation="KNOWLEDGE_DRIVEN",
            purpose=(f"以知识表「{rs.rule_table}」逐行迭代，按分派列「{rs.discriminator_column or '?'}」"
                     "分派到该分派值专属的执行逻辑，应用到业务明细上，得到命中行"),
            capability=("输出命中知识条目的业务行（附带条目编号 / 分派值，便于回溯）；"
                        "知识条目有几万条也能跑——每个分派值只需推导一次执行逻辑，按行复用"),
            data_in=[f"上一步业务明细 + 知识表「{rs.rule_table}」（每行一条知识条目）"],
            data_out=["命中的业务明细 + 知识上下文（条目编号、分派值、依据）"],
            template_kind="knowledge_driven_join",
            input_tables=[steps[-1].output, rs.rule_table],
            output=f"step_{sid}_ruled",
            strategy="knowledge_driven_join",
            params={
                "source": steps[-1].output,
                "knowledge_table": rs.rule_table,
                "dispatch_key": rs.discriminator_column,
                "item_id": rs.rule_id_column,
            },
            external_data_needed=[
                "无 LLM 时无法为每个分派值派生+校验专属 SQL；请配置 LLM 后重新推导，"
                "或用 refine_flow_step 手工为每个分派值提供 sql。",
            ],
            status="blocked",
        ))
        sid += 1

    # 节点 4：对齐历史结果结构（FORMAT）
    if results:
        rt = results[0]
        rcols = [c.name for c in rt.columns]
        steps.append(FlowStep(
            step_id=sid,
            step_name=f"对齐输出结构「{rt.table_name}」",
            operation="FORMAT",
            purpose="选择/重命名/补齐列，使输出与历史结果表同结构、同格式",
            capability=f"输出 {len(rcols)} 列：{ '、'.join(rcols[:8]) }{ '…' if len(rcols) > 8 else '' }",
            data_in=["上游输出"],
            data_out=[f"{rt.table_name}（同结构）"],
            template_kind="passthrough",
            input_tables=[steps[-1].output],
            output_columns=rcols,
            output=f"step_{sid}_output",
            strategy="passthrough",
            params={"source": steps[-1].output},
            status="draft",
        ))
        sid += 1

    return steps


# ===========================================================================
# 知识表结构映射推导（LLM 依据真实样本值判断字段角色，启发式仅作先验/兜底）
# ===========================================================================
class _KnowledgeSchemaLLM(BaseModel):
    dispatch_key_column: str = ""
    item_id_column: str = ""
    condition_columns: list[str] = Field(default_factory=list)
    parameter_columns: list[str] = Field(default_factory=list)
    dispatch_map: dict[str, str] = Field(default_factory=dict)
    field_role_map: dict[str, str] = Field(default_factory=dict)
    ambiguous_questions: list[str] = Field(default_factory=list)
    summary: str = ""


def _get_ks_system() -> str:
    return _load_prompt("inference/knowledge_schema.md")


def infer_knowledge_schema_llm(
    scenario: Scenario,
    nl_analysis: dict | None = None,
) -> tuple[Optional[KnowledgeSchemaMapping], list[str]]:
    """知识表结构映射：LLM 依据知识表**真实样本值** + NL 规则模式判断字段角色与
    dispatch_map，而不是像纯启发式那样只凭字段名关键词命中（"编号"→item_id，
    "名称"→item_name 命中的第一列就用，不管值对不对）。无 LLM / 调用失败时
    回退到纯启发式先验，保证始终有一个可用结果。

    返回 (KnowledgeSchemaMapping | None, ambiguous_questions)。
    """
    heuristic = knowledge_schema.infer_knowledge_schema(scenario)
    kt = knowledge_schema.find_knowledge_table(scenario)
    if kt is None:
        return None, []

    llm = get_structured_llm()
    if llm is None:
        return heuristic, []

    if nl_analysis is None:
        try:
            from . import nl_rule_analyzer  # noqa: PLC0415
            nl_analysis = nl_rule_analyzer.analyze_nl_rules(scenario)
        except Exception:  # noqa: BLE001
            nl_analysis = {}

    business_tables = [t for t in scenario.tables_meta if t.role in (TableRole.INPUT.value, "input")]
    kt_cols = ", ".join(
        f"{c.name}({c.dtype}|{c.semantic_role}"
        + (f"|样本:{c.sample_values[:5]}" if c.sample_values else "") + ")"
        for c in kt.columns
    ) or "（知识表未识别出任何列——可能是非结构化文本文件，请如实反映在 summary 中）"
    biz_desc = "\n".join(
        f"- 表「{t.table_name}」字段：" + ", ".join(
            f"{c.name}({c.dtype}|{c.semantic_role})" for c in t.columns
        )
        for t in business_tables
    ) or "（无业务输入表）"

    heuristic_hint = ""
    if heuristic:
        heuristic_hint = (
            "启发式先验（仅凭字段名关键词猜测，可能有误，请结合真实样本值校正而非照抄）：\n"
            f"候选分派键列={heuristic.dispatch_key_column or '未识别'}，"
            f"候选条目编号列={heuristic.item_id_column or '未识别'}，"
            f"候选条件列={heuristic.condition_columns}，"
            f"候选参数列={heuristic.parameter_columns}，"
            f"候选分派映射={heuristic.dispatch_map}，"
            f"候选字段角色映射={heuristic.field_role_map}\n"
        )
    nl_hint = ""
    if nl_analysis and nl_analysis.get("has_nl_rules"):
        from . import nl_rule_analyzer  # noqa: PLC0415
        nl_hint = ("NL 规则模式分析（从知识表条件文本启发式识别出的模式分布，"
                   "可直接作为 dispatch_map 的候选来源）：\n"
                   + nl_rule_analyzer.format_nl_analysis(nl_analysis) + "\n")

    try:
        prompt = [
            SystemMessage(content=_get_ks_system()),
            HumanMessage(content=(
                f"业务场景：{scenario.name}\n{scenario.description}\n\n"
                f"知识表「{kt.table_name}」字段（含类型/语义角色/真实样本值）：\n{kt_cols}\n\n"
                f"业务表字段（field_role_map 的目标列必须来自这里，不要编造不存在的列）：\n{biz_desc}\n\n"
                f"{heuristic_hint}\n{nl_hint}\n"
                "请依据知识表字段的**真实样本值**判断各列角色与 dispatch_map（而不是仅凭字段名）；"
                "若知识表本身是非结构化文本（如整份是一段自然语言、无法拆出分派键列），"
                "如实在 summary 中说明，并把无法归类的部分写入 ambiguous_questions，不要编造字段。"
            )),
        ]
        out: Optional[_KnowledgeSchemaLLM] = llm.with_structured_output(
            _KnowledgeSchemaLLM, method="function_calling"
        ).invoke(prompt)
    except Exception:  # noqa: BLE001
        out = None
    if out is None:
        # 部分模型在函数调用解析失败时不抛异常、直接返回 None——必须显式判空，
        # 不能假设 .invoke() 一定给出对象（这正是"'NoneType' object has no
        # attribute ..."这类崩溃的根源），否则退化路径形同虚设。
        return heuristic, []

    # 合并：LLM 给出的非空值优先，字段级回退到启发式先验（不因 LLM 遗漏个别字段而整体退化）
    merged = KnowledgeSchemaMapping(
        knowledge_table=kt.table_name,
        dispatch_key_column=out.dispatch_key_column or (heuristic.dispatch_key_column if heuristic else ""),
        item_id_column=out.item_id_column or (heuristic.item_id_column if heuristic else ""),
        condition_columns=out.condition_columns or (heuristic.condition_columns if heuristic else []),
        parameter_columns=out.parameter_columns or (heuristic.parameter_columns if heuristic else []),
        dispatch_map=out.dispatch_map or (heuristic.dispatch_map if heuristic else {}),
        field_role_map=out.field_role_map or (heuristic.field_role_map if heuristic else {}),
        summary=out.summary or (heuristic.summary if heuristic else ""),
    )

    # v1.0.7 铁律：不再为每个分派值预先派生+固化一条 SQL。规则可能有成百上千条，
    # 每条的判断逻辑千差万别，提前逐条"解题"既跑不完也不可能穷尽真实业务的各种要求。
    # 知识表结构映射到此为止——它只负责说清楚"知识表长什么样、字段怎么对应业务表"，
    # 真正"这条规则该怎么查"交给验证通道里会思考的 LLM，用 search_knowledge 读到
    # 规则原文 + describe_schema 看到真实字段后，现场用 query_data 构造查询，
    # 而不是让平台在蒸馏阶段替它把所有可能的规则都预先写成代码。
    return merged, out.ambiguous_questions


def infer_flow(scenario: Scenario) -> FlowResult:
    """推导业务流程（先蒸馏知识结构映射，再据结构反推流程节点）。

    v1.1 改进：
    1. 复用独立「数据链路追踪」阶段保存的样本，LLM 看到因果关联样本而非随机行
    2. 加入 NL 规则分析，帮助识别知识表中自然语言规则的模式
    3. 追踪链作为推导依据，节点 capability 更准确
    """
    if not scenario.tables_meta:
        return FlowResult(summary="尚未上传业务表。")

    # 0. 追踪采样（以结果表为入口逆向找关联行）——只复用独立「数据链路追踪」
    #    或人工修正关联后保存的结果，不在流程推导里隐式重跑大表追踪。
    trace_report = None
    if scenario.trace_chain and not scenario.trace_chain.get("degraded"):
        trace_report = scenario.trace_chain
    elif scenario.relations and scenario.relations.trace_chain and not scenario.relations.trace_chain.get("degraded"):
        trace_report = scenario.relations.trace_chain

    # 1. NL 规则模式分析（先算，供知识表结构映射与流程推导共用）
    nl_analysis: dict = {}
    nl_analysis_desc = ""
    try:
        from . import nl_rule_analyzer  # noqa: PLC0415
        nl_analysis = nl_rule_analyzer.analyze_nl_rules(scenario)
        if nl_analysis.get("has_nl_rules"):
            nl_analysis_desc = nl_rule_analyzer.format_nl_analysis(nl_analysis)
    except Exception:  # noqa: BLE001
        pass

    # 2. 蒸馏知识表结构映射：LLM 依据知识表真实样本值判断字段角色/dispatch_map，
    #    而不是只凭字段名关键词猜测；无 LLM 或调用失败时自动回退启发式先验。
    ks, ks_questions = infer_knowledge_schema_llm(scenario, nl_analysis=nl_analysis)
    rs = rule_schema.infer_rule_schema(scenario)             # v1.0.3 兼容版（纯启发式镜像）

    # 3. LLM 推流程节点（带能力描述与模板算子）
    llm = get_structured_llm()
    steps: list[FlowStep] = []
    questions: list[str] = []
    summary: str = ""
    llm_flow_error: str = ""   # LLM 推导失败原因——必须让用户知道，绝不静默降级

    if llm is not None:
        tables_desc = _describe_tables_with_trace(scenario, trace_report, include_semantic=True)
        trace_chain = _describe_trace_chain(trace_report)
        prompt = [
            SystemMessage(content=_get_flow_system()),
            HumanMessage(content=(
                f"业务场景：{scenario.name}\n{scenario.description}\n\n"
                f"业务表（含语义角色，样本为追踪采样结果）：\n{tables_desc}\n\n"
                f"已确认关联：\n{_describe_relations(scenario)}\n\n"
                f"知识表结构映射：\n{_describe_knowledge_schema(ks or rs)}\n\n"
                + (f"NL 规则模式分析：\n{nl_analysis_desc}\n\n" if nl_analysis_desc else "")
                + f"因果追踪链（结果行→业务行→知识行）：\n{trace_chain}\n\n"
                "请基于以上因果追踪链反推业务流程节点链，每个节点严格按要求填齐字段。\n"
                "追踪链展示了哪些业务行/知识行共同产生了哪条结果行，据此推导各节点的输入输出和能力。"
            )),
        ]
        # 结构化输出偶发失败（函数调用解析错/网络抖动）——重试一次再认输，
        # 且失败原因必须被记录并呈现给用户，不能悄悄换成启发式骨架冒充推导结果。
        for attempt in (1, 2):
            try:
                out: _FlowLLM | None = llm.with_structured_output(
                    _FlowLLM, method="function_calling"
                ).invoke(prompt)
                # 部分模型在函数调用解析失败时不抛异常、直接返回 None——必须显式判空
                # （infer_knowledge_schema_llm 已吃过同样的亏，这里同样要防）。
                if out is None:
                    llm_flow_error = "模型返回了空的结构化结果（函数调用解析失败）"
                    continue
                if out.flow_steps:
                    steps = [_llm_step_to_model(s) for s in out.flow_steps]
                    questions = list(out.ambiguous_questions)
                    summary = out.summary
                    llm_flow_error = ""
                    break
                llm_flow_error = "模型返回了 0 个流程节点"
            except Exception as exc:  # noqa: BLE001
                llm_flow_error = f"{type(exc).__name__}: {str(exc)[:200]}"

    # 兜底：启发式骨架——但必须显式告知这是降级结果，不能冒充 LLM 推导
    degraded_to_heuristic = False
    if not steps:
        steps = _heuristic_flow(scenario, rs)
        degraded_to_heuristic = bool(steps)
        has_knowledge = ks is not None or rs is not None
        if llm is None:
            summary = (f"已生成 {len(steps)} 步流程骨架（启发式，未配置 LLM）。"
                       + ("含知识驱动节点。" if has_knowledge else ""))
        else:
            summary = (
                f"⚠️ LLM 流程推导失败（{llm_flow_error or '未知原因'}，已重试仍未成功），"
                f"当前展示的是 {len(steps)} 步**启发式通用骨架**，节点较粗、可能无法完整还原"
                "业务场景。建议重新发送「推导业务流程」重试；若持续失败请检查 LLM 配置。"
            )
    if degraded_to_heuristic and llm is not None:
        questions.append(
            "⚠️ 本次流程节点来自启发式降级（LLM 推导失败："
            f"{llm_flow_error or '未知原因'}），不是基于因果追踪链的真实推导。"
            "是否重新推导？（重新发送「推导业务流程」即可重试）"
        )

    # 3. 规整 step_id（确保 1..N 递增）
    for i, s in enumerate(steps, start=1):
        s.step_id = i

    # 4. 校验：节点 params 是否把知识表/业务表某一具体条目的字面值硬编码了
    #    （这会导致技能只能处理这一条记录，无法泛化到知识表里的其它同类条目）
    try:
        literal_findings = validators.detect_literal_params(steps, scenario.tables_meta)
    except Exception:  # noqa: BLE001
        literal_findings = []

    all_questions = list(ks_questions) + list(questions) + literal_findings + _trace_quality_questions(trace_report)
    clarification_items = clarifications.build_clarifications(
        all_questions, context="deduce_flow"
    )
    all_questions = clarifications.normalized_question_texts(clarification_items)

    # 5. 生成图与 Mermaid
    graph = _build_flow_graph(steps)
    mermaid = _build_flow_mermaid(steps)

    return FlowResult(
        flow_steps=steps,
        flow_graph=graph,
        mermaid=mermaid,
        ambiguous_questions=all_questions,
        clarifications=clarification_items,
        knowledge_schema=ks,  # v1.0.4 通用知识表结构映射（LLM 优先，启发式兜底）
        rule_schema=rs,       # v1.0.3 向后兼容
        summary=summary or f"共 {len(steps)} 个流程节点。",
    )
