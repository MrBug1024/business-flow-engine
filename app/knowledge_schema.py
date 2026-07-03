"""知识表结构映射推断（v1.0.4，通用化版本）。

这是对 rule_schema.py 的通用化重构：
* 术语变更：rule_table → knowledge_table，discriminator → dispatch_key，
  nl_description_columns → condition_columns，discriminator_to_template → dispatch_map
* 领域无关：不假设"违规类型"/"审计"等特定领域的分派值
* 支持 role=knowledge（v1.0.4 新增）和 role=rule（向后兼容）的表

核心思想（延续 v1.0.3，v1.0.7 修正执行方式）：
  值千变万化，唯一不变的是结构。这里只学**结构**，不学"每条规则具体怎么判断"：
    ① 知识表的列角色（分派列 / 条目编号列 / 自然语言条件列 / 参数列）
    ② 分派值 → 一句话人类可读说明（如「重复」→ co_occurrence 这类标签仅供人快速
       了解知识表全貌，不是执行器要挑选的算子名，不驱动任何执行）
    ③ 知识字段语义 → 业务表字段的对应关系
  真实业务规则可能有成百上千条、判断逻辑千差万别，不可能在蒸馏阶段为每条规则
  预先固化 SQL/判断条件——这件事留给运行时读到规则原文的 LLM 现场推理、现场查询。
"""

from __future__ import annotations

import re

from . import table_io
from .models import (
    KnowledgeSchemaMapping,
    Scenario,
    TableMeta,
    TableRole,
)

# 知识表列角色识别（领域无关命名特征，best-effort）
_DISPATCH_KEY_HINTS = ("类型", "分类", "类别", "category", "kind", "type")
_ITEM_ID_HINTS = ("序号", "编号", "编码", "rule_id", "id", "no", "seq", "item_id", "编码")
_CONDITION_HINTS = (
    "情形", "描述", "说明", "清单", "依据", "示例", "案例", "条件",
    "规则", "logic", "description", "条款", "要求", "标准",
)
_PARAM_HINTS = (
    "阈值", "数量", "金额", "比例", "limit", "threshold",
    "max", "min", "value", "上限", "下限", "系数",
)

# field_role_map 的通用语义角色候选 → 命名特征提示词（best-effort）
_GENERIC_FIELD_HINTS: dict[str, tuple[str, ...]] = {
    "item_name": ("名称", "项目", "产品", "商品", "服务", "name", "item"),
    "item_code": ("编码", "编号", "code", "no"),
    "quantity": ("数量", "qty", "count", "quantity"),
    "amount": ("金额", "费用", "价格", "amount", "fee", "price"),
    "entity_id": ("id", "编号", "单号", "流水", "ID"),
    "time": ("日期", "时间", "date", "time"),
}
# 角色 → 优先匹配的字段语义角色（来自 infer_field_semantics 的推断，权重远高于纯命名猜测）
_ROLE_SEMANTIC_PREFERENCE: dict[str, tuple[str, ...]] = {
    "item_name": ("DIM", "CATEGORY"),
    "item_code": ("FK", "PK"),
    "quantity": ("METRIC",),
    "amount": ("METRIC",),
    "entity_id": ("PK", "FK"),
    "time": ("TIME",),
}
# item_name/item_code 不应指向"机构/人员/部门"这类组织实体列——即便列名含"名称/编号"，
# 它们描述的是谁在操作，不是被处理的知识条目对象本身（跨领域通用词，非医保/审计专用）。
_NON_ITEM_ENTITY_HINTS = ("机构", "单位", "人员", "部门", "科室", "医院", "医师", "医生", "经办")
# time 角色应优先落在"业务事件发生时间"，而不是个人属性（出生日期）或系统审计时间戳
# （创建/更新/经办时间）——两者都会通过"日期/时间"命名 hint 命中，但都不是事件时间。
_NON_EVENT_TIME_HINTS = ("创建", "更新", "出生", "经办")

