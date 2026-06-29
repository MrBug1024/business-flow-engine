"""规则表解析（Layer 1：元数据/采样层，无 AI）。

v1.0.1 关键原则：**规则表 = 领域知识库**。本模块把规则表（如医保违规审核规则清单）
解析为结构化的「规则模板库」——每一行/每一类违规对应一个 `RuleTemplate`，
含违规类型、关键词、逻辑描述、政策依据、案例示例、用途等。

与「业务数据表」不同，规则表是**结构化知识**而非海量明细，因此可以整表解析；
但解析全程为确定性 Python 逻辑，**不经过 AI**，AI 只读取由此产出的结构化摘要。
"""

from __future__ import annotations

import re

from . import table_io
from .models import RuleLibrary, RuleTemplate, Scenario, TableMeta

# 规则表命名特征
_RULE_TABLE_HINTS = ("规则", "rule", "清单", "policy", "知识", "config")

# 规则表列名 → 语义角色 的关键词映射（按优先级匹配）
_COL_ROLE_HINTS: dict[str, tuple[str, ...]] = {
    "violation_type": ("违规类型", "违规行为", "违规", "类型"),
    "category": ("大类", "类别", "分类"),
    "seq": ("序号", "编号", "规则编号"),
    "logic_description": ("情形", "清单", "逻辑", "描述", "规则", "情况", "说明"),
    "policy_basis": ("政策依据", "政策", "依据", "法规"),
    "example": ("示例", "案例", "样例", "参考"),
    "usage": ("用途", "场景", "检查方式", "用于"),
    "year": ("年份", "年度"),
}

# 关键词抽取时切分的标点
_SPLIT_RE = re.compile(r"[，。、；：,.;:（）()【】\[\]\s/]+")
_STOPWORDS = {
    "的", "了", "和", "与", "或", "及", "等", "为", "在", "对", "将", "以",
    "示例", "如", "某", "某某", "医院", "情况", "下", "并", "无", "未",
}


def is_rule_table(table: TableMeta) -> bool:
    """根据表名判断是否为规则表（领域知识库）。"""
    name = (table.table_name or "").lower()
    return any(h in name or h in table.table_name for h in _RULE_TABLE_HINTS)


def find_rule_table(scenario: Scenario) -> TableMeta | None:
    """在场景已上传表中定位规则表。"""
    candidates = [t for t in scenario.tables_meta if is_rule_table(t)]
    if not candidates:
        return None
    # 列数最多的更可能是真正的规则清单（违规类型、依据、示例等列齐全）
    return max(candidates, key=lambda t: t.col_count)


def _map_columns(columns: list[str]) -> dict[str, str]:
    """把规则表实际列名映射到语义角色。每个角色取首个命中的列。"""
    role_to_col: dict[str, str] = {}
    used: set[str] = set()
    for role, hints in _COL_ROLE_HINTS.items():
        for col in columns:
            if col in used:
                continue
            cl = str(col)
            if any(h in cl for h in hints):
                role_to_col[role] = col
                used.add(col)
                break
    return role_to_col


def _extract_keywords(text: str, limit: int = 6) -> list[str]:
    """从逻辑描述中抽取若干关键词（确定性，无 AI）。"""
    if not text:
        return []
    tokens = [t for t in _SPLIT_RE.split(str(text)) if t]
    keywords: list[str] = []
    for tok in tokens:
        tok = tok.strip()
        if len(tok) < 2 or tok in _STOPWORDS or tok.isdigit():
            continue
        if tok not in keywords:
            keywords.append(tok)
        if len(keywords) >= limit:
            break
    return keywords


def _clean(value) -> str:
    if value is None:
        return ""
    import pandas as pd

    if isinstance(value, float) and pd.isna(value):
        return ""
    return str(value).strip()


def parse_rule_table(table: TableMeta) -> RuleLibrary:
    """把规则表整表解析为规则模板库（无 AI）。"""
    frame = table_io.load_full_frame(table.file_path)
    columns = [str(c) for c in frame.columns]
    roles = _map_columns(columns)

    col_vt = roles.get("violation_type")
    col_logic = roles.get("logic_description")
    col_cat = roles.get("category")
    col_seq = roles.get("seq")
    col_basis = roles.get("policy_basis")
    col_example = roles.get("example")
    col_usage = roles.get("usage")

    templates: list[RuleTemplate] = []
    for idx, row in frame.iterrows():
        logic = _clean(row.get(col_logic)) if col_logic else ""
        vtype = _clean(row.get(col_vt)) if col_vt else ""
        # 违规类型缺失时退而用逻辑描述的前若干字符兜底，保证每条规则可寻址
        if not vtype:
            vtype = (logic[:12] + "…") if logic else f"规则{idx + 1}"
        seq = _clean(row.get(col_seq)) if col_seq else str(idx + 1)
        templates.append(
            RuleTemplate(
                rule_id=f"rule_{seq or idx + 1}".replace(" ", ""),
                seq=seq,
                category=_clean(row.get(col_cat)) if col_cat else "",
                violation_type=vtype,
                logic_description=logic,
                policy_basis=_clean(row.get(col_basis)) if col_basis else "",
                example=_clean(row.get(col_example)) if col_example else "",
                usage=_clean(row.get(col_usage)) if col_usage else "",
                keywords=_extract_keywords(logic or vtype),
                status="parsed",
            )
        )

    # 按违规类型汇总
    vtypes: list[str] = []
    for t in templates:
        if t.violation_type not in vtypes:
            vtypes.append(t.violation_type)
    summary = (
        f"已从规则表「{table.table_name}」解析出 {len(templates)} 条规则，"
        f"覆盖 {len(vtypes)} 种违规类型。"
    )
    return RuleLibrary(source_table=table.table_name, templates=templates, summary=summary)


def violation_type_groups(library: RuleLibrary) -> dict[str, list[RuleTemplate]]:
    """按违规类型分组（同一类型可能对应多条细则）。"""
    groups: dict[str, list[RuleTemplate]] = {}
    for t in library.templates:
        groups.setdefault(t.violation_type, []).append(t)
    return groups
