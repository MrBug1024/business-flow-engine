#!/usr/bin/env python3
"""Run a distilled business scenario through one bounded portable entrypoint."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any, Iterable, Sequence


SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_CONTRACT = SCRIPT_DIR.parent / "references" / "operational-data-contract.json"
DEFAULT_FLOW = SCRIPT_DIR.parent / "references" / "flow-contract.json"
DEFAULT_RECIPES = SCRIPT_DIR.parent / "references" / "compiled-recipes.json"
MAX_OUTPUT_ROWS = 200
MAX_OUTPUT_CHARS = 512_000
MAX_SEARCH_TERMS = 96
MAX_CONTINUATION_FILTERS = 8
SEARCH_SEPARATORS = r"[\s,，;；。.!！?？:：/\\|()（）\[\]【】\-—_]+"
STOP_TERMS = {
    "执行", "审核", "请", "帮我", "处理", "分析", "判断", "一下", "需要", "进行",
    "业务", "场景", "结果", "规则", "政策", "条件", "要求", "按照", "根据", "完成",
}
REQUEST_PREFIXES = (
    "please", "help", "run", "execute", "process", "analyze", "audit", "review", "check",
    "请", "帮我", "执行", "进行", "处理", "分析", "审核", "审计", "核查", "检查", "查询", "定位", "生成",
)
CJK_TEXT = re.compile(r"[\u4e00-\u9fff]{3,}")

GENERIC_COLUMN_ROLE_MARKERS: dict[str, tuple[str, ...]] = {
    "identifier": (
        "id", "uuid", "key", "code", "编号", "编码", "序号", "标识", "唯一",
    ),
    "subject": (
        "name", "title", "entity", "item", "object", "名称", "标题", "对象", "项目",
    ),
    "selector": (
        "type", "category", "class", "group", "kind", "tag", "类别", "类型", "分类", "分组", "标签",
    ),
    "narrative": (
        "description", "detail", "text", "reason", "basis", "reference", "condition", "criteria",
        "说明", "描述", "内容", "原因", "依据", "条件", "标准", "备注", "用途", "示例", "问题",
    ),
    "decision": (
        "decision", "result", "outcome", "status", "state", "flag", "结论", "结果", "状态", "标志", "是否",
    ),
    "measure": (
        "amount", "price", "quantity", "count", "total", "rate", "score", "value", "金额", "价格", "数量", "次数", "总额", "比例", "分值", "值",
    ),
    "temporal": (
        "date", "time", "year", "month", "day", "start", "end", "duration", "日期", "时间", "年份", "月份", "开始", "结束", "周期", "天数",
    ),
}


class ExecutorError(ValueError):
    pass


def load_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise ExecutorError(f"Missing contract file: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ExecutorError(f"Invalid JSON contract: {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ExecutorError(f"Contract must be a JSON object: {path}")
    return payload


def resolve_contract(raw: str, default: Path) -> Path:
    path = Path(raw).expanduser().resolve() if str(raw).strip() else default.resolve()
    return path


def bounded_text(value: Any, limit: int = 2_000) -> Any:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    text = str(value)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def normalized_text(value: Any) -> str:
    return re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", str(value or "").casefold())


def infer_column_semantic_role(column: dict[str, Any]) -> str:
    kind = str(column.get("kind", "")).casefold()
    if kind in {"id", "identifier", "uuid", "key", "code"}:
        return "identifier"
    if kind in {"number", "numeric", "decimal", "integer", "float"}:
        return "measure"
    if kind in {"date", "datetime", "time", "timestamp"}:
        return "temporal"
    text = normalized_text(column.get("query_name") or column.get("name"))
    scores = {
        role: sum(1 for marker in markers if normalized_text(marker) in text)
        for role, markers in GENERIC_COLUMN_ROLE_MARKERS.items()
    }
    role, score = max(scores.items(), key=lambda item: item[1])
    return role if score else "attribute"


def unique_terms(request: str) -> list[str]:
    text = re.sub(r"\s+", " ", str(request or "")).strip()
    raw_parts = [part.strip(" -—_\t") for part in re.split(SEARCH_SEPARATORS, text)]
    terms: list[str] = []

    def add(value: str) -> None:
        value = str(value or "").strip(" -—_\t")
        if (
            len(value) < 2
            or value.isdigit()
            or value in STOP_TERMS
            or value.casefold() in {"execute", "audit", "review", "process", "analyze"}
            or value in terms
            or len(terms) >= MAX_SEARCH_TERMS
        ):
            return
        terms.append(value)

    for part in raw_parts:
        add(part)
        lowered = part.casefold()
        without_prefix = part
        for prefix in REQUEST_PREFIXES:
            if lowered.startswith(prefix.casefold()) and len(part) > len(prefix) + 1:
                without_prefix = part[len(prefix):].lstrip(" ：:-—_")
                break
        add(without_prefix)

        # Chinese prose commonly joins adjacent business entities without word
        # boundaries. Generate bounded character n-grams from each continuous
        # CJK segment so a rule/entity lookup can recover its anchors. This is
        # language handling only: no industry vocabulary is embedded here.
        for segment in CJK_TEXT.findall(without_prefix):
            max_width = min(6, len(segment))
            for width in range(max_width, 2, -1):
                for start in range(0, len(segment) - width + 1):
                    add(segment[start:start + width])

    if not terms and len(text) >= 2:
        add(text[:80])
    return terms


def row_key(columns: Sequence[str], row: Sequence[Any]) -> str:
    payload = json.dumps([list(columns), [bounded_text(value) for value in row]], ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def term_match_score(row: Sequence[Any], terms: Sequence[str]) -> tuple[list[str], int]:
    text = " ".join(str(value or "") for value in row).casefold()
    matched = [term for term in terms if str(term).casefold() in text]
    return matched, sum(max(1, len(term)) for term in matched)


def request_anchor_score(row: Sequence[Any] | dict[str, Any], request: str) -> tuple[list[str], int]:
    """Score non-overlapping CJK request anchors found in one candidate row.

    Character n-grams are useful for recall when Chinese requests omit word
    boundaries, but counting every overlapping n-gram makes a repeated generic
    phrase look more important than several distinct requested entities.  This
    comparator keeps only a maximum-length, non-overlapping cover of the
    request. It is vocabulary-free and applies to any Chinese business domain.
    """
    values = row.values() if isinstance(row, dict) else row
    row_text = " ".join(str(value or "") for value in values).casefold()
    matched: list[str] = []
    coverage = 0
    for segment in CJK_TEXT.findall(str(request or "")):
        covered: set[int] = set()
        candidates: list[tuple[int, int, str]] = []
        for width in range(min(6, len(segment)), 2, -1):
            for start in range(0, len(segment) - width + 1):
                phrase = segment[start:start + width]
                if phrase.casefold() in row_text:
                    candidates.append((start, start + width, phrase))
        candidates.sort(key=lambda item: (-(item[1] - item[0]), item[0], item[2]))
        for start, end, phrase in candidates:
            if any(position in covered for position in range(start, end)):
                continue
            covered.update(range(start, end))
            matched.append(phrase)
            coverage += end - start
    return matched, coverage


def rows_as_objects(columns: Sequence[str], rows: Iterable[Sequence[Any]]) -> list[dict[str, Any]]:
    result = []
    for row in rows:
        result.append({str(column): bounded_text(row[index]) if index < len(row) else None for index, column in enumerate(columns)})
    return result


def source_map(contract: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        str(item.get("source_id", "")): item
        for item in contract.get("sources", [])
        if isinstance(item, dict) and str(item.get("source_id", ""))
    }


def tabular_runtime():
    candidate_dirs = [
        SCRIPT_DIR,
        SCRIPT_DIR.parents[1] / "portable-tabular-reader" / "scripts",
        SCRIPT_DIR.parents[2] / "portable-tabular-reader" / "scripts",
    ]
    for candidate in candidate_dirs:
        if candidate.is_dir() and str(candidate) not in sys.path:
            sys.path.insert(0, str(candidate))
    try:
        import query_tabular
    except ImportError as exc:  # pragma: no cover - exercised in target runtime
        raise ExecutorError("The bundled tabular runtime is unavailable") from exc
    return query_tabular


def recipe_runtime():
    """Load the bundled recipe evaluator without relying on a host installation."""
    if str(SCRIPT_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPT_DIR))
    try:
        import recipe_runtime as recipes
    except ImportError as exc:  # pragma: no cover - exercised in target runtime
        raise ExecutorError("The bundled deterministic recipe runtime is unavailable") from exc
    return recipes


def contract_summary(contract: dict[str, Any], flow: dict[str, Any]) -> dict[str, Any]:
    sources = source_map(contract)
    execution_plan = flow.get("execution_plan") if isinstance(flow.get("execution_plan"), dict) else {}
    return {
        "status": "success",
        "scenario": contract.get("scenario", {}),
        "execution_mode": flow.get("execution_mode", "evidence_pipeline"),
        "primary_entrypoint": "execute",
        "required_sequence": contract.get("query_policy", {}).get("required_sequence", []),
        "runtime_source_ids": contract.get("runtime_source_ids", []),
        "rule_source_ids": contract.get("rule_source_ids", []),
        "execution_plan": execution_plan,
        "sources": [
            {
                "source_id": source_id,
                "view_name": source.get("view_name"),
                "path": source.get("path"),
                "kind": source.get("kind"),
                "lifecycle": source.get("lifecycle", "runtime_input"),
                "runtime_required": source.get("runtime_required", True),
                "roles": source.get("roles", []),
                "columns": [
                    item.get("query_name") or item.get("name")
                    for table in source.get("tables", [])
                    if isinstance(table, dict)
                    for item in table.get("columns", [])
                    if isinstance(item, dict) and (item.get("query_name") or item.get("name"))
                ][:300],
            }
            for source_id, source in sources.items()
        ],
        "main_flow": flow.get("main_flow", []),
        "stages": flow.get("stages", []),
        "capabilities": {
            "rule_search": True,
            "bounded_runtime_source_search": True,
            "runtime_join_validation": True,
            "traceable_evidence_package": True,
            "semantic_decision": "agent_review_required",
        },
    }


def search_source(
    reader: Any, contract: dict[str, Any], data_root: Path, source_id: str,
    terms: Sequence[str], bindings: dict[str, str], max_rows: int,
    connection: Any | None = None, registration: dict[str, Any] | None = None,
) -> dict[str, Any]:
    terms = [str(term).strip() for term in terms if str(term).strip()]
    if connection is not None:
        source = source_map(contract).get(str(source_id))
        if source is None:
            raise ExecutorError(f"Unknown source_id: {source_id}")
        table = next((item for item in source.get("tables", []) if isinstance(item, dict)), {})
        columns = [
            str(item.get("query_name") or item.get("name") or "")
            for item in table.get("columns", [])
            if isinstance(item, dict) and str(item.get("query_name") or item.get("name") or "")
        ]
        if not columns:
            raise ExecutorError(f"Source {source_id} has no searchable columns")
        relation = reader.quote_identifier(str(source.get("view_name", "")))
        combined = "concat_ws(' ', " + ", ".join(
            f"coalesce(cast({reader.quote_identifier(column)} as varchar), '')"
            for column in columns
        ) + ")"
        predicates = " OR ".join(f"{combined} ILIKE ?" for _ in terms)
        score_expression = " + ".join(
            f"CASE WHEN {combined} ILIKE ? THEN {max(1, len(term))} ELSE 0 END"
            for term in terms
        )
        cursor = connection.execute(
            f"SELECT * FROM {relation} WHERE {predicates} ORDER BY {score_expression} DESC LIMIT {max_rows + 1}",
            [f"%{term}%" for term in terms] + [f"%{term}%" for term in terms],
        )
        raw = reader.complete_rule_payload(cursor, max_rows)
        rows = []
        for row in raw.get("rows", []):
            matched_terms, score = term_match_score(row, terms)
            rows.append({
                "row": list(row),
                "matched_terms": matched_terms,
                "score": score,
            })
        rows.sort(key=lambda item: (-int(item["score"]), -len(item["matched_terms"])))
        return {
            "source_id": source_id,
            "columns": columns,
            "rows": rows_as_objects(columns, [item["row"] for item in rows]),
            "row_scores": [
                {"row_index": index, "matched_terms": item["matched_terms"], "score": item["score"]}
                for index, item in enumerate(rows)
            ],
            "matched_terms": sorted({term for item in rows for term in item["matched_terms"]}),
            "row_count_returned": len(rows),
            "truncated": bool(raw.get("truncated")),
            "registrations": [registration] if registration else [],
            "errors": [],
        }
    by_key: dict[str, dict[str, Any]] = {}
    errors: list[str] = []
    columns: list[str] = []
    registrations: list[dict[str, Any]] = []
    used_terms: list[str] = []
    for term in terms:
        try:
            payload = reader.search_contract(contract, data_root, source_id, [term], max_rows, bindings)
        except Exception as exc:
            errors.append(f"{term}: {exc}")
            continue
        columns = [str(item) for item in payload.get("columns", [])]
        registrations = payload.get("registrations", [])
        rows = payload.get("rows", [])
        if rows:
            used_terms.append(term)
        for row in rows:
            key = row_key(columns, row)
            existing = by_key.get(key, {})
            by_key[key] = {
                "row": list(row),
                "matched_terms": sorted(set(existing.get("matched_terms", [])) | {term}),
            }
    ranked = []
    for item in by_key.values():
        matched_terms, score = term_match_score(item["row"], terms)
        ranked.append({**item, "matched_terms": matched_terms, "score": score})
    ranked.sort(key=lambda item: (-int(item["score"]), -len(item["matched_terms"])))
    return {
        "source_id": source_id,
        "columns": columns,
        "rows": rows_as_objects(columns, [item["row"] for item in ranked[:max_rows]]),
        "row_scores": [
            {"row_index": index, "matched_terms": item["matched_terms"], "score": item["score"]}
            for index, item in enumerate(ranked[:max_rows])
        ],
        "matched_terms": sorted(set(used_terms)),
        "row_count_returned": min(len(ranked), max_rows),
        "truncated": len(ranked) > max_rows,
        "registrations": registrations,
        "errors": errors,
    }


def write_artifact(
    path: Path, payload: dict[str, Any], kind: str = "scenario_evidence_package",
) -> dict[str, Any]:
    path = path.expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_bytes(encoded)
    temporary.replace(path)
    return {
        "kind": kind,
        "path": str(path),
        "format": "json",
        "sha256": hashlib.sha256(encoded).hexdigest(),
        "size_bytes": len(encoded),
    }


def _stable_result_id(payload: dict[str, Any]) -> str:
    deterministic = payload.get("deterministic_result") if isinstance(payload.get("deterministic_result"), dict) else {}
    selected_rule = payload.get("selected_rule") if isinstance(payload.get("selected_rule"), dict) else {}
    seed = {
        "recipe_id": deterministic.get("recipe_id"),
        "rule": selected_rule.get("row", {}),
        "columns": deterministic.get("columns", []),
        "rows": deterministic.get("rows", []),
        "summary": deterministic.get("summary", {}),
    }
    return "result-" + hashlib.sha256(
        json.dumps(seed, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:16]


def result_handle(payload: dict[str, Any], artifact_path: Path) -> dict[str, Any] | None:
    """Describe the only safe follow-up boundary for a completed result."""
    deterministic = payload.get("deterministic_result")
    if payload.get("status") != "completed_deterministically" or not isinstance(deterministic, dict):
        return None
    coverage = deterministic.get("coverage") if isinstance(deterministic.get("coverage"), dict) else {}
    columns = [str(item) for item in deterministic.get("columns", []) if str(item)]
    return {
        "schema_version": 1,
        "kind": "deterministic_result_handle",
        "result_id": _stable_result_id(payload),
        "artifact": str(artifact_path.expanduser().resolve()),
        "filterable_columns": columns,
        "complete_for_projection": bool(coverage.get("complete_for_all_matching_runtime_rows")),
        "continuation_command": "continue --result <scenario-evidence-package.json> --filter <field>=<value>",
        "rule": {
            "source_id": (payload.get("selected_rule") or {}).get("source_id"),
            "recipe_id": deterministic.get("recipe_id"),
        },
    }


def _continuation_filters(values: Sequence[str]) -> list[tuple[str, str]]:
    if len(values) > MAX_CONTINUATION_FILTERS:
        raise ExecutorError(f"At most {MAX_CONTINUATION_FILTERS} deterministic result filters are allowed")
    filters: list[tuple[str, str]] = []
    for raw in values:
        field, separator, value = str(raw).partition("=")
        field, value = field.strip(), value.strip()
        if not separator or not field or not value:
            raise ExecutorError("Each --filter must use field=value")
        filters.append((field, value))
    return filters


def _as_number(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def continue_deterministic_result(result_path: Path, filter_values: Sequence[str], max_rows: int) -> dict[str, Any]:
    """Project a completed result artifact without reopening source data.

    The third-party Agent may ask a follow-up only through this command.  It
    cannot switch data sources, re-resolve a rule, or invent SQL; it receives a
    filtered view of the same completed deterministic result.
    """
    payload = load_json(result_path.expanduser().resolve())
    if payload.get("status") != "completed_deterministically":
        raise ExecutorError("Continuation requires a completed deterministic result artifact")
    deterministic = payload.get("deterministic_result")
    if not isinstance(deterministic, dict):
        raise ExecutorError("Result artifact has no deterministic_result")
    coverage = deterministic.get("coverage") if isinstance(deterministic.get("coverage"), dict) else {}
    if not coverage.get("complete_for_all_matching_runtime_rows"):
        return {
            "status": "blocked_incomplete_result_projection",
            "message": "The stored result is truncated, so a follow-up filter would be incomplete. Request a persisted full result rather than querying raw sources from the Agent.",
            "result_handle": payload.get("result_handle"),
        }
    columns = [str(item) for item in deterministic.get("columns", []) if str(item)]
    filters = _continuation_filters(filter_values)
    unknown = sorted({field for field, _value in filters} - set(columns))
    if unknown:
        raise ExecutorError("Continuation filter field is not in the deterministic result: " + ", ".join(unknown))
    rows = [item for item in deterministic.get("rows", []) if isinstance(item, dict)]
    selected = [
        row for row in rows
        if all(normalized_text(value) in normalized_text(row.get(field)) for field, value in filters)
    ]
    measure = str((deterministic.get("summary") or {}).get("measure_field", ""))
    measure_sum = sum(
        number for number in (_as_number(row.get(measure)) for row in selected) if number is not None
    ) if measure else None
    group_by = [str(item) for item in deterministic.get("group_by", []) if str(item)]
    group_count = len({tuple(str(row.get(field, "")) for field in group_by) for row in selected}) if group_by else len(selected)
    projected = {
        **deterministic,
        "rows": selected[:max(1, min(int(max_rows), MAX_OUTPUT_ROWS))],
        "summary": {
            **(deterministic.get("summary") or {}),
            "matched_row_count": len(selected),
            "matched_group_count": group_count,
            "measure_sum": measure_sum,
        },
        "coverage": {
            "mode": "deterministic_result_projection",
            "complete_for_all_matching_runtime_rows": True,
            "returned_row_count": min(len(selected), max(1, min(int(max_rows), MAX_OUTPUT_ROWS))),
            "total_matched_row_count": len(selected),
            "truncated": len(selected) > max(1, min(int(max_rows), MAX_OUTPUT_ROWS)),
            "parent_result_id": (payload.get("result_handle") or {}).get("result_id", _stable_result_id(payload)),
        },
    }
    return {
        "status": "continued_deterministically",
        "result_handle": payload.get("result_handle") or result_handle(payload, result_path),
        "filters": [{"field": field, "contains": value} for field, value in filters],
        "deterministic_result": projected,
        "next_step": {
            "action": "report_projected_deterministic_result",
            "do_not_repeat": ["execute", "search-rules", "query", "raw_source_file_read"],
            "completion": "Report the filtered result as a projection of the named completed result handle.",
        },
    }


def blocked_execution_payload(
    request: str,
    terms: Sequence[str],
    flow: dict[str, Any],
    source_ids: Sequence[str],
    message: str,
) -> dict[str, Any]:
    """Return a stable, non-retryable evidence-gap result for runtime blockers."""
    return {
        "status": "blocked_missing_or_incompatible_sources",
        "request": request,
        "search_terms": list(terms),
        "rule_selection": "none",
        "selected_rule": None,
        "preflight": {
            "status": "blocked",
            "requested_source_ids": sorted({str(item) for item in source_ids if str(item)}),
            "registrations": [],
            "errors": [{"message": message}],
            "message": "Runtime evidence is unavailable or incompatible; no business conclusion was produced.",
        },
        "rule_matches": [],
        "data_matches": [],
        "join_validations": [],
        "flow": {
            "execution_mode": flow.get("execution_mode", "evidence_pipeline"),
            "main_flow": flow.get("main_flow", []),
            "stages": flow.get("stages", []),
            "controls": flow.get("controls", []),
        },
        "evidence_policy": {
            "all_rows_bounded": True,
            "source_provenance_included": True,
            "raw_source_files_not_loaded": True,
            "requires_agent_judgment": True,
            "semantic_decision_boundary": "Evidence is incomplete; the Agent must resolve the reported source gap before applying the rule.",
        },
        "next_action": "resolve_blocking_evidence_gap",
    }


def source_columns(contract: dict[str, Any], source_id: str) -> list[str]:
    source = source_map(contract).get(str(source_id), {})
    table = next((item for item in source.get("tables", []) if isinstance(item, dict)), {})
    return [
        str(item.get("query_name") or item.get("name") or "")
        for item in table.get("columns", [])
        if isinstance(item, dict) and str(item.get("query_name") or item.get("name") or "")
    ]


def source_column_metadata(contract: dict[str, Any], source_id: str) -> list[dict[str, Any]]:
    source = source_map(contract).get(str(source_id), {})
    metadata: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for table in source.get("tables", []) if isinstance(source.get("tables"), list) else []:
        if not isinstance(table, dict):
            continue
        table_name = str(table.get("sheet_or_table") or table.get("table") or table.get("table_name") or "")
        for column in table.get("columns", []) if isinstance(table.get("columns"), list) else []:
            if not isinstance(column, dict):
                continue
            name = str(column.get("query_name") or column.get("name") or "")
            if not name or (table_name, name) in seen:
                continue
            seen.add((table_name, name))
            metadata.append({
                "source_id": str(source_id),
                "table": table_name,
                "column": name,
                "kind": str(column.get("kind", "other")),
                "semantic_role": infer_column_semantic_role(column),
            })
    return metadata


def source_semantic_columns(contract: dict[str, Any], source_id: str) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    for item in source_column_metadata(contract, source_id):
        result.setdefault(str(item["semantic_role"]), []).append(str(item["column"]))
    return result


def derive_rule_constraints(
    selected_rule: dict[str, Any] | None, contract: dict[str, Any], request: str = "",
) -> dict[str, Any]:
    """Extract bounded, inspectable rule signals without pretending to adjudicate them."""
    row = selected_rule.get("row", {}) if isinstance(selected_rule, dict) else {}
    # Example/reference columns describe historical evidence, not a second
    # normative predicate for the current request.
    normative_text = " ".join(
        str(value or "")
        for key, value in row.items()
        if not any(marker in normalized_text(key) for marker in ("example", "sample", "reference", "示例", "参考"))
    )
    request_text = str(request or "")
    pattern = re.compile(
        r"(\u4e0d\u8d85\u8fc7|\u8d85\u8fc7|\u4e0d\u5c11\u4e8e|\u81f3\u5c11|\u4e0d\u4f4e\u4e8e|\u5927\u4e8e\u7b49\u4e8e|\u5927\u4e8e|\u5c0f\u4e8e\u7b49\u4e8e|\u5c0f\u4e8e|at\s+most|at\s+least|greater\s+than|less\s+than|equal\s+to)"
        r"\s*([0-9]+(?:\.[0-9]+)?)\s*(\u5929|\u65e5|\u6b21|\u5143|\u4e2a|\u4ef6|\u9879|days?|times?|items?|units?|percent|%)?"
        , re.IGNORECASE
    )
    def parse_constraints(text: str) -> list[dict[str, Any]]:
        return [
            {
                "operator": match.group(1),
                "threshold": float(match.group(2)) if "." in match.group(2) else int(match.group(2)),
                "unit": match.group(3) or "",
                "source_text": match.group(0),
            }
            for match in pattern.finditer(text)
        ]
    normative_constraints = parse_constraints(normative_text)
    request_constraints = parse_constraints(request_text)
    constraints = normative_constraints or request_constraints
    field_candidates: dict[str, list[dict[str, Any]]] = {}
    for source_id in contract.get("runtime_source_ids", []):
        semantic_columns = source_semantic_columns(contract, str(source_id))
        for group, columns in semantic_columns.items():
            if columns:
                field_candidates.setdefault(group, []).append({
                    "source_id": str(source_id),
                    "columns": columns[:40],
                })
    rule_row_id = None
    if selected_rule:
        rule_source_id = str(selected_rule.get("source_id", ""))
        identifier_columns = source_semantic_columns(contract, rule_source_id).get("identifier", [])
        rule_row_id = next((row.get(column) for column in identifier_columns if row.get(column) not in {None, ""}), None)
    application_plan = {
        "subject_match": {
            "fields": field_candidates.get("subject", []),
            "instruction": "Confirm the subject/entity named by the request and any explicit scope before evaluating the governing record.",
        },
        "temporal_or_quantitative": {
            "fields": field_candidates.get("temporal", []) + field_candidates.get("measure", []),
            "constraints": constraints,
            "instruction": "Compare runtime values using the stated operator and unit; do not treat a missing value as compliant.",
        },
        "categorical_or_state_condition": {
            "fields": field_candidates.get("selector", []) + field_candidates.get("decision", []),
            "instruction": "Evaluate categorical/state conditions from the complete linked record and mark uncertainty when the required fact is absent.",
        },
        "decision_boundary": "A positive business conclusion requires a target record plus evidence that a governing condition is or is not met; absence of evidence is not a conclusion.",
    }
    return {
        "constraints": constraints,
        "normative_constraints": normative_constraints,
        "request_constraints": request_constraints,
        "constraint_source": "selected_rule_row" if normative_constraints else "user_request_fallback",
        "rule_row_id": rule_row_id,
        "field_candidates": field_candidates,
        "application_plan": application_plan,
        "semantic_decision_required": True,
        "boundary": "Normative constraints exclude reference-example columns; request constraints are shown separately. The Agent must still apply the rule to candidate evidence.",
    }


def result_contract(flow: dict[str, Any]) -> dict[str, Any]:
    stages = [item for item in flow.get("stages", []) if isinstance(item, dict)]
    final_stage = stages[-1] if stages else {}
    declared = flow.get("output_contract") if isinstance(flow.get("output_contract"), dict) else {}
    templates = flow.get("design_time_output_templates") if isinstance(flow.get("design_time_output_templates"), list) else []
    template_columns = [
        str(column)
        for template in templates if isinstance(template, dict)
        for column in template.get("output_columns", [])
        if str(column)
    ]
    template_column_set = set(template_columns)
    semantic_by_column = {
        str(item.get("column", "")): str(item.get("semantic_role", "attribute"))
        for template in templates if isinstance(template, dict)
        for item in template.get("column_semantics", []) if isinstance(item, dict) and str(item.get("column", ""))
    }
    role_columns = lambda roles: [
        column for column in template_columns
        if semantic_by_column.get(column, "attribute") in roles
    ]
    field_mapping = {
        "business_conclusion": role_columns({"decision", "selector"}),
        "decision_reason": role_columns({"narrative"}),
        "scope_or_measure": role_columns({"measure", "temporal"}),
        "rule_provenance": [],
        "data_provenance": role_columns({"identifier"}),
    }
    if not field_mapping["rule_provenance"]:
        field_mapping["rule_provenance"] = [
            column for column in template_columns
            if semantic_by_column.get(column) == "identifier"
        ][:4]
    return {
        "stage_id": final_stage.get("stage_id", final_stage.get("id")),
        "output_node_ids": final_stage.get("output_node_ids", []),
        "output_names": [
            str(final_stage.get("outcome", "")) or str(final_stage.get("name", ""))
        ] if final_stage else [],
        "required_fields": [
            "business_conclusion",
            "decision_reason",
            "scope_or_measure",
            "rule_provenance",
            "data_provenance",
            "coverage",
            "uncertainties",
        ],
        "field_mapping": field_mapping,
        "design_time_templates": [
            {
                "template_id": item.get("template_id"),
                "name": item.get("name"),
                "format": item.get("format"),
                "columns": item.get("output_columns", []),
                "runtime_required": False,
            }
            for item in templates if isinstance(item, dict)
        ],
        "declared_output_contract": declared,
        "full_result_export": "Use export-contract only when the user explicitly requests all rows; keep stdout bounded.",
    }


def execution_steps(
    flow: dict[str, Any], status: str, rule_selection: str,
    candidate_evidence: dict[str, Any], result: dict[str, Any],
) -> list[dict[str, Any]]:
    """Expose the inferred sequence as state, rather than prompt memory."""
    stages = [item for item in flow.get("stages", []) if isinstance(item, dict)]
    stage_by_id = {
        str(item.get("stage_id", item.get("id", ""))): item
        for item in stages if str(item.get("stage_id", item.get("id", "")))
    }
    execution_plan = flow.get("execution_plan") if isinstance(flow.get("execution_plan"), dict) else {}
    plan_steps = [item for item in execution_plan.get("steps", []) if isinstance(item, dict)]
    evidence_status = str(candidate_evidence.get("status", "no_candidate_row"))
    steps: list[dict[str, Any]] = []

    def add(stage_id: str, name: str, state: str, action: str, detail: str) -> None:
        steps.append({
            "order": len(steps) + 1,
            "stage_id": stage_id,
            "name": name,
            "state": state,
            "action": action,
            "detail": detail,
        })

    add(
        "runtime.scope",
        "resolve_request_scope",
        "completed" if result.get("preflight", {}).get("status") == "success" else "blocked",
        "use_declared_runtime_sources",
        "The request was normalized into bounded search anchors and the declared source contract was checked.",
    )
    if rule_selection == "unique":
        rule_state = "completed"
        rule_detail = "Exactly one complete rule row was selected; preserve the full row as rule provenance."
    elif rule_selection == "not_applicable":
        rule_state = "not_required"
        rule_detail = "This capability model has no structured governing-record source; apply the accepted flow and declared controls to runtime evidence."
    elif rule_selection == "multiple":
        rule_state = "blocked"
        rule_detail = "More than one rule candidate remains; narrow by explicit user conditions before adjudication."
    else:
        rule_state = "blocked"
        rule_detail = "No complete rule row matched the request; do not infer a policy from partial text."
    add("runtime.rule_resolution", "locate_complete_governing_record", rule_state, "use_selected_record", rule_detail)

    add(
        "runtime.optional_enrichment",
        "optional_enrichment_check",
        "not_required" if rule_selection == "unique" else "pending",
        "activate_only_if_declared",
        "No optional enrichment is activated unless the accepted capability model declares it necessary and available.",
    )

    evidence_state = "blocked" if rule_selection not in {"unique", "not_applicable"} else (
        "blocked" if evidence_status == "no_candidate_row" else "completed"
    )
    add(
        "runtime.evidence_materialization",
        "read_and_validate_runtime_evidence",
        evidence_state,
        "use_bounded_evidence_and_validated_lineage",
        "Runtime rows are selected from the ranked source set and linked only through accepted key sets.",
    )

    if rule_selection not in {"unique", "not_applicable"}:
        adjudicate_state = "blocked"
        adjudicate_detail = "Business evaluation cannot start until one complete governing record is selected."
    elif evidence_status == "no_candidate_row":
        adjudicate_state = "blocked"
        adjudicate_detail = "The governing record is selected, but no candidate runtime record matched the request anchors."
    else:
        adjudicate_state = "ready_for_agent"
        adjudicate_detail = "Candidate rows and validated relationship paths are materialized for one bounded business-evaluation pass."
    for stage_id in [
        str(item) for item in flow.get("main_flow", []) if str(item)
    ] or [
        stage_id for stage_id in stage_by_id
    ]:
        stage = stage_by_id.get(stage_id, {})
        add(
            stage_id,
            str(stage.get("name") or stage.get("stage_type") or "apply_accepted_stage"),
            adjudicate_state,
            "apply_accepted_stage_once",
            str(stage.get("objective") or stage.get("outcome") or adjudicate_detail),
        )
    if not stages and not plan_steps:
        add("runtime.business_evaluation", "apply_accepted_procedure", adjudicate_state, "apply_procedure_once", adjudicate_detail)
    add(
        "runtime.output",
        "materialize_auditable_result",
        "pending_agent_conclusion" if adjudicate_state == "ready_for_agent" else "blocked",
        "write_result_contract",
        "Return the conclusion, reason, scope or measure, rule provenance, data provenance, coverage and uncertainties.",
    )
    return steps


def next_step_for_agent(
    status: str, candidate_evidence: dict[str, Any], output_contract: dict[str, Any],
    rule_selection: str = "unique",
) -> dict[str, Any]:
    if status == "ready_for_agent_judgment":
        coverage = candidate_evidence.get("coverage") if isinstance(candidate_evidence.get("coverage"), dict) else {}
        complete = bool(coverage.get("complete_for_all_matching_runtime_rows", False))
        has_governing_record = rule_selection == "unique"
        return {
            "id": "adjudicate_and_deliver",
            "actor": "third_party_agent",
            "action": "apply_rule_to_candidate_evidence_once" if has_governing_record else "apply_accepted_procedure_to_candidate_evidence_once",
            "read": (["selected_rule", "rule_constraints"] if has_governing_record else []) + ["candidate_evidence.records", "execution_plan", "execution_steps", "result_contract"],
            "required_fields": output_contract.get("required_fields", []),
            "query_allowed_only_if": "candidate_evidence or its instruction explicitly reports a missing field or unresolved relationship",
            "do_not_repeat": ["execute", "search-rules", "describe", "raw_source_file_read"],
            "coverage": "exhaustive_bounded_result" if complete else "bounded_preview_requires_scope_disclosure",
            "completion": (
                "Produce the business conclusion and auditable evidence references in the result contract."
                if complete else
                "Produce the conclusion for the bounded preview, explicitly disclose truncation, and do not claim the preview is exhaustive."
            ),
        }
    return {
        "id": "resolve_evidence_gap",
        "actor": "third_party_agent_or_human",
        "action": "report_and_resolve_blocker",
        "read": ["preflight", "rule_matches", "data_matches", "execution_steps"],
        "query_allowed_only_if": "the blocker identifies a concrete missing source, field, or validated link",
        "do_not_repeat": ["blind_retry_same_request", "guess_rule", "raw_source_file_read"],
        "completion": "Obtain the named missing input or ask the user for a disambiguating condition.",
    }


def deterministic_execution_payload(
    request: str,
    terms: Sequence[str],
    flow: dict[str, Any],
    preflight: dict[str, Any],
    rule_matches: list[dict[str, Any]],
    selected_rule: dict[str, Any],
    rule_constraints: dict[str, Any],
    result: dict[str, Any],
) -> dict[str, Any]:
    """Return one terminal handoff when a reviewed recipe evaluated the rule."""
    output_contract = result_contract(flow)
    recipe_id = str(result.get("recipe_id", ""))
    coverage = result.get("coverage", {}) if isinstance(result.get("coverage"), dict) else {}
    rule_row = selected_rule.get("row", {}) if isinstance(selected_rule.get("row"), dict) else {}
    # Ranking grams are an internal recall mechanism.  A completed recipe has
    # already selected its governing record, so exposing scores, candidate
    # lists, and request n-grams only bloats the Agent context and encourages
    # an unnecessary second interpretation.  Keep the complete selected rule
    # as provenance and send only the result-directed execution chain.
    rule_provenance = {
        "source_id": str(selected_rule.get("source_id", "")),
        "row": rule_row,
    }
    execution_steps = [
        {
            "order": 1,
            "stage_id": "runtime.scope",
            "name": "resolve_request_scope",
            "state": "completed",
            "action": "use_declared_runtime_sources",
            "detail": "The selected rule and only the recipe-declared runtime sources were checked.",
        },
        {
            "order": 2,
            "stage_id": "runtime.rule_resolution",
            "name": "locate_complete_governing_record",
            "state": "completed",
            "action": "use_selected_record",
            "detail": "Exactly one complete governing rule record was selected.",
        },
        {
            "order": 3,
            "stage_id": "runtime.deterministic_recipe",
            "name": "execute_reviewed_recipe",
            "state": "completed",
            "action": "execute_parameterized_recipe_once",
            "detail": f"Reviewed recipe {recipe_id or '<unnamed>'} produced the result directly from validated runtime rows.",
        },
        {
            "order": 4,
            "stage_id": "runtime.output",
            "name": "materialize_auditable_result",
            "state": "completed",
            "action": "deliver_deterministic_result",
            "detail": "The Agent must report this result and its coverage without re-querying the source data.",
        },
    ]
    return {
        "status": "completed_deterministically",
        "request": request,
        "rule_selection": "unique",
        "selected_rule": rule_provenance,
        "preflight": preflight,
        "candidate_evidence": {
            "status": "deterministic_result",
            "instruction": "Use deterministic_result as the business fact. Do not issue a second rule search or source query for this completed request.",
            "coverage": coverage,
        },
        "deterministic_result": result,
        "execution_steps": execution_steps,
        "result_contract": output_contract,
        "evidence_policy": {
            "all_rows_bounded": True,
            "source_provenance_included": True,
            "raw_source_files_not_loaded": True,
            "requires_agent_judgment": False,
            "semantic_decision_boundary": "The reviewed recipe is the deterministic decision boundary for this governing rule.",
        },
        "next_action": "deliver_deterministic_result",
        "next_step": {
            "id": "deliver_deterministic_result",
            "actor": "third_party_agent",
            "action": "report_completed_result_without_reexecution",
            "read": ["selected_rule", "deterministic_result", "result_contract", "execution_steps"],
            "query_allowed_only_if": "never for a filter or summary that can be computed from deterministic_result; only a named result field absent from the package may justify a new request",
            "do_not_repeat": ["execute", "search-rules", "query", "raw_source_file_read"],
            "coverage": "exhaustive_result" if coverage.get("complete_for_all_matching_runtime_rows") else "bounded_result_requires_truncation_disclosure",
            "completion": "Report the deterministic rows, counts, amount summary, rule provenance and coverage boundary.",
        },
    }


def uncompiled_rule_family_payload(
    request: str,
    flow: dict[str, Any],
    preflight: dict[str, Any],
    selected_rule: dict[str, Any],
) -> dict[str, Any]:
    """Fail closed when a complete rule has no reviewed executable family.

    The old behavior returned a bounded preview and silently delegated the
    actual rule interpretation to the Agent.  Rule-family authoring belongs
    in the workbench, where its inputs and output policy can be reviewed.
    """
    return {
        "status": "blocked_uncompiled_rule_family",
        "request": request,
        "rule_selection": "unique",
        "selected_rule": {
            "source_id": selected_rule.get("source_id"),
            "row": selected_rule.get("row", {}),
        },
        "preflight": preflight,
        "candidate_evidence": {
            "status": "not_started",
            "instruction": "No runtime data was opened because the selected rule has no reviewed deterministic rule-family template.",
        },
        "execution_steps": [
            {
                "order": 1,
                "stage_id": "runtime.rule_resolution",
                "state": "completed",
                "action": "use_selected_record",
            },
            {
                "order": 2,
                "stage_id": "workbench.rule_family_review",
                "state": "blocked",
                "action": "review_and_compile_rule_family",
                "detail": "Review an explicit source field, grouping key, predicate derivation and result projection before runtime execution.",
            },
        ],
        "evidence_policy": {
            "raw_source_files_not_loaded": True,
            "requires_agent_judgment": False,
            "semantic_decision_boundary": "An Agent may not infer executable predicates from this rule at runtime.",
        },
        "next_action": "review_and_compile_rule_family",
        "next_step": {
            "actor": "distillation_workbench_reviewer",
            "action": "create_reviewed_rule_family_template",
            "do_not_repeat": ["execute", "search-rules", "query", "raw_source_file_read"],
            "completion": "Publish one reviewed declarative template or explicitly mark the rule family as human-only.",
        },
    }


def batch_record_groups(
    reader: Any,
    contract: dict[str, Any],
    connection: Any,
    data_source_ids: Sequence[str],
    anchor_source_id: str,
    anchor_candidates: Sequence[dict[str, Any]],
    links: Sequence[dict[str, Any]],
    max_rows: int,
) -> list[dict[str, Any]]:
    """Build per-anchor traces with one bounded query per validated link."""
    groups: list[dict[str, Any]] = []
    for record_index, anchor_item in enumerate(anchor_candidates, start=1):
        groups.append({
            "record_id": f"{anchor_source_id}:{record_index}",
            "anchor": anchor_item,
            "sources": [{
                "source_id": anchor_source_id,
                "columns": source_columns(contract, anchor_source_id),
                "rows": [anchor_item.get("row", {})],
                "filter": {"kind": "request_anchor_match", "matched_terms": anchor_item.get("matched_terms", [])},
            }],
            "validated_paths": [],
            "errors": [],
            "_rows_by_source": {anchor_source_id: [anchor_item.get("row", {})]},
        })
    source_queue = [anchor_source_id]
    processed_links: set[str] = set()
    runtime_ids = {str(item) for item in data_source_ids}
    while source_queue:
        current_id = source_queue.pop(0)
        for link in links:
            link_id = str(link.get("link_id", ""))
            if not link_id or link_id in processed_links:
                continue
            left = str(link.get("source_id", ""))
            right = str(link.get("target_id", ""))
            if current_id not in {left, right}:
                continue
            target_id = right if current_id == left else left
            if target_id not in runtime_ids:
                processed_links.add(link_id)
                continue
            try:
                key_set = reader.contract_key_set(link, 0)
                key_pairs = key_set.get("key_pairs", [])
                target_fields = [
                    str(pair.get("target_field") if current_id == left else pair.get("source_field"))
                    for pair in key_pairs
                ]
                key_to_records: dict[tuple[str, ...], set[str]] = {}
                conditions: list[str] = []
                params: list[Any] = []
                for group in groups:
                    for row in group.get("_rows_by_source", {}).get(current_id, [])[:1]:
                        values: list[Any] = []
                        predicates: list[str] = []
                        valid = True
                        for pair in key_pairs:
                            current_field = pair.get("source_field") if current_id == left else pair.get("target_field")
                            target_field = pair.get("target_field") if current_id == left else pair.get("source_field")
                            value = row.get(str(current_field)) if isinstance(row, dict) else None
                            if value is None or str(value) == "":
                                valid = False
                                break
                            values.append(value)
                            predicates.append(f"{reader.quote_identifier(str(target_field))} = ?")
                        if not valid or not predicates:
                            continue
                        key = tuple(str(value) for value in values)
                        key_to_records.setdefault(key, set()).add(str(group["record_id"]))
                        conditions.append("(" + " AND ".join(predicates) + ")")
                        params.extend(values)
                if not conditions:
                    processed_links.add(link_id)
                    continue
                relation = reader.quote_identifier(str(source_map(contract)[target_id].get("view_name", "")))
                query_limit = min(MAX_OUTPUT_ROWS, max_rows * max(1, len(groups)) + 1)
                cursor = connection.execute(
                    f"SELECT * FROM {relation} WHERE {' OR '.join(conditions)} LIMIT {query_limit}",
                    params,
                )
                payload = reader.cursor_payload(cursor, query_limit - 1)
                rows = rows_as_objects(payload.get("columns", []), payload.get("rows", []))
                rows_by_record: dict[str, list[dict[str, Any]]] = {}
                for row in rows:
                    key = tuple(str(row.get(field)) for field in target_fields)
                    for record_id in key_to_records.get(key, set()):
                        rows_by_record.setdefault(record_id, []).append(row)
                for group in groups:
                    record_id = str(group["record_id"])
                    matched_rows = rows_by_record.get(record_id, [])
                    if not matched_rows:
                        continue
                    if target_id in group.get("_rows_by_source", {}):
                        # A second validated path may reach the same source;
                        # keep the first materialized rows and retain only one
                        # evidence copy per source in this record group.
                        continue
                    group["sources"].append({
                        "source_id": target_id,
                        "columns": payload.get("columns", []),
                        "rows": matched_rows[:max_rows],
                        "row_count_returned": len(matched_rows),
                        "truncated": bool(payload.get("truncated")),
                        "filter": {"kind": "validated_link", "link_id": link_id, "from_source_id": current_id},
                    })
                    group["_rows_by_source"].setdefault(target_id, []).extend(matched_rows[:max_rows])
                    group["validated_paths"].append({
                        "link_id": link_id,
                        "from_source_id": current_id,
                        "to_source_id": target_id,
                        "key_set": key_set,
                        "row_count_returned": len(matched_rows),
                        "truncated": bool(payload.get("truncated")),
                    })
                if any(target_id in group.get("_rows_by_source", {}) for group in groups):
                    source_queue.append(target_id)
                processed_links.add(link_id)
            except Exception as exc:
                for group in groups:
                    group["errors"].append({"link_id": link_id, "source_id": target_id, "message": str(exc)})
                processed_links.add(link_id)
    for group in groups:
        group.pop("_rows_by_source", None)
    return groups


def evidence_coverage(
    data_matches: Sequence[dict[str, Any]], candidate_evidence: dict[str, Any],
) -> dict[str, Any]:
    truncated_sources = [
        str(item.get("source_id", ""))
        for item in data_matches
        if isinstance(item, dict) and bool(item.get("truncated"))
    ]
    record_groups = candidate_evidence.get("records") if isinstance(candidate_evidence.get("records"), list) else []
    errors = candidate_evidence.get("errors") if isinstance(candidate_evidence.get("errors"), list) else []
    complete = not truncated_sources and not errors and bool(record_groups)
    return {
        "mode": "bounded_preview",
        "complete_for_all_matching_runtime_rows": complete,
        "record_group_count": len(record_groups),
        "record_group_limit": 20,
        "truncated_source_ids": truncated_sources,
        "errors": errors,
        "agent_boundary": (
            "This is an auditable bounded preview, not an exhaustive all-row result; do not claim that no other matching records exist."
            if not complete else
            "All selected runtime matches fit within the bounded result and no truncation was reported."
        ),
    }


def materialize_candidate_evidence(
    reader: Any,
    contract: dict[str, Any],
    connection: Any,
    data_source_ids: Sequence[str],
    data_matches: Sequence[dict[str, Any]],
    join_results: Sequence[dict[str, Any]],
    max_rows: int,
) -> dict[str, Any]:
    """Follow validated links from the highest-scoring entity row to concrete records."""
    candidates: list[dict[str, Any]] = []
    for result in data_matches:
        source_id = str(result.get("source_id", ""))
        rows = result.get("rows", []) if isinstance(result.get("rows"), list) else []
        for score in result.get("row_scores", []) if isinstance(result.get("row_scores"), list) else []:
            index = int(score.get("row_index", -1))
            if index < 0 or index >= len(rows) or int(score.get("score", 0)) <= 0:
                continue
            candidates.append({
                "source_id": source_id,
                "row": rows[index],
                "matched_terms": score.get("matched_terms", []),
                "score": int(score.get("score", 0)),
            })
    if not candidates:
        return {
            "status": "no_candidate_row",
            "anchor": None,
            "sources": [],
            "validated_paths": [],
            "message": "The selected rule was found, but no runtime source row matched the request anchors.",
        }

    candidates.sort(key=lambda item: (-item["score"], -len(item["matched_terms"])))
    anchor = candidates[0]
    anchor_source_id = str(anchor["source_id"])
    collected: dict[str, dict[str, Any]] = {
        anchor_source_id: {
            "source_id": anchor_source_id,
            "columns": source_columns(contract, anchor_source_id),
            "rows": [item["row"] for item in candidates if item["source_id"] == anchor_source_id][: min(20, max_rows)],
            "filter": {
                "kind": "request_anchor_match",
                "matched_terms": sorted({term for item in candidates if item["source_id"] == anchor_source_id for term in item["matched_terms"]}),
            },
        }
    }
    validated_ids = {
        str(item.get("link_id", ""))
        for item in join_results
        if isinstance(item, dict) and item.get("status") == "success"
    }
    links = [
        item for item in contract.get("links", [])
        if isinstance(item, dict) and str(item.get("link_id", "")) in validated_ids
    ]
    frontier = [anchor_source_id]
    path_records: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    anchor_candidates = [
        item for item in candidates if str(item.get("source_id")) == anchor_source_id
    ][: min(20, max_rows)]
    record_groups = batch_record_groups(
        reader, contract, connection, data_source_ids, anchor_source_id,
        anchor_candidates, links, max_rows,
    )
    grouped_paths = [
        path
        for group in record_groups
        for path in group.get("validated_paths", [])
    ]
    grouped_errors = [
        error
        for group in record_groups
        for error in group.get("errors", [])
    ]
    aggregate_sources: dict[str, dict[str, Any]] = {}
    aggregate_seen: dict[str, set[str]] = {}
    for group in record_groups:
        for source in group.get("sources", []):
            source_id = str(source.get("source_id", ""))
            if not source_id:
                continue
            aggregate_sources.setdefault(source_id, {
                "source_id": source_id,
                "columns": source.get("columns", []),
                "rows": [],
                "filter": {"kind": "aggregate_preview", "record_groups": len(record_groups)},
            })
            aggregate_seen.setdefault(source_id, set())
            for row in source.get("rows", []) if isinstance(source.get("rows"), list) else []:
                row_digest = row_key(source.get("columns", []), list(row.values())) if isinstance(row, dict) else str(row)
                if row_digest in aggregate_seen[source_id] or len(aggregate_sources[source_id]["rows"]) >= max_rows:
                    continue
                aggregate_seen[source_id].add(row_digest)
                aggregate_sources[source_id]["rows"].append(row)
    for source_id, source in aggregate_sources.items():
        source["row_count_returned"] = len(source["rows"])
        source["truncated"] = len(aggregate_seen.get(source_id, set())) > len(source["rows"])
    return {
        "status": "ready" if len(aggregate_sources) > 1 else "anchor_only",
        "anchor": {
            "source_id": anchor_source_id,
            "matched_terms": anchor.get("matched_terms", []),
            "score": anchor.get("score", 0),
            "row": anchor.get("row", {}),
        },
        "sources": list(aggregate_sources.values()),
        "records": record_groups,
        "validated_paths": grouped_paths,
        "errors": [*errors, *grouped_errors],
        "bounded": True,
        "instruction": "Apply the complete selected rule to each record group. Within a group, the anchor row and linked visit/settlement rows are the only corresponding evidence; do not join aggregate previews by guesswork. Do not restart rule search unless this package reports a missing field.",
    }


def execute_evidence_pipeline(
    request: str, data_root: Path, contract: dict[str, Any], flow: dict[str, Any],
    bindings: dict[str, str], max_rows: int, validate_joins: bool,
) -> dict[str, Any]:
    """Execute a capability whose accepted flow has no structured rule source.

    A scenario is not invalid merely because its governing logic is encoded in
    the accepted flow, controls, documents, or an Agent procedure.  In that
    case the executor still performs source binding, bounded retrieval and
    lineage validation, then hands the evidence to the Agent once.
    """
    reader = tabular_runtime()
    sources = source_map(contract)
    runtime_ids = [
        source_id for source_id in contract.get("runtime_source_ids", [])
        if str(source_id) in sources and sources[str(source_id)].get("kind") == "tabular"
    ]
    terms = unique_terms(request)
    if not runtime_ids:
        return blocked_execution_payload(
            request, terms, flow, [],
            "No tabular runtime source is declared for this evidence pipeline.",
        )
    try:
        connection, registrations = reader.open_contract_connection(contract, data_root, set(runtime_ids), bindings)
    except Exception as exc:
        return blocked_execution_payload(request, terms, flow, runtime_ids, str(exc))
    registration_by_id = {
        str(item.get("source_id", "")): item for item in registrations if isinstance(item, dict)
    }
    preflight = {
        "status": "success",
        "requested_source_ids": sorted(runtime_ids),
        "registrations": registrations,
        "errors": [],
        "message": "Referenced runtime sources are available and schema-compatible.",
    }
    try:
        data_matches = [
            search_source(
                reader, contract, data_root, source_id, terms, bindings, max_rows,
                connection, registration_by_id.get(str(source_id)),
            )
            for source_id in runtime_ids
        ]
        matched_data_ids = {
            str(item.get("source_id")) for item in data_matches
            if int(item.get("row_count_returned", 0)) > 0
        }
        join_results: list[dict[str, Any]] = []
        if validate_joins:
            for link in contract.get("links", []):
                if not isinstance(link, dict) or link.get("runtime_eligible") is False:
                    continue
                left, right = str(link.get("source_id", "")), str(link.get("target_id", ""))
                if left not in matched_data_ids or right not in matched_data_ids:
                    continue
                link_id = str(link.get("link_id", ""))
                if not link_id:
                    continue
                try:
                    join_results.append(reader.validate_contract_link(contract, data_root, link_id, 0, bindings))
                except Exception as exc:
                    join_results.append({"status": "blocked", "link_id": link_id, "message": str(exc)})
        has_errors = bool(preflight.get("errors")) or any(item.get("errors") for item in data_matches)
        status = "blocked_missing_or_incompatible_sources" if has_errors else (
            "ready_for_agent_judgment" if any(int(item.get("row_count_returned", 0)) > 0 for item in data_matches)
            else "blocked_runtime_evidence_not_found"
        )
        candidate_evidence = {
            "status": "no_candidate_row",
            "anchor": None,
            "sources": [],
            "validated_paths": [],
            "message": "No runtime record matched the request anchors.",
            "governing_record_required": False,
        }
        if status == "ready_for_agent_judgment":
            candidate_evidence = materialize_candidate_evidence(
                reader, contract, connection, runtime_ids, data_matches, join_results, max_rows,
            )
            candidate_evidence["governing_record_required"] = False
            candidate_evidence["coverage"] = evidence_coverage(data_matches, candidate_evidence)
        output_contract = result_contract(flow)
        result_shell = {"preflight": preflight}
        steps = execution_steps(flow, status, "not_applicable", candidate_evidence, result_shell)
        next_step = next_step_for_agent(status, candidate_evidence, output_contract, "not_applicable")
        return {
            "status": status,
            "request": request,
            "search_terms": terms,
            "rule_selection": "not_applicable",
            "selected_rule": None,
            "rule_constraints": {"constraints": [], "semantic_decision_required": True},
            "source_column_metadata": {
                str(source_id): source_column_metadata(contract, str(source_id))
                for source_id in runtime_ids
            },
            "preflight": preflight,
            "rule_matches": [],
            "data_matches": data_matches,
            "join_validations": join_results,
            "candidate_evidence": candidate_evidence,
            "execution_steps": steps,
            "result_contract": output_contract,
            "flow": {
                "execution_mode": flow.get("execution_mode", "evidence_pipeline"),
                "main_flow": flow.get("main_flow", []),
                "stages": flow.get("stages", []),
                "controls": flow.get("controls", []),
            },
            "execution_plan": flow.get("execution_plan", {}),
            "evidence_policy": {
                "all_rows_bounded": True,
                "source_provenance_included": True,
                "raw_source_files_not_loaded": True,
                "requires_agent_judgment": True,
                "semantic_decision_boundary": "The accepted flow and controls are applied by the Agent to the bounded evidence; no absent policy fact is inferred.",
            },
            "next_action": "apply_accepted_procedure_to_bounded_evidence" if status == "ready_for_agent_judgment" else "resolve_blocking_evidence_gap",
            "next_step": next_step,
        }
    finally:
        connection.close()
    while frontier:
        current_id = frontier.pop(0)
        current_rows = collected.get(current_id, {}).get("rows", [])
        if not current_rows:
            continue
        for link in links:
            left = str(link.get("source_id", ""))
            right = str(link.get("target_id", ""))
            if current_id not in {left, right}:
                continue
            target_id = right if current_id == left else left
            if target_id not in set(str(item) for item in data_source_ids) or target_id in collected:
                continue
            try:
                key_set = reader.contract_key_set(link, 0)
                conditions: list[str] = []
                params: list[Any] = []
                for row in current_rows[: min(20, max_rows)]:
                    predicates: list[str] = []
                    for pair in key_set.get("key_pairs", []):
                        current_field = pair.get("source_field") if current_id == left else pair.get("target_field")
                        target_field = pair.get("target_field") if current_id == left else pair.get("source_field")
                        value = row.get(str(current_field)) if isinstance(row, dict) else None
                        if value is None or str(value) == "":
                            continue
                        predicates.append(f"{reader.quote_identifier(str(target_field))} = ?")
                        params.append(value)
                    if predicates:
                        conditions.append("(" + " AND ".join(predicates) + ")")
                if not conditions:
                    continue
                relation = reader.quote_identifier(str(source_map(contract)[target_id].get("view_name", "")))
                cursor = connection.execute(
                    f"SELECT * FROM {relation} WHERE {' OR '.join(conditions)} LIMIT {max_rows + 1}",
                    params,
                )
                payload = reader.cursor_payload(cursor, max_rows)
                rows = rows_as_objects(payload.get("columns", []), payload.get("rows", []))
                collected[target_id] = {
                    "source_id": target_id,
                    "columns": payload.get("columns", []),
                    "rows": rows,
                    "row_count_returned": payload.get("row_count_returned", len(rows)),
                    "truncated": bool(payload.get("truncated")),
                    "filter": {"kind": "validated_link", "link_id": link.get("link_id"), "from_source_id": current_id},
                }
                path_records.append({
                    "link_id": link.get("link_id"),
                    "from_source_id": current_id,
                    "to_source_id": target_id,
                    "key_set": key_set,
                    "row_count_returned": len(rows),
                    "truncated": bool(payload.get("truncated")),
                })
                frontier.append(target_id)
            except Exception as exc:
                errors.append({"link_id": link.get("link_id"), "source_id": target_id, "message": str(exc)})
    # Keep an exact per-record trace as well as the aggregate previews above.
    # Aggregate rows are useful for discovery but are unsafe for adjudication:
    # an Agent must never guess which visit/settlement belongs to a detail row.
    record_groups: list[dict[str, Any]] = []
    anchor_candidates = [
        item for item in candidates if str(item.get("source_id")) == anchor_source_id
    ][: min(20, max_rows)]
    for record_index, anchor_item in enumerate(anchor_candidates, start=1):
        record_sources: dict[str, dict[str, Any]] = {
            anchor_source_id: {
                "source_id": anchor_source_id,
                "columns": source_columns(contract, anchor_source_id),
                "rows": [anchor_item.get("row", {})],
                "filter": {"kind": "request_anchor_match", "matched_terms": anchor_item.get("matched_terms", [])},
            }
        }
        record_frontier = [anchor_source_id]
        record_paths: list[dict[str, Any]] = []
        record_errors: list[dict[str, Any]] = []
        while record_frontier:
            current_id = record_frontier.pop(0)
            current_rows = record_sources.get(current_id, {}).get("rows", [])
            for link in links:
                left = str(link.get("source_id", ""))
                right = str(link.get("target_id", ""))
                if current_id not in {left, right}:
                    continue
                target_id = right if current_id == left else left
                if target_id not in set(str(item) for item in data_source_ids) or target_id in record_sources:
                    continue
                try:
                    key_set = reader.contract_key_set(link, 0)
                    conditions: list[str] = []
                    params: list[Any] = []
                    for row in current_rows[:1]:
                        predicates: list[str] = []
                        for pair in key_set.get("key_pairs", []):
                            current_field = pair.get("source_field") if current_id == left else pair.get("target_field")
                            target_field = pair.get("target_field") if current_id == left else pair.get("source_field")
                            value = row.get(str(current_field)) if isinstance(row, dict) else None
                            if value is None or str(value) == "":
                                continue
                            predicates.append(f"{reader.quote_identifier(str(target_field))} = ?")
                            params.append(value)
                        if predicates:
                            conditions.append("(" + " AND ".join(predicates) + ")")
                    if not conditions:
                        continue
                    relation = reader.quote_identifier(str(source_map(contract)[target_id].get("view_name", "")))
                    cursor = connection.execute(
                        f"SELECT * FROM {relation} WHERE {' OR '.join(conditions)} LIMIT {max_rows + 1}",
                        params,
                    )
                    payload = reader.cursor_payload(cursor, max_rows)
                    rows = rows_as_objects(payload.get("columns", []), payload.get("rows", []))
                    record_sources[target_id] = {
                        "source_id": target_id,
                        "columns": payload.get("columns", []),
                        "rows": rows,
                        "row_count_returned": payload.get("row_count_returned", len(rows)),
                        "truncated": bool(payload.get("truncated")),
                        "filter": {"kind": "validated_link", "link_id": link.get("link_id"), "from_source_id": current_id},
                    }
                    record_paths.append({
                        "link_id": link.get("link_id"),
                        "from_source_id": current_id,
                        "to_source_id": target_id,
                        "key_set": key_set,
                        "row_count_returned": len(rows),
                        "truncated": bool(payload.get("truncated")),
                    })
                    record_frontier.append(target_id)
                except Exception as exc:
                    record_errors.append({"link_id": link.get("link_id"), "source_id": target_id, "message": str(exc)})
        record_groups.append({
            "record_id": f"{anchor_source_id}:{record_index}",
            "anchor": anchor_item,
            "sources": list(record_sources.values()),
            "validated_paths": record_paths,
            "errors": record_errors,
        })
    return {
        "status": "ready" if len(collected) > 1 else "anchor_only",
        "anchor": {
            "source_id": anchor_source_id,
            "matched_terms": anchor.get("matched_terms", []),
            "score": anchor.get("score", 0),
            "row": anchor.get("row", {}),
        },
        "sources": list(collected.values()),
        "records": record_groups,
        "validated_paths": path_records,
        "errors": errors,
        "bounded": True,
        "instruction": "Apply the complete selected rule to each record group. Within a group, the anchor row and linked visit/settlement rows are the only corresponding evidence; do not join aggregate previews by guesswork. Do not restart rule search unless this package reports a missing field.",
    }


def execute(
    request: str, data_root: Path, contract: dict[str, Any], flow: dict[str, Any],
    bindings: dict[str, str], max_rows: int, validate_joins: bool,
    compiled_recipes: Sequence[dict[str, Any]] = (),
) -> dict[str, Any]:
    if not str(request or "").strip():
        raise ExecutorError("A non-empty business request is required")
    reader = tabular_runtime()
    sources = source_map(contract)
    rule_ids = [
        source_id for source_id in contract.get("rule_source_ids", [])
        if str(source_id) in sources and sources[str(source_id)].get("kind") == "tabular"
    ]
    declared_rule_ids = [str(item) for item in contract.get("rule_source_ids", []) if str(item)]
    runtime_ids = [
        source_id for source_id in contract.get("runtime_source_ids", [])
        if str(source_id) in sources and sources[str(source_id)].get("kind") == "tabular"
    ]
    terms = unique_terms(request)
    if not rule_ids and not declared_rule_ids:
        return execute_evidence_pipeline(
            request, data_root, contract, flow, bindings, max_rows, validate_joins
        )
    if not rule_ids:
        return {
            "status": "blocked_unstructured_governing_source",
            "request": request,
            "search_terms": terms,
            "message": "A governing source is declared, but it is not a structured tabular source supported by this primary executor. Use the declared document/knowledge foundation and preserve provenance.",
            "requires_agent_judgment": True,
            "next_action": "use_declared_non_tabular_governing_source",
        }
    # Resolve the rule against the smallest possible runtime scope first. A
    # large source must not be opened merely to discover that the rule is
    # ambiguous; the previous multi-stage pattern paid that cost repeatedly.
    selected_ids = sorted(set(rule_ids))
    try:
        connection, registrations = reader.open_contract_connection(
            contract, data_root, set(selected_ids), bindings
        )
    except Exception as exc:
        return blocked_execution_payload(
            request,
            terms,
            flow,
            selected_ids,
            str(exc),
        )
    registration_by_id = {
        str(item.get("source_id", "")): item for item in registrations if isinstance(item, dict)
    }
    try:
        preflight = {
            "status": "success",
            "requested_source_ids": sorted(selected_ids),
            "registrations": registrations,
            "errors": [],
            "message": "Referenced runtime sources are available and schema-compatible.",
        }
        rule_matches = [
            search_source(
                reader, contract, data_root, source_id, terms, bindings, max_rows,
                connection, registration_by_id.get(str(source_id)),
            )
            for source_id in rule_ids
        ]
        matched_rule_rows = sum(int(item.get("row_count_returned", 0)) for item in rule_matches)
    finally:
        connection.close()
    ranked_rule_candidates = sorted(
        [
            {
                "source_id": result.get("source_id"),
                "row": result.get("rows", [])[score.get("row_index", -1)]
                if 0 <= int(score.get("row_index", -1)) < len(result.get("rows", [])) else {},
                "matched_terms": score.get("matched_terms", []),
                "score": int(score.get("score", 0)),
                "request_anchor_matches": request_anchor_score(
                    result.get("rows", [])[score.get("row_index", -1)]
                    if 0 <= int(score.get("row_index", -1)) < len(result.get("rows", [])) else {},
                    request,
                )[0],
                "request_anchor_coverage": request_anchor_score(
                    result.get("rows", [])[score.get("row_index", -1)]
                    if 0 <= int(score.get("row_index", -1)) < len(result.get("rows", [])) else {},
                    request,
                )[1],
            }
            for result in rule_matches
            for score in result.get("row_scores", [])
        ],
        key=lambda item: (
            -int(item["request_anchor_coverage"]),
            -len(item["request_anchor_matches"]),
            -int(item["score"]),
            -len(item["matched_terms"]),
        ),
    )
    top_score = int(ranked_rule_candidates[0]["score"]) if ranked_rule_candidates else 0
    second_score = int(ranked_rule_candidates[1]["score"]) if len(ranked_rule_candidates) > 1 else 0
    top_terms = len(ranked_rule_candidates[0].get("matched_terms", [])) if ranked_rule_candidates else 0
    top_anchor_coverage = int(ranked_rule_candidates[0].get("request_anchor_coverage", 0)) if ranked_rule_candidates else 0
    second_anchor_coverage = int(ranked_rule_candidates[1].get("request_anchor_coverage", 0)) if len(ranked_rule_candidates) > 1 else 0
    top_anchor_count = len(ranked_rule_candidates[0].get("request_anchor_matches", [])) if ranked_rule_candidates else 0
    anchor_margin = max(2, (second_anchor_coverage * 15 + 99) // 100)
    uniquely_anchored = bool(
        ranked_rule_candidates
        and top_anchor_count >= 2
        and top_anchor_coverage >= second_anchor_coverage + anchor_margin
    )
    uniquely_ranked = bool(
        ranked_rule_candidates
        and (
            len(ranked_rule_candidates) == 1
            or uniquely_anchored
            or (top_terms >= 2 and top_score >= max(2, second_score * 2))
        )
    )
    rule_selection = (
        "none" if matched_rule_rows == 0
        else "unique" if uniquely_ranked
        else "multiple"
    )
    selected_rule = ranked_rule_candidates[0] if rule_selection == "unique" else None
    rule_constraints = derive_rule_constraints(selected_rule, contract, request)
    if selected_rule is not None:
        recipes = recipe_runtime()
        try:
            recipe = recipes.select_recipe(compiled_recipes, selected_rule)
            required_ids = recipes.required_source_ids(recipe) if recipe is not None else set()
        except Exception as exc:
            return blocked_execution_payload(
                request,
                terms,
                flow,
                selected_ids,
                f"Compiled recipe selection could not run safely: {exc}",
            )
        if recipe is not None:
            declared_runtime_ids = set(str(item) for item in runtime_ids)
            undeclared = sorted(required_ids - declared_runtime_ids)
            if undeclared:
                return blocked_execution_payload(
                    request,
                    terms,
                    flow,
                    sorted(required_ids),
                    "Compiled recipe references non-runtime source(s): " + ", ".join(undeclared),
                )
            try:
                recipe_connection, recipe_registrations = reader.open_contract_connection(
                    contract, data_root, required_ids, bindings,
                )
            except Exception as exc:
                return blocked_execution_payload(request, terms, flow, sorted(required_ids), str(exc))
            try:
                columns_by_source = {
                    source_id: source_columns(contract, source_id)
                    for source_id in required_ids
                }
                deterministic_result = recipes.execute_recipe(
                    recipe,
                    reader,
                    contract,
                    recipe_connection,
                    columns_by_source,
                    max_rows,
                    selected_rule,
                )
            except Exception as exc:
                return blocked_execution_payload(
                    request,
                    terms,
                    flow,
                    sorted(required_ids),
                    f"Compiled recipe could not run safely: {exc}",
                )
            finally:
                recipe_connection.close()
            preflight["requested_source_ids"] = sorted(set(selected_ids) | required_ids)
            preflight["registrations"] = [*registrations, *recipe_registrations]
            preflight["message"] = "The governing rule and only the deterministic recipe sources are available and schema-compatible."
            return deterministic_execution_payload(
                request,
                terms,
                flow,
                preflight,
                rule_matches,
                selected_rule,
                rule_constraints,
                deterministic_result,
            )
        return uncompiled_rule_family_payload(
            request, flow, preflight, selected_rule,
        )
    data_matches: list[dict[str, Any]] = []
    join_results: list[dict[str, Any]] = []
    data_connection: Any | None = None
    data_ids = [source_id for source_id in runtime_ids if source_id not in rule_ids]
    if not preflight.get("errors") and rule_selection == "unique" and data_ids:
        # Search the large sources only after a complete rule is selected.
        # Use the non-overlapping anchors that actually selected this rule.
        # Request n-grams are recall aids; arbitrary lexical ordering of them
        # must not discard a confirmed entity before runtime evidence is read.
        data_terms = [
            str(term) for term in (selected_rule or {}).get("request_anchor_matches", [])
            if 3 <= len(str(term)) <= 32
        ]
        if not data_terms:
            data_terms = sorted(
                (term for term in terms if len(term) <= 32),
                key=lambda term: (-len(term), term),
            )[:3]
        data_terms = list(dict.fromkeys(data_terms))[:8]
        try:
            data_connection, data_registrations = reader.open_contract_connection(
                contract, data_root, set(data_ids), bindings
            )
        except Exception as exc:
            preflight["status"] = "blocked"
            preflight["message"] = "Some runtime evidence sources are unavailable or incompatible."
            preflight["errors"].append({
                "source_ids": sorted({str(item) for item in data_ids}),
                "message": str(exc),
            })
        else:
            data_registration_by_id = {
                str(item.get("source_id", "")): item
                for item in data_registrations if isinstance(item, dict)
            }
            try:
                data_matches = [
                    search_source(
                        reader, contract, data_root, source_id, data_terms, bindings, max_rows,
                        data_connection, data_registration_by_id.get(str(source_id)),
                    )
                    for source_id in data_ids
                ]
            except Exception:
                data_connection.close()
                data_connection = None
                raise
            preflight["requested_source_ids"] = sorted(set(selected_ids + data_ids))
            preflight["registrations"] = [*registrations, *data_registrations]
    matched_data_ids = {
        str(item.get("source_id"))
        for item in data_matches
        if int(item.get("row_count_returned", 0)) > 0
    }
    if validate_joins and rule_selection == "unique":
        for link in contract.get("links", []):
            if not isinstance(link, dict) or link.get("runtime_eligible") is False:
                continue
            left = str(link.get("source_id", ""))
            right = str(link.get("target_id", ""))
            # Only one side needs to match the request. The other side can be
            # contextual evidence reachable through a declared, validated key;
            # requiring a text hit on both sides drops legitimate linked rows.
            if (
                left not in set(data_ids)
                or right not in set(data_ids)
                or not ({left, right} & matched_data_ids)
            ):
                continue
            link_id = str(link.get("link_id", ""))
            if not link_id:
                continue
            try:
                key_set = reader.contract_key_set(link, 0)
                key_pairs = key_set.get("key_pairs", []) if isinstance(key_set, dict) else []
                left_columns = set(source_columns(contract, left))
                right_columns = set(source_columns(contract, right))
                schema_compatible = bool(key_pairs) and all(
                    str(pair.get("source_field", "")) in left_columns
                    and str(pair.get("target_field", "")) in right_columns
                    for pair in key_pairs if isinstance(pair, dict)
                )
                if not schema_compatible:
                    raise ExecutorError("Declared link has no schema-compatible key pair")
                # Full-table join validation is prohibitively expensive for
                # large workbooks and is unnecessary here. The materializer
                # below performs the decisive bounded lookup from each matched
                # anchor row using this declared key set.
                join_results.append({
                    "status": "success",
                    "link_id": link_id,
                    "key_set": key_set,
                    "validation_mode": "declared_key_schema_then_bounded_anchor_lookup",
                    "message": "Key fields are schema-compatible; target rows are verified only for matched anchor records.",
                })
            except Exception as exc:
                join_results.append({"status": "blocked", "link_id": link_id, "message": str(exc)})
    has_errors = bool(preflight.get("errors")) or any(item.get("errors") for item in [*rule_matches, *data_matches])
    if has_errors:
        status = "blocked_missing_or_incompatible_sources"
    elif rule_selection == "none":
        status = "blocked_rule_not_found"
    elif rule_selection == "multiple":
        status = "blocked_rule_selection_required"
    else:
        status = "ready_for_agent_judgment"
    candidate_evidence = {
        "status": "no_candidate_row",
        "anchor": None,
        "sources": [],
        "validated_paths": [],
        "message": "Candidate evidence was not materialized because rule selection or runtime source availability was incomplete.",
    }
    if status == "ready_for_agent_judgment" and data_connection is not None:
        candidate_evidence = materialize_candidate_evidence(
            reader, contract, data_connection, data_ids, data_matches, join_results, max_rows,
        )
        candidate_evidence["coverage"] = evidence_coverage(data_matches, candidate_evidence)
    if data_connection is not None:
        data_connection.close()
        data_connection = None
    output_contract = result_contract(flow)
    result_shell = {"preflight": preflight}
    steps = execution_steps(flow, status, rule_selection, candidate_evidence, result_shell)
    next_step = next_step_for_agent(status, candidate_evidence, output_contract, rule_selection)
    return {
        "status": status,
        "request": request,
        "search_terms": terms,
        "rule_selection": rule_selection,
        "selected_rule": selected_rule,
        "rule_constraints": rule_constraints,
        "source_column_metadata": {
            str(source_id): source_column_metadata(contract, str(source_id))
            for source_id in runtime_ids
        },
        "preflight": preflight,
        "rule_matches": rule_matches,
        "data_matches": data_matches,
        "join_validations": join_results,
        "candidate_evidence": candidate_evidence,
        "execution_steps": steps,
        "result_contract": output_contract,
        "flow": {
            "execution_mode": flow.get("execution_mode", "evidence_pipeline"),
            "main_flow": flow.get("main_flow", []),
            "stages": flow.get("stages", []),
            "controls": flow.get("controls", []),
        },
        "execution_plan": flow.get("execution_plan", {}),
        "evidence_policy": {
            "all_rows_bounded": True,
            "source_provenance_included": True,
            "raw_source_files_not_loaded": True,
            "requires_agent_judgment": True,
            "semantic_decision_boundary": "This executable prepares evidence; the Agent must apply the complete selected rule and report uncertainty.",
        },
        "next_action": (
            "apply_complete_rule_to_bounded_evidence"
            if status == "ready_for_agent_judgment" else "resolve_blocking_evidence_gap"
        ),
        "next_step": next_step,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--contract", default=str(DEFAULT_CONTRACT))
    parser.add_argument("--flow-contract", default=str(DEFAULT_FLOW))
    parser.add_argument("--recipes", default=str(DEFAULT_RECIPES))
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("describe")
    search = commands.add_parser("search-rules")
    search.add_argument("--data-root", required=True)
    search.add_argument("--source-id", action="append", default=[])
    search.add_argument("--term", action="append", required=True)
    search.add_argument("--bind", action="append", default=[])
    search.add_argument("--max-rows", type=int, default=20)
    execute_parser = commands.add_parser("execute", aliases=["audit", "run", "produce"])
    execute_parser.add_argument("--request", required=True)
    execute_parser.add_argument("--data-root", required=True)
    execute_parser.add_argument("--bind", action="append", default=[])
    execute_parser.add_argument("--max-rows", type=int, default=50)
    execute_parser.add_argument("--output", default="")
    execute_parser.add_argument("--no-join-validation", action="store_true")
    continuation = commands.add_parser("continue")
    continuation.add_argument("--result", required=True)
    continuation.add_argument("--filter", action="append", default=[])
    continuation.add_argument("--max-rows", type=int, default=200)
    query = commands.add_parser("query")
    query.add_argument("--data-root", required=True)
    query.add_argument("--sql", required=True)
    query.add_argument("--link-id", action="append", default=[])
    query.add_argument("--bind", action="append", default=[])
    query.add_argument("--max-rows", type=int, default=200)
    return parser


def run(argv: Sequence[str] | None = None) -> dict[str, Any]:
    args = build_parser().parse_args(argv)
    contract = load_json(resolve_contract(args.contract, DEFAULT_CONTRACT))
    flow = load_json(resolve_contract(args.flow_contract, DEFAULT_FLOW))
    compiled_recipes = recipe_runtime().load_recipes(resolve_contract(args.recipes, DEFAULT_RECIPES))
    command = str(args.command)
    if command == "describe":
        return contract_summary(contract, flow)
    if command == "continue":
        return continue_deterministic_result(Path(args.result), args.filter, args.max_rows)
    reader = tabular_runtime()
    data_root = Path(args.data_root).expanduser().resolve()
    if not data_root.is_dir():
        raise ExecutorError(f"Data root does not exist: {data_root}")
    bindings = reader.parse_source_bindings(args.bind)
    limit = max(1, min(int(args.max_rows), MAX_OUTPUT_ROWS))
    if command == "search-rules":
        source_ids = [str(item) for item in args.source_id if str(item)] or [
            str(item) for item in contract.get("rule_source_ids", []) if str(item)
        ]
        if not source_ids:
            raise ExecutorError("No rule source is declared")
        return {
            "status": "success",
            "mode": "complete_matching_rows",
            "search_terms": [str(item).strip() for item in args.term if str(item).strip()],
            "results": [
                search_source(reader, contract, data_root, source_id, args.term, bindings, limit)
                for source_id in source_ids
            ],
        }
    if command == "query":
        return reader.query_contract(contract, data_root, args.sql, limit, args.link_id, bindings)
    payload = execute(
        args.request, data_root, contract, flow, bindings, limit,
        validate_joins=not bool(args.no_join_validation), compiled_recipes=compiled_recipes,
    )
    if args.output:
        artifact_path = Path(args.output)
        handle = result_handle(payload, artifact_path)
        if handle is not None:
            payload["result_handle"] = handle
        payload["artifact"] = write_artifact(artifact_path, payload)
        handoff_path = artifact_path.with_name(artifact_path.stem + ".agent.json")
        payload["agent_handoff"] = write_artifact(
            handoff_path,
            agent_handoff_payload(payload),
            kind="scenario_agent_handoff",
        )
    return payload


def compact_stdout_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Keep the Agent-facing command response small; the artifact remains complete."""
    selected = payload.get("selected_rule") if isinstance(payload.get("selected_rule"), dict) else None
    candidate = payload.get("candidate_evidence") if isinstance(payload.get("candidate_evidence"), dict) else {}
    sources = candidate.get("sources") if isinstance(candidate.get("sources"), list) else []
    deterministic = payload.get("deterministic_result") if isinstance(payload.get("deterministic_result"), dict) else {}
    return {
        "status": payload.get("status", "success"),
        "rule_selection": payload.get("rule_selection"),
        "selected_rule": {
            "source_id": selected.get("source_id"),
            "matched_terms": selected.get("matched_terms", []),
            "score": selected.get("score", 0),
            "row": selected.get("row", {}),
        } if selected else None,
        "candidate_evidence": {
            "status": candidate.get("status"),
            "anchor_source_id": (candidate.get("anchor") or {}).get("source_id") if isinstance(candidate.get("anchor"), dict) else None,
            "source_count": len(sources),
            "record_count": len(candidate.get("records", [])) if isinstance(candidate.get("records"), list) else 0,
            "row_counts": {
                str(item.get("source_id")): len(item.get("rows", []))
                for item in sources if isinstance(item, dict)
            },
            "validated_path_count": len(candidate.get("validated_paths", [])) if isinstance(candidate.get("validated_paths"), list) else 0,
            "coverage": candidate.get("coverage"),
            "message": candidate.get("message") or candidate.get("instruction"),
        },
        "deterministic_result": {
            "recipe_id": deterministic.get("recipe_id"),
            "summary": deterministic.get("summary", {}),
            "coverage": deterministic.get("coverage", {}),
            "row_count_returned": len(deterministic.get("rows", [])) if isinstance(deterministic.get("rows"), list) else 0,
        } if deterministic else None,
        "execution_steps": payload.get("execution_steps", []),
        "next_action": payload.get("next_action"),
        "next_step": payload.get("next_step"),
        "artifact": payload.get("artifact"),
        "agent_handoff": payload.get("agent_handoff"),
        "result_handle": payload.get("result_handle"),
    }