# 通用分派值 → 处理模式（启发式默认，AI 可覆盖/扩展）
# 不含领域特定术语（"违规"等）
_DEFAULT_PATTERN_BY_KEYWORD: tuple[tuple[tuple[str, ...], str], ...] = (
    (("重复", "捆绑", "联用", "分解"), "co_occurrence"),
    (("超量", "超标", "超限", "限量", "超过", "超出"), "threshold"),
    (("互斥", "已含", "不应", "排除"), "exclusive_conflict"),
    (("频次", "频率", "次数", "多次", "重复使用"), "frequency_overflow"),
    (("集合", "差集", "交集", "比对", "名单"), "set_compare"),
    (("时序", "顺序", "流程", "步骤"), "sequence_detect"),
    (("映射", "翻译", "对照", "换算", "查找"), "lookup"),
    (("汇总", "聚合", "统计", "合计"), "aggregate"),
)


def _match_hints(col: str, hints: tuple[str, ...]) -> bool:
    return any(h in str(col) or h in str(col).lower() for h in hints)


def find_knowledge_table(scenario: Scenario) -> TableMeta | None:
    """定位知识表：优先取角色为 knowledge 的表，向后兼容 rule 角色。"""
    # v1.0.4 新角色
    candidates = [t for t in scenario.tables_meta if t.role == "knowledge"]
    # 向后兼容 v1.0.3 的 rule 角色
    if not candidates:
        candidates = [t for t in scenario.tables_meta if t.role == TableRole.RULE.value]
    if not candidates:
        return None
    # 多张时取列数最多者（更可能是主知识表）
    return max(candidates, key=lambda t: t.col_count)


def _column_role_map(knowledge_table: TableMeta) -> dict[str, list[str]]:
    """识别知识表每一列的角色（语义无关，best-effort）。"""
    cols = [c.name for c in knowledge_table.columns]
    used: set[str] = set()
    dispatch_key: list[str] = []
    item_id: list[str] = []
    condition: list[str] = []
    parameter: list[str] = []

    # 分派键列（仅取一个）
    for col in cols:
        if _match_hints(col, _DISPATCH_KEY_HINTS) and col not in used:
            dispatch_key.append(col)
            used.add(col)
            break

    # 条目编号列（仅取一个）
    for col in cols:
        if col in used:
            continue
        if _match_hints(col, _ITEM_ID_HINTS):
            item_id.append(col)
            used.add(col)
            break

    # 参数列（数值参数，可多个）
    for col in cols:
        if col in used:
            continue
        if _match_hints(col, _PARAM_HINTS):
            parameter.append(col)
            used.add(col)

    # 自然语言条件列（剩余的描述性列）
    for col in cols:
        if col in used:
            continue
        if _match_hints(col, _CONDITION_HINTS):
            condition.append(col)
            used.add(col)

    # 兜底：未分类的列一律归入条件列
    for col in cols:
        if col not in used:
            condition.append(col)

    return {
        "dispatch_key": dispatch_key,
        "item_id": item_id,
        "parameter": parameter,
        "condition": condition,
    }


def _dispatch_key_values(
    knowledge_table: TableMeta,
    col: str,
    limit: int = 30,
) -> list[str]:
    """读出分派键列的取值空间（去重，限量）。"""
    try:
        values = table_io.column_value_set(knowledge_table.file_path, col)
    except Exception:  # noqa: BLE001
        return []
    return [str(v).strip() for v in list(values)[:limit] if str(v).strip()]


def _guess_pattern(value: str) -> str:
    """根据分派值的字面，猜一个默认处理模式（完全领域无关）。"""
    for kws, pattern in _DEFAULT_PATTERN_BY_KEYWORD:
        if any(k in value for k in kws):
            return pattern
    return "keyword"  # 兜底：未识别时先用关键词命中（运行时由 AI/用户精化）


