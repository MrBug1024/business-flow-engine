"""通用确定性转换策略库（场景无关，无 AI、无字面字段名硬编码）。

每种策略把「**列角色/参数**」编译成可执行的 DuckDB SQL。参数（哪个表、哪些键列、
哪个度量列、阈值、分派列…）作为数据传入，**不把任何业务字段名写死在代码里**。

v1.0.6 起移除了 rule_driven_join / co_occurrence / exclusive_conflict / frequency_overflow /
sequence_detect 这几个"业务判断形状"算子——它们本质上是把"这条规则到底是共存冲突还是
频次超限还是阈值比较"这个业务判断硬套进 5、6 种固定 SQL 形状，真实业务规则的判断逻辑
千变万化，硬套模板要么跑不出真实要的结果、要么语义错了都不知道。v1.0.7 铁律进一步
明确：知识表驱动的每条规则判断逻辑不在蒸馏阶段预先固化为任何 SQL，交给运行时读到
规则原文的 LLM 现场推理、现场查询（见 `skill_builder._knowledge_engine`），不再走这里。

本文件保留的都是**领域无关的关系代数操作**（筛选/去重/聚合/连接/列变换），这些是任何
业务场景都用得到的结构性操作，不涉及"这算不算违规"这类业务判断；任何这里表达不了的
逻辑，直接用 `sql`/`manual` 策略传入手写 SQL 即可。

策略清单：
    passthrough    单表筛选/选列
    dedup          键列重复行
    threshold      数值阈值比较
    keyword        文本列关键词命中
    aggregate      按维度分组聚合
    join           多表按关联键连接
    column_select  列选取/重命名
    lookup         值映射/翻译（LEFT JOIN 知识表取值）
    formula        按公式计算新列
    set_compare    两组数据差集/交集比对
    sql / manual   直接给定 SQL（表达不了的逻辑都走这里）
    blocked        缺数据/口径，运行时拒绝执行
"""

from __future__ import annotations

import re
from typing import Any


# ---------------------------------------------------------------------------
# 标识符 / 字面量
# ---------------------------------------------------------------------------
def q(identifier: str) -> str:
    """DuckDB 标识符引用（双引号，转义内部双引号）。"""
    return '"' + str(identifier).replace('"', '""') + '"'


def lit(value: Any) -> str:
    """SQL 字符串字面量（单引号，转义内部单引号）。"""
    return "'" + str(value).replace("'", "''") + "'"


_NUMERIC_HINTS = ("int", "float", "decimal", "double", "number", "numeric")
_KEY_NAME_HINTS = ("id", "code", "no", "key", "编号", "编码", "序号", "单号", "流水")


def is_numeric_dtype(dtype: str) -> bool:
    d = (dtype or "").lower()
    return any(h in d for h in _NUMERIC_HINTS)


def is_key_like(name: str) -> bool:
    n = re.sub(r"[\s_\-]+", "", str(name).strip().lower())
    return n == "id" or any(n.endswith(h) or h in n for h in _KEY_NAME_HINTS)


# ===========================================================================
# 策略 → SQL 编译
# ===========================================================================
def build_sql(strategy: str, params: dict[str, Any]) -> str:
    """把策略 + 参数编译为 DuckDB SQL。未知策略或缺参数时返回空串。"""
    fn = _BUILDERS.get(strategy)
    if fn is None:
        return ""
    try:
        return fn(params or {})
    except Exception:  # noqa: BLE001
        return ""


def _src(params: dict[str, Any]) -> str:
    t = params.get("source") or params.get("table")
    if not t:
        raise ValueError("missing source")
    return q(t)


def _where_clause(params: dict[str, Any]) -> str:
    where = (params.get("where") or "").strip()
    return f"\nWHERE {where}" if where else ""


def _passthrough(params: dict[str, Any]) -> str:
    return f"SELECT * FROM {_src(params)}{_where_clause(params)}"


def _dedup(params: dict[str, Any]) -> str:
    keys = params.get("key_columns") or params.get("keys") or []
    if not keys:
        raise ValueError("dedup needs key_columns")
    src = _src(params)
    key_q = ", ".join(q(k) for k in keys)
    on = " AND ".join(f"d.{q(k)} = g.{q(k)}" for k in keys)
    notnull = " AND ".join(f"{q(k)} IS NOT NULL" for k in keys)
    return (
        f"WITH _dup AS (\n"
        f"  SELECT {key_q}\n  FROM {src}\n  WHERE {notnull}\n"
        f"  GROUP BY {key_q}\n  HAVING COUNT(*) > 1\n)\n"
        f"SELECT d.*\nFROM {src} d\nJOIN _dup g ON {on}"
    )


