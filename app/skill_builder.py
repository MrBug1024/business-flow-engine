"""技能落盘（v1.0.2：Engineer's Toolbox —— 单一、参数化、SQL/DuckDB 执行器）。

每个业务场景固化为**一个**通用审核技能：

    skills/business_audit_executor/
        SKILL.md                  技能说明 + 接口规范
        domain_knowledge.json     数据字典 + ER 关系 + 结果表结构契约（Phase 1）
        rule_templates.json       每个违规类型一个 SQL 模板（Phase 2/4）
        scripts/skill_executor.py 自包含执行器（DuckDB），暴露 list_audit_types / execute_audit
        scripts/run.py            CLI 包装

运行时纪律（规范 Phase 5）：
* `execute_audit(violation_type, data_path)`：查 rule_templates → 取该类型 SQL → DuckDB 在新数据上执行
  → 结果列对齐 domain_knowledge 的历史结果结构 → 返回 DataFrame。
* **不调用任何 AI**、**不重新分析 schema**、**不使用写死的假查询**。
* 缺外部数据的类型（status=blocked）直接报错并说明缺什么，绝不静默返回 0。
"""

from __future__ import annotations

import shutil
from pathlib import Path

from . import sql_builder
from .models import DomainKnowledge, Scenario, Skill
from .storage import store

_AUDIT_SKILL_ID = "business_audit_executor"


# ===========================================================================
# 自包含执行器模板（不依赖本工程，可独立分发）
# ===========================================================================
_EXECUTOR_TEMPLATE = '''"""业务审核通用执行器（自动生成 · 自包含 · DuckDB）。

Engineer's Toolbox：掌握本业务域的数据字典与规则库（SQL 模板），可对「与历史数据
同结构」的新数据，执行**任意**违规类型的审核——审核类型作为参数传入。

接口：
    list_audit_types() -> list[str]
    execute_audit(violation_type, data_path) -> pandas.DataFrame
        data_path: 数据文件夹（按表名匹配同名文件），或 {表名: 文件路径} 字典。

铁律：运行时不调用 AI、不重新分析 schema、不使用写死假查询；
缺数据的规则（blocked）直接报错说明缺什么；命中 0 行也会打印实际执行的 SQL。
"""

import json
import logging
from pathlib import Path

import duckdb
import pandas as pd

_ROOT = Path(__file__).resolve().parent.parent
_DOMAIN_PATH = _ROOT / "domain_knowledge.json"
_RULES_PATH = _ROOT / "rule_templates.json"

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger("skill_executor")


def _load_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _domain():
    return _load_json(_DOMAIN_PATH)


def _templates():
    return _load_json(_RULES_PATH).get("templates", [])


def list_audit_types():
    """返回规则库中全部违规类型（无论是否已校验）。"""
    seen = []
    for t in _templates():
        vt = t.get("violation_type")
        if vt and vt not in seen:
            seen.append(vt)
    return seen


def _read_raw(path, header_row=0):
    p = str(path).lower()
    skip = header_row or None
    if p.endswith((".xlsx", ".xls")):
        return pd.read_excel(path, skiprows=skip)
    sep = "\\t" if p.endswith(".tsv") else ","
    for enc in ("utf-8-sig", "utf-8", "gbk", "gb18030"):
        try:
            return pd.read_csv(path, skiprows=skip, sep=sep, encoding=enc)
        except UnicodeDecodeError:
            continue
    return pd.read_csv(path, skiprows=skip, sep=sep)


def _read_table(path, header_row=0):
    """读表并缓存为 parquet（Excel 读取慢；命中缓存可达成「百万行 10 秒内」）。"""
    cache = Path(str(path) + ".cache.parquet")
    try:
        if cache.exists() and cache.stat().st_mtime >= Path(path).stat().st_mtime:
            return pd.read_parquet(cache)
    except Exception:
        pass
    df = _read_raw(path, header_row)
    try:
        df.to_parquet(cache, index=False)
    except Exception:
        pass
    return df


def _resolve_files(data_path, required_tables):
    """把 data_path 解析为 {表名: 文件路径}。支持文件夹（按表名匹配同名文件）或字典。"""
    if isinstance(data_path, dict):
        return dict(data_path)
    folder = Path(data_path)
    if not folder.is_dir():
        raise FileNotFoundError(f"数据路径不是文件夹也不是映射：{data_path}")
    by_stem = {f.stem: str(f) for f in folder.iterdir()
               if f.suffix.lower() in (".csv", ".tsv", ".xlsx", ".xls")}
    return {name: by_stem[name] for name in required_tables if name in by_stem}


def _header_rows():
    return {t["table_name"]: t.get("header_row", 0) for t in _domain().get("tables", [])}


def execute_audit(violation_type, data_path):
    """对新数据执行指定违规类型的审核，返回违规明细 DataFrame。"""
    tmpls = [t for t in _templates() if t.get("violation_type") == violation_type]
    if not tmpls:
        raise ValueError(f"未知违规类型：{violation_type}。可用类型见 list_audit_types()。")
    tmpl = next((t for t in tmpls if t.get("sql")), tmpls[0])

    if tmpl.get("status") == "blocked" or not tmpl.get("sql"):
        need = tmpl.get("external_data_needed") or ["该违规类型的判定口径或外部标准数据"]
        raise RuntimeError(
            f"违规类型「{violation_type}」缺少必要数据/口径：{need}。已拒绝执行（非 0 结果）。"
        )

    required = tmpl.get("required_tables") or []
    files = _resolve_files(data_path, required or None)
    missing = [t for t in required if t not in files]
    if missing:
        raise FileNotFoundError(f"缺少所需数据表文件：{missing}；已提供：{list(files.keys())}")

    header_rows = _header_rows()
    load = required or list(files.keys())
    frames = {name: _read_table(files[name], header_rows.get(name, 0)) for name in load}

    sql = tmpl["sql"]
    con = duckdb.connect()
    try:
        for name, df in frames.items():
            con.register(name, df)
        result = con.execute(sql).fetchdf()
    except Exception as exc:
        log.error("SQL 执行失败：%s", exc)
        log.error("实际执行 SQL：\\n%s", sql)
        raise
    finally:
        con.close()

    # 结果列对齐历史结果结构契约（缺列补空、定序）
    out_cols = tmpl.get("output_columns") or []
    if out_cols:
        result = result.reindex(columns=out_cols)

    if len(result) == 0:
        log.warning("违规类型「%s」命中 0 行。实际执行 SQL：\\n%s", violation_type, sql)
    else:
        log.info("违规类型「%s」命中 %d 行。", violation_type, len(result))
    return result


if __name__ == "__main__":
    import sys

    if len(sys.argv) == 1:
        print("可用违规类型：")
        for t in list_audit_types():
            print("  -", t)
    elif len(sys.argv) >= 3:
        vt, path = sys.argv[1], sys.argv[2]
        df = execute_audit(vt, path)
        print(f"命中 {len(df)} 行，列：{list(df.columns)[:8]}...")
        print(df.head())
    else:
        print("用法：python skill_executor.py [violation_type data_path]")
'''