def infer_knowledge_schema(scenario: Scenario) -> KnowledgeSchemaMapping | None:
    """从知识表蒸馏出结构映射（无 AI；后续可由 AI 完善 field_role_map）。

    关键：知识表多大不影响本函数——只读列名与分派列的取值空间，不读条件正文。
    """
    knowledge_table = find_knowledge_table(scenario)
    if knowledge_table is None:
        return None

    role_map = _column_role_map(knowledge_table)
    dispatch_key = role_map["dispatch_key"][0] if role_map["dispatch_key"] else ""
    item_id = role_map["item_id"][0] if role_map["item_id"] else ""
    parameter_cols = role_map["parameter"]
    condition_cols = role_map["condition"]

    # 默认分派映射（启发式，AI 可覆盖）
    dispatch_map: dict[str, str] = {}
    if dispatch_key:
        for v in _dispatch_key_values(knowledge_table, dispatch_key):
            dispatch_map.setdefault(v, _guess_pattern(v))

    # 字段角色映射（best-effort，基于业务表列名 + 已推断的字段语义角色）
    # 结构：{语义角色标签: "业务表名.列名"}
    #
    # 旧实现的问题：按 table→column 遍历顺序"第一个命中就用"，会把"人员编号"误判成
    # item_code（因为"编号"两个 hint 列表都含）、把"出生日期"误判成 time（因为
    # 只要含"日期"就命中，不管是不是交易时间）。改为全局打分：
    #   1) 优先选语义角色（infer_field_semantics 推断，权重远高于纯命名猜测）与目标
    #      角色匹配的列（如 time 角色只在没有 TIME 语义列时才退化去匹配任意含"日期"的列）；
    #   2) 同一列不会被两个不同角色同时占用，避免一个字段身兼数职却把更合适的字段挤掉。
    field_role_map: dict[str, str] = {}
    business_tables = [
        t for t in scenario.tables_meta if t.role in (TableRole.INPUT.value, "input")
    ]
    used_columns: set[tuple[str, str]] = set()
    item_table: str = ""  # item_name 落在哪张表，item_code 优先跟它同表（同一条目的名称+编码通常同表）
    for role_label, hints in _GENERIC_FIELD_HINTS.items():
        preferred_roles = _ROLE_SEMANTIC_PREFERENCE.get(role_label, ())
        is_item_role = role_label in ("item_name", "item_code")
        candidates: list[tuple[int, str, str]] = []
        for t in business_tables:
            for col in t.columns:
                if (t.table_name, col.name) in used_columns:
                    continue
                hint_hit = next((h for h in hints if h in col.name), None)
                if hint_hit is None:
                    continue
                if is_item_role and any(h in col.name for h in _NON_ITEM_ENTITY_HINTS):
                    continue  # "单位名称"/"人员编号"这类组织实体列不算item候选
                if role_label == "time" and any(h in col.name for h in _NON_EVENT_TIME_HINTS):
                    continue  # 出生日期/创建时间/更新时间/经办时间不算业务事件时间候选
                sem_bonus = 100 if col.semantic_role in preferred_roles else 0
                colocation_bonus = 50 if (role_label == "item_code" and t.table_name == item_table) else 0
                candidates.append((sem_bonus + colocation_bonus + len(hint_hit), t.table_name, col.name))
        if not candidates:
            continue
        candidates.sort(key=lambda x: -x[0])
        _, best_table, best_col = candidates[0]
        field_role_map[role_label] = f"{best_table}.{best_col}"
        used_columns.add((best_table, best_col))
        if role_label == "item_name":
            item_table = best_table

    summary_parts = [f"知识表「{knowledge_table.table_name}」结构映射推断完成："]
    if dispatch_key:
        summary_parts.append(f"分派键列「{dispatch_key}」({len(dispatch_map)} 种取值)")
    else:
        summary_parts.append("未识别明确的分派键列（将由 AI 在流程推导时补全）")
    if item_id:
        summary_parts.append(f"条目编号列「{item_id}」")
    if condition_cols:
        summary_parts.append(f"自然语言条件列 {len(condition_cols)} 个：{condition_cols[:3]}")
    if parameter_cols:
        summary_parts.append(f"参数列 {len(parameter_cols)} 个：{parameter_cols[:3]}")

    return KnowledgeSchemaMapping(
        knowledge_table=knowledge_table.table_name,
        dispatch_key_column=dispatch_key,
        item_id_column=item_id,
        condition_columns=condition_cols,
        parameter_columns=parameter_cols,
        dispatch_map=dispatch_map,
        field_role_map=field_role_map,
        summary="；".join(summary_parts) + "。",
    )


# ---------------------------------------------------------------------------
# 向后兼容：旧 API 的 shim（rule_schema.py 中的函数名映射到本模块）
# ---------------------------------------------------------------------------
def find_rule_table(scenario: Scenario) -> TableMeta | None:
    """向后兼容 rule_schema.find_rule_table。"""
    return find_knowledge_table(scenario)


def infer_rule_schema(scenario: Scenario):
    """向后兼容 rule_schema.infer_rule_schema，返回 KnowledgeSchemaMapping。"""
    return infer_knowledge_schema(scenario)