def agent_handoff_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Project only decision-grade evidence into the file an Agent should read."""
    candidate = payload.get("candidate_evidence") if isinstance(payload.get("candidate_evidence"), dict) else {}
    deterministic = payload.get("deterministic_result") if isinstance(payload.get("deterministic_result"), dict) else None

    def project_source(source: dict[str, Any]) -> dict[str, Any]:
        columns = [str(item) for item in source.get("columns", []) if str(item)]
        metadata = {
            str(item.get("column", "")): str(item.get("semantic_role", "attribute"))
            for item in payload.get("source_column_metadata", {}).get(str(source.get("source_id", "")), [])
            if isinstance(item, dict) and str(item.get("column", ""))
        }
        preferred_roles = {"identifier", "subject", "selector", "narrative", "decision", "measure", "temporal"}
        selected = [column for column in columns if metadata.get(column, "attribute") in preferred_roles][:40]
        if not selected:
            selected = columns[:20]
        rows = []
        for row in source.get("rows", []) if isinstance(source.get("rows"), list) else []:
            if not isinstance(row, dict):
                continue
            rows.append({column: row.get(column) for column in selected})
        return {
            "source_id": source.get("source_id"),
            "rows": rows,
            "selected_columns": selected,
            "omitted_column_count": max(0, len(columns) - len(selected)),
            "row_count_returned": source.get("row_count_returned", len(rows)),
            "filter": source.get("filter", {}),
            "validated_link": source.get("filter", {}).get("link_id") if isinstance(source.get("filter"), dict) else None,
        }

    records = []
    for group in candidate.get("records", []) if isinstance(candidate.get("records"), list) else []:
        if not isinstance(group, dict):
            continue
        records.append({
            "record_id": group.get("record_id"),
            "matched_terms": (group.get("anchor") or {}).get("matched_terms", []) if isinstance(group.get("anchor"), dict) else [],
            "score": (group.get("anchor") or {}).get("score", 0) if isinstance(group.get("anchor"), dict) else 0,
            "sources": [project_source(source) for source in group.get("sources", []) if isinstance(source, dict)],
            "validated_paths": group.get("validated_paths", []),
            "errors": group.get("errors", []),
        })
    return {
        "status": payload.get("status"),
        "request": payload.get("request"),
        "rule_selection": payload.get("rule_selection"),
        "selected_rule": payload.get("selected_rule"),
        "rule_constraints": payload.get("rule_constraints"),
        "source_column_metadata": payload.get("source_column_metadata", {}),
        "execution_plan": payload.get("execution_plan", {}),
        "candidate_evidence": {
            "status": candidate.get("status"),
            "anchor": candidate.get("anchor"),
            "records": records,
            "coverage": candidate.get("coverage"),
            "instruction": candidate.get("instruction"),
        },
        "deterministic_result": deterministic,
        "result_handle": payload.get("result_handle"),
        "execution_steps": payload.get("execution_steps", []),
        "result_contract": payload.get("result_contract", {}),
        "next_step": payload.get("next_step"),
        "source_of_truth": "This handoff is the Agent-facing projection. The sibling scenario_evidence_package is the full bounded audit trace.",
    }


def main(argv: Sequence[str] | None = None) -> int:
    try:
        payload = run(argv)
        code = 0
    except Exception as exc:
        payload = {"status": "error", "message": str(exc)}
        code = 2
    encoded_payload = compact_stdout_payload(payload) if payload.get("artifact") else payload
    # Command output is a transport boundary.  Keep it ASCII JSON so a host
    # that incorrectly decodes child streams with a legacy Windows code page
    # still receives valid JSON.  Full UTF-8 evidence stays in --output files.
    encoded = json.dumps(encoded_payload, ensure_ascii=True, indent=2)
    if len(encoded) > MAX_OUTPUT_CHARS:
        encoded = json.dumps({
            "status": payload.get("status", "success"),
            "message": "Evidence package exceeded the stdout budget; use the artifact written by --output.",
            "artifact": payload.get("artifact"),
        }, ensure_ascii=True, indent=2)
    print(encoded)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