_OP_MAP = {">": ">", ">=": ">=", "<": "<", "<=": "<=", "==": "=", "=": "=", "!=": "<>", "<>": "<>"}


def _threshold(params: dict[str, Any]) -> str:
    left = params.get("left") or params.get("column")
    op = _OP_MAP.get((params.get("op") or ">").strip())
    if not left or not op:
        raise ValueError("threshold needs left & valid op")
    right_is_col = bool(params.get("right_is_column"))
    right = params.get("right")
    if right is None:
        raise ValueError("threshold needs right")
    src = _src(params)
    left_q = q(left)
    if right_is_col:
        right_expr = q(right)
        guard = f"{left_q} IS NOT NULL AND {right_expr} IS NOT NULL"
    else:
        right_expr = lit(right) if isinstance(right, str) and not _is_number(right) else str(right)
        guard = f"{left_q} IS NOT NULL"
    return f"SELECT *\nFROM {src}\nWHERE {guard}\n  AND {left_q} {op} {right_expr}"


def _is_number(s: Any) -> bool:
    try:
        float(str(s))
        return True
    except (TypeError, ValueError):
        return False


def _keyword(params: dict[str, Any]) -> str:
    col = params.get("text_column") or params.get("column")
    kws = [k for k in (params.get("keywords") or []) if str(k).strip()]
    if not col or not kws:
        raise ValueError("keyword needs text_column & keywords")
    joiner = " AND " if (params.get("match") == "all") else " OR "
    likes = joiner.join(f"{q(col)} LIKE {lit('%' + str(k) + '%')}" for k in kws)
    return f"SELECT *\nFROM {_src(params)}\nWHERE {likes}"


_AGG_FUNCS = {"sum", "count", "avg", "min", "max", "count_distinct"}


def _aggregate(params: dict[str, Any]) -> str:
    dims = params.get("group_by") or params.get("dims") or []
    metrics = params.get("metrics") or []
    if not metrics:
        raise ValueError("aggregate needs metrics")
    src = _src(params)
    select_parts = [q(d) for d in dims]
    for m in metrics:
        func = (m.get("func") or "sum").lower()
        alias = m.get("alias") or f"{func}_{m.get('column', 'v')}"
        col = m.get("column")
        if func == "count" and not col:
            expr = "COUNT(*)"
        elif func == "count_distinct":
            expr = f"COUNT(DISTINCT {q(col)})"
        elif func in _AGG_FUNCS:
            expr = f"{func.upper()}({q(col)})"
        else:
            raise ValueError(f"bad agg func {func}")
        select_parts.append(f"{expr} AS {q(alias)}")
    select = ",\n       ".join(select_parts)
    group = ("\nGROUP BY " + ", ".join(q(d) for d in dims)) if dims else ""
    having = (f"\nHAVING {params['having']}" if params.get("having") else "")
    return f"SELECT {select}\nFROM {src}{group}{having}"


def _join(params: dict[str, Any]) -> str:
    base = params.get("base") or params.get("source")
    joins = params.get("joins") or []
    if not base or not joins:
        raise ValueError("join needs base & joins")
    parts = [f"SELECT *\nFROM {q(base)} t0"]
    for i, j in enumerate(joins, start=1):
        how = "LEFT JOIN" if (j.get("how") == "left") else "JOIN"
        tbl = j.get("table")
        left, right = j.get("left"), j.get("right")
        if not (tbl and left and right):
            raise ValueError("join entry needs table/left/right")
        parts.append(f"{how} {q(tbl)} t{i} ON t0.{q(left)} = t{i}.{q(right)}")
    sql = "\n".join(parts)
    if params.get("where"):
        sql += f"\nWHERE {params['where']}"
    return sql


def _manual(params: dict[str, Any]) -> str:
    return (params.get("sql") or "").strip()


# ===========================================================================
# v1.0.4 新增模式
# ===========================================================================
def _column_select(params: dict[str, Any]) -> str:
    """列选取：从源表选取指定列（可重命名）。

    params:
        source   源表
        columns  list[str | {source_col, alias}]
                 字符串直接用列名；dict 表示重命名
    """
    src = _src(params)
    cols = params.get("columns") or []
    if not cols:
        return f"SELECT * FROM {src}"
    parts: list[str] = []
    for col in cols:
        if isinstance(col, dict):
            src_col = col.get("source_col") or col.get("col") or ""
            alias = col.get("alias") or src_col
            parts.append(f"{q(src_col)} AS {q(alias)}")
        else:
            parts.append(q(str(col)))
    return f"SELECT {', '.join(parts)}\nFROM {src}"