_RUN_TEMPLATE = '''"""CLI 包装：转调 skill_executor。"""

from skill_executor import execute_audit, list_audit_types

if __name__ == "__main__":
    import sys

    if len(sys.argv) >= 3:
        print(execute_audit(sys.argv[1], sys.argv[2]).head())
    else:
        print("可用违规类型：", list_audit_types())
'''


# ===========================================================================
# SKILL.md
# ===========================================================================
def _write_skill_md(skill_dir: Path, scenario: Scenario, domain: DomainKnowledge) -> None:
    lib = scenario.rule_library
    templates = lib.templates if lib else []
    vtypes = lib.violation_types if lib else []
    n_exec = sum(1 for t in templates if t.sql)
    n_blocked = sum(1 for t in templates if t.status == "blocked")
    n_verified = sum(1 for t in templates if t.status == "verified")

    tables_hint = "\n".join(
        f"- `{t.table_name}`（{t.role}）：{t.row_count} 行；字段 {', '.join(c.name for c in t.columns[:12])}"
        + ("…" if len(t.columns) > 12 else "")
        for t in domain.tables
    ) or "（无）"
    types_hint = "\n".join(
        f"- {t.violation_type}（{t.status}{'，策略:' + t.strategy if t.strategy else ''}）"
        for t in templates[:80]
    ) or "（规则库为空）"
    result_cols = domain.result_schema.get("__default__", [])

    md = f"""---
name: {_AUDIT_SKILL_ID}
operation: EXECUTE_AUDIT
scenario: {scenario.name}
is_main: true
engine: duckdb
audit_types: {len(vtypes)}
executable_templates: {n_exec}
verified_templates: {n_verified}
blocked_templates: {n_blocked}
---

# 业务审核通用技能（Engineer's Toolbox）

掌握本业务域的**数据字典、表关联与规则库（SQL 模板）**，对「与历史数据同结构」的新数据，
通过 DuckDB 执行**任意违规类型**的审核——审核类型作为参数传入，而非写死单一规则。

## 适用业务场景
{scenario.name} —— {scenario.description or "（无描述）"}

## 掌握的数据结构（domain_knowledge.json）
{tables_hint}

结果列契约（输出对齐历史结果结构，共 {len(result_cols)} 列）：
{', '.join(result_cols[:20])}{'…' if len(result_cols) > 20 else ''}

## 可执行的审核类型（共 {len(vtypes)} 种；可执行 SQL {n_exec}，已校验 {n_verified}，缺数据 {n_blocked}）
{types_hint}

## 接口规范
- 执行器：`scripts/skill_executor.py`（自包含，仅依赖 duckdb + pandas）。
- `list_audit_types() -> list[str]`：返回全部违规类型。
- `execute_audit(violation_type, data_path) -> DataFrame`：
  - `data_path`：新数据文件夹（按表名匹配同名文件）或 `{{表名: 文件路径}}`。
  - 查 `rule_templates.json` 取该类型 SQL → DuckDB 执行 → 结果列对齐 `domain_knowledge.json` 的结果契约。
  - **不调用 AI、不重新分析 schema**；命中 0 行会打印实际 SQL；`blocked` 类型直接报错说明缺什么。

## 命令行
```
python scripts/skill_executor.py                       # 列出全部审核类型
python scripts/skill_executor.py 超标准收费 /path/to/data  # 执行某类型审核
```
"""
    (skill_dir / "SKILL.md").write_text(md, encoding="utf-8")


