"""追踪驱动采样（Trace-Driven Sampling）— v1.0.6。

核心思想：不独立采样，而是以结果表 R 的样本为"入口"，
沿列值交叉追踪到各业务表取"关联行"，保证发给 AI 的样本具有因果关系。

问题背景：
  旧方式：各表独立取前 N 行 → AI 看到的行互不相干 → 无法推导逻辑关系
  新方式：从结果表取样本行 → 提取追踪键值 → 在各表中找"关联行"→ 因果完整的样本包

v1.0.6 修复的一个真实 bug：Excel 业务表曾经也只搜前 2000 行（`_load_scan_frame`），
真实业务表几十万行很常见（如 74 万行的项目明细表），命中的具体行几乎不可能落在
文件最前面——实测某案例里 3 个待追踪的就诊 ID 分别落在第 10.9 万/43.3 万/69 万行，
全部在 2000 行之外，导致"明明有关联却报告 0% 重叠/追不到"。现在 Excel/JSON/MD
一律走 `table_io.load_full_frame_cached` 整表搜索（CSV/TSV 本就用 DuckDB 全文件流式
查询，不受影响）；为了不让每次调用都重新读一遍大文件，整表加载结果按
(路径, mtime) 在进程内缓存。

铁律合规：
  - 发给 AI 的总行数 ≤ max_trace_rows × 表数（通常 < 200）——**搜索**范围是全量，
    但最终塞进 AI 上下文的**命中样本**仍然限量，两者不是一回事。
  - CSV/TSV 用 DuckDB read_csv 对完整文件做 WHERE 过滤（流式读取，不全量加载到内存）；
    Excel/JSON/MD 用 pandas 整表加载（进程内缓存，避免重复读盘）。
  - 保证样本具有跨表的键值关联性
"""

from __future__ import annotations

import re
from typing import Any

from app.domain.models import Scenario, TableMeta, TableRole
from app.data.table_io import _sanitize_rows

_MAX_TRACE_ROWS = 50
_SAMPLE_SIZE = 1  # 默认严格以结果表第一条非空记录为锚点；可由调用方显式放宽

# 列名语义判断
_ID_HINTS = ("id", "编号", "编码", "序号", "流水", "单号", "no", "key", "code", "num", "number")
_CATEGORY_HINTS = ("类型", "类别", "分类", "名称", "category", "kind", "type", "name", "class")
_TIME_HINTS = ("日期", "时间", "年月", "date", "time", "month", "year", "dt", "created", "updated")


def _is_id_col(name: str) -> bool:
    n = name.lower().replace("_", "").replace(" ", "")
    return any(h in n for h in _ID_HINTS)


def _is_category_col(name: str) -> bool:
    n = name.lower().replace("_", "").replace(" ", "")
    return any(h in n for h in _CATEGORY_HINTS)


def _is_time_col(name: str) -> bool:
    n = name.lower().replace("_", "").replace(" ", "")
    return any(h in n for h in _TIME_HINTS)


def _no_trace_result(warning: str) -> dict[str, Any]:
    """未追踪到因果行时的标准返回；不再用随机样本污染后续推导。"""
    return {
        "matched_rows": [],
        "matched_by": "",
        "trace_confidence": "low",
        "warning": warning,
    }


def _col_name_overlap(col_a: str, col_b: str) -> bool:
    """两列名是否存在包含关系（忽略大小写与常见分隔符）。"""
    a = col_a.lower().replace("_", "").replace(" ", "").replace("-", "")
    b = col_b.lower().replace("_", "").replace(" ", "").replace("-", "")
    if a == b:
        return True
    if len(a) >= 2 and a in b:
        return True
    if len(b) >= 2 and b in a:
        return True
    return False


