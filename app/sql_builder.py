"""确定性 SQL 模板生成 + 领域知识构建（Phase 1/2/4，无 AI）。

把规则库的每个违规类型，依据「违规类型/关键词 + 可用字段」选择策略，生成可执行的
DuckDB SQL 模板，并产出 domain_knowledge（数据字典 + 结果表结构契约）。

策略：
* over_standard（超标准/超限价收费）：明细表中 单价 > 定价上限金额（>0）。
* duplicate（重复收费）：同一就诊下同一医保目录编码出现多次的明细行。
* keyword（默认）：按规则关键词在「目录名称」列做 LIKE 命中。
* blocked：无可用字段/关键词，或显式依赖外部数据 → 运行时拒绝执行并说明缺什么。

注意：运行时只执行这些 SQL（DuckDB），不调用 AI、不重新分析 schema。
"""

from __future__ import annotations

from . import executor, rule_parser
from .models import (
    DomainColumn,
    DomainKnowledge,
    DomainTable,
    RuleLibrary,
    RuleTemplate,
    Scenario,
    TableMeta,
)


# ===========================================================================
# 标识符 / 字面量
# ===========================================================================
def q(identifier: str) -> str:
    """DuckDB 标识符引用（双引号，转义内部双引号）。"""
    return '"' + str(identifier).replace('"', '""') + '"'


def lit(value: str) -> str:
    """SQL 字符串字面量（单引号，转义内部单引号）。"""
    return "'" + str(value).replace("'", "''") + "'"


# ===========================================================================
# 表角色与字段定位
# ===========================================================================
def _role(table: TableMeta) -> str:
    if rule_parser.is_rule_table(table):
        return "rule"
    if executor._is_result_like(table):
        return "result"
    return "input"


def _cols(table: TableMeta) -> list[str]:
    return [c.name for c in table.columns]


def _input_tables(scenario: Scenario) -> list[TableMeta]:
    return [t for t in scenario.tables_meta if _role(t) == "input"]


def _detail_table(scenario: Scenario) -> TableMeta | None:
    """定位「明细表」：含 单价/数量/医保目录编码 的最大输入表。"""
    candidates = []
    for t in _input_tables(scenario):
        cols = set(_cols(t))
        score = sum(1 for c in ("单价", "数量", "医保目录编码", "明细项目费用总额") if c in cols)
        if score >= 2:
            candidates.append((score, t.row_count, t))
    if not candidates:
        return None
    candidates.sort(key=lambda x: (x[0], x[1]), reverse=True)
    return candidates[0][2]


def _has(table: TableMeta, *cols: str) -> bool:
    have = set(_cols(table))
    return all(c in have for c in cols)


# ===========================================================================
# 领域知识（domain_knowledge）
# ===========================================================================
def _result_tables(scenario: Scenario) -> list[TableMeta]:
    return [t for t in scenario.tables_meta if _role(t) == "result"]


def build_domain_knowledge(scenario: Scenario) -> DomainKnowledge:
    """构建数据字典 + ER 关系 + 结果表结构契约（result_schema）。"""
    tables = [
        DomainTable(
            table_name=t.table_name,
            role=_role(t),
            file=t.display_name or f"{t.table_name}",
            row_count=t.row_count,
            header_row=t.header_row,
            columns=[DomainColumn(name=c.name, dtype=c.dtype) for c in t.columns],
        )
        for t in scenario.tables_meta
    ]
    relations = scenario.relations.relations if scenario.relations else []

    # 结果表结构契约：违规类型 → 结果列；并取首个结果表为 __default__
    result_schema: dict[str, list[str]] = {}
    vtypes = scenario.rule_library.violation_types if scenario.rule_library else []
    results = _result_tables(scenario)
    if results:
        result_schema["__default__"] = _cols(results[0])
    for rt in results:
        for vt in vtypes:
            if vt and len(vt) >= 3 and vt in rt.table_name:
                result_schema[vt] = _cols(rt)

    return DomainKnowledge(
        scenario=scenario.name,
        tables=tables,
        relations=relations,
        result_schema=result_schema,
    )


def output_columns_for(domain: DomainKnowledge, violation_type: str,
                       fallback: list[str]) -> list[str]:
    """某违规类型的结果列：优先该类型的历史结果结构，其次默认结果结构，再退化为给定列。"""
    if violation_type in domain.result_schema:
        return list(domain.result_schema[violation_type])
    if "__default__" in domain.result_schema:
        return list(domain.result_schema["__default__"])
    return list(fallback)


# ===========================================================================
# 单条规则 → SQL 模板
# ===========================================================================
_OVER_STD_HINTS = ("超标准", "超限价", "超定价", "超物价")
_DUP_HINTS = ("重复",)
# 用于 LIKE 命中的文本列优先级
_TEXT_COL_PREF = ("医保目录名称", "医药机构目录名称", "商品名", "项目名称", "收费项目名称")
# 关键词过滤时剔除的过泛词
_KW_STOP = {"收费", "项目", "费用", "医保", "目录", "医疗", "服务", "违规", "标准", "管理"}


