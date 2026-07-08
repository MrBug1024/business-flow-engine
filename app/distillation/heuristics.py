"""启发式推导。

当未配置 LLM 时，作为「降级路径」给出确定性的关联/流程/技能推导；
当配置了 LLM 时，这些函数也可作为 Agent 的辅助分析工具（如值重叠率计算）。

推导思路与需求一致：
* 关联关系：字段名语义相似 + 数据类型兼容 + 样本值重叠率。
* 业务流程：以结果表为终点逆向追溯（过滤 → 关联 → 聚合 → 计算）。
* 技能库：将每个流程步骤固化为一个可复用 Skill。
"""

from __future__ import annotations

import re
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

import pandas as pd

from app.data import table_io
from app.distillation import clarifications
from app.domain.models import (
    GraphData,
    GraphEdge,
    GraphNode,
    Relation,
    RelationResult,
    Scenario,
    TableMeta,
)

# 数值/可聚合类型
_NUMERIC_HINTS = ("int", "float", "decimal", "double", "number")


def _normalize(name: str) -> str:
    """归一化字段名，便于相似度比较。"""
    return re.sub(r"[\s_\-]+", "", name.strip().lower())


def _name_similarity(a: str, b: str) -> float:
    na, nb = _normalize(a), _normalize(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    # 一方为另一方的后缀（如 product_id 对 id），视为强信号
    if na.endswith(nb) or nb.endswith(na):
        return 0.9
    return SequenceMatcher(None, na, nb).ratio()


def _dtype_compatible(a: str, b: str) -> bool:
    a, b = a.lower(), b.lower()
    a_num = any(h in a for h in _NUMERIC_HINTS)
    b_num = any(h in b for h in _NUMERIC_HINTS)
    return a_num == b_num


def _is_keyish(col_name: str) -> bool:
    n = _normalize(col_name)
    return n == "id" or n.endswith("id") or n.endswith("code") or n.endswith("no")


def _classify(table: TableMeta) -> str:
    """图谱节点类型：取表角色（用户标注/确认），未知归为 table。"""
    role = table.role or "unknown"
    return role if role in ("result", "knowledge", "rule", "input") else "table"


# ===========================================================================
# 关联关系推导
# ===========================================================================
_MAX_CANDIDATES_PER_TABLE_PAIR = 3  # 每对表最多保留几个独立候选列对（不止报最强的那一个）


def candidate_relations(
    scenario: Scenario,
    trace_report: dict[str, Any] | None = None,
    stats: dict[str, int] | None = None,
) -> list[Relation]:
    """计算「真实样本值包含率 + 字段名相似度」双证据支持的候选关联。

    这是关联推导的**唯一权威证据来源**：不管是无 LLM 的启发式兜底路径
    (`deduce_relations`)，还是 LLM 推理路径 (`inference.infer_relations`)，
    都必须用本函数算出的真实值重叠证据，而不是只凭字段名相似或几行随机/追踪样本
    去猜——字段名相似但值不重叠（如「商品名称」vs「商品编码」值域完全不同）应判低分，
    字段名不像但值高度重叠（如两表用不同列名存同一批业务单号）应判高分且**值证据优先于
    字段名**：哪怕字段名完全不像，只要值真的对得上，也必须报出来。

    v1.0.7 起每对表不再只报"最强的那一个"列对，而是保留最多
    `_MAX_CANDIDATES_PER_TABLE_PAIR` 个独立候选——单字段关联可能存在多个都成立
    （如同时通过「就诊ID」和「结算ID」关联），或者需要人工/`trace_sampling` 进一步
    判断是否要组合成复合键才能唯一确定对应关系。

    同一张表的同一列在一次调用内只从磁盘读取一次（frame_cache），
    避免对每个候选列对都重新扫描整份文件。
    """
    tables = scenario.tables_meta
    frame_cache: dict[str, pd.DataFrame] = {}
    value_set_cache: dict[tuple[str, str], set] = {}
    relations: list[Relation] = []
    for i, left in enumerate(tables):
        for right in tables[i + 1:]:
            if stats is not None:
                stats["table_pairs"] = stats.get("table_pairs", 0) + 1
            for lcol, rcol, score, evidence, rel_type in _best_column_matches(
                left, right, frame_cache, value_set_cache,
                trace_report=trace_report,
                stats=stats,
            ):
                relations.append(
                    Relation(
                        from_table=left.table_name,
                        from_column=lcol,
                        to_table=right.table_name,
                        to_column=rcol,
                        relation_type=rel_type,
                        confidence=round(score, 2),
                        evidence=evidence,
                    )
                )
    return relations


def deduce_relations(scenario: Scenario) -> RelationResult:
    tables = scenario.tables_meta
    relations = candidate_relations(scenario)
    questions: list[str] = []

    confident = [r for r in relations if r.confidence >= 0.8]
    weak_count = len(relations) - len(confident)
    relations = confident
    graph = _build_relation_graph(tables, relations)
    clarification_items = clarifications.build_clarifications(
        questions, context="deduce_relations"
    )
    summary = f"共发现 {len(confident)} 条确定关联。"
    if weak_count:
        summary += f" 已忽略 {weak_count} 条缺少足够值证据的弱候选，避免误导后续流程。"
    return RelationResult(
        relations=relations,
        ambiguous_questions=clarifications.normalized_question_texts(clarification_items),
        clarifications=clarification_items,
        graph_data=graph,
        summary=summary,
    )


_PROBE_SIZE = 50  # 值探测阶段最多抽样比对的元素个数


def _best_column_matches(
    left: TableMeta,
    right: TableMeta,
    frame_cache: dict | None = None,
    value_set_cache: dict | None = None,
    trace_report: dict[str, Any] | None = None,
    stats: dict[str, int] | None = None,
) -> list[tuple[str, str, float, str, str]]:
    """在两张表之间寻找最多 `_MAX_CANDIDATES_PER_TABLE_PAIR` 个独立候选关联列对。

    评分优先级：**真实值证据 > 字段名相似度**（问题反馈：旧版权重是
    `0.6*name_sim + 0.3*overlap`，字段名占大头，导致"两列值完全相等但字段名
    完全不像"的真实关联因为过不了字段名门槛而被剪枝掉、根本没机会算值重叠。
    现在反过来：先探测值层面有没有交集，值证据够强时字段名像不像不重要；
    值层面探测不到任何交集时直接跳过，不再把"仅凭字段名"的弱信号抛给用户确认。

    性能：不再用字段名相似度做剪枝（那样会漏判问题1里的场景），改成两级漏斗——
    ① 每列的去重值集合按 (文件, 列名) 缓存，同一列参与多少次比较都只构建一次；
    ② 比较前先用"较小集合里最多 `_PROBE_SIZE` 个值有没有命中较大集合"探测一下，
       探测不到才跳过完整的精确 containment 计算——较小集合本身很小（本项目里
       典型是"结果表/规则表"，通常几十行）时这个探测其实是穷举，不会漏判。
    """
    fcache = frame_cache if frame_cache is not None else {}
    vcache = value_set_cache if value_set_cache is not None else {}
    candidates: list[tuple[str, str, float, str, str]] = []

    for lc in left.columns:
        lset: set | None = None
        for rc in right.columns:
            if stats is not None:
                stats["column_pairs"] = stats.get("column_pairs", 0) + 1
            if not _dtype_compatible(lc.dtype, rc.dtype):
                if stats is not None:
                    stats["dtype_skipped"] = stats.get("dtype_skipped", 0) + 1
                continue
            if not _should_compare_columns(left, lc, right, rc, trace_report):
                if stats is not None:
                    stats["prefilter_skipped"] = stats.get("prefilter_skipped", 0) + 1
                continue
            if stats is not None:
                stats["value_compared"] = stats.get("value_compared", 0) + 1
            if lset is None:
                lset = _get_value_set(left, lc.name, fcache, vcache)
            if not lset:
                break
            rset = _get_value_set(right, rc.name, fcache, vcache)
            if not rset:
                continue

            name_sim = _name_similarity(lc.name, rc.name)
            key_bonus = 0.1 if (_is_keyish(lc.name) or _is_keyish(rc.name)) else 0.0
            probe_hit = _probe_overlap(lset, rset)

            if not probe_hit:
                # 没有真实值重叠就不产出候选。字段名相似只能作为排序辅助，不能单独
                # 变成待用户确认的问题，否则会出现"序号/年月/年龄是否关联"这类噪声。
                continue
            else:
                overlap = len(lset & rset) / min(len(lset), len(rset))
                score = min(0.75 * overlap + 0.2 * name_sim + key_bonus, 0.99)
                if score < 0.5:
                    continue
                evidence = f"值包含率 {overlap:.0%}、字段名相似度 {name_sim:.0%}、类型兼容"

            rel_type = "foreign_key" if score >= 0.8 else "possible_link"
            candidates.append((lc.name, rc.name, score, evidence, rel_type))

    candidates.sort(key=lambda x: -x[2])
    # 同一个左列只保留其最高分的一次匹配，避免"最强那一列"独占全部候选名额，
    # 让不同左列各自有机会入选（问题2：为复合键/多个独立关联留出发现空间）。
    seen_left: set[str] = set()
    deduped: list[tuple[str, str, float, str, str]] = []
    for cand in candidates:
        if cand[0] in seen_left:
            continue
        seen_left.add(cand[0])
        deduped.append(cand)
        if len(deduped) >= _MAX_CANDIDATES_PER_TABLE_PAIR:
            break
    return deduped


def _column_role(col) -> str:
    return (getattr(col, "semantic_role", "") or "").upper()


def _is_blank_value(value: Any) -> bool:
    if value is None:
        return True
    text = str(value).strip().lower()
    return text in ("", "nan", "none", "null")


def _jsonable_values(values: list[Any]) -> set:
    out = set()
    for value in values:
        if _is_blank_value(value):
            continue
        try:
            out.add(table_io._jsonable(value))
        except Exception:  # noqa: BLE001
            continue
    return out


def _trace_rows_for_table(table: TableMeta, trace_report: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not trace_report or trace_report.get("degraded"):
        return []
    if trace_report.get("result_table") == table.table_name:
        return list(trace_report.get("result_sample") or [])
    info = (trace_report.get("trace_map") or {}).get(table.table_name) or {}
    if info.get("matched_by") == "random":
        return []
    return list(info.get("matched_rows") or [])


def _cheap_sample_values(table: TableMeta, col, trace_report: dict[str, Any] | None) -> set:
    values = list(getattr(col, "sample_values", []) or [])[:20]
    for row in _trace_rows_for_table(table, trace_report)[:5]:
        if col.name in row:
            values.append(row.get(col.name))
    for row in (table.sample_rows or [])[:3]:
        if col.name in row:
            values.append(row.get(col.name))
    return _jsonable_values(values)


def _cheap_sample_overlap(
    left: TableMeta,
    lc,
    right: TableMeta,
    rc,
    trace_report: dict[str, Any] | None,
) -> bool:
    lvals = _cheap_sample_values(left, lc, trace_report)
    if not lvals:
        return False
    rvals = _cheap_sample_values(right, rc, trace_report)
    return bool(rvals and (lvals & rvals))


def _should_compare_columns(
    left: TableMeta,
    lc,
    right: TableMeta,
    rc,
    trace_report: dict[str, Any] | None = None,
) -> bool:
    """轻量判断列对是否值得做全量值集合验证。

    这里不产出关联结论，只决定是否值得扫描大表。真正是否有关联仍由后续全量值重叠证据决定。
    """
    name_sim = _name_similarity(lc.name, rc.name)
    if name_sim >= 0.58:
        return True
    if _cheap_sample_overlap(left, lc, right, rc, trace_report):
        return True

    lrole, rrole = _column_role(lc), _column_role(rc)
    if lrole in ("PK", "FK") and rrole in ("PK", "FK"):
        return True
    if _is_keyish(lc.name) and _is_keyish(rc.name):
        return True
    if (_is_keyish(lc.name) or _is_keyish(rc.name)) and name_sim >= 0.25:
        return True
    if lrole in ("DIM", "CATEGORY") and rrole in ("DIM", "CATEGORY") and name_sim >= 0.42:
        return True
    return False


def _cached_frame(file_path: str, cache: dict) -> pd.DataFrame:
    """读取某文件的**整表**数据帧（判断"两列值是否真的重叠"必须看全量，不能只看
    前 2000 行的采样——业务表几十万行时，真正命中的行大概率不在前 2000 行里，
    只用采样会把"没搜到"误判成"不存在"）。

    `table_io.load_full_frame_cached` 已按 (路径, mtime) 做进程内缓存；这里的
    `cache` 只是同一次 candidate_relations 调用内的本地记忆，避免重复走一次
    mtime stat + dict 查找。
    """
    if file_path not in cache:
        try:
            cache[file_path] = table_io.load_full_frame_cached(file_path)
        except Exception:  # noqa: BLE001
            cache[file_path] = pd.DataFrame()
    return cache[file_path]


def _get_value_set(table: TableMeta, col: str, frame_cache: dict, value_set_cache: dict) -> set:
    """取某表某列的全量去重值集合，按 (文件路径, 列名) 缓存。

    不缓存的话，一列参与"和另一张表逐列比较"时会被反复重建几十上百次
    （宽表两两比较是列数平方级的）；缓存后每列只构建一次，之后全是字典查找。
    """
    key = (table.file_path, col)
    if key in value_set_cache:
        return value_set_cache[key]
    frame = _cached_frame(table.file_path, frame_cache)
    if col not in frame.columns:
        value_set_cache[key] = set()
        return value_set_cache[key]
    try:
        vset = {table_io._jsonable(v) for v in frame[col].dropna().tolist()}
    except Exception:  # noqa: BLE001
        vset = set()
    value_set_cache[key] = vset
    return vset


def _probe_overlap(lset: set, rset: set, probe_size: int = _PROBE_SIZE) -> bool:
    """探测两个集合是否存在交集：取较小集合最多 `probe_size` 个值，看有没有命中
    较大集合。较小集合本身不超过 `probe_size` 时是穷举（精确），只有较小集合本身
    很大（几万+去重值）时才是抽样探测（可能漏判极稀疏的重叠，但这类情况本身
    对关联判断的参考价值也低）。
    """
    smaller, larger = (lset, rset) if len(lset) <= len(rset) else (rset, lset)
    if len(smaller) <= probe_size:
        sample = smaller
    else:
        import random  # noqa: PLC0415
        sample = random.sample(list(smaller), probe_size)
    return any(v in larger for v in sample)


def _build_relation_graph(tables: list[TableMeta], relations: list[Relation]) -> GraphData:
    nodes = [
        GraphNode(id=t.table_name, label=t.table_name, type=_classify(t)) for t in tables
    ]
    edges = [
        GraphEdge(
            source=r.from_table,
            target=r.to_table,
            label=f"{r.from_column}→{r.to_column}",
        )
        for r in relations
    ]
    return GraphData(nodes=nodes, edges=edges)


# 业务流程推导现已由 `inference.infer_flow` 负责（含字段语义 + 知识结构映射 + 节点能力描述）。
# 本模块只保留关联推导启发式与公用图构建函数。