# ===========================================================================
# 落盘
# ===========================================================================
def materialize_skills(scenario: Scenario) -> list[Skill]:
    """生成并落盘**单一**参数化审核技能（Engineer's Toolbox）；保留进化技能。"""
    base = store.skills_dir(scenario.id)
    evolved = [s for s in scenario.skills if s.is_evolved]
    evolved_ids = {s.skill_id for s in evolved}
    # 清理旧的自动产物（目录与历史 JSON）
    for child in base.iterdir():
        if child.is_dir() and child.name not in evolved_ids:
            shutil.rmtree(child, ignore_errors=True)
        elif child.is_file() and child.name in ("rule_library.json", "er_model.json"):
            child.unlink(missing_ok=True)

    # Phase 1 + Phase 2/4：构建/刷新 domain_knowledge 与 SQL 模板库（确定性，幂等）
    domain = sql_builder.build_domain_knowledge(scenario)
    scenario.domain_knowledge = domain
    if scenario.rule_library is not None:
        sql_builder.build_rule_sql_library(scenario, domain)

    skill_dir = base / _AUDIT_SKILL_ID
    scripts_dir = skill_dir / "scripts"
    scripts_dir.mkdir(parents=True, exist_ok=True)

    # 落盘契约文件
    (skill_dir / "domain_knowledge.json").write_text(
        domain.model_dump_json(indent=2), encoding="utf-8"
    )
    rule_payload = (scenario.rule_library.model_dump_json(indent=2)
                    if scenario.rule_library is not None else '{"templates": []}')
    (skill_dir / "rule_templates.json").write_text(rule_payload, encoding="utf-8")

    # 落盘执行器
    (scripts_dir / "skill_executor.py").write_text(_EXECUTOR_TEMPLATE, encoding="utf-8")
    (scripts_dir / "run.py").write_text(_RUN_TEMPLATE, encoding="utf-8")
    _write_skill_md(skill_dir, scenario, domain)

    vtypes = scenario.rule_library.violation_types if scenario.rule_library else []
    n_exec = sum(1 for t in (scenario.rule_library.templates if scenario.rule_library else []) if t.sql)
    main_skill = Skill(
        skill_id=_AUDIT_SKILL_ID,
        name="业务审核通用技能",
        operation="EXECUTE_AUDIT",
        description=(f"参数化 SQL/DuckDB 审核执行器：掌握 {len(scenario.tables_meta)} 张表与 "
                     f"{len(vtypes)} 种违规类型（{n_exec} 条可执行 SQL），"
                     "可对新数据执行任意类型审核（list_audit_types / execute_audit）。"),
        is_main=True,
        path=str(skill_dir),
    )
    return [main_skill, *evolved]


def materialize_evolved_skill(scenario: Scenario, skill: Skill) -> Skill:
    """落盘一个「进化技能」（用户手动为业务场景扩展的能力）。"""
    skill_dir = store.skills_dir(scenario.id) / skill.skill_id
    scripts_dir = skill_dir / "scripts"
    scripts_dir.mkdir(parents=True, exist_ok=True)
    runner = f'''"""{skill.name}（进化技能，自动生成骨架）。

{skill.description}
"""

import pandas as pd


def run(tables: dict, **params) -> pd.DataFrame:
    """对输入的 {{表名: DataFrame}} 执行「{skill.name}」。"""
    # TODO: 依据业务口径实现
    return next(iter(tables.values())) if tables else pd.DataFrame()
'''
    (scripts_dir / "run.py").write_text(runner, encoding="utf-8")
    md = f"""---
name: {skill.skill_id}
operation: {skill.operation or "EVOLVED"}
scenario: {scenario.name}
is_evolved: true
---

# {skill.name}

{skill.description}

## 接口规范
- 入口脚本：`scripts/run.py`，`run(tables, **params)`。
- 输入：与历史数据同结构的业务表（`{{表名: DataFrame}}`）。
"""
    (skill_dir / "SKILL.md").write_text(md, encoding="utf-8")
    skill.path = str(skill_dir)
    return skill
