"""验证与执行层（Layer 3：Python，无 AI 决策；场景无关）。

纪律：转换逻辑（SQL）在**完整原始数据**上由 DuckDB 执行，产出结果文件（复刻历史结果格式），
再与历史结果对照，只把「差异摘要」（命中/缺失/多出 + 少量样本）回传给 AI。AI 永不接触全量数据。

职责：
    1) produce             —— 执行某产出规格的 SQL/pipeline，得到结果 DataFrame，并复刻为输出文件。
    2) execute_and_compare —— 一站式：执行 + 复刻落盘（历史结果仅作列结构模板，不做行级比对）。

v1.0.3 关键修复：**多节点 pipeline 现在真正能跨语句执行**。
* 若 OutputSpec 有 pipeline（FlowStep 列表），把每个节点的 SQL 拼接成一段多语句脚本，
  在同一 DuckDB 连接内依次执行，上游节点的 VIEW 对下游可见。
* 若 OutputSpec 仅有单步 sql，按原方式执行。
表角色、源表、列契约全部取自知识包（数据），代码里**没有任何业务字段名/表名**。
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from app.data import output_render, sql_engine
from app.domain.models import OutputSpec, Scenario, TableMeta, TableRole, ValidationReport


# ===========================================================================
# 表定位（按角色，不靠文件名猜）
# ===========================================================================
def _input_business_tables(scenario: Scenario) -> list[TableMeta]:
    """业务输入表：角色为 input；若全未标注角色，则退化为「非知识、非结果」。"""
    roled = [t for t in scenario.tables_meta if t.role == TableRole.INPUT.value]
    if roled:
        return roled
    return [t for t in scenario.tables_meta
            if t.role not in (
                TableRole.KNOWLEDGE.value,
                TableRole.RULE.value,
                TableRole.RESULT.value,
            )]


def _table_files(
    scenario: Scenario,
    data_sources: dict[str, str] | None,
    only: list[str] | None = None,
) -> dict[str, str]:
    """构建 {表名: 文件路径}（不读盘）。data_sources 优先；否则取业务输入表 + 知识表。"""
    if data_sources:
        base = dict(data_sources)
    else:
        base = {t.table_name: t.file_path for t in _input_business_tables(scenario)}
        # 知识表也可能被转换 SQL 引用
        for t in scenario.tables_meta:
            if t.role in (TableRole.KNOWLEDGE.value, TableRole.RULE.value):
                base.setdefault(t.table_name, t.file_path)
    if only:
        base = {k: v for k, v in base.items() if k in only}
    return base


def _align_columns(df: pd.DataFrame, columns: list[str]) -> pd.DataFrame:
    """把结果列对齐到结果结构契约（缺列补 NA，多余列丢弃，顺序固定）。"""
    if not columns:
        return df
    return df.reindex(columns=columns)


# ===========================================================================
# 执行 + 输出复刻
# ===========================================================================
def _compose_pipeline_sql(spec: OutputSpec) -> str:
    """把 pipeline 节点的 SQL 组合成一段**多语句脚本**：
    每个节点的 SQL 被包装为 `CREATE OR REPLACE TEMP VIEW <output> AS ...;`，
    最后追加 `SELECT * FROM <last_output>` 以拉出结果。

    若某节点的 SQL 已经是 `CREATE [OR REPLACE] [TEMP] VIEW` 语句，则原样保留（不重复包装）。
    """
    parts: list[str] = []
    last_view: str = ""
    for step in spec.pipeline:
        if not (step.sql and step.sql.strip()):
            continue
        sql = step.sql.strip().rstrip(";").strip()
        view = step.output.strip() or f"step_{step.step_id}_out"
        upper_head = sql.lstrip().upper()
        if upper_head.startswith("CREATE"):
            # 节点已自己写 CREATE VIEW，直接采用
            parts.append(sql + ";")
        else:
            parts.append(f'CREATE OR REPLACE TEMP VIEW "{view}" AS\n{sql};')
        last_view = view
    if not parts:
        return ""
    if last_view:
        parts.append(f'SELECT * FROM "{last_view}"')
    return "\n\n".join(parts)


def produce(
    scenario: Scenario,
    spec: OutputSpec,
    data_sources: dict[str, str] | None = None,
    out_dir: str | Path | None = None,
    align: bool = True,
    write_file: bool = True,
) -> tuple[pd.DataFrame | None, str, str, str]:
    """执行某产出规格的 SQL/pipeline，返回 (结果DataFrame|None, 实际SQL, 错误, 产出文件路径)。

    - 若 spec 有 pipeline（FlowStep 列表），优先用拼接出的多语句脚本执行。
    - 否则回退到 spec.sql 单步执行。
    - blocked / 无 SQL：返回错误，绝不静默 0。
    """
    # 1) 决定本次执行用的实际 SQL：pipeline 优先，单步 sql 兜底
    actual_sql = ""
    if spec.pipeline:
        actual_sql = _compose_pipeline_sql(spec)
    if not actual_sql:
        actual_sql = (spec.sql or "").strip()

    if spec.status == "blocked" or not actual_sql:
        need = spec.external_data_needed or ["该产出的转换口径或外部标准数据"]
        return None, actual_sql or spec.sql or "", (
            f"BLOCKED: 产出「{spec.name}」缺少必要数据/口径：{need}。已拒绝执行（而非返回 0 行）。"
        ), ""

    files = _table_files(scenario, data_sources, only=spec.required_tables or None)
    try:
        df, sql = sql_engine.execute_template_sql(actual_sql, spec.required_tables, files)
    except Exception as exc:  # noqa: BLE001  连同 SQL 一并上报
        return None, actual_sql, f"{type(exc).__name__}: {exc}", ""

    if align:
        df = _align_columns(df, spec.columns)

    artifact = ""
    if write_file and out_dir is not None:
        path = output_render.render(df, spec.fmt, out_dir, spec.name, columns=spec.columns or None)
        artifact = str(path)
    return df, sql, "", artifact


def execute_and_compare(
    scenario: Scenario,
    spec: OutputSpec,
    data_sources: dict[str, str] | None = None,
    out_dir: str | Path | None = None,
) -> ValidationReport:
    """执行产出 SQL → 复刻落盘。

    历史结果表仅用作列结构模板（通过 spec.columns 对齐），不做行级比对。
    passed = produced_count > 0。
    """
    produced, sql, error, artifact = produce(
        scenario, spec, data_sources, out_dir=out_dir, align=True,
        write_file=out_dir is not None,
    )

    if error:
        return ValidationReport(
            output_id=spec.output_id, output_name=spec.name, executed_sql=sql, error=error,
            passed=False,
            message=(f"执行未成功：{error}" + (f"\n—— 实际执行 SQL ——\n{sql}" if sql else "")),
        )

    rows = len(produced) if produced is not None else 0
    passed = rows > 0
    if passed:
        msg = f"✅ 已产出 {rows} 行。"
    else:
        msg = f"⚠️ 执行完成但结果为 0 行，请检查过滤条件或 SQL 逻辑。\n—— 实际执行 SQL ——\n{sql}"
    return ValidationReport(
        output_id=spec.output_id, output_name=spec.name, executed_sql=sql,
        artifact_path=artifact,
        produced_count=rows,
        passed=passed,
        message=msg,
    )
