"""验证与执行层（Layer 3：Python，无 AI 决策）。

v1.0.1 关键纪律：AI 生成的逻辑必须在**完整原始数据**上由 Python 执行，再与历史结果表
对照，只把「差异摘要」（命中/缺失/多出 + 少量样本）回传给 AI。AI 永远看不到全量数据。

职责：
    1) run_template —— 用 DuckDB 执行某规则模板的 SQL，产出违规明细（记录实际 SQL）。
    2) compare      —— 复刻结果 vs 历史结果，按主键比对，产出 ValidationReport。
    3) find_historical_table —— 为某违规类型定位对应的历史结果表。
"""

from __future__ import annotations

from typing import Any

import pandas as pd

from . import rule_parser, sql_engine, table_io
from .models import RuleTemplate, Scenario, TableMeta, ValidationReport

# 可能作为「主键」的列名特征（用于复刻 vs 历史 的行级比对）
_KEY_HINTS = ("流水号", "明细", "结算id", "就诊id", "单据", "编号", "id", "no", "code")
# 金额/数量类列（比对主键时应排除）
_AMOUNT_HINTS = ("金额", "费用", "数量", "价", "amount", "fee", "price", "qty")


# ===========================================================================
# 历史结果表定位
# ===========================================================================
def _is_business_or_result(table: TableMeta) -> bool:
    return not rule_parser.is_rule_table(table)


# 历史结果表的命名特征：通常是「违规类型-具体描述」或含结果/违规标记
_RESULT_HINTS = ("-", "结果", "违规", "明细清单", "result", "清单")
# 核心业务输入表（非结果表）的命名特征
_INPUT_HINTS = ("就诊", "结算", "明细表", "台账", "流水表")


def _looks_like_input_table(name: str) -> bool:
    has_input = any(h in name for h in _INPUT_HINTS)
    has_result = any(h in name for h in _RESULT_HINTS if h != "-")
    return has_input and not has_result and "-" not in name


def find_historical_table(scenario: Scenario, violation_type: str) -> TableMeta | None:
    """为某违规类型定位历史结果表。

    采用「表名包含违规类型」的强信号，并排除核心业务输入表（如就诊表/项目明细表）。
    要求违规类型至少 3 字，避免「项目」之类短词误匹配明细表。
    """
    vt = (violation_type or "").strip()
    if len(vt) < 3:
        return None
    matches: list[TableMeta] = []
    for t in scenario.tables_meta:
        if rule_parser.is_rule_table(t):
            continue
        name = t.table_name
        if vt in name and not _looks_like_input_table(name):
            matches.append(t)
    if not matches:
        return None
    # 优先命名呈「结果表」形态（含 '-' 或结果标记）的候选
    matches.sort(key=lambda t: (any(h in t.table_name for h in _RESULT_HINTS), len(t.table_name)),
                 reverse=True)
    return matches[0]


# ===========================================================================
# 数据装载
# ===========================================================================
def _is_result_like(table: TableMeta) -> bool:
    """是否像历史结果表（输出），而非业务输入表。"""
    name = table.table_name
    return ("-" in name) or any(h in name for h in _RESULT_HINTS if h != "-")


def _input_business_tables(scenario: Scenario) -> list[TableMeta]:
    """场景中的业务输入表：排除规则表（知识库）与历史结果表（输出）。"""
    return [t for t in scenario.tables_meta
            if not rule_parser.is_rule_table(t) and not _is_result_like(t)]


def _table_files(
    scenario: Scenario,
    data_sources: dict[str, str] | None,
    only: list[str] | None = None,
) -> dict[str, str]:
    """构建 {表名: 文件路径}（不读盘）。

    - data_sources 指定新数据文件时优先；
    - 否则取场景的业务输入表（排除规则表与历史结果表）；
    - only 给定时仅保留这些表（按 required_tables 精准定位，避免无谓大表 IO）。
    """
    base: dict[str, str]
    if data_sources:
        base = dict(data_sources)
    else:
        base = {t.table_name: t.file_path for t in _input_business_tables(scenario)}
    if only:
        base = {k: v for k, v in base.items() if k in only}
    return base


def _align_columns(df: pd.DataFrame, output_columns: list[str]) -> pd.DataFrame:
    """把结果列对齐到结果表结构契约（缺失列补 NA，多余列丢弃，顺序固定）。

    保证 execute_audit 的输出列与 domain_knowledge 中的历史结果结构**完全一致**。
    """
    if not output_columns:
        return df
    return df.reindex(columns=output_columns)


# ===========================================================================
# 规则执行（SQL / DuckDB）
# ===========================================================================
def run_template(
    scenario: Scenario,
    template: RuleTemplate,
    data_sources: dict[str, str] | None = None,
    align: bool = True,
) -> tuple[pd.DataFrame | None, str, str]:
    """执行某规则模板的 SQL，返回 (结果DataFrame|None, 实际执行的SQL, 错误)。

    - blocked / 无 SQL：返回 (None, sql, 'BLOCKED:...') —— 运行时拒绝并说明缺什么，绝不静默返回 0。
    - 执行成功：按 output_columns 对齐结果列（align=True 时）。
    - 全程不调用 AI、不重新分析 schema。
    """
    if template.status == "blocked" or not (template.sql and template.sql.strip()):
        need = template.external_data_needed or ["该违规类型的判定口径或外部标准数据"]
        return None, template.sql or "", (
            f"BLOCKED: 违规类型「{template.violation_type}」缺少必要数据/口径：{need}。"
            "已拒绝执行（而非返回 0 行）。"
        )
    files = _table_files(scenario, data_sources, only=template.required_tables or None)
    try:
        df, sql = sql_engine.execute_template_sql(
            template.sql, template.required_tables, files
        )
    except Exception as exc:  # noqa: BLE001  连同 SQL 一并上报，便于排查
        return None, template.sql, f"{type(exc).__name__}: {exc}"
    if align:
        df = _align_columns(df, template.output_columns)
    return df, sql, ""


