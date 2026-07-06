"""SQL/DuckDB 执行核心（Layer 3）。

规范 Section 7 指定用 DuckDB 在数据上跑 SQL。本模块是平台侧的执行核心：
把若干表（DataFrame）注册为 DuckDB 视图，按 **SQL 模板** 执行，返回结果与「实际执行的 SQL」。

设计要点：
* 表名 = 注册视图名（保留中文原名），SQL 中用双引号引用标识符；
* 运行时**绝不**调用 AI、**绝不**重新分析 schema——只执行既定 SQL；
* 无论命中与否都回传 executed_sql；出错回传 error，便于排查「永远 0 结果」。

注意：打包后的技能（skills/.../scripts/run.py）是**自包含**的，复刻了这里的执行逻辑，
不依赖本包。两处逻辑应保持一致（同为 DuckDB + 同样的读表/注册/执行约定）。
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from . import table_io


def read_table(path: str) -> pd.DataFrame:
    """读取一张表为 DataFrame（自动识别表头，兼容 CSV/TSV/Excel）。

    首次读取后落一份缓存（pickle 兼容混合 dtype），后续命中缓存秒级返回。
    """
    p = Path(path)
    cache_pkl = p.with_suffix(p.suffix + ".cache.pkl")
    cache_pq = p.with_suffix(p.suffix + ".cache.parquet")
    try:
        if cache_pkl.exists() and cache_pkl.stat().st_mtime >= p.stat().st_mtime:
            return pd.read_pickle(cache_pkl)
    except Exception:  # noqa: BLE001
        pass
    try:
        if cache_pq.exists() and cache_pq.stat().st_mtime >= p.stat().st_mtime:
            return pd.read_parquet(cache_pq)
    except Exception:  # noqa: BLE001
        pass
    df = table_io.load_full_frame(path)
    try:
        df.to_pickle(cache_pkl)
    except Exception:  # noqa: BLE001
        pass
    return df


def _split_sql_statements(sql: str) -> list[str]:
    """把多语句 SQL 按分号切分，**尊重单/双引号内的分号**。

    这是为了让 pipeline 的多个 `CREATE OR REPLACE VIEW ...; ... ; SELECT ...` 能在
    同一 DuckDB 连接里依次执行 —— 上游节点的 VIEW 对下游节点可见。
    """
    out: list[str] = []
    buf: list[str] = []
    in_s = False    # 单引号内
    in_d = False    # 双引号内
    in_line_comment = False
    for ch in sql:
        if in_line_comment:
            buf.append(ch)
            if ch == "\n":
                in_line_comment = False
            continue
        if ch == "'" and not in_d:
            in_s = not in_s
        elif ch == '"' and not in_s:
            in_d = not in_d
        elif ch == "-" and buf and buf[-1] == "-" and not in_s and not in_d:
            in_line_comment = True
        elif ch == ";" and not in_s and not in_d:
            stmt = "".join(buf).strip()
            if stmt:
                out.append(stmt)
            buf = []
            continue
        buf.append(ch)
    tail = "".join(buf).strip()
    if tail:
        out.append(tail)
    return out


def run_sql(sql: str, tables: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """把 tables 注册为 DuckDB 视图并执行 SQL，返回**最后一个查询语句**的结果 DataFrame。

    支持多语句 SQL（用 `;` 分隔）：例如 pipeline 模式下，前 N 个语句创建视图、最后一个
    SELECT 返回结果。如果脚本中没有任何 SELECT 语句，则返回空 DataFrame。

    任何执行错误都向上抛出（调用方负责连同实际执行的 SQL 一起上报）。
    """
    import duckdb

    statements = _split_sql_statements(sql)
    if not statements:
        return pd.DataFrame()

    con = duckdb.connect()
    try:
        for name, df in tables.items():
            con.register(name, df)
        last_result: pd.DataFrame = pd.DataFrame()
        for stmt in statements:
            cur = con.execute(stmt)
            try:
                # 只有 SELECT/RETURNING 等才有 fetchdf 结果；CREATE VIEW 等忽略
                last_result = cur.fetchdf()
            except Exception:  # noqa: BLE001
                continue
        return last_result
    finally:
        con.close()


def execute_template_sql(
    sql: str,
    required_tables: list[str],
    table_files: dict[str, str],
) -> tuple[pd.DataFrame, str]:
    """加载所需表 → 执行 SQL，返回 (结果DataFrame, 实际执行的SQL)。

    - required_tables 为空时加载 table_files 中的全部表；
    - 缺表会以清晰错误抛出（而非静默 0 结果）。
    """
    only = required_tables or list(table_files.keys())
    frames: dict[str, pd.DataFrame] = {}
    for name in only:
        path = table_files.get(name)
        if not path:
            raise FileNotFoundError(
                f"SQL 需要表「{name}」，但未在数据源中找到对应文件。已提供：{list(table_files.keys())}"
            )
        frames[name] = read_table(path)
    result = run_sql(sql, frames)
    return result, sql