def _lookup(params: dict[str, Any]) -> str:
    """值映射/翻译：用知识表对源表做值映射（LEFT JOIN + 取映射值）。

    params:
        source       源表
        lookup_table 知识/映射表
        source_key   源表关联键列
        lookup_key   知识表关联键列
        value_cols   需要从知识表取过来的列 list[str | {col, alias}]
        how          连接方式（left/inner，默认 left）
    """
    src = params.get("source") or params.get("table")
    lt = params.get("lookup_table")
    sk = params.get("source_key")
    lk = params.get("lookup_key")
    value_cols = params.get("value_cols") or []
    if not (src and lt and sk and lk):
        raise ValueError("lookup needs source, lookup_table, source_key, lookup_key")
    how = "LEFT JOIN" if (params.get("how") or "left") != "inner" else "JOIN"

    val_parts: list[str] = []
    for vc in value_cols:
        if isinstance(vc, dict):
            col = vc.get("col") or vc.get("source_col") or ""
            alias = vc.get("alias") or col
            val_parts.append(f"lk.{q(col)} AS {q(alias)}")
        else:
            val_parts.append(f"lk.{q(str(vc))}")

    val_select = (", " + ", ".join(val_parts)) if val_parts else ""
    return (
        f"SELECT src.*{val_select}\n"
        f"FROM {q(src)} src\n"
        f"{how} {q(lt)} lk ON src.{q(sk)} = lk.{q(lk)}"
    )


def _formula(params: dict[str, Any]) -> str:
    """公式计算：在源表基础上按公式计算新列（行数不变）。

    params:
        source    源表
        formulas  list[{alias, expr}]
                  expr 是 DuckDB 表达式，可引用源表列名（无需别名前缀）
    """
    src = _src(params)
    formulas = params.get("formulas") or []
    if not formulas:
        return f"SELECT * FROM {src}"
    extra = ", ".join(
        f"{f.get('expr', '')} AS {q(f.get('alias', f'col_{i}'))}"
        for i, f in enumerate(formulas)
    )
    return f"SELECT *, {extra}\nFROM {src}"


def _set_compare(params: dict[str, Any]) -> str:
    """集合比对：两组数据做差集/交集（"该有的没有" / "不该有的有"）。

    params:
        left_source  左侧数据集
        right_source 右侧数据集
        key_columns  list[str] 用于比对的列
        mode         "left_only"（左有右无）/ "right_only"（右有左无）/ "both"（交集）
    """
    left = params.get("left_source") or params.get("source")
    right = params.get("right_source")
    keys = params.get("key_columns") or params.get("keys") or []
    mode = (params.get("mode") or "left_only").lower()
    if not (left and right and keys):
        raise ValueError("set_compare needs left_source, right_source, key_columns")
    key_q = ", ".join(q(k) for k in keys)
    on_clause = " AND ".join(f"l.{q(k)} = r.{q(k)}" for k in keys)
    r_null_check = " AND ".join(f"r.{q(k)} IS NULL" for k in keys)
    l_null_check = " AND ".join(f"l.{q(k)} IS NULL" for k in keys)

    if mode == "left_only":
        # 左有右无（"该有的没有"）
        return (
            f"SELECT l.*\n"
            f"FROM {q(left)} l\n"
            f"LEFT JOIN {q(right)} r ON {on_clause}\n"
            f"WHERE {r_null_check}"
        )
    elif mode == "right_only":
        # 右有左无（"不该有的有"）
        return (
            f"SELECT r.*\n"
            f"FROM {q(right)} r\n"
            f"LEFT JOIN {q(left)} l ON {on_clause}\n"
            f"WHERE {l_null_check}"
        )
    else:
        # 交集（both）
        return (
            f"SELECT l.*\n"
            f"FROM {q(left)} l\n"
            f"JOIN {q(right)} r ON {on_clause}"
        )


_BUILDERS = {
    "passthrough": _passthrough,
    "dedup": _dedup,
    "threshold": _threshold,
    "keyword": _keyword,
    "aggregate": _aggregate,
    "join": _join,
    "sql": _manual,
    "manual": _manual,
    "column_select": _column_select,
    "lookup": _lookup,
    "formula": _formula,
    "set_compare": _set_compare,
}
