"""自然语言规则分析器（v1.0.6）。

从知识表的 NL 描述列中做粗粒度信号扫描，仅用于**辅助**知识表结构映射推导——
给 `inference.infer_knowledge_schema_llm` 提供一份"这些分派值的文本大概长什么样"的
参考（如提示"这条规则文本里出现了阈值数字"），本身不产出任何执行逻辑，也不假设
每条规则必然属于 keyword/threshold/co_occurrence 这几种固定形状之一——具体判断逻辑
留给运行时读到规则原文的 LLM 现场推理，这里只做粗粒度信号提示。
不依赖 LLM，仅基于启发式关键词扫描——速度快、零成本，可在蒸馏前自动运行。

输出结构：
    {
        "has_nl_rules": bool,
        "total_rules": int,
        "pattern_breakdown": {信号名: [{"row": ..., "signals": [...]}]},
        "dispatch_key_values": {分派键值: 信号名},   # 仅供 LLM 参考的粗粒度提示，不驱动执行
        "nl_columns": [含 NL 文本的列名],
        "summary": str,
    }
"""

from __future__ import annotations

import re
from typing import Any

from app.domain.models import Scenario, TableRole

# ---------------------------------------------------------------------------
# 规则模式信号词（按优先级排列）
# ---------------------------------------------------------------------------
_PATTERN_SIGNALS: dict[str, list[str]] = {
    "co_occurrence": [
        "同时.*申报", "同时.*开具", "同时.*使用", "同时.*出现",
        "不得同时", "互斥", "冲突", "同一.*同时",
    ],
    "frequency_overflow": [
        r"\d+次[/／每]", r"每[月年日周]\w{0,4}次", "次数.*超过", "超过.*次",
        "不超过.*次", "频次", "频率", "限.*次", r"\d+次以[内上下]",
    ],
    "threshold": [
        r"超过.*[元分角万千百]", r"不超过.*[元分角万千百]",
        "限额", "上限", "下限", "金额.*超", "费用.*超",
        r">\s*\d+", r">=\s*\d+", r"≥\s*\d+",
    ],
    "exclusive_conflict": [
        "禁止", "不得", "不能.*同时", "排斥", "禁用", "不允许",
        "不应.*同时", "避免.*同时",
    ],
    "dedup": [
        "重复", "不得重复", "唯一", "同一.*不得.*多次", "仅限.*一次",
        "不重复", "去重",
    ],
    "join": [
        "关联", "对应", "匹配", "参照", "按照.*查", "以.*为准",
    ],
    "keyword": [],  # 兜底：无上述模式时归为 keyword
}

_NL_TEXT_HINTS = (
    "说明", "描述", "规则", "内容", "条件", "要求",
    "备注", "约束", "定义", "规定",
    "description", "rule", "content", "condition",
)

_SHORT_VALUE_MAX_LEN = 50  # NL文本判断：超过此长度才算"自然语言"


def _is_nl_column(col_name: str, sample_values: list) -> bool:
    """启发式判断某列是否含 NL 文本。"""
    name = col_name.lower().replace("_", "").replace(" ", "")
    if any(h in name for h in _NL_TEXT_HINTS):
        return True
    avg_len = (sum(len(str(v)) for v in sample_values) / max(len(sample_values), 1)
               if sample_values else 0)
    return avg_len > _SHORT_VALUE_MAX_LEN


def _detect_pattern(text: str) -> tuple[str, list[str]]:
    """对单条规则文本识别最可能的处理模式。

    Returns:
        (pattern_name, matched_signals)
    """
    if not text:
        return "keyword", []
    for pattern, signals in _PATTERN_SIGNALS.items():
        if not signals:
            continue
        matched = []
        for sig in signals:
            if re.search(sig, text):
                matched.append(sig)
        if matched:
            return pattern, matched
    return "keyword", []


def _load_knowledge_table(scenario: Scenario) -> tuple[str, list[str], list[dict]]:
    """加载知识表数据，返回 (table_name, nl_columns, rows)。"""
    from pathlib import Path  # noqa: PLC0415
    from app.data import table_io  # noqa: PLC0415

    kt = next(
        (t for t in scenario.tables_meta if t.role in (TableRole.RULE.value, TableRole.KNOWLEDGE.value)),
        None,
    )
    if kt is None:
        return "", [], []

    # 找出 NL 列
    nl_cols = [
        c.name for c in kt.columns
        if _is_nl_column(c.name, c.sample_values or [])
    ]
    if not nl_cols:
        # 退化：取所有 object 型列
        nl_cols = [c.name for c in kt.columns if c.dtype in ("object", "str", "string", "O")]

    # 加载数据（用 scan_frame，最多 2000 行，足够分析模式分布）
    try:
        df = table_io._load_scan_frame(Path(kt.file_path))
    except Exception:  # noqa: BLE001
        return kt.table_name, nl_cols, []

    rows = df.head(500).to_dict("records")
    return kt.table_name, nl_cols, rows