# ===========================================================================
# 对照比对
# ===========================================================================
def _detect_key_columns(produced: pd.DataFrame, historical: pd.DataFrame) -> list[str]:
    """选取用于行级比对的主键列：两表共有、且形似标识符的列。"""
    common = [c for c in produced.columns if c in historical.columns]
    keyish = [c for c in common if any(h in str(c).lower() for h in _KEY_HINTS)]
    if keyish:
        return keyish[:2]  # 取至多 2 个标识列组成联合主键
    # 退化：用全部共有列（排除金额类）做整行比对
    return [c for c in common if not any(h in str(c).lower() for h in _AMOUNT_HINTS)] or common


def _key_series(df: pd.DataFrame, cols: list[str]) -> pd.Series:
    return df[cols].astype(str).agg("|".join, axis=1)


def compare(
    produced: pd.DataFrame,
    historical: pd.DataFrame,
    violation_type: str = "",
    rule_id: str = "",
    historical_table: str = "",
) -> ValidationReport:
    """比对复刻结果与历史结果，产出差异摘要（仅含统计与少量样本）。"""
    if produced is None:
        produced = pd.DataFrame()
    if produced.empty or historical.empty:
        return ValidationReport(
            violation_type=violation_type, rule_id=rule_id,
            historical_table=historical_table,
            produced_count=int(len(produced)), historical_count=int(len(historical)),
            passed=False,
            message="复刻结果或历史结果为空，无法比对。请检查规则逻辑或历史结果表。",
        )

    keys = _detect_key_columns(produced, historical)
    if not keys:
        return ValidationReport(
            violation_type=violation_type, rule_id=rule_id,
            historical_table=historical_table,
            produced_count=int(len(produced)), historical_count=int(len(historical)),
            passed=False, message="复刻结果与历史结果无共同列，无法按主键比对。",
        )

    prod_keys = set(_key_series(produced, keys))
    hist_series = _key_series(historical, keys)
    hist_keys = set(hist_series)

    matched = prod_keys & hist_keys
    missing = hist_keys - prod_keys
    extra = prod_keys - hist_keys
    hist_count = len(hist_keys)
    match_rate = len(matched) / hist_count if hist_count else 0.0

    def _samples(df: pd.DataFrame, cols: list[str], wanted: set[str], n: int = 3):
        ser = _key_series(df, cols)
        rows = df[ser.isin(wanted)].head(n)
        out: list[dict[str, Any]] = []
        for _, r in rows.iterrows():
            out.append({str(k): table_io._jsonable(v) for k, v in r.to_dict().items()})
        return out

    passed = match_rate >= 0.95 and len(extra) <= max(1, int(0.05 * hist_count))
    msg = (
        f"按主键 {keys} 比对：历史 {hist_count} 行，复刻 {len(prod_keys)} 行，"
        f"命中 {len(matched)}，缺失 {len(missing)}，多出 {len(extra)}，命中率 {match_rate:.1%}。"
        + ("✅ 校验通过。" if passed else "⚠️ 尚未达标，需调整规则逻辑或与用户对齐口径。")
    )
    return ValidationReport(
        violation_type=violation_type, rule_id=rule_id, historical_table=historical_table,
        produced_count=int(len(prod_keys)), historical_count=hist_count,
        matched=len(matched), missing=len(missing), extra=len(extra),
        match_rate=round(match_rate, 4), passed=passed, key_columns=keys,
        sample_missing=_samples(historical, keys, missing),
        sample_extra=_samples(produced, keys, extra),
        message=msg,
    )


def execute_and_compare(
    scenario: Scenario,
    template: RuleTemplate,
    data_sources: dict[str, str] | None = None,
) -> ValidationReport:
    """执行规则 SQL 并与历史结果表对照（Layer 3 一站式）。

    无论命中与否都记录 executed_sql；被拦截/出错/0 结果都给出明确信息与 SQL，绝不静默 0。
    """
    historical_meta = find_historical_table(scenario, template.violation_type)
    # 对照校验需用 SQL 原始产出（不强行对齐结果列，以便按真实键比对）
    produced, sql, error = run_template(scenario, template, data_sources, align=False)

    if error:
        return ValidationReport(
            violation_type=template.violation_type, rule_id=template.rule_id,
            executed_sql=sql, error=error, passed=False,
            message=(f"执行未成功：{error}"
                     + (f"\n—— 实际执行 SQL ——\n{sql}" if sql else "")),
        )

    if historical_meta is None:
        msg = f"复刻产出 {len(produced)} 行违规明细，但未找到「{template.violation_type}」对应的历史结果表，无法对照校验。"
        if len(produced) == 0:
            msg += f"\n⚠️ 结果为 0 行。—— 实际执行 SQL ——\n{sql}"
        return ValidationReport(
            violation_type=template.violation_type, rule_id=template.rule_id,
            produced_count=int(len(produced)), executed_sql=sql, passed=False, message=msg,
        )

    historical = table_io.load_full_frame(historical_meta.file_path)
    report = compare(
        produced, historical,
        violation_type=template.violation_type, rule_id=template.rule_id,
        historical_table=historical_meta.table_name,
    )
    report.executed_sql = sql
    if report.produced_count == 0:
        report.message += f"\n⚠️ 复刻结果为 0 行。—— 实际执行 SQL ——\n{sql}"
    return report
