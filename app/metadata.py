"""元数据报告（Layer 1 → Layer 2 的「蓝图」）。

v1.0.1 关键纪律：**绝不把原始数据行喂给 AI**。AI 推理层只读取本模块产出的
「元数据报告」：表结构、字段类型、基数估计、每表 1~3 条样本，以及规则库的
结构化摘要（按违规类型聚合）。AI 是读蓝图的架构师，而非翻数据的工人。
"""

from __future__ import annotations

import json

from . import rule_parser
from .models import RuleLibrary, Scenario


def build_metadata_report(scenario: Scenario) -> str:
    """构建供 AI 阅读的元数据报告（Markdown 文本）。"""
    lines: list[str] = [f"# 业务场景元数据报告：{scenario.name}"]
    if scenario.description:
        lines.append(f"场景描述：{scenario.description}")
    lines.append("")

    # ---- 业务表结构（含 1~3 条样本）----
    lines.append("## 一、数据表结构（含少量样本，绝非全量）")
    if not scenario.tables_meta:
        lines.append("（尚未上传任何业务表）")
    for t in scenario.tables_meta:
        role = "规则表(知识库)" if rule_parser.is_rule_table(t) else "业务表"
        lines.append(f"\n### 表「{t.table_name}」［{role}］")
        lines.append(f"- 规模：约 {t.row_count} 行 × {t.col_count} 列"
                     + (f"（表头位于第 {t.header_row + 1} 行）" if t.header_row else ""))
        field_desc = "；".join(
            f"{c.name}({c.dtype}, 空值率{c.null_rate:.0%})" for c in t.columns
        )
        lines.append(f"- 字段：{field_desc}")
        if t.sample_rows:
            sample = json.dumps(t.sample_rows[:2], ensure_ascii=False)
            lines.append(f"- 样本行(≤2)：{sample}")

    # ---- 关联关系（ER 模型）----
    lines.append("\n## 二、已推导的表关联（ER 模型）")
    if scenario.relations and scenario.relations.relations:
        for r in scenario.relations.relations:
            lines.append(
                f"- {r.from_table}.{r.from_column} → {r.to_table}.{r.to_column}"
                f"（{r.relation_type}，置信度 {r.confidence:.0%}）"
            )
    else:
        lines.append("（尚未推导关联关系）")

    # ---- 规则库摘要 ----
    lines.append("\n## 三、规则库摘要（领域知识库）")
    lines.append(_describe_rule_library(scenario.rule_library))

    return "\n".join(lines)


def _describe_rule_library(library: RuleLibrary | None, max_types: int = 40) -> str:
    """规则库的结构化摘要：按违规类型聚合，每类给出代表性逻辑与示例片段。"""
    if library is None or not library.templates:
        return "（尚未解析规则库。若已上传规则表，请先调用 parse_rules）"

    groups = rule_parser.violation_type_groups(library)
    out: list[str] = [library.summary, f"共 {len(groups)} 种违规类型："]
    for i, (vtype, tmpls) in enumerate(groups.items()):
        if i >= max_types:
            out.append(f"…（其余 {len(groups) - max_types} 种违规类型从略，可用 list_audit_types 查看）")
            break
        rep = tmpls[0]
        logic = (rep.logic_description or "")[:60]
        status = {
            "parsed": "未细化",
            "unverified": "可执行(未校验)",
            "verified": "已校验✓",
            "blocked": "缺数据/口径",
        }.get(rep.status, rep.status)
        out.append(
            f"- 【{vtype}】（{len(tmpls)} 条细则，状态：{status}）逻辑：{logic}"
        )
    return "\n".join(out)
