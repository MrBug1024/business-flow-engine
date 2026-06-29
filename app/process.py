"""Phase 0：业务流程发现（Business Process Discovery）。

规范 Phase 0 的目标：在任何 schema/规则推导之前，先把「这个业务到底在做什么」讲清楚，
产出 `business_process.md`（白话描述 + 处理步骤 + 输入/规则/结果表识别 + 流程 Mermaid 图），
并经用户**显式批准**后方可放行后续阶段。

本模块为**确定性 Python 逻辑（Layer 1，无 AI）**：只读取已有元数据与场景描述，
据表名特征识别三类表，组织出一份草稿文档。AI（若已配置）可在此基础上润色与提问，
但文档结构与表识别由此处保证可复现。
"""

from __future__ import annotations

from . import executor, rule_parser
from .models import BusinessProcess, Scenario


def classify_tables(scenario: Scenario) -> tuple[list[str], list[str], list[str]]:
    """把已上传表分为（业务输入表、规则/标准表、历史结果表）。

    规则表：表名含「规则/清单」等知识库特征（rule_parser）。
    结果表：非规则表且命名呈「结果/违规/含-」形态（executor）。
    输入表：其余业务明细表。
    """
    input_tables: list[str] = []
    rule_tables: list[str] = []
    result_tables: list[str] = []
    for t in scenario.tables_meta:
        if rule_parser.is_rule_table(t):
            rule_tables.append(t.table_name)
        elif executor._is_result_like(t):
            result_tables.append(t.table_name)
        else:
            input_tables.append(t.table_name)
    return input_tables, rule_tables, result_tables


def _mm(label: str) -> str:
    """清洗 Mermaid 节点标签中的危险字符。"""
    return (label or "").replace('"', "'").replace("[", "(").replace("]", ")").replace("\n", " ").strip()


def build_mermaid(
    input_tables: list[str], rule_tables: list[str], result_tables: list[str]
) -> str:
    """根据三类表生成业务处理的 Mermaid flowchart。"""
    lines = ["flowchart LR"]
    # 输入
    for i, name in enumerate(input_tables):
        lines.append(f'  in{i}["📥 {_mm(name)}"]')
    if not input_tables:
        lines.append('  in0["📥 业务输入数据（待上传）"]')
    # 规则库
    if rule_tables:
        lines.append(f'  rules["📋 规则库：{_mm("、".join(rule_tables))}"]')
    else:
        lines.append('  rules["📋 规则库（待上传规则表）"]')
    # 处理
    lines.append('  proc["⚙️ 业务处理：关联 → 过滤 → 规则匹配 → 聚合"]')
    # 输出
    if result_tables:
        for j, name in enumerate(result_tables):
            lines.append(f'  out{j}["📤 {_mm(name)}"]')
    else:
        lines.append('  out0["📤 审核结果明细"]')
    # 连边
    n_in = max(len(input_tables), 1)
    for i in range(n_in):
        lines.append(f"  in{i} --> proc")
    lines.append("  rules --> proc")
    n_out = max(len(result_tables), 1)
    for j in range(n_out):
        lines.append(f"  proc --> out{j}")
    return "\n".join(lines)


def build_markdown(scenario: Scenario, bp: BusinessProcess) -> str:
    """组装 business_process.md 文本（规范 Phase 0 要求的全部要素）。"""
    lib = scenario.rule_library
    n_types = len(lib.violation_types) if lib and lib.templates else 0

    def _bullet(names: list[str], empty: str) -> str:
        return "\n".join(f"- `{n}`" for n in names) if names else f"- （{empty}）"

    lines: list[str] = [
        f"# 业务流程说明：{scenario.name}",
        "",
        "## 一、业务问题（白话描述）",
        bp.description or "（待补充：请用一两句话描述这个业务要解决什么问题。）",
        "",
        "## 二、处理步骤",
    ]
    lines += [f"{i + 1}. {s}" for i, s in enumerate(bp.steps)] or ["1. （待补充）"]
    lines += [
        "",
        "## 三、数据表识别",
        "",
        "**业务输入表**（流程的原始数据来源）：",
        _bullet(bp.input_tables, "尚未上传业务输入表"),
        "",
        "**规则 / 标准表**（领域知识库，定义审核口径）：",
        _bullet(bp.rule_tables, "尚未上传规则表"),
        "",
        "**历史结果表**（用于校验某条规则的样例输出）：",
        _bullet(bp.result_tables, "尚未上传历史结果表"),
        "",
        "## 四、业务流程图",
        "",
        "```mermaid",
        bp.mermaid,
        "```",
        "",
        "---",
        (f"> 规则库已解析：覆盖 **{n_types}** 种违规类型。" if n_types
         else "> 提示：规则库尚未解析；批准本文档后将进入 Schema 与规则解析阶段。"),
        "> 请确认以上对业务的理解是否准确。**批准后**方可进入后续推导阶段。",
    ]
    return "\n".join(lines)


def discover_process(scenario: Scenario, description: str = "") -> BusinessProcess:
    """基于现有元数据与场景描述，生成 Phase 0 业务流程文档草稿（未批准）。"""
    inputs, rules, results = classify_tables(scenario)

    desc = (description or scenario.description or "").strip()
    if not desc:
        desc = (
            f"「{scenario.name}」基于业务历史数据与规则库进行审核：从业务输入表读取明细，"
            "依据规则库定义的违规口径进行关联、过滤与聚合，输出违规明细结果。"
        )

    steps = [
        f"数据进入：读取业务输入表（{('、'.join(inputs)) or '待上传'}）。",
        f"知识载入：加载规则/标准表（{('、'.join(rules)) or '待上传'}）作为审核口径。",
        "关联与过滤：按业务键关联相关表，依据规则条件筛选候选记录。",
        "规则匹配与聚合：对每种违规类型套用其逻辑（过滤/聚合/阈值），判定违规。",
        f"结果产出：输出违规明细，结构对齐历史结果表（{('、'.join(results)) or '待上传'}）。",
    ]

    mermaid = build_mermaid(inputs, rules, results)
    bp = BusinessProcess(
        description=desc,
        steps=steps,
        input_tables=inputs,
        rule_tables=rules,
        result_tables=results,
        mermaid=mermaid,
        approved=False,
    )
    bp.markdown = build_markdown(scenario, bp)
    return bp
