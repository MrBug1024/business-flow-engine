"""校验与反馈闭环（Validators）— v1.0.7。

各步骤的校验逻辑：
  ① 上传后  —— validate_trace_connectivity：追踪采样关联性是否足够支持 AI 推导？
  ② 推关联后 —— sanitize_relations：关联是否引用了根本不存在的表/字段（AI 编造）？
              preserve_confirmed_relations / upsert_confirmed_relation：人工确认的关联
              必须原样保留，不被下一轮推导覆盖。
  ③ 推流程后 —— detect_literal_params：节点参数是否把某条具体记录的字面值硬编码了？

知识驱动产出的执行结果不再由平台侧统一比对/诊断——真实业务规则的判断逻辑千差万别，
是否命中、命中得对不对，交给验证通道里读到规则原文的 LLM 现场判断（见
verification_agent.py），不是平台预置一套通用比对公式能覆盖的。
"""

from __future__ import annotations

from enum import Enum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .models import FlowStep, Relation, RelationResult, TableMeta


# ===========================================================================
# 通用校验结论
# ===========================================================================
class ValidationLevel(str, Enum):
    PASS = "pass"
    WARNING = "warning"
    FAIL = "fail"


class ValidationResult:
    def __init__(
        self,
        level: ValidationLevel,
        message: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        self.level = level
        self.message = message
        self.details = details or {}
        self.passed = level == ValidationLevel.PASS

    def __repr__(self) -> str:
        return f"<ValidationResult {self.level}: {self.message}>"

    def to_dict(self) -> dict[str, Any]:
        return {"level": self.level, "message": self.message, "details": self.details}


def _pass(message: str, **details: Any) -> ValidationResult:
    return ValidationResult(ValidationLevel.PASS, message, details)


def _warn(message: str, **details: Any) -> ValidationResult:
    return ValidationResult(ValidationLevel.WARNING, message, details)


def _fail(message: str, **details: Any) -> ValidationResult:
    return ValidationResult(ValidationLevel.FAIL, message, details)


# ===========================================================================
# Step ①→② 之间：追踪采样关联性验证
# ===========================================================================
def validate_trace_connectivity(trace_report: dict[str, Any]) -> ValidationResult:
    """检查追踪采样的关联性是否足够支持 AI 推导。

    PASS:    有至少一个 key 值在多个表中匹配到行（有因果关联）
    WARNING: 有表完全追不上（部分关联）
    FAIL:    所有表都追不上，或总行数 < 5（关联性严重不足）

    Args:
        trace_report: trace_sampling() 返回的报告

    Returns:
        ValidationResult with level PASS / WARNING / FAIL
    """
    if not trace_report:
        return _fail("追踪报告为空，无法判断关联性")

    total_rows = trace_report.get("total_rows", 0)
    trace_map = trace_report.get("trace_map", {})
    unmatched = trace_report.get("unmatched_tables", [])
    degraded = trace_report.get("degraded", False)
    result_sample = trace_report.get("result_sample", [])

    if degraded or not result_sample:
        return _warn(
            "未找到结果表，降级为随机采样——AI 推导置信度可能不足",
            unmatched_tables=unmatched,
            total_rows=total_rows,
        )

    if total_rows < 5:
        return _fail(
            f"样本关联性严重不足（总行数 {total_rows} < 5），建议检查数据是否正确上传",
            total_rows=total_rows,
        )

    # 统计通过关键 key 匹配（非随机）的表数
    high_conf_tables = [
        t for t, info in trace_map.items()
        if info.get("trace_confidence") in ("high", "medium")
        and info.get("matched_by") != "random"
    ]

    if len(unmatched) > 0 and len(high_conf_tables) == 0:
        return _fail(
            f"所有业务表都无法关联追踪，以下表追不上：{unmatched}",
            unmatched_tables=unmatched,
            total_rows=total_rows,
        )

    if unmatched:
        return _warn(
            f"以下表未能通过键值关联追踪，退化为随机采样：{unmatched}",
            unmatched_tables=unmatched,
            high_confidence_tables=high_conf_tables,
            total_rows=total_rows,
        )

    return _pass(
        f"追踪关联性充足：{len(high_conf_tables)} 张表通过键值匹配，"
        f"总样本 {total_rows} 行",
        high_confidence_tables=high_conf_tables,
        total_rows=total_rows,
        trace_summary=trace_report.get("trace_summary", ""),
    )


# ===========================================================================
# Step ②之后：关联关系"幻觉"校验
# ===========================================================================
def sanitize_relations(
    relations: list["Relation"],
    tables_meta: list["TableMeta"],
) -> tuple[list["Relation"], list[str]]:
    """剔除 LLM 编造出来的、引用了根本不存在的表/字段的"关联关系"。

    真实发生过的案例：模型给出一条 `规则表.序号 → 结果表.序号` 的关联，但结果表
    压根没有「序号」这一列——模型看到结果表文件名里含有规则编号（如"规412"），
    联想到规则表有「序号」字段，就编了一条对应关系，事后连它自己给的 evidence
    都承认"结果表样本中未出现序号字段"。这种情况不该只是置信度打个折，而是
    整条关联都不成立，必须剔除，不能留在结果里误导后续的流程推导。

    Returns:
        (清洗后的关联列表, 面向用户的问题列表)
    """
    columns_by_table: dict[str, set[str]] = {
        t.table_name: {c.name for c in t.columns} for t in tables_meta
    }

    sane: list["Relation"] = []
    questions: list[str] = []
    for r in relations:
        from_cols = columns_by_table.get(r.from_table)
        to_cols = columns_by_table.get(r.to_table)
        if from_cols is None or to_cols is None:
            bad_table = r.from_table if from_cols is None else r.to_table
            questions.append(
                f"❌ 已剔除一条无效关联：{r.from_table}.{r.from_column} → "
                f"{r.to_table}.{r.to_column}——「{bad_table}」不是本场景中存在的表。"
            )
            continue
        if r.from_column not in from_cols or r.to_column not in to_cols:
            bad_side = (
                f"{r.from_table}.{r.from_column}" if r.from_column not in from_cols
                else f"{r.to_table}.{r.to_column}"
            )
            questions.append(
                f"❌ 已剔除一条无效关联：{r.from_table}.{r.from_column} → "
                f"{r.to_table}.{r.to_column}——「{bad_side}」这一列在真实表结构里不存在"
                f"（疑似凭表名/结果文件名联想编造，而非真实字段），需要重新核实这条关联。"
            )
            continue
        sane.append(r)
    return sane, questions


def filter_low_confidence_relations(
    relations: list["Relation"],
    min_confidence: float = 0.8,
) -> tuple[list["Relation"], int]:
    """过滤未确认的弱关联候选。

    弱候选不进入 ER 图，也不作为问题反复追问用户。普通用户不应该被要求判断
    "两个字段名看起来像不像关联键"；如果缺少足够证据，系统应先保守跳过。
    """
    kept: list["Relation"] = []
    dropped = 0
    for r in relations:
        if getattr(r, "confirmed", False):
            kept.append(r)
            continue
        if float(getattr(r, "confidence", 0.0) or 0.0) >= min_confidence:
            kept.append(r)
            continue
        dropped += 1
    return kept, dropped


def _relation_key(r: "Relation") -> tuple:
    from_cols = tuple(r.from_columns or [r.from_column])
    to_cols = tuple(r.to_columns or [r.to_column])
    return (r.from_table, from_cols, r.to_table, to_cols)


def upsert_confirmed_relation(
    relation_result: "RelationResult",
    from_table: str,
    from_column: str,
    to_table: str,
    to_column: str,
    from_columns: list[str] | None = None,
    to_columns: list[str] | None = None,
    relation_type: str = "foreign_key",
) -> "Relation":
    """人工确认一条关联：已存在就地提升为已确认（置信度锁定 1.0），不存在则新增。

    REST 接口（前端"确认关联"按钮）和对话工具（用户在聊天里明确纠正）共用同一份
    逻辑，保证两个入口的语义完全一致。
    """
    from .models import Relation  # noqa: PLC0415

    req_from_cols = tuple(from_columns or [from_column])
    req_to_cols = tuple(to_columns or [to_column])
    for r in relation_result.relations:
        if _relation_key(r) == (from_table, req_from_cols, to_table, req_to_cols):
            r.confirmed = True
            r.confidence = 1.0
            r.relation_type = relation_type
            return r

    new_relation = Relation(
        from_table=from_table,
        from_column=from_column,
        to_table=to_table,
        to_column=to_column,
        from_columns=list(req_from_cols),
        to_columns=list(req_to_cols),
        relation_type=relation_type,
        confidence=1.0,
        confirmed=True,
        evidence="人工确认",
    )
    relation_result.relations.append(new_relation)
    return new_relation


def preserve_confirmed_relations(
    old_relations: list["Relation"] | None,
    new_relations: list["Relation"],
) -> list["Relation"]:
    """人工确认过的关联，重新推导时必须原样保留，不能被新一轮 AI 推导悄悄覆盖/丢弃。

    背景：用户已经肉眼核实过某两个字段值完全相等、确认这是一条真实关联，但每次
    「推导关联关系」都是重新跑一遍、结果整体替换——人工确认的东西无法"生效"，
    等于白确认。现在：新推导结果里，已确认关联若还在，直接用人工确认过的那个
    版本（保留 confirmed=True 和人工设定的置信度，不被新一轮打分覆盖）；
    新推导没有再找到的已确认关联，也要原样追加回来，不能凭空消失。
    """
    if not old_relations:
        return new_relations
    confirmed = [r for r in old_relations if r.confirmed]
    if not confirmed:
        return new_relations

    merged = list(new_relations)
    new_keys = {_relation_key(r): i for i, r in enumerate(merged)}
    for r in confirmed:
        key = _relation_key(r)
        if key in new_keys:
            merged[new_keys[key]] = r
        else:
            merged.append(r)
    return merged


# ===========================================================================
# Step ③ 之后：节点参数字面值硬编码校验
# ===========================================================================
_KNOWN_PARAM_MODE_WORDS = {
    "count", "sum", "avg", "min", "max", "*",
    "inner", "left", "right", "outer", "cross",
    "week", "day", "month", "year", "quarter",
    "asc", "desc", "and", "or",
}


def _iter_param_values(obj: Any, path: str = ""):
    """递归遍历 params（dict/list 嵌套）里的每一个叶子字符串值，产出 (路径, 值)。"""
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield from _iter_param_values(v, f"{path}.{k}" if path else str(k))
    elif isinstance(obj, list):
        for v in obj:
            yield from _iter_param_values(v, path)
    elif isinstance(obj, str):
        yield path, obj


def detect_literal_params(
    steps: list["FlowStep"],
    tables_meta: list["TableMeta"],
) -> list[str]:
    """扫描流程节点 params，找出疑似把「知识表/业务表某一具体条目的字面值」
    硬编码进通用节点参数的情况（如 params 里出现 `"keyword": "中医刮痧"` 这种
    具体业务值，而不是字段名/角色占位符）。

    这类硬编码会导致对应节点、进而整个技能只能处理这一个具体值，换一个知识表条目
    或换一种用户查询就完全不work——这正是技能"学会一种查询就再也不会别的"的根因之一。

    判定信号：某个 params 叶子字符串值
      1) 不是任何已知表名/列名（那种是合法的字段占位符，不算硬编码）；
      2) 不是常见的模板控制词（count/sum/inner/week 等）；
      3) 且这个值本身**逐字**出现在某张表某一列的真实样本值里——说明它极可能是从
         某一条具体记录里摘出来的字面值，而不是通用参数。

    Returns:
        面向用户的问题列表（可直接并入 ambiguous_questions，走既有的"有疑问必停"流程）。
    """
    known_names: set[str] = set()
    sample_value_index: dict[str, list[str]] = {}
    for t in tables_meta:
        known_names.add(t.table_name)
        for c in t.columns:
            known_names.add(c.name)
            for v in (c.sample_values or []):
                sv = str(v).strip()
                if sv and 2 <= len(sv) <= 40:
                    sample_value_index.setdefault(sv, []).append(f"{t.table_name}.{c.name}")

    findings: list[str] = []
    for step in steps:
        if not step.params:
            continue
        for key, val in _iter_param_values(step.params):
            v = val.strip()
            if not v or v in known_names or v.lower() in _KNOWN_PARAM_MODE_WORDS:
                continue
            hits = sample_value_index.get(v)
            if not hits:
                continue
            findings.append(
                f"步骤{step.step_id}「{step.step_name}」的参数 {key}=\"{v}\" 疑似把 "
                f"{hits[0]} 的某一条具体记录的值写死进了节点参数——这样该节点只能处理这一个值，"
                "无法泛化到知识表/业务表中的其它同类条目。请确认：这个值是否应该改为从知识表"
                "按行读取（每行一个不同的值），还是确实是本场景固定不变的业务口径？"
            )
    return findings

