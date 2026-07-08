"""元数据报告（Layer 1 → Layer 2 的「蓝图」，v1.0.4）。

纪律不变：**绝不**把原始数据行喂给 AI。AI 推理层只读取本模块产出的「元数据报告」。

v1.0.4 新增：
* Trace-Driven Sampling —— 优先展示独立「数据链路追踪」阶段保存的样本，
  保证发给 AI 的样本行在跨表之间具有因果关联性（解决"AI 看到互不相干的数据"问题）。
* 知识表术语统一 —— 报告中使用「知识表」代替「规则表」，
  knowledge_schema 代替 rule_schema（向后兼容旧字段）。
* 采样关联性验证 —— 附带 validate_trace_connectivity 的结论。
"""

from __future__ import annotations

import json

from app.domain.models import KnowledgeSchemaMapping, RuleSchemaMapping, Scenario, TableRole
from . import trace_sampling as _ts
from app.domain.validators import validate_trace_connectivity

# 元数据报告总字符上限（防止 LLM 上下文溢出导致 500 错误）
_MAX_REPORT_CHARS = 10_000
# 追踪采样章节字符上限（超出时截断最后几张表的预览）
_MAX_TRACE_SECTION_CHARS = 4_000
# 追踪章节中最多展示几张表的完整预览（防止表数很多时撑爆 context）
_MAX_TRACE_PREVIEW_TABLES = 5

_ROLE_LABEL = {
    TableRole.INPUT.value: "业务输入表",
    TableRole.KNOWLEDGE.value: "知识表(含规则/标准/目录/公式)",
    TableRole.RULE.value: "知识表(旧称:规则表)",
    TableRole.RESULT.value: "历史结果表",
    TableRole.UNKNOWN.value: "未标注角色",
}


def _format_knowledge_schema(ks: KnowledgeSchemaMapping | RuleSchemaMapping | None) -> list[str]:
    """统一格式化知识表/规则表结构映射（兼容两种模型）。"""
    if ks is None:
        return ["（本场景无知识表，或知识表结构尚未推导）"]

    lines: list[str] = []

    if isinstance(ks, KnowledgeSchemaMapping):
        lines.append(f"- 知识表：{ks.knowledge_table}")
        lines.append(f"- 分派键列：{ks.dispatch_key_column or '（未识别）'}")
        lines.append(f"- 条目编号列：{ks.item_id_column or '（未识别）'}")
        if ks.dispatch_map:
            mapping = "；".join(f"{k}→{v}" for k, v in list(ks.dispatch_map.items())[:10])
            suffix = f"（共 {len(ks.dispatch_map)} 种）" if len(ks.dispatch_map) > 10 else ""
            lines.append(f"- 分派值→说明（不驱动执行）：{mapping}{suffix}")
        if ks.condition_columns:
            lines.append(f"- 自然语言条件列：{ks.condition_columns}")
        if ks.parameter_columns:
            lines.append(f"- 参数列：{ks.parameter_columns}")
        if ks.field_role_map:
            lines.append(f"- 字段角色映射：{dict(list(ks.field_role_map.items())[:5])}")
    else:
        # 向后兼容旧 RuleSchemaMapping
        lines.append(f"- 知识表：{ks.rule_table}")
        lines.append(f"- 分派键列（旧称：分派列）：{ks.discriminator_column or '（未识别）'}")
        lines.append(f"- 规则编号列：{ks.rule_id_column or '（未识别）'}")
        if ks.discriminator_to_template:
            mapping = "；".join(f"{k}→{v}" for k, v in ks.discriminator_to_template.items())
            lines.append(f"- 分派值→说明（不驱动执行）：{mapping}")
        if ks.nl_description_columns:
            lines.append(f"- 自然语言条件列：{ks.nl_description_columns}")
        if ks.parameter_columns:
            lines.append(f"- 参数列：{ks.parameter_columns}")

    return lines