def _meaningful_keywords(template: RuleTemplate, limit: int = 3) -> list[str]:
    out: list[str] = []
    for k in template.keywords:
        k = (k or "").strip()
        if len(k) < 2 or k in _KW_STOP or k.isdigit():
            continue
        if k not in out:
            out.append(k)
        if len(out) >= limit:
            break
    return out


def build_sql_for_template(
    template: RuleTemplate, scenario: Scenario, domain: DomainKnowledge
) -> RuleTemplate:
    """为单个规则模板选择策略并生成 SQL；就地更新模板并返回。"""
    detail = _detail_table(scenario)
    vt = template.violation_type or ""
    template.required_tables = [detail.table_name] if detail else []

    if detail is None:
        template.status = "blocked"
        template.external_data_needed = ["可识别的费用明细表（含 单价/数量/医保目录编码 等列）"]
        template.sql = ""
        template.strategy = "blocked"
        return template

    dt = q(detail.table_name)
    vt_lit = lit(vt or "违规")

    # ---- 策略 1：超标准收费（单价 > 定价上限金额）----
    if any(h in vt for h in _OVER_STD_HINTS) and _has(detail, "单价", "定价上限金额"):
        template.strategy = "over_standard"
        template.status = "unverified"
        template.filter_logic = "单价 > 定价上限金额 且 定价上限金额 > 0"
        template.sql = (
            f"SELECT *,\n"
            f"       {vt_lit} AS \"违规类型\",\n"
            f"       '单价' || CAST({q('单价')} AS VARCHAR) || ' 超过定价上限' || CAST({q('定价上限金额')} AS VARCHAR) AS \"违规说明\"\n"
            f"FROM {dt}\n"
            f"WHERE {q('定价上限金额')} IS NOT NULL AND {q('定价上限金额')} > 0\n"
            f"  AND {q('单价')} IS NOT NULL AND {q('单价')} > {q('定价上限金额')}"
        )
    # ---- 策略 2：重复收费（同一就诊同一目录编码多次）----
    elif any(h in vt for h in _DUP_HINTS) and _has(detail, "就诊ID", "医保目录编码"):
        template.strategy = "duplicate"
        template.status = "unverified"
        template.aggregation = "GROUP BY 就诊ID, 医保目录编码 HAVING COUNT(*) > 1"
        template.sql = (
            f"WITH dup AS (\n"
            f"  SELECT {q('就诊ID')}, {q('医保目录编码')}\n"
            f"  FROM {dt}\n"
            f"  WHERE {q('就诊ID')} IS NOT NULL AND {q('医保目录编码')} IS NOT NULL\n"
            f"  GROUP BY {q('就诊ID')}, {q('医保目录编码')}\n"
            f"  HAVING COUNT(*) > 1\n"
            f")\n"
            f"SELECT d.*, {vt_lit} AS \"违规类型\", '同一就诊重复收取同一项目' AS \"违规说明\"\n"
            f"FROM {dt} d\n"
            f"JOIN dup ON d.{q('就诊ID')} = dup.{q('就诊ID')}\n"
            f"       AND d.{q('医保目录编码')} = dup.{q('医保目录编码')}"
        )
    else:
        # ---- 策略 3：关键词命中（默认）----
        text_col = next((c for c in _TEXT_COL_PREF if _has(detail, c)), None)
        kws = _meaningful_keywords(template)
        if text_col and kws:
            template.strategy = "keyword"
            template.status = "unverified"
            likes = " OR ".join(f"{q(text_col)} LIKE {lit('%' + k + '%')}" for k in kws)
            template.filter_logic = f"{text_col} 命中关键词 {kws}"
            template.sql = (
                f"SELECT *, {vt_lit} AS \"违规类型\",\n"
                f"       '命中规则关键词：{'、'.join(kws)}' AS \"违规说明\"\n"
                f"FROM {dt}\n"
                f"WHERE {likes}"
            )
        else:
            # ---- 无可用策略：blocked，声明缺什么 ----
            template.strategy = "blocked"
            template.status = "blocked"
            template.sql = ""
            need = []
            if not text_col:
                need.append("可供文本匹配的项目名称列")
            if not kws:
                need.append("该规则的可判定关键词/阈值（描述过于宽泛，需补充判定口径）")
            template.external_data_needed = need or ["该规则的判定口径或外部标准数据"]

    template.output_columns = output_columns_for(domain, vt, _cols(detail))
    return template


def build_rule_sql_library(scenario: Scenario, domain: DomainKnowledge) -> RuleLibrary:
    """为规则库中所有模板生成 SQL（Phase 2/4：每个违规类型一个模板）。"""
    lib = scenario.rule_library
    if lib is None:
        return RuleLibrary(summary="规则库为空。")
    for t in lib.templates:
        build_sql_for_template(t, scenario, domain)
    n_exec = sum(1 for t in lib.templates if t.sql)
    n_blocked = sum(1 for t in lib.templates if t.status == "blocked")
    lib.summary = (
        f"规则库共 {len(lib.templates)} 条规则 / {len(lib.violation_types)} 种违规类型；"
        f"已生成可执行 SQL {n_exec} 条，缺数据/口径 {n_blocked} 条。"
    )
    return lib