# ===========================================================================
# 唯一性交叉校验（问题反馈：单字段匹配可能不足以唯一确定对应关系）
# ===========================================================================
def _cross_validate_and_narrow(
    matched_rows: list[dict[str, Any]],
    trace_keys: dict[str, Any],
    matched_cols: list[str],
    anchor_row: dict[str, Any] | None = None,
    col_mappings: dict[str, list[str]] | None = None,
) -> tuple[list[dict[str, Any]], str | None, list[str]]:
    """用锚点行其它已知的值，交叉校验一次匹配是否真的唯一确定。

    背景：`matched_cols`（如["就诊ID"]）匹配到的这批行，理论上应该都属于
    同一次业务事件；但如果锚点行还有其它已知的单值字段，且目标表里也有对应列，
    就该拿这些字段再核实一遍——如果匹配到的行里有一部分在这些字段上跟锚点对不上，
    说明现有匹配列不足以唯一确定对应关系（真实业务里常见：一次就诊可能对应多次
    结算，"就诊ID"命中的结算记录里只有一条的"结算ID"跟锚点一致）。

    "对应列"的两个来源（按可信度排列）：
    ① `col_mappings`：关联关系（人工确认的或值证据推导的）给出的
       锚点列 → 目标表列 的明确对应——**不依赖字段名相似**。这是为了覆盖
       "结果表.违规说明 ↔ 知识表.国家问题清单"这类字段名完全不像、只有值相等的
       真实复合关联：靠字段名永远发现不了，必须由关联关系本身把列对应带进来。
    ② 字段名相近（`_col_name_overlap`）：没有关联证据时的弱先验兜底。

    与"一对多"的正常业务形态（如一次结算对应多条费用明细，所有明细的结算ID
    都跟锚点一致）区分开：只有当额外字段能**真正收窄**结果时才报告，收窄不动
    （所有行本来就都一致）不算问题。

    Returns:
        (收窄后的 matched_rows，若发生收窄则给出的警告文案，用到的复合键列名列表)
        未发生收窄时：(原样返回, None, [])
    """
    if not matched_rows:
        return matched_rows, None, []

    row_cols = set(matched_rows[0].keys())
    matched_set = set(matched_cols)
    extra_checks: list[tuple[str, str]] = []

    # 来源①：关联关系给出的列对应（值证据优先，不看字段名像不像）
    if anchor_row and col_mappings:
        for result_col, target_cols in col_mappings.items():
            v = anchor_row.get(result_col)
            s = "" if v is None else str(v).strip()
            if not s or s.lower() in ("nan", "none", "null"):
                continue
            for tc in target_cols:
                if tc in row_cols and tc not in matched_set:
                    extra_checks.append((tc, s))

    # 来源②：锚点行里"值唯一确定"的其它 ID/分类键，按字段名相近匹配（弱先验兜底）
    for key_type in ("id_values", "category_values"):
        for trace_col, values in trace_keys.get(key_type, {}).items():
            if len(values) != 1:
                continue
            anchor_val = next(iter(values))
            for col in row_cols:
                if col in matched_set:
                    continue
                if _col_name_overlap(trace_col, col):
                    extra_checks.append((col, anchor_val))

    # 去重（保持来源①在前的优先顺序）
    seen: set[tuple[str, str]] = set()
    deduped: list[tuple[str, str]] = []
    for chk in extra_checks:
        if chk not in seen:
            seen.add(chk)
            deduped.append(chk)

    if not deduped:
        return matched_rows, None, []

    narrowed = matched_rows
    used_cols: list[str] = []
    for col, anchor_val in deduped:
        sub = [r for r in narrowed if str(r.get(col, "")).strip() == str(anchor_val).strip()]
        # 只有"确实收窄了、但没收窄到 0"才采纳这个额外字段；筛没了大概率是类型/格式
        # 不严格匹配导致的假阴性，不能因此把真实命中的行也扔掉。
        if sub and len(sub) < len(narrowed):
            narrowed = sub
            used_cols.append(col)

    if not used_cols or len(narrowed) == len(matched_rows):
        return matched_rows, None, []

    by_label = "+".join(matched_cols)
    warning = (
        f"⚠️ 仅凭「{by_label}」匹配到 {len(matched_rows)} 行，其中部分行的"
        f"「{'、'.join(used_cols)}」与锚点已知值不一致；已用"
        f"「{by_label}+{'+'.join(used_cols)}」复合键收窄到 {len(narrowed)} 行。"
        f"建议这条关联关系改用复合键，而不是单独的「{by_label}」——单字段在这个"
        f"场景下不足以唯一确定对应关系。"
    )
    return narrowed, warning, [*matched_cols, *used_cols]


# ===========================================================================
# DuckDB 辅助函数（全文件流式查询，不受 _SCAN_CAP 限制）
# ===========================================================================
def _quote_col(col: str) -> str:
    """安全引用列名（双引号转义）。"""
    return '"' + col.replace('"', '""') + '"'


def _build_in_clause(col: str, values: set[str]) -> str:
    """构造 SQL IN 子句：CAST("col" AS VARCHAR) IN ('v1', 'v2')。"""
    if not values:
        return "FALSE"
    q = _quote_col(col)
    vals = ", ".join("'" + v.replace("'", "''") + "'" for v in values)
    return f"CAST({q} AS VARCHAR) IN ({vals})"


