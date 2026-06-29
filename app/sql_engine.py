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
from typing import Any

import pandas as pd

from . import table_io


def read_table(path: str) -> pd.DataFrame:
    """读取一张表为 DataFrame（自动识别表头，兼容 CSV/TSV/Excel）。

    Excel 读取慢（openpyxl），首次读取后落一份 parquet 缓存；后续命中缓存秒级返回，
    以满足「百万行级、10 秒内」的运行时目标（规范 Section 5）。
    """
    p = Path(path)
    cache = p.with_suffix(p.suffix + ".cache.parquet")
    try:
        if cache.exists() and cache.stat().st_mtime >= p.stat().st_mtime:
            return pd.read_parquet(cache)
    except Exception:  # noqa: BLE001  缓存不可用则照常读源文件
        pass
    df = table_io.load_full_frame(path)
    try:
        df.to_parquet(cache, index=False)
    except Exception:  # noqa: BLE001  无 parquet 引擎时跳过缓存，不影响正确性
        pass
    return df


def run_sql(sql: str, tables: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """把 tables 注册为 DuckDB 视图并执行 SQL，返回结果 DataFrame。

    任何执行错误都向上抛出（调用方负责连同 SQL 一起记录/上报）。
    """
    import duckdb

    con = duckdb.connect()
    try:
        for name, df in tables.items():
            con.register(name, df)
        return con.execute(sql).fetchdf()
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


def map_folder_to_tables(folder: str, table_names: list[str]) -> dict[str, str]:
    """把一个数据文件夹映射为 {表名: 文件路径}：按文件名（去后缀）与表名匹配。"""
    p = Path(folder)
    files = {f.stem: str(f) for f in p.iterdir()
             if f.suffix.lower() in (".csv", ".tsv", ".xlsx", ".xls")} if p.is_dir() else {}
    mapping: dict[str, str] = {}
    for name in table_names:
        if name in files:
            mapping[name] = files[name]
    return mapping


def jsonable_records(df: pd.DataFrame, limit: int | None = None) -> list[dict[str, Any]]:
    """把 DataFrame 前若干行转为可 JSON 序列化的 records。"""
    sub = df.head(limit) if limit else df
    out: list[dict[str, Any]] = []
    for _, row in sub.iterrows():
        out.append({str(k): table_io._jsonable(v) for k, v in row.to_dict().items()})
    return out