def analyze_nl_rules(scenario: Scenario) -> dict[str, Any]:
    """分析知识表的 NL 规则，返回规则模式统计和 dispatch_map 候选。"""
    table_name, nl_cols, rows = _load_knowledge_table(scenario)

    if not rows:
        return {
            "has_nl_rules": False,
            "total_rules": 0,
            "pattern_breakdown": {},
            "dispatch_key_values": {},
            "nl_columns": [],
            "summary": "无知识表或知识表为空。",
        }

    # 找分派键列（语义角色为 CATEGORY 的列，或列名含"类型/类别/分类"）
    kt = next(
        (t for t in scenario.tables_meta if t.role in (TableRole.RULE.value, TableRole.KNOWLEDGE.value)),
        None,
    )
    dispatch_col = ""
    if kt:
        from app.domain.models import SemanticRole  # noqa: PLC0415
        cat_cols = [c.name for c in kt.columns if c.semantic_role == SemanticRole.CATEGORY.value]
        if not cat_cols:
            cat_cols = [c.name for c in kt.columns
                        if any(k in c.name for k in ("类型", "类别", "分类", "category", "type", "kind"))]
        dispatch_col = cat_cols[0] if cat_cols else ""

    pattern_breakdown: dict[str, list[dict]] = {}
    dispatch_key_values: dict[str, str] = {}
    has_nl = False

    for row in rows:
        text = " ".join(str(row.get(c, "")) for c in nl_cols if row.get(c))
        if not text.strip():
            continue
        has_nl = True
        pattern, signals = _detect_pattern(text)
        pattern_breakdown.setdefault(pattern, []).append({
            "row_preview": {c: str(row.get(c, ""))[:80] for c in nl_cols[:3]},
            "signals": signals,
        })
        # 记录分派键值 → 推断的模式
        if dispatch_col and row.get(dispatch_col):
            dk_val = str(row[dispatch_col]).strip()
            if dk_val and dk_val not in dispatch_key_values:
                dispatch_key_values[dk_val] = pattern

    total = sum(len(v) for v in pattern_breakdown.values())
    breakdown_summary = "; ".join(
        f"{pat}({len(items)}条)" for pat, items in sorted(pattern_breakdown.items(), key=lambda x: -len(x[1]))
    )

    return {
        "has_nl_rules": has_nl,
        "total_rules": total,
        "pattern_breakdown": pattern_breakdown,
        "dispatch_key_values": dispatch_key_values,
        "nl_columns": nl_cols,
        "dispatch_column": dispatch_col,
        "knowledge_table": table_name,
        "summary": f"知识表「{table_name}」共 {total} 条规则，模式分布：{breakdown_summary or '未识别'}",
    }


def format_nl_analysis(analysis: dict[str, Any]) -> str:
    """将 NL 分析结果格式化为 LLM 可读的文本描述。"""
    if not analysis.get("has_nl_rules"):
        return ""

    lines = [
        f"知识表「{analysis.get('knowledge_table', '?')}」NL 规则分析：",
        f"- 共 {analysis['total_rules']} 条规则",
        f"- NL 描述列：{analysis.get('nl_columns', [])}",
        f"- 分派键列：{analysis.get('dispatch_column', '（未识别）')}",
        "",
        "规则模式分布（用于选择 template_kind）：",
    ]
    breakdown = analysis.get("pattern_breakdown", {})
    for pat, items in sorted(breakdown.items(), key=lambda x: -len(x[1])):
        example = items[0]["row_preview"] if items else {}
        signals = items[0].get("signals", []) if items else []
        lines.append(f"  - {pat}（{len(items)} 条）：示例={example}，信号词={signals}")

    lines.append("")
    lines.append("建议 dispatch_map（分派键值 → 处理模式）：")
    dkv = analysis.get("dispatch_key_values", {})
    if dkv:
        for k, v in list(dkv.items())[:20]:
            lines.append(f"  \"{k}\" → \"{v}\"")
    else:
        lines.append("  （未识别分派键，建议设 template_kind=keyword 作为通用兜底）")

    return "\n".join(lines)