def _duckdb_full_query(
    file_path: str,
    header_row: int,
    sep: str,
    trace_keys: dict[str, Any],
    max_rows: int,
    knowledge_mode: bool = False,
    allow_weak_fallbacks: bool = False,
) -> dict[str, Any] | None:
    """用 DuckDB 对完整 CSV/TSV 文件做 WHERE 过滤采样。

    DuckDB read_csv 以流式方式扫描文件并推下 WHERE 谓词，不把整张表加载到内存，
    满足铁律「文件不完整加载到内存」。

    Returns:
        匹配到的追踪结果 dict，或 None（DuckDB 无法处理时调用方回退到 scan_frame）。
    """
    try:
        import duckdb  # noqa: PLC0415
    except ImportError:
        return None

    try:
        # Windows 路径：反斜杠 → 正斜杠（DuckDB 在 Windows 上接受正斜杠）
        path_fwd = str(file_path).replace("\\", "/")
        path_sql = path_fwd.replace("'", "''")

        skip_val = max(header_row, 0)
        delim_param = "delim='\\t'" if sep == "\t" else "delim=','"

        read_expr = (
            f"read_csv('{path_sql}', "
            f"header=true, skip={skip_val}, "
            f"{delim_param}, "
            f"ignore_errors=true)"
        )

        con = duckdb.connect()

        # 获取实际列名（DESCRIBE 只读元数据，不加载数据）
        try:
            info = con.execute(f"DESCRIBE SELECT * FROM {read_expr} LIMIT 0").fetchdf()
            available_cols: list[str] = list(info["column_name"]) if "column_name" in info.columns else []
        except Exception:  # noqa: BLE001
            return None

        if not available_cols:
            return None

        def _try_query(where_clause: str) -> "Any | None":
            """执行带 WHERE 的 SELECT，失败返回 None。"""
            try:
                df = con.execute(
                    f"SELECT * FROM {read_expr} WHERE {where_clause} LIMIT {max_rows}"
                ).fetchdf()
                return df if not df.empty else None
            except Exception:  # noqa: BLE001
                return None

        # 优先级 1：ID 精确匹配
        for trace_col, values in trace_keys.get("id_values", {}).items():
            for mc in available_cols:
                if _col_name_overlap(trace_col, mc):
                    df = _try_query(_build_in_clause(mc, values))
                    if df is not None:
                        return {
                            "matched_rows": _sanitize_rows(df.to_dict("records")),
                            "matched_by": mc,
                            "matched_values": sorted(values),
                            "trace_confidence": "high",
                        }

        if not allow_weak_fallbacks:
            return None

        # 优先级 2：分类值匹配
        for trace_col, values in trace_keys.get("category_values", {}).items():
            for mc in available_cols:
                match_ok = _col_name_overlap(trace_col, mc) or (
                    knowledge_mode and _is_category_col(mc)
                )
                if match_ok:
                    df = _try_query(_build_in_clause(mc, values))
                    if df is not None:
                        return {
                            "matched_rows": _sanitize_rows(df.to_dict("records")),
                            "matched_by": mc,
                            "matched_values": sorted(values),
                            "trace_confidence": "high" if knowledge_mode else "medium",
                        }

        # 优先级 3：时间范围
        for _trace_col, (start, end) in trace_keys.get("time_ranges", {}).items():
            for mc in available_cols:
                if _is_time_col(mc):
                    q_mc = _quote_col(mc)
                    start_s = start.replace("'", "''")
                    end_s = end.replace("'", "''")
                    where = (
                        f"CAST({q_mc} AS VARCHAR) >= '{start_s}' "
                        f"AND CAST({q_mc} AS VARCHAR) <= '{end_s}'"
                    )
                    df = _try_query(where)
                    if df is not None:
                        return {
                            "matched_rows": _sanitize_rows(df.to_dict("records")),
                            "matched_by": mc,
                            "trace_confidence": "medium",
                        }

        # 优先级 4：文本片段 LIKE 匹配
        text_frags = trace_keys.get("text_fragments", [])
        if text_frags:
            for mc in available_cols:
                for frag in text_frags[:5]:
                    if len(frag) < 2:
                        continue
                    frag_sql = frag.replace("'", "''").replace("\\", "\\\\")
                    where = f"CAST({_quote_col(mc)} AS VARCHAR) LIKE '%{frag_sql}%'"
                    df = _try_query(where)
                    if df is not None:
                        return {
                            "matched_rows": _sanitize_rows(df.to_dict("records")),
                            "matched_by": mc,
                            "trace_confidence": "low",
                        }

        return None  # 未匹配，调用方记录为未追踪

    except Exception:  # noqa: BLE001
        return None  # DuckDB 不可用（GBK 编码/权限/格式异常），调用方回退整表/未追踪


# ===========================================================================
# 步骤 1：取结果表样本行
# ===========================================================================
def _sample_result_rows(file_path: str, sample_size: int = _SAMPLE_SIZE) -> list[dict[str, Any]]:
    """从结果表取 sample_size 行作为追踪入口，跳过全空行。"""
    from pathlib import Path  # noqa: PLC0415
    from app.data import table_io  # noqa: PLC0415

    try:
        frame = table_io._load_scan_frame(Path(file_path))
    except Exception:  # noqa: BLE001
        return []

    if frame.empty:
        return []

    rows: list[dict[str, Any]] = []
    from app.data.table_io import _jsonable  # noqa: PLC0415
    for _, row in frame.iterrows():
        d = {str(k): _jsonable(v) for k, v in row.to_dict().items()}
        # 跳过全空行
        non_null = [v for v in d.values() if v is not None and str(v).strip() not in ("", "nan", "None")]
        if non_null:
            rows.append(d)
        if len(rows) >= sample_size:
            break
    return rows