def build_metadata_report(scenario: Scenario, use_trace_sampling: bool = False) -> str:
    """构建元数据蓝图报告。

    v1.0.4 新增 Trace-Driven Sampling：当有结果表且 use_trace_sampling=True 时，
    用追踪驱动采样代替独立随机采样，AI 看到的是有因果关联的跨表样本。

    Args:
        scenario:            业务场景
        use_trace_sampling:  是否允许现场执行追踪驱动采样（默认 False；优先展示已保存链路）
    """
    lines: list[str] = [f"# 业务场景元数据报告：{scenario.name}"]
    if scenario.description:
        lines.append(f"场景描述：{scenario.description}")
    if scenario.trace_chain:
        lines.append(f"数据链路追踪：已完成（{scenario.trace_chain.get('trace_summary', '已保存链路样本')}）")
    else:
        lines.append("数据链路追踪：尚未执行；请先执行「数据链路追踪」再推导关联关系。")
    lines.append("")

    # ---- 一、数据表结构（含字段语义） ----
    lines.append("## 一、数据表结构（含字段语义，仅少量样本）")
    if not scenario.tables_meta:
        lines.append("（尚未上传任何业务表）")
    for t in scenario.tables_meta:
        role = _ROLE_LABEL.get(t.role, "未标注角色")
        confirmed = " ✓" if t.role_confirmed else ""
        lines.append(f"\n### 表「{t.table_name}」［{role}{confirmed}］")
        lines.append(
            f"- 规模：约 {t.row_count} 行 × {t.col_count} 列"
            + (f"（表头位于第 {t.header_row + 1} 行）" if t.header_row else "")
        )
        field_desc = "；".join(
            (
                f"{c.name}({c.dtype}|{c.semantic_role}"
                + (f"|{c.semantic}" if c.semantic and c.semantic != c.name else "")
                + ")"
            )
            for c in t.columns
        )
        lines.append(f"- 字段：{field_desc}")
        if t.sample_rows:
            sample = json.dumps(t.sample_rows[:2], ensure_ascii=False)
            lines.append(f"- 样本行(独立抽样≤2)：{sample}")

    # ---- 二、追踪驱动采样（v1.0.4 新增） ----
    has_result_table = any(t.role == TableRole.RESULT.value for t in scenario.tables_meta)

    saved_trace = scenario.trace_chain or (
        scenario.relations.trace_chain if scenario.relations else {}
    )

    if (saved_trace or use_trace_sampling) and scenario.tables_meta:
        lines.append("\n## 二、追踪驱动关联样本（Trace-Driven Sampling）")
        if not has_result_table:
            lines.append(
                "（无结果表，降级为独立随机采样；"
                "有结果表后可获得因果关联的跨表样本，推导置信度将显著提升）"
            )
        else:
            trace_report = saved_trace or _ts.trace_sampling(scenario)
            val = validate_trace_connectivity(trace_report)

            # 校验结论
            level_emoji = {"pass": "✅", "warning": "⚠️", "fail": "❌"}.get(val.level, "")
            lines.append(f"**关联性校验**：{level_emoji} {val.message}")
            lines.append(f"追踪摘要：{trace_report.get('trace_summary', '')}")
            lines.append(f"总追踪样本行数：{trace_report.get('total_rows', 0)}")

            # 结果表样本
            result_sample = trace_report.get("result_sample", [])
            if result_sample:
                rt_name = trace_report.get("result_table", "结果表")
                lines.append(f"\n**「{rt_name}」样本（追踪入口 {len(result_sample)} 行）**：")
                lines.append(json.dumps(result_sample, ensure_ascii=False)[:800])

            # 各表追踪结果（限制展示表数，防止 context 溢出）
            trace_map = trace_report.get("trace_map", {})
            if trace_map:
                lines.append("\n**各表追踪到的关联行：**")
                shown = 0
                for tbl_name, info in trace_map.items():
                    conf = info.get("trace_confidence", "?")
                    by = info.get("matched_by", "")
                    rows = info.get("matched_rows", [])
                    n = len(rows)
                    warning = info.get("warning", "")

                    if not by or by == "random":
                        lines.append(f"\n  - 「{tbl_name}」⚠️ 未追踪到稳定因果行（{warning or '不作为推导样本'}）")
                    else:
                        lines.append(
                            f"\n  - 「{tbl_name}」通过「{by}」追踪 {n} 行（置信度:{conf}）"
                        )

                    # 最多展示前 N 张表的完整行预览，其余只显示摘要
                    if rows and shown < _MAX_TRACE_PREVIEW_TABLES:
                        preview = json.dumps(rows[:2], ensure_ascii=False)
                        lines.append(f"    样本预览：{preview[:400]}")
                        shown += 1
                    elif rows:
                        lines.append(f"    （已省略预览，共 {n} 行，置信度:{conf}）")

            unmatched = trace_report.get("unmatched_tables", [])
            if unmatched:
                lines.append(f"\n⚠️ 以下表追踪失败（无数据关联路径）：{unmatched}")
    else:
        lines.append("\n## 二、数据关联摘要")
        lines.append("（尚未执行数据链路追踪；上传只解析表结构，请先执行「数据链路追踪」以获得因果关联样本）")

    # ---- 三、已推导的表关联（ER 模型） ----
    lines.append("\n## 三、已推导的表关联（ER 模型）")
    if scenario.relations and scenario.relations.relations:
        for r in scenario.relations.relations:
            lines.append(
                f"- {r.from_table}.{r.from_column} → {r.to_table}.{r.to_column}"
                f"（{r.relation_type}，置信度 {r.confidence:.0%}）"
            )
    else:
        lines.append("（尚未推导关联关系）")

    # ---- 四、知识表结构映射（v1.0.4 通用化） ----
    lines.append("\n## 四、知识表结构映射（Trace-Driven Dispatch）")
    if scenario.flow:
        # 优先用 v1.0.4 的 knowledge_schema，回退到 v1.0.3 的 rule_schema
        ks = scenario.flow.knowledge_schema or scenario.flow.rule_schema
    else:
        ks = None
    lines.extend(_format_knowledge_schema(ks))

    # ---- 五、业务流程节点（含可读能力描述） ----
    lines.append("\n## 五、业务流程节点（含可读能力描述）")
    if scenario.flow and scenario.flow.flow_steps:
        for s in scenario.flow.flow_steps:
            lines.append(
                f"\n### 步骤{s.step_id}：{s.step_name}（操作:{s.operation} / 模式:{s.template_kind}）"
            )
            if s.purpose:
                lines.append(f"- 该做什么：{s.purpose}")
            if s.capability:
                lines.append(f"- 能做什么：{s.capability}")
            if s.data_in:
                lines.append(f"- 数据输入：{'；'.join(s.data_in)}")
            if s.data_out:
                lines.append(f"- 数据输出：{'；'.join(s.data_out)}")
            lines.append(f"- 状态：{s.status}")
    else:
        lines.append("（尚未推导业务流程）")

    # ---- 六、产出规格 ----
    lines.append("\n## 六、产出规格")
    if scenario.outputs:
        for o in scenario.outputs:
            cols = "、".join(o.columns[:12]) + ("…" if len(o.columns) > 12 else "")
            lines.append(
                f"- 【{o.name}】格式 {o.fmt}，状态 {o.status}，"
                f"策略 {o.strategy or '—'}；输出列：{cols}"
            )
    else:
        lines.append(
            "（尚未派生产出规格；先 deduce_flow 再 generate_skills 即可派生）"
        )

    report = "\n".join(lines)

    # 总字符上限保护：若整份报告过大，优先截断追踪采样章节的尾部
    if len(report) > _MAX_REPORT_CHARS:
        # 找到第三章节开始位置，把第二章节截断
        sec3 = report.find("\n## 三、")
        if sec3 > 0:
            sec2 = report.find("\n## 二、")
            if sec2 > 0:
                # 保留二的摘要行（前 _MAX_TRACE_SECTION_CHARS 字符），然后拼接三～六
                sec2_end = min(sec2 + _MAX_TRACE_SECTION_CHARS, sec3)
                report = (
                    report[:sec2_end]
                    + "\n  ...（追踪样本内容过大，已截断，不影响结构推导）"
                    + report[sec3:]
                )

        # 最终兜底：整体截断
        if len(report) > _MAX_REPORT_CHARS:
            report = report[:_MAX_REPORT_CHARS] + "\n\n...（报告内容过大，已截断）"

    return report
