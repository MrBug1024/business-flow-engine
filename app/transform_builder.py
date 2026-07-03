"""产出规格构建（v1.0.3，确定性，无 AI，场景无关）。

职责：
1) `build_domain_knowledge(scenario)`：把场景蒸馏为「领域知识包」（数据字典 + ER + 字段语义
   + 各产出的结果结构契约 + 规则结构映射）。给执行技能在运行时使用，**无任何写死字段名**。
2) `build_outputs(scenario)`：基于（流程节点 + 历史结果表）派生 OutputSpec。
   产出 = 一段流程节点管线 + 输出格式 + 结果列契约。

红线：表角色取自用户标注；字段语义/规则结构由 inference 阶段蒸馏；运行时只跑 SQL。
"""

from __future__ import annotations

import re

from . import output_render, strategies
from .models import (
    DomainColumn,
    DomainKnowledge,
    DomainTable,
    FlowStep,
    OutputSpec,
    Scenario,
    SemanticRole,
    TableMeta,
    TableRole,
)


def _role(table: TableMeta) -> str:
    return table.role or TableRole.UNKNOWN.value


def _cols(table: TableMeta) -> list[str]:
    return [c.name for c in table.columns]


def _result_tables(scenario: Scenario) -> list[TableMeta]:
    return [t for t in scenario.tables_meta if _role(t) == TableRole.RESULT.value]


def _slug(name: str) -> str:
    s = re.sub(r"[\s/\\]+", "_", str(name).strip())
    s = re.sub(r"[^0-9A-Za-z_一-鿿]", "", s)
    return s[:60] or "output"


# ===========================================================================
# 领域知识（含字段语义 + 规则结构）
# ===========================================================================
def build_domain_knowledge(scenario: Scenario) -> DomainKnowledge:
    tables = [
        DomainTable(
            table_name=t.table_name,
            role=_role(t),
            file=t.display_name or t.table_name,
            row_count=t.row_count,
            header_row=t.header_row,
            columns=[
                DomainColumn(
                    name=c.name, dtype=c.dtype,
                    semantic=c.semantic, semantic_role=c.semantic_role,
                ) for c in t.columns
            ],
        )
        for t in scenario.tables_meta
    ]
    relations = scenario.relations.relations if scenario.relations else []

    result_schema: dict[str, list[str]] = {}
    results = _result_tables(scenario)
    if results:
        result_schema["__default__"] = _cols(results[0])
    for rt in results:
        result_schema[_slug(rt.table_name)] = _cols(rt)

    # 字段语义扁平化：{"表.字段": "含义（角色）"}
    field_semantics: dict[str, str] = {}
    for t in scenario.tables_meta:
        for c in t.columns:
            if c.semantic or c.semantic_role != SemanticRole.UNKNOWN.value:
                field_semantics[f"{t.table_name}.{c.name}"] = (
                    f"{c.semantic or c.name}（{c.semantic_role}）"
                )

    rule_schema = scenario.flow.rule_schema if scenario.flow else None

    return DomainKnowledge(
        scenario=scenario.name,
        tables=tables,
        relations=relations,
        result_schema=result_schema,
        field_semantics=field_semantics,
        rule_schema=rule_schema,
    )


# ===========================================================================
# 产出规格：基于流程节点 + 历史结果结构
# ===========================================================================
def _compile_step_sql(step: FlowStep) -> str:
    """为单个流程节点编译 SQL（若节点已有手工 SQL 则保留）。"""
    if step.sql:
        return step.sql
    if step.strategy and step.params:
        sql = strategies.build_sql(step.strategy, step.params)
        return sql or ""
    if step.template_kind and step.params:
        sql = strategies.build_sql(step.template_kind, step.params)
        return sql or ""
    return ""


def _refresh_pipeline_sql(steps: list[FlowStep]) -> list[FlowStep]:
    """刷新所有节点的 SQL（确保 pipeline 可执行 / 标 blocked）。"""
    refreshed: list[FlowStep] = []
    for s in steps:
        s.sql = _compile_step_sql(s)
        if s.sql:
            if s.status not in ("verified",):
                s.status = "executable"
        else:
            if not s.external_data_needed:
                s.external_data_needed = ["待 AI/用户细化节点参数"]
            s.status = "blocked" if not s.sql else s.status
        refreshed.append(s)
    return refreshed


def _last_step_sql(steps: list[FlowStep]) -> str:
    """取管线最后一个有可执行 SQL 的节点 SQL（作为产出的兜底单步 SQL）。"""
    for s in reversed(steps):
        if s.sql:
            return s.sql
    return ""


def build_outputs(scenario: Scenario, domain: DomainKnowledge) -> list[OutputSpec]:
    """据每张历史结果表蒸馏/刷新产出规格。

    新逻辑：产出 pipeline = scenario.flow.flow_steps（若已推导），最后一个节点的 SQL 作为
    兜底单步 SQL；列契约对齐历史结果表；输出格式按历史结果文件后缀复刻。
    """
    results = _result_tables(scenario)
    existing = {o.output_id: o for o in scenario.outputs}
    flow_steps = list(scenario.flow.flow_steps) if scenario.flow else []
    flow_steps = _refresh_pipeline_sql([s.model_copy() for s in flow_steps])

    specs: list[OutputSpec] = []
    required_input = [t.table_name for t in scenario.tables_meta
                      if t.role in (TableRole.INPUT.value, TableRole.RULE.value)]

    for rt in results:
        oid = _slug(rt.table_name)
        prev = existing.get(oid)
        if prev and (prev.strategy in ("manual", "sql") or prev.status == "verified"):
            prev.columns = _cols(rt)
            prev.fmt = prev.fmt or output_render.infer_format(rt.file_path)
            prev.pipeline = flow_steps
            specs.append(prev)
            continue

        sql = _last_step_sql(flow_steps)
        spec = OutputSpec(
            output_id=oid,
            name=rt.table_name,
            description=f"复刻历史结果「{rt.table_name}」",
            fmt=output_render.infer_format(rt.file_path),
            result_table=rt.table_name,
            columns=_cols(rt),
            required_tables=required_input,
            pipeline=flow_steps,
            strategy=flow_steps[-1].template_kind if flow_steps else "",
            params=flow_steps[-1].params if flow_steps else {},
            sql=sql,
            status="executable" if sql else "blocked",
            external_data_needed=(
                [] if sql else ["流程节点尚未给出可执行 SQL：请先推导业务流程或细化节点"]
            ),
        )
        specs.append(spec)

    seen = {s.output_id for s in specs}
    for o in scenario.outputs:
        if o.output_id not in seen and (o.strategy in ("manual", "sql") or not o.result_table):
            specs.append(o)
    return specs