# ===========================================================================
# 步骤 2：提取追踪键值
# ===========================================================================
def _extract_trace_keys(result_rows: list[dict[str, Any]]) -> dict[str, Any]:
    """从结果行中提取可用作追踪的列值，分为 id/category/time/text 四类。"""
    if not result_rows:
        return {}

    all_cols = list(result_rows[0].keys())

    id_values: dict[str, set[str]] = {}
    category_values: dict[str, set[str]] = {}
    time_ranges: dict[str, tuple[str, str]] = {}
    text_fragments: list[str] = []

    for col in all_cols:
        vals = []
        for row in result_rows:
            v = row.get(col)
            if v is not None:
                s = str(v).strip()
                if s and s.lower() not in ("nan", "none", ""):
                    vals.append(s)
        if not vals:
            continue

        if _is_time_col(col):
            sorted_vals = sorted(set(vals))
            time_ranges[col] = (sorted_vals[0], sorted_vals[-1])
        elif _is_id_col(col):
            id_values[col] = set(vals)
        elif _is_category_col(col):
            category_values[col] = set(vals)
        else:
            # 收集短词作为文本片段（过长的可能是描述性文字）
            for v in vals:
                if 1 < len(v) <= 20 and v not in text_fragments:
                    text_fragments.append(v)

    return {
        "id_values": id_values,
        "category_values": category_values,
        "time_ranges": time_ranges,
        "text_fragments": text_fragments[:10],
    }


# ===========================================================================
# 关联关系驱动的精确匹配（优先级最高，绕开一切名称模糊匹配；支持复合键 AND）
# ===========================================================================
def _try_exact_key_match(
    table: TableMeta,
    pairs: list[tuple[str, Any]],
    max_rows: int,
) -> dict[str, Any] | None:
    """按「目标表列 = 锚点值」的**合取**条件精确查询（复合键 = 所有列同时相等）。

    这是关联关系驱动的追踪：列名像不像完全不重要——列对应关系由关联关系本身
    （人工确认的、或值证据推导出的）明确给出。历史版本只取 pairs[0] 一对键值、
    其余列丢弃，导致用户确认了复合键（如 结果表.(违规类型,违规说明) ↔
    知识表.(违规类型,国家问题清单)）后，追踪仍只按第一列过滤，同类目下几十条
    知识行全部命中、样本依旧是错的。现在所有列一起作为 AND 条件参与过滤。

    Args:
        pairs: [(目标表列名, 锚点值), ...]，全部条件同时成立才算命中
    """
    from pathlib import Path  # noqa: PLC0415
    from app.data import table_io  # noqa: PLC0415

    if not pairs:
        return None
    path = Path(table.file_path)
    suffix = path.suffix.lower()

    try:
        if suffix in (".csv", ".tsv", ".txt"):
            import duckdb  # noqa: PLC0415
            header_row = table_io.resolve_header_row(str(path))
            sep = table_io._csv_sep(path)
            path_sql = str(path).replace("\\", "/").replace("'", "''")
            skip_val = max(header_row, 0)
            delim_param = "delim='\\t'" if sep == "\t" else "delim=','"
            read_expr = (
                f"read_csv('{path_sql}', header=true, skip={skip_val}, "
                f"{delim_param}, ignore_errors=true)"
            )
            where = " AND ".join(
                f"TRIM(CAST({_quote_col(col)} AS VARCHAR)) = "
                f"'{str(val).strip().replace(chr(39), chr(39) * 2)}'"
                for col, val in pairs
            )
            con = duckdb.connect()
            try:
                result_df = con.execute(
                    f"SELECT * FROM {read_expr} WHERE {where} LIMIT {max_rows}"
                ).fetchdf()
            finally:
                con.close()
        else:
            frame = table_io.load_full_frame_cached(str(path))
            mask = None
            for col, val in pairs:
                if col not in frame.columns:
                    return None
                m = frame[col].astype(str).str.strip() == str(val).strip()
                mask = m if mask is None else (mask & m)
            result_df = frame[mask].head(max_rows)
    except Exception:  # noqa: BLE001
        return None

    if result_df is None or result_df.empty:
        return None
    return {
        "matched_rows": _sanitize_rows(result_df.to_dict("records")),
        "matched_by": "+".join(col for col, _ in pairs),
        "matched_cols": [col for col, _ in pairs],
        "matched_values": [str(v) for _, v in pairs],
        "trace_confidence": "high",
    }


def _try_relation_key_matches(
    table: TableMeta,
    key_matches: list[dict[str, Any]] | None,
    max_rows: int,
) -> dict[str, Any] | None:
    """按优先级逐个尝试关联关系给出的键集（人工确认在前、推导关联按置信度降序）。

    命中即返回，并在 matched_by 里标注证据来源，让用户在前端一眼看出这批样本
    是靠哪条关联、什么证据追出来的。
    """
    for km in (key_matches or []):
        result = _try_exact_key_match(table, km.get("pairs") or [], max_rows)
        if result is None:
            continue
        src = km.get("source", "relation")
        conf = float(km.get("confidence", 0.0))
        label = "人工确认关联" if src == "confirmed" else f"推导关联·置信度{conf:.0%}"
        result["matched_by"] = f"{result['matched_by']}（{label}）"
        result["matched_source"] = src
        result["trace_confidence"] = "high" if (src == "confirmed" or conf >= 0.8) else "medium"
        return result
    return None


# ===========================================================================
# 步骤 3：追踪业务表
# ===========================================================================
def _trace_business_table(
    table: TableMeta,
    trace_keys: dict[str, Any],
    max_rows: int = _MAX_TRACE_ROWS,
    key_matches: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """对单个业务表执行追踪采样。

    策略：
    0. 若这张表与结果表之间存在关联关系（**人工确认**的绝对优先，其后是推导出的
       值证据关联，按置信度降序），直接按关联给出的列对应做精确匹配（支持复合键
       AND），优先级高于以下所有"字段名相近"启发式——追踪链必须与已推导/已修正
       的关联关系保持一致，用户明确纠正过的东西，系统不能再自作主张。
    1. CSV/TSV → 用 DuckDB 对完整文件做 WHERE 过滤（不受 2000 行限制）
    2. Excel/JSON/MD 或 DuckDB 失败 → 回退到**整表**加载（带进程内缓存）+ pandas 过滤。
       这里必须是整表而不是 2000 行采样：真实业务表几十万行很常见，命中的具体行
       几乎不可能恰好落在文件最前面，用采样搜会系统性地把"没搜到"误判成"不存在"，
       进而让 AI 收到一堆互不相干的样本去瞎猜关联/流程。
    3. 仍无匹配 → 标记未追踪，不提供随机样本给后续 AI 推导
    """
    from pathlib import Path  # noqa: PLC0415
    from app.data import table_io  # noqa: PLC0415

    relation_result = _try_relation_key_matches(table, key_matches, max_rows)
    if relation_result is not None:
        return relation_result
    # 关联键没找到行（可能锚点这次没有对应字段的值），退回正常的启发式流程

    path = Path(table.file_path)
    suffix = path.suffix.lower()

    # --- 路径 A：DuckDB 全文件流式查询（CSV/TSV）---
    if suffix in (".csv", ".tsv", ".txt"):
        header_row = table_io.resolve_header_row(str(path))
        sep = table_io._csv_sep(path)
        result = _duckdb_full_query(
            str(path), header_row, sep, trace_keys, max_rows, knowledge_mode=False,
            allow_weak_fallbacks=False,
        )
        if result is not None:
            return result
        # DuckDB 已经对全文件搜过一遍没找到；不能再塞随机行给 AI。
        return _no_trace_result("⚠️ 未找到与结果锚点关联的业务行；该表不作为推导样本。")

    # --- 路径 B：整表加载（Excel/JSON/MD 从未被 DuckDB 搜过，必须整表才能真正判断
    #     有没有关联；CSV 已在路径 A 全文件搜过，不会走到这里）---
    try:
        df = table_io.load_full_frame_cached(str(path))
    except Exception:  # noqa: BLE001
        return _no_trace_result("⚠️ 表数据读取失败，无法追踪。")

    if df.empty:
        return _no_trace_result("⚠️ 表数据为空，无法追踪。")

    cols = [str(c) for c in df.columns]

    def _str_col(col: str):
        return df[col].astype(str).str.strip()

    # Excel/JSON/MD 走到这里，df 已是整表。业务表只接受严格 key-like 证据：
    # 明确关系键优先；没有关系键时，只用结果锚点里的 ID/key 列去匹配名称相近的目标列。
    # 不再用分类、时间、文本片段或全列值扫描兜底，避免把偶然相同的描述/状态/名称值
    # 当成因果链路样本。
    # 优先级 1：ID 精确匹配
    for trace_col, values in trace_keys.get("id_values", {}).items():
        for mc in cols:
            if _col_name_overlap(trace_col, mc):
                mask = _str_col(mc).isin(values)
                matched = df[mask].head(max_rows)
                if not matched.empty:
                    return {
                        "matched_rows": _sanitize_rows(matched.to_dict("records")),
                        "matched_by": mc,
                        "matched_values": sorted(values),
                        "trace_confidence": "high",
                    }

    # 全失败：未追踪。不要用随机样本冒充链路样本。
    return _no_trace_result("⚠️ 未找到与结果锚点关联的业务行；该表不作为推导样本。")


# ===========================================================================
# 步骤 4：追踪知识表（规则表）
# ===========================================================================
def _trace_knowledge_table(
    table: TableMeta,
    trace_keys: dict[str, Any],
    max_rows: int = 10,
    key_matches: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """对知识表执行追踪采样（category_values 优先，再 text_fragments）。

    同 _trace_business_table：CSV/TSV 用 DuckDB 全文件查询；Excel/JSON/MD 用整表加载
    （带缓存），不能只搜前 2000 行——否则大知识表一样会出现"明明有关联但搜不到"。
    关联关系（人工确认优先、推导关联次之，支持复合键）同样优先于一切启发式（优先级0）。
    """
    from pathlib import Path  # noqa: PLC0415
    from app.data import table_io  # noqa: PLC0415

    relation_result = _try_relation_key_matches(table, key_matches, max_rows)
    if relation_result is not None:
        return relation_result

    path = Path(table.file_path)
    suffix = path.suffix.lower()

    # --- 路径 A：DuckDB 全文件流式查询（CSV/TSV）---
    if suffix in (".csv", ".tsv", ".txt"):
        header_row = table_io.resolve_header_row(str(path))
        sep = table_io._csv_sep(path)
        result = _duckdb_full_query(
            str(path), header_row, sep, trace_keys, max_rows, knowledge_mode=True,
            allow_weak_fallbacks=True,
        )
        if result is not None:
            return result
        return _no_trace_result("⚠️ 未找到与结果锚点关联的知识行；该表不作为推导样本。")

    # --- 路径 B：整表加载（Excel/JSON/MD）---
    try:
        df = table_io.load_full_frame_cached(str(path))
    except Exception:  # noqa: BLE001
        return _no_trace_result("⚠️ 表数据读取失败，无法追踪。")

    if df.empty:
        return _no_trace_result("⚠️ 表数据为空，无法追踪。")

    cols = [str(c) for c in df.columns]

    def _str_col(col: str):
        return df[col].astype(str).str.strip()

    # 策略 1：用 category_values 匹配知识表的分类列
    for trace_col, values in trace_keys.get("category_values", {}).items():
        for mc in cols:
            if _col_name_overlap(trace_col, mc) or _is_category_col(mc):
                mask = _str_col(mc).isin(values)
                matched = df[mask].head(max_rows)
                if not matched.empty:
                    return {
                        "matched_rows": _sanitize_rows(matched.to_dict("records")),
                        "matched_by": mc,
                        "matched_values": sorted(values),
                        "trace_confidence": "high",
                    }

    # 策略 2：用 text_fragments 在条件描述列中搜索
    text_frags = trace_keys.get("text_fragments", [])
    if text_frags:
        text_cols = [c for c in cols if df[c].dtype == object or df[c].dtype.kind in ("O", "U")]
        for tc in text_cols:
            sc = _str_col(tc)
            for frag in text_frags:
                if len(frag) < 2:
                    continue
                try:
                    mask = sc.str.contains(re.escape(frag), na=False)
                    matched = df[mask].head(max_rows)
                    if not matched.empty:
                        return {
                            "matched_rows": _sanitize_rows(matched.to_dict("records")),
                            "matched_by": tc,
                            "trace_confidence": "medium",
                        }
                except Exception:  # noqa: BLE001
                    continue

    # 全失败：未追踪。不要用随机样本冒充链路样本。
    return _no_trace_result("⚠️ 未找到与结果锚点关联的知识行；该表不作为推导样本。")


# ===========================================================================
# 步骤 5：组装关联样本报告
# ===========================================================================
def _build_trace_report(
    result_sample: list[dict[str, Any]],
    trace_map: dict[str, dict[str, Any]],
    unmatched_tables: list[str],
    result_table_name: str = "",
    anchor_result_row_index: int = 1,
    candidate_result_rows: int = 1,
) -> dict[str, Any]:
    """构建发给 AI 的结构化关联样本报告。"""
    total_rows = len(result_sample) + sum(
        len(v.get("matched_rows", []))
        for v in trace_map.values()
        if v.get("matched_by") != "random"
    )

    summary_parts = []
    for tbl, info in trace_map.items():
        n = len(info.get("matched_rows", []))
        by = info.get("matched_by", "")
        conf = info.get("trace_confidence", "low")
        if by and by != "random":
            summary_parts.append(f"通过「{by}」追踪到「{tbl}」{n}行(置信度:{conf})")

    return {
        "result_sample": result_sample,
        "result_table": result_table_name,
        "anchor_result_row_index": anchor_result_row_index,
        "candidate_result_rows": candidate_result_rows,
        "trace_map": trace_map,
        "unmatched_tables": unmatched_tables,
        "total_rows": total_rows,
        "trace_summary": "；".join(summary_parts) if summary_parts else "未追踪到稳定因果链路",
    }


# ===========================================================================
# 降级：随机采样（无结果表时）
# ===========================================================================
def _fallback_random_sampling(scenario: Scenario, reason: str = "") -> dict[str, Any]:
    """无结果表或追踪失败时，退化为独立随机采样，但打上警告标记。"""
    from pathlib import Path  # noqa: PLC0415
    from app.data import table_io  # noqa: PLC0415

    trace_map: dict[str, dict[str, Any]] = {}
    for t in scenario.tables_meta:
        try:
            df = table_io._load_scan_frame(Path(t.file_path))
            sample = table_io._sanitize_rows(df.head(5).to_dict("records"))
        except Exception:  # noqa: BLE001
            sample = []
        trace_map[t.table_name] = {
            "matched_rows": sample,
            "matched_by": "random",
            "trace_confidence": "low",
            "warning": "⚠️ 无结果表，随机采样",
        }

    total = sum(len(v.get("matched_rows", [])) for v in trace_map.values())
    return {
        "result_sample": [],
        "result_table": "",
        "trace_map": trace_map,
        "unmatched_tables": [],
        "total_rows": total,
        "trace_summary": f"降级随机采样（{reason}）：各表独立取前 5 行，样本间无因果关联",
        "degraded": True,
    }


# ===========================================================================
# 主入口
# ===========================================================================
def trace_sampling(
    scenario: Scenario,
    result_table_name: str | None = None,
    sample_size: int = _SAMPLE_SIZE,
    max_trace_rows: int = _MAX_TRACE_ROWS,
) -> dict[str, Any]:
    """完整追踪采样流程。

    以结果表为入口，沿键值关联追踪各业务表和知识表，
    保证发给 AI 的样本具有跨表的因果关系。

    Args:
        scenario:           业务场景（含所有表元数据）
        result_table_name:  结果表名（追踪入口），None 时自动寻找
        sample_size:        从结果表取的样本行数（默认 1，即第一条非空结果记录）
        max_trace_rows:     每个业务表最多追踪行数（默认 50）

    Returns:
        trace_report: 关联样本报告（供 AI 推导使用）
    """
    # 1. 找到结果表
    if result_table_name:
        result_table = next(
            (t for t in scenario.tables_meta if t.table_name == result_table_name), None
        )
    else:
        result_table = next(
            (t for t in scenario.tables_meta if t.role == TableRole.RESULT.value), None
        )

    if result_table is None:
        return _fallback_random_sampling(scenario, reason="无结果表")

    # 2. 取结果表样本行。默认只取第一条非空结果记录；调用方显式传入更大的
    # sample_size 时，才会逐条试锚点并选择链路最完整的一条。
    result_rows = _sample_result_rows(result_table.file_path, sample_size)
    if not result_rows:
        return _fallback_random_sampling(scenario, reason="结果表为空")

    # 3. 逐条候选结果行分别追踪，取"追出链路最完整"的那一条作为锚点。
    #
    #    关键教训：旧实现把 5 条结果行的键值**混在一起**提取（如 id_values["就诊ID"] =
    #    {行1的ID, 行2的ID, ..., 行5的ID}），再分别去各表里搜"有没有命中任意一个"。
    #    这样搜出来的"关联行"可能来自不同的结果行——比如给 LLM 展示的"结果行"是第1行，
    #    但"关联到就诊表"的那一行其实是靠第3行的就诊ID命中的，两者根本不是同一次
    #    因果事件，只是碰巧都在同一批 5 行样本里。真实发生过：LLM 自己发现"结果行锚点
    #    的就诊ID是A，但关联到就诊表返回的是ID为B的记录"，正是这个设计缺陷暴露出来的。
    #    现在改为一次只用**一条**结果行的键值去追，保证同一份 trace_map 里所有表的
    #    matched_rows 都真实对应同一条结果行——这才是一条完整的因果链，而不是拼凑。
    non_result_tables = [t for t in scenario.tables_meta if t.role != TableRole.RESULT.value]

    # 关联关系驱动的追踪键集（v1.1.1 重构）：
    # 之前只收集**人工确认**关联、且只取单列（from_column/to_column），复合键列表
    # （from_columns/to_columns）被整个丢掉——用户确认了"结果表两列 ↔ 知识表两列"的
    # 复合关联后，追踪仍只按第一列过滤，样本照旧是错的。同时，推导出来的高置信度
    # 值证据关联也完全没参与追踪，追踪层自己再按"字段名相近"瞎猜一遍。
    # 现在：每条与结果表相连的关联（人工确认的一律采纳；未确认的按置信度过滤）都
    # 变成一个完整键集（含全部复合列），人工确认排最前、推导关联按置信度降序，
    # 逐个尝试精确匹配；只有全部关联键都追不上时才退回字段名启发式。
    keysets_by_table: dict[str, list[dict[str, Any]]] = {}
    # 关联给出的 锚点列→目标表列 映射（供交叉校验收窄用，含较低置信度候选）
    col_mappings_by_table: dict[str, dict[str, list[str]]] = {}
    _seen_keysets: set[tuple[str, tuple]] = set()
    if scenario.relations:
        for r in scenario.relations.relations:
            if r.from_table == result_table.table_name:
                target = r.to_table
                res_cols = r.from_columns or [r.from_column]
                tgt_cols = r.to_columns or [r.to_column]
            elif r.to_table == result_table.table_name:
                target = r.from_table
                res_cols = r.to_columns or [r.to_column]
                tgt_cols = r.from_columns or [r.from_column]
            else:
                continue
            pairs = [(tc, rc) for tc, rc in zip(tgt_cols, res_cols) if tc and rc]
            if not pairs:
                continue
            if r.confirmed or r.confidence >= 0.6:
                dedup_key = (target, tuple(sorted(pairs)))
                if dedup_key not in _seen_keysets:
                    _seen_keysets.add(dedup_key)
                    keysets_by_table.setdefault(target, []).append({
                        "pairs": pairs,
                        "source": "confirmed" if r.confirmed else "relation",
                        "confidence": r.confidence,
                    })
            if r.confirmed or r.confidence >= 0.5:
                mapping = col_mappings_by_table.setdefault(target, {})
                for tgt_col, res_col in pairs:
                    cols = mapping.setdefault(res_col, [])
                    if tgt_col not in cols:
                        cols.append(tgt_col)
    for sets in keysets_by_table.values():
        sets.sort(key=lambda k: (k["source"] != "confirmed", -k["confidence"]))

    # 锚点行打分权重：人工确认关联命中 > 推导关联命中 > 字段名启发式命中 > 随机。
    # 这保证被选中的因果链优先是"沿用户确认/系统推导的关联"追出来的那条。
    _SOURCE_WEIGHT = {"confirmed": 3, "relation": 2}

    def _table_max_weight(tname: str) -> int:
        sets = keysets_by_table.get(tname, [])
        if any(k["source"] == "confirmed" for k in sets):
            return 3
        return 2 if sets else 1

    max_possible = sum(_table_max_weight(t.table_name) for t in non_result_tables)

    best_row = result_rows[0]
    best_row_index = 1
    best_trace_map: dict[str, dict[str, Any]] = {}
    best_unmatched: list[str] = list(non_result_tables and [t.table_name for t in non_result_tables])
    best_score = -1

    for row_index, row in enumerate(result_rows, start=1):
        trace_keys = _extract_trace_keys([row])  # 只含这一行的键值，不与其它行混
        trace_map: dict[str, dict[str, Any]] = {}
        unmatched_tables: list[str] = []

        for table in non_result_tables:
            # 用当前锚点行的真实值实例化该表的关联键集（跳过锚点值为空的列）
            key_matches: list[dict[str, Any]] = []
            for ks in keysets_by_table.get(table.table_name, []):
                pairs = [
                    (tgt_col, row[res_col])
                    for tgt_col, res_col in ks["pairs"]
                    if res_col in row and row[res_col] not in (None, "")
                    and str(row[res_col]).strip().lower() not in ("nan", "none", "null")
                ]
                if len(pairs) == len(ks["pairs"]):
                    key_matches.append({**ks, "pairs": pairs})

            has_confirmed = any(
                k["source"] == "confirmed" for k in keysets_by_table.get(table.table_name, [])
            )
            is_knowledge = table.role in (TableRole.RULE.value, "knowledge")
            if is_knowledge:
                result = _trace_knowledge_table(table, trace_keys, max_rows=10, key_matches=key_matches)
            else:
                result = _trace_business_table(
                    table, trace_keys, max_rows=max_trace_rows, key_matches=key_matches,
                )

            # 唯一性交叉校验：仅凭现有匹配列可能不足以唯一确定对应关系——用锚点行
            # 其它已知值（关联映射列优先，字段名相近兜底）再筛一遍，能收窄说明该用
            # 复合键。人工确认的关联不再二次校验（用户已拍板，系统不自作主张）。
            matched_by = result.get("matched_by", "")
            if (result.get("matched_source") != "confirmed"
                    and matched_by and matched_by != "random" and result.get("matched_rows")):
                matched_cols = result.get("matched_cols") or [matched_by]
                narrowed, warning, composite_cols = _cross_validate_and_narrow(
                    result["matched_rows"], trace_keys, matched_cols,
                    anchor_row=row,
                    col_mappings=col_mappings_by_table.get(table.table_name),
                )
                if warning:
                    result = {**result, "matched_rows": narrowed, "warning": warning,
                              "composite_suggested": True, "composite_columns": composite_cols}

            # 用户确认过的关系是最高优先级。如果确认键未命中，不能再退回其它弱匹配
            # 生成看似相关但实际违背用户修正意见的样本。
            if has_confirmed and result.get("matched_source") != "confirmed":
                result = _no_trace_result(
                    "⚠️ 已存在人工确认的关联，但按确认的关联键未命中这条结果锚点；"
                    "未使用其它弱匹配样本。请核对字段值格式或换一条结果样本重试。"
                )

            if result.get("matched_rows") and result.get("matched_by") != "random":
                trace_map[table.table_name] = result
            elif result.get("matched_rows"):
                trace_map[table.table_name] = result
                unmatched_tables.append(table.table_name)
            else:
                trace_map[table.table_name] = result
                unmatched_tables.append(table.table_name)

        score = 0
        for info in trace_map.values():
            if (
                not info.get("matched_rows")
                or not info.get("matched_by")
                or info.get("matched_by") == "random"
            ):
                continue
            score += _SOURCE_WEIGHT.get(info.get("matched_source", ""), 1)
        if score > best_score:
            best_score, best_row = score, row
            best_row_index = row_index
            best_trace_map, best_unmatched = trace_map, unmatched_tables
        if score >= max_possible:
            break  # 这一行已经以最高证据等级追全了每张表，没必要再试后面的候选行

    # 4. 组装报告（result_sample 只含被选中的这一条锚点行，与 trace_map 是同一条因果链）
    return _build_trace_report(
        [best_row],
        best_trace_map,
        best_unmatched,
        result_table.table_name,
        anchor_result_row_index=best_row_index,
        candidate_result_rows=len(result_rows),
    )
