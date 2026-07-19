#!/usr/bin/env python3
"""Build and validate one scenario-level relationship graph from bounded evidence.

The progressive engine remains a low-level evidence probe. This module distills its
results plus document statements and table structure into bounded evidence cards.
An Agent synthesizes semantic claims from those cards, and this module validates
that the result is one connected, evidence-backed main chain with attached branches.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
from collections import Counter, defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence

from progressive_engine import (
    ColumnMeta,
    ProgressiveAnalyzer,
    TableMeta,
    header_semantic,
    iter_document_segments,
    iter_table_rows,
)


SCHEMA_VERSION = 1
MAX_SNIPPET = 320
MAX_CARD_STATEMENT = 480
MAX_EVIDENCE_PAGE = 20
MAX_BRIEF_CARDS = 40
MAX_BRIEF_STATEMENTS = 10
MAX_SCENARIO_NODES = 10
MAX_SCENARIO_EDGES = 14
MAX_SCENARIO_BRANCHES = 3
NODE_TYPES = {
    "trigger", "actor", "input", "activity", "object", "rule", "decision",
    "state", "system", "output",
}
EDGE_TYPES = {
    "triggers", "consumes", "produces", "transforms", "governed_by", "precedes",
    "depends_on", "branches_to", "updates", "returns_to", "references", "performed_by",
    "feeds", "joins_with", "governs", "derives",
}
FLOW_EDGE_TYPES = {
    "triggers", "consumes", "produces", "transforms", "precedes", "depends_on",
    "branches_to", "updates", "returns_to", "feeds", "derives",
}
ORDER_EDGE_TYPES = {"triggers", "precedes", "branches_to", "returns_to"}
EDGE_ENDPOINT_TYPES: dict[str, tuple[set[str], set[str]]] = {
    "consumes": ({"activity", "decision", "system"}, {"input", "object", "state"}),
    "produces": ({"activity", "decision", "system"}, {"output", "object", "state"}),
    "governed_by": ({"activity", "decision", "system"}, {"rule"}),
    "performed_by": ({"activity", "decision"}, {"actor"}),
    "feeds": (
        {"trigger", "input", "object", "state", "system"},
        {"activity", "decision", "system", "object", "output"},
    ),
    "joins_with": ({"input", "object", "state"}, {"input", "object", "state"}),
    "governs": ({"rule"}, {"activity", "decision", "system", "object", "output"}),
    "derives": (
        {"input", "object", "activity", "decision", "system", "state"},
        {"object", "state", "output"},
    ),
}
RELATION_MARKERS = (
    "requires", "required by", "depends on", "based on", "according to", "produces",
    "generates", "submits", "approves", "rejects", "triggers", "before", "after",
    "if ", "then", "uses", "contains", "belongs to", "results in", "maps to", "sends",
    "receives", "updates", "returns", "validates", "transforms", "flows to", "followed by",
    "依赖", "需要", "依据", "根据", "生成", "产生", "提交", "审批", "审核", "拒绝",
    "触发", "之前", "之后", "如果", "使用", "包含", "属于", "导致", "映射",
    "发送", "接收", "更新", "返回", "校验", "验证", "转换", "流转", "进入", "输出",
    "定位", "选择", "决定", "调用", "获取",
    "不得", "禁止", "必须", "应当", "不可", "重复收费", "同时收取", "对应", "违规",
    "异常", "判断", "筛查", "核查", "匹配", "must", "shall", "cannot", "may not",
)
SEQUENCE_MARKERS = (
    "triggers", "before", "after", "then", "followed by", "flows to", "returns",
    "触发", "之前", "之后", "随后", "然后", "流转", "进入", "返回",
)
BRANCH_MARKERS = (
    "if ", "otherwise", "else", "when", "unless", "approve", "reject",
    "如果", "否则", "当", "除非", "审批", "审核", "拒绝", "通过", "不通过",
)
HEADER_ROLES: dict[str, tuple[str, ...]] = {
    "actor": (
        "actor", "user", "customer", "patient", "employee", "owner", "operator", "provider",
        "person", "member", "organization", "department", "staff", "角色", "用户", "客户",
        "患者", "人员", "职工", "操作人", "经办人", "机构", "组织", "部门", "医生", "员工",
    ),
    "time": (
        "date", "time", "year", "month", "day", "created", "updated", "start", "end",
        "日期", "时间", "年度", "月份", "创建", "更新", "开始", "结束", "发生时间",
    ),
    "state": (
        "status", "state", "result", "outcome", "flag", "enabled", "valid", "状态", "结果",
        "结论", "标志", "是否", "有效", "阶段", "进度",
    ),
    "input": (
        "input", "request", "application", "source", "form", "payload", "origin", "输入",
        "请求", "申请", "来源", "表单", "原始", "入参", "材料",
    ),
    "output": (
        "output", "response", "report", "receipt", "notice", "输出", "响应", "报告", "回执",
        "通知", "清单", "结果",
    ),
    "rule": (
        "rule", "policy", "condition", "threshold", "limit", "ratio", "standard", "config",
        "规则", "政策", "条件", "阈值", "限额", "比例", "标准", "配置", "口径",
    ),
    "decision": (
        "approve", "approval", "audit", "review", "reject", "decision", "verify", "check",
        "审批", "审核", "复核", "拒绝", "判定", "校验", "检查", "通过",
    ),
    "measure": (
        "amount", "price", "quantity", "count", "rate", "score", "total", "cost", "fee",
        "金额", "价格", "数量", "次数", "比例", "分值", "总额", "成本", "费用",
    ),
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def atomic_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def print_agent_json(payload: Any, *, stream: Any = None) -> None:
    """Emit tool-facing JSON without spending model context on indentation."""
    print(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        file=stream or sys.stdout,
    )


def compact_text(value: Any, limit: int) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text[:limit] + ("..." if len(text) > limit else "")


def normalize_name(value: Any) -> str:
    return re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", str(value or "").casefold())


def stable_id(prefix: str, *parts: Any) -> str:
    payload = json.dumps(parts, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return prefix + hashlib.sha1(payload.encode("utf-8")).hexdigest()[:12]


def make_card(
    kind: str,
    strength: str,
    statement: str,
    sources: Sequence[dict[str, str]],
    facts: dict[str, Any] | None = None,
    snippet: str = "",
) -> dict[str, Any]:
    clean_sources = [
        {"file": str(item.get("file", "")), "locator": str(item.get("locator", ""))}
        for item in sources
    ]
    clean_facts = facts or {}
    clean_statement = compact_text(statement, MAX_CARD_STATEMENT)
    clean_snippet = compact_text(snippet, MAX_SNIPPET)
    return {
        "id": stable_id("E-", kind, clean_sources, clean_facts, clean_statement, clean_snippet),
        "kind": kind,
        "strength": strength,
        "statement": clean_statement,
        "sources": clean_sources,
        "facts": clean_facts,
        "snippet": clean_snippet,
    }


def classify_header(header: str) -> str:
    normalized = normalize_name(header)
    scores = {
        role: sum(1 for marker in markers if normalize_name(marker) in normalized)
        for role, markers in HEADER_ROLES.items()
    }
    role, score = max(scores.items(), key=lambda item: item[1])
    return role if score else "object"


def table_role(role_counts: Counter[str], file_name: str, sheet_name: str) -> str:
    text = normalize_name(file_name + " " + sheet_name)
    if any(marker in text for marker in ("rule", "policy", "规则", "政策", "配置")):
        return "rule_or_policy_material"
    if role_counts["decision"]:
        return "decision_or_validation_record"
    if role_counts["output"] and role_counts["state"]:
        return "result_or_outcome_record"
    if role_counts["measure"] and role_counts["state"]:
        return "transaction_or_outcome_record"
    if role_counts["time"] and (role_counts["actor"] or role_counts["state"]):
        return "event_or_activity_record"
    if role_counts["input"]:
        return "input_or_request_material"
    if role_counts["rule"] >= 3:
        return "rule_or_policy_material"
    if role_counts["measure"]:
        return "measurement_or_transaction_record"
    return "business_object_record"


def table_cards(field_result: dict[str, Any]) -> list[dict[str, Any]]:
    cards: list[dict[str, Any]] = []
    for file_info in field_result.get("files", []):
        path = str(file_info.get("path", ""))
        tables = file_info.get("tables", [])
        cards.append(make_card(
            "file_structure",
            "contextual",
            f"{path} is a {file_info.get('kind', 'unknown')} source with {len(tables)} table(s).",
            [{"file": path, "locator": "file"}],
            {
                "extension": file_info.get("extension", ""),
                "size_bytes": file_info.get("size", 0),
                "table_count": len(tables),
                "inventory_status": file_info.get("inventory_status", "ok"),
            },
        ))
        for table in tables:
            role_columns: dict[str, list[str]] = defaultdict(list)
            for column in table.get("columns", []):
                role_columns[classify_header(str(column.get("name", "")))].append(str(column.get("name", "")))
            role_counts = Counter({role: len(names) for role, names in role_columns.items()})
            locator = f"table:{table.get('table_name', '')};header"
            role = table_role(role_counts, path, str(table.get("table_name", "")))
            bounded_columns: dict[str, list[str]] = {}
            remaining_column_budget = 40
            for key, values in sorted(role_columns.items()):
                if not values or remaining_column_budget <= 0:
                    continue
                selected = values[:min(8, remaining_column_budget)]
                bounded_columns[key] = selected
                remaining_column_budget -= len(selected)
            omitted = max(0, int(table.get("column_count", 0)) - sum(len(v) for v in bounded_columns.values()))
            cards.append(make_card(
                "table_schema",
                "structural",
                f"{path} / {table.get('table_name', '')} structurally resembles {role}; column roles are grouped without reading data rows.",
                [{"file": path, "locator": locator}],
                {
                    "table": table.get("table_name", ""),
                    "estimated_rows": table.get("row_count"),
                    "column_count": table.get("column_count", 0),
                    "inferred_material_role": role,
                    "columns_by_role": bounded_columns,
                    "omitted_column_count": omitted,
                },
            ))
            active_roles = [name for name, count in role_counts.items() if count]
            if len(active_roles) >= 2:
                cards.append(make_card(
                    "table_process_signal",
                    "structural",
                    f"{path} / {table.get('table_name', '')} co-locates business roles {', '.join(sorted(active_roles))}; this supports a material-level relationship but does not by itself determine chronology.",
                    [{"file": path, "locator": locator}],
                    {
                        "table": table.get("table_name", ""),
                        "co_located_roles": sorted(active_roles),
                        "inferred_material_role": role,
                    },
                ))
    return cards


def field_relationship_cards(field_result: dict[str, Any]) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str], dict[str, Any]] = {}
    for relation in field_result.get("relations", []):
        if relation.get("verdict") != "confirmed":
            continue
        source = str(relation.get("source", ""))
        target = str(relation.get("target", ""))
        source_column = str(relation.get("source_column", ""))
        target_column = str(relation.get("target_column", ""))
        source_normalized = normalize_name(source_column)
        target_normalized = normalize_name(target_column)
        source_kind, source_base = header_semantic(source_column)
        target_kind, target_base = header_semantic(target_column)
        same_header = bool(source_normalized and source_normalized == target_normalized)
        same_base = bool(
            source_base and target_base and source_base == target_base
            and source_kind == target_kind and source_kind != "other"
        )
        explicit_duplicate = relation.get("type") == "exact_duplicate"
        if not (same_header or same_base or explicit_duplicate):
            continue
        evidence = relation.get("evidence", [])
        if evidence:
            first = evidence[0]
            sources = [
                {"file": source, "locator": str(first.get("source_locator", ""))},
                {"file": target, "locator": str(first.get("target_locator", ""))},
            ]
        else:
            sources = [
                {"file": source, "locator": f"column:{source_column}"},
                {"file": target, "locator": f"column:{target_column}"},
            ]
        group = groups.setdefault((source, target), {
            "sources": sources,
            "correspondences": {},
            "relation_ids": [],
            "evidence_count": 0,
            "confidence": 0.0,
        })
        key = (source_column, target_column)
        candidate = {
            "source_field": source_column,
            "target_field": target_column,
            "alignment": "same_header" if same_header else "same_semantic_base" if same_base else "whole_file_duplicate",
            "confidence": relation.get("confidence", 0),
            "evidence_count": relation.get("evidence_count", 0),
        }
        existing = group["correspondences"].get(key)
        if existing is None or candidate["confidence"] > existing["confidence"]:
            group["correspondences"][key] = candidate
        group["relation_ids"].append(relation.get("id", ""))
        group["evidence_count"] += int(relation.get("evidence_count", 0))
        group["confidence"] = max(group["confidence"], float(relation.get("confidence", 0)))

    cards: list[dict[str, Any]] = []
    for (source, target), group in sorted(groups.items()):
        correspondences = sorted(
            group["correspondences"].values(),
            key=lambda item: (-item["confidence"], item["source_field"], item["target_field"]),
        )
        cards.append(make_card(
            "field_relationship",
            "direct",
            f"{source} and {target} have {len(correspondences)} semantically aligned field correspondence(s) with exact bounded fingerprint evidence. This proves cross-material traceability, not business order or causation.",
            group["sources"],
            {
                "source_file": source,
                "target_file": target,
                "correspondences": correspondences[:20],
                "omitted_correspondence_count": max(0, len(correspondences) - 20),
                "field_relation_ids": sorted(set(group["relation_ids"]))[:30],
                "confidence": group["confidence"],
                "evidence_count": group["evidence_count"],
            },
        ))
    return cards


def reconstruct_table(file_info: dict[str, Any], table: dict[str, Any]) -> TableMeta:
    columns = [
        ColumnMeta(
            str(column.get("name", "")),
            str(column.get("query_name", column.get("name", ""))),
            int(column.get("index", index)),
            str(column.get("kind", "other")),
            str(column.get("base", "")),
        )
        for index, column in enumerate(table.get("columns", []))
    ]
    return TableMeta(
        str(table.get("key", f"{file_info.get('path', '')}:{table.get('table_name', '')}")),
        int(table.get("file_id", file_info.get("id", 0))),
        str(file_info.get("path", "")),
        str(table.get("table_name", "")),
        table.get("row_count"),
        int(table.get("column_count", len(columns))),
        columns,
        str(table.get("engine", "openpyxl")),
    )


def table_relation_statement_cards(
    input_root: Path,
    field_result: dict[str, Any],
    cell_budget: int,
    character_budget: int,
    cards_per_file: int,
) -> tuple[list[dict[str, Any]], list[str]]:
    cards: list[dict[str, Any]] = []
    warnings: list[str] = []
    all_names = [str(item.get("path", "")) for item in field_result.get("files", [])]
    for file_info in field_result.get("files", []):
        if file_info.get("kind") != "tabular":
            continue
        relative = str(file_info.get("path", ""))
        other_names = [name for name in all_names if name != relative]
        candidates: list[tuple[int, str, str, str, list[str]]] = []
        characters = 0
        for raw_table in file_info.get("tables", []):
            rows, columns = raw_table.get("row_count"), raw_table.get("column_count", 0)
            if rows is None or int(rows) * int(columns) > cell_budget:
                continue
            table = reconstruct_table(file_info, raw_table)
            try:
                for row_number, values in iter_table_rows(table, input_root / relative, table.columns):
                    for column in table.columns:
                        raw_value = values.get(column.name)
                        if not isinstance(raw_value, str) or len(raw_value.strip()) < 6:
                            continue
                        characters += len(raw_value)
                        for statement in split_statements(raw_value):
                            score, mentioned = statement_score(statement, other_names)
                            if score:
                                locator = f"table:{table.table_name};row:{row_number};column:{column.name}"
                                candidates.append((score, locator, statement, column.name, mentioned))
                        if characters >= character_budget:
                            break
                    if characters >= character_budget:
                        warnings.append(f"Small-table semantic evidence budget reached for {relative}")
                        break
            except Exception as exc:
                warnings.append(f"Small-table semantic extraction failed for {relative}: {type(exc).__name__}: {exc}")
            if characters >= character_budget:
                break
        candidates.sort(key=lambda item: (-item[0], item[1], item[2]))
        seen: set[str] = set()
        for score, locator, statement, column, mentioned in candidates:
            fingerprint = normalize_name(statement)
            if not fingerprint or fingerprint in seen:
                continue
            seen.add(fingerprint)
            markers = sorted({marker for marker in RELATION_MARKERS if marker.casefold() in statement.casefold()})[:12]
            mentioned = [name for name in mentioned if name != relative]
            cards.append(make_card(
                "table_relation_statement" if markers else "material_topic_alignment",
                "direct" if markers and score >= 5 else "structural",
                f"{relative} contains a localized relationship-bearing table statement."
                if markers else f"{relative} contains a localized topic that aligns with another material.",
                [{"file": relative, "locator": locator}],
                {"column": column, "relation_markers": markers, "mentioned_files": mentioned},
                statement,
            ))
            if len(seen) >= cards_per_file:
                break
    return cards, warnings


def split_statements(text: str) -> Iterator[str]:
    for part in re.split(r"(?<=[.!?。！？；;])\s*|[\r\n]+", text):
        statement = compact_text(part, MAX_SNIPPET)
        if len(statement) >= 6:
            yield statement


def split_goal_statements(text: str) -> Iterator[str]:
    for part in re.split(r"(?<=[.!?。！？；;，,])\s*|[\r\n]+", text):
        statement = compact_text(part, MAX_SNIPPET)
        if len(statement) >= 6:
            yield statement


def relation_tokens(text: str) -> set[str]:
    folded = text.casefold()
    tokens = {word for word in re.findall(r"[a-z0-9]{3,}", folded) if word not in {"xlsx", "csv", "table"}}
    chinese = "".join(re.findall(r"[\u4e00-\u9fff]", folded))
    tokens.update(chinese[index:index + 2] for index in range(max(0, len(chinese) - 1)))
    return tokens - {"重复", "复收", "收费", "同时", "收取", "费用", "规则", "结果", "明细"}


def statement_score(statement: str, all_file_names: Sequence[str]) -> tuple[int, list[str]]:
    folded = statement.casefold()
    markers = [marker for marker in RELATION_MARKERS if marker.casefold() in folded]
    score = 3 * len(set(markers))
    if any(marker.casefold() in folded for marker in SEQUENCE_MARKERS):
        score += 2
    if any(marker.casefold() in folded for marker in BRANCH_MARKERS):
        score += 2
    statement_tokens = relation_tokens(statement)
    mentioned: list[str] = []
    for name in all_file_names:
        file_name = Path(name).name
        if len(file_name) < 4:
            continue
        overlap = statement_tokens & relation_tokens(Path(name).stem)
        explicit = file_name.casefold() in folded
        if explicit or len(overlap) >= 3:
            mentioned.append(name)
            score += 6 if explicit else min(6, len(overlap))
    return score, mentioned


def looks_like_heading(text: str) -> bool:
    stripped = text.strip()
    if not 2 <= len(stripped) <= 100:
        return False
    return bool(re.match(r"^(?:#{1,6}\s+|\d+(?:\.\d+)*[.)、\s]|[一二三四五六七八九十]+[、.]|[-*•]\s+)", stripped))


def document_cards(
    input_root: Path,
    field_result: dict[str, Any],
    ocr_mode: str,
    character_budget: int,
    cards_per_file: int,
) -> tuple[list[dict[str, Any]], list[str]]:
    cards: list[dict[str, Any]] = []
    warnings: list[str] = []
    all_names = [str(item.get("path", "")) for item in field_result.get("files", [])]
    for file_info in field_result.get("files", []):
        if file_info.get("kind") != "document":
            continue
        relative = str(file_info.get("path", ""))
        other_names = [name for name in all_names if name != relative]
        path = input_root / relative
        candidates: list[tuple[int, str, str, list[str]]] = []
        headings: list[tuple[str, str]] = []
        characters = 0
        try:
            for locator, text in iter_document_segments(path, ocr_mode):
                characters += len(text)
                for statement in split_statements(text):
                    score, mentioned = statement_score(statement, other_names)
                    if score:
                        candidates.append((score, locator, statement, mentioned))
                    elif looks_like_heading(statement) and len(headings) < 24:
                        headings.append((locator, statement))
                if characters >= character_budget:
                    warnings.append(f"Document evidence budget reached for {relative}")
                    break
        except Exception as exc:
            warnings.append(f"Document evidence extraction failed for {relative}: {type(exc).__name__}: {exc}")
            continue
        candidates.sort(key=lambda item: (-item[0], item[1], item[2]))
        seen: set[str] = set()
        for score, locator, statement, mentioned in candidates:
            fingerprint = normalize_name(statement)
            if not fingerprint or fingerprint in seen:
                continue
            seen.add(fingerprint)
            relation_markers = sorted({marker for marker in RELATION_MARKERS if marker.casefold() in statement.casefold()})[:12]
            mentioned = [name for name in mentioned if name != relative]
            strength = "direct" if score >= 5 else "structural"
            cards.append(make_card(
                "document_relation_statement" if relation_markers else "material_topic_alignment",
                strength if relation_markers else "structural",
                f"{relative} contains a localized relationship-bearing statement."
                if relation_markers else f"{relative} contains a localized topic that aligns with another material.",
                [{"file": relative, "locator": locator}],
                {"relation_markers": relation_markers, "mentioned_files": mentioned},
                statement,
            ))
            if len(seen) >= cards_per_file:
                break
        if headings:
            cards.append(make_card(
                "document_outline",
                "contextual",
                f"{relative} exposes a bounded structural outline for interpreting its business role.",
                [{"file": relative, "locator": headings[0][0]}],
                {"headings": [text for _locator, text in headings[:16]], "omitted_heading_count": max(0, len(headings) - 16)},
            ))
    return cards, warnings


def goal_card(goal_file: Path | None) -> list[dict[str, Any]]:
    if goal_file is None or not goal_file.is_file():
        return []
    all_lines = goal_file.read_text(encoding="utf-8", errors="replace").splitlines()
    section_lines: list[tuple[int, str]] = []
    in_scenario = False
    found_section = False
    for line_number, line in enumerate(all_lines, 1):
        if re.match(r"^##\s+Scenario Description\s*$", line, re.IGNORECASE):
            in_scenario = True
            found_section = True
            continue
        if in_scenario and re.match(r"^##\s+", line):
            break
        if in_scenario:
            section_lines.append((line_number, line))
    if not found_section:
        section_lines = list(enumerate(all_lines, 1))
    raw_text = "\n".join(line for _line_number, line in section_lines)
    text = compact_text(raw_text, 1200)
    if not text:
        return []
    cards = [make_card(
        "scenario_goal",
        "contextual",
        "The user-provided scenario description supplies purpose and vocabulary but cannot prove a data relationship by itself.",
        [{"file": str(goal_file), "locator": "description"}],
        {"description": text},
    )]
    seen: set[str] = set()
    for line_number, line in section_lines:
        for statement in split_goal_statements(line):
            score, _mentioned = statement_score(statement, [])
            fingerprint = normalize_name(statement)
            if score and fingerprint not in seen:
                seen.add(fingerprint)
                cards.append(make_card(
                    "goal_relation_statement",
                    "direct",
                    "The user-provided scenario description explicitly states a business relationship or condition.",
                    [{"file": str(goal_file), "locator": f"line:{line_number}"}],
                    {"relation_markers": sorted({marker for marker in RELATION_MARKERS if marker.casefold() in statement.casefold()})[:12]},
                    statement,
                ))
            if len(seen) >= 20:
                return cards
    return cards


def deduplicate_and_bound(cards: Iterable[dict[str, Any]], max_cards: int) -> list[dict[str, Any]]:
    priority = {"direct": 0, "structural": 1, "corroborating": 2, "contextual": 3}
    unique = {card["id"]: card for card in cards}
    ordered = sorted(unique.values(), key=lambda item: (priority.get(item["strength"], 9), item["kind"], item["id"]))
    if max_cards > 0 and len(ordered) > max_cards:
        required = [card for card in ordered if card["kind"] == "file_structure"]
        remaining = [card for card in ordered if card["kind"] != "file_structure"]
        ordered = required + remaining[:max(0, max_cards - len(required))]
    return ordered


def claims_template(cards_path: Path, files: Sequence[str]) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "instructions": {
            "evidence_cards": str(cards_path),
            "rule": "Build a macro data-relationship map with 5-8 scenario concepts. Fields and records are evidence, never graph nodes.",
            "main_chain": "Provide one primary data path from business input/data domain to the derived result. Attach rule and reference dependencies as side edges.",
            "direction": "Prefer data -> processing/result with feeds or derives, rule -> decision/result with governs, and cross-source traceability with joins_with.",
            "branch": "A branch must end at output/state/object or return to an attached chain node with returns_to.",
            "complexity": f"At most {MAX_SCENARIO_NODES} nodes, {MAX_SCENARIO_EDGES} edges, and {MAX_SCENARIO_BRANCHES} branches.",
            "preflight": "Create every directed primary-path edge, then fix all validation errors in one bounded rewrite before retrying.",
            "evidence": "Every node, edge, branch, and excluded-file decision must cite evidence card IDs.",
        },
        "scenario": {"name": "", "purpose": ""},
        "nodes": [],
        "edges": [],
        "main_chain": [],
        "branches": [],
        "coverage": {
            "included_files": [],
            "excluded_files": [],
            "inventory_files": list(files),
        },
    }


def prepare_evidence(args: argparse.Namespace) -> dict[str, Any]:
    input_root = Path(args.input).resolve()
    output_root = Path(args.output).resolve()
    if not input_root.is_dir():
        raise ValueError(f"Input directory does not exist: {input_root}")
    if input_root == output_root or input_root in output_root.parents:
        raise ValueError("Output directory must be outside the input data directory")
    output_root.mkdir(parents=True, exist_ok=True)
    if args.field_result:
        field_result_path = Path(args.field_result).resolve()
        field_result = json.loads(field_result_path.read_text(encoding="utf-8"))
    else:
        args.defer_documents = True
        field_result_path = output_root / "_field-evidence" / "relations.json"
        field_result = ProgressiveAnalyzer(input_root, field_result_path.parent, args).run()
    if field_result.get("status") != "complete":
        result = {
            "schema_version": SCHEMA_VERSION,
            "status": "partial",
            "ready_for_synthesis": False,
            "generated_at": utc_now(),
            "field_evidence": str(field_result_path),
            "coverage": field_result.get("coverage", {}),
            "message": "Low-level evidence probing is incomplete; rerun the same analyze command to resume.",
        }
        atomic_json(output_root / "prepare-status.json", result)
        return result

    cards: list[dict[str, Any]] = []
    cards.extend(goal_card(Path(args.goal_file) if args.goal_file else None))
    cards.extend(table_cards(field_result))
    cards.extend(field_relationship_cards(field_result))
    table_statements, table_warnings = table_relation_statement_cards(
        input_root,
        field_result,
        args.semantic_table_cell_budget,
        args.table_character_budget,
        args.table_cards_per_file,
    )
    cards.extend(table_statements)
    extracted, warnings = document_cards(
        input_root,
        field_result,
        args.ocr_mode,
        args.document_character_budget,
        args.document_cards_per_file,
    )
    cards.extend(extracted)
    warnings = table_warnings + warnings
    cards = deduplicate_and_bound(cards, args.max_evidence_cards)
    files = [str(item.get("path", "")) for item in field_result.get("files", [])]
    cards_path = output_root / "evidence-cards.json"
    card_payload = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": utc_now(),
        "input_root": str(input_root),
        "field_evidence": str(field_result_path),
        "card_count": len(cards),
        "cards": cards,
        "warnings": warnings,
        "coverage": {
            "files": files,
            "file_count": len(files),
            "card_kinds": dict(sorted(Counter(card["kind"] for card in cards).items())),
            "guarantee": "Cards contain bounded schema, localized document statements, and aggregated fingerprints; no complete data rows or full documents are exposed.",
        },
    }
    atomic_json(cards_path, card_payload)
    template_path = output_root / "scenario-claims.template.json"
    atomic_json(template_path, claims_template(cards_path, files))
    brief = synthesis_brief(card_payload)
    brief_path = output_root / "synthesis-brief.json"
    atomic_json(brief_path, brief)
    result = {
        "schema_version": SCHEMA_VERSION,
        "status": "ready_for_synthesis",
        "ready_for_synthesis": True,
        "generated_at": utc_now(),
        "card_count": len(cards),
        "warnings": warnings,
        "artifacts": {
            "evidence_cards": str(cards_path),
            "synthesis_brief": str(brief_path),
            "claims_template": str(template_path),
            "field_evidence": str(field_result_path),
        },
        "synthesis_brief": brief,
    }
    atomic_json(output_root / "prepare-status.json", result)
    return result


def compact_evidence_card(card: dict[str, Any]) -> dict[str, Any]:
    kind = str(card.get("kind", ""))
    facts = card.get("facts") if isinstance(card.get("facts"), dict) else {}
    compact_facts: dict[str, Any]
    if kind == "scenario_goal":
        compact_facts = {"description": compact_text(facts.get("description", ""), 720)}
    elif kind == "table_schema":
        columns = facts.get("columns_by_role") if isinstance(facts.get("columns_by_role"), dict) else {}
        compact_facts = {
            "table": facts.get("table", ""),
            "estimated_rows": facts.get("estimated_rows"),
            "column_count": facts.get("column_count"),
            "inferred_material_role": facts.get("inferred_material_role", ""),
            "columns_by_role": {
                str(role): list(values)[:4]
                for role, values in columns.items()
                if isinstance(values, list) and values
            },
            "omitted_column_count": facts.get("omitted_column_count", 0),
        }
    elif kind == "field_relationship":
        correspondences = facts.get("correspondences") if isinstance(facts.get("correspondences"), list) else []
        compact_facts = {
            "source_file": facts.get("source_file", ""),
            "target_file": facts.get("target_file", ""),
            "correspondences": [
                {
                    "source_field": item.get("source_field", ""),
                    "target_field": item.get("target_field", ""),
                    "confidence": item.get("confidence", 0),
                    "evidence_count": item.get("evidence_count", 0),
                }
                for item in correspondences[:6]
                if isinstance(item, dict)
            ],
            "omitted_correspondence_count": int(facts.get("omitted_correspondence_count", 0))
            + max(0, len(correspondences) - 6),
            "confidence": facts.get("confidence", 0),
            "evidence_count": facts.get("evidence_count", 0),
        }
    else:
        compact_facts = facts
    return {
        "id": card.get("id", ""),
        "kind": kind,
        "strength": card.get("strength", ""),
        "statement": compact_text(card.get("statement", ""), MAX_CARD_STATEMENT),
        "sources": list(card.get("sources", []))[:2],
        "facts": compact_facts,
        "snippet": compact_text(card.get("snippet", ""), MAX_SNIPPET),
    }


def synthesis_brief(payload: dict[str, Any]) -> dict[str, Any]:
    cards = [card for card in payload.get("cards", []) if isinstance(card, dict)]
    core_kinds = {
        "scenario_goal", "goal_relation_statement", "file_structure", "table_schema",
        "table_process_signal", "field_relationship",
    }
    statement_kinds = {
        "document_relation_statement", "table_relation_statement", "material_topic_alignment",
    }
    kind_order = {
        "scenario_goal": 0,
        "goal_relation_statement": 1,
        "file_structure": 2,
        "table_schema": 3,
        "table_process_signal": 4,
        "field_relationship": 5,
    }
    context_text = " ".join(
        str(card.get("snippet") or card.get("facts", {}).get("description") or "")
        for card in cards
        if card.get("kind") in {"scenario_goal", "goal_relation_statement"}
    )
    context_text += " " + " ".join(str(item) for item in payload.get("coverage", {}).get("files", []))
    context_tokens = relation_tokens(context_text)
    core = sorted(
        (card for card in cards if card.get("kind") in core_kinds),
        key=lambda card: (
            kind_order.get(str(card.get("kind")), 99),
            str(card.get("sources", [{}])[0].get("file", "")),
            str(card.get("id", "")),
        ),
    )

    def statement_rank(card: dict[str, Any]) -> tuple[int, str]:
        text = f"{card.get('snippet', '')} {card.get('statement', '')}"
        overlap = len(relation_tokens(text) & context_tokens)
        direct = 3 if card.get("strength") == "direct" else 1
        document = 2 if card.get("kind") == "document_relation_statement" else 0
        return (-(overlap * 4 + direct + document), str(card.get("id", "")))

    statements = sorted(
        (card for card in cards if card.get("kind") in statement_kinds),
        key=statement_rank,
    )[:MAX_BRIEF_STATEMENTS]
    selected = (core[:MAX_BRIEF_CARDS - len(statements)] + statements)[:MAX_BRIEF_CARDS]
    files = list(payload.get("coverage", {}).get("files", []))
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "ready_for_synthesis",
        "card_count": len(cards),
        "selected_card_count": len(selected),
        "omitted_card_count": max(0, len(cards) - len(selected)),
        "coverage": {
            "file_count": len(files),
            "files": files[:100],
            "omitted_file_count": max(0, len(files) - 100),
            "card_kinds": payload.get("coverage", {}).get("card_kinds", {}),
        },
        "cards": [compact_evidence_card(card) for card in selected],
        "next_action": (
            "Read references/scenario-synthesis.md, write scenario-claims.json from these evidence IDs, "
            "then run finalize. Query additional evidence only by file or ID when a required edge remains unsupported."
        ),
    }


def evidence_page(
    payload: dict[str, Any],
    offset: int,
    limit: int,
    kinds: set[str] | None = None,
    file_name: str = "",
    ids: set[str] | None = None,
) -> dict[str, Any]:
    cards = payload.get("cards", [])
    if kinds:
        cards = [card for card in cards if card.get("kind") in kinds]
    if file_name:
        cards = [card for card in cards if file_name in referenced_files(card)]
    if ids:
        cards = [card for card in cards if card.get("id") in ids]
    offset = max(0, offset)
    limit = max(1, min(limit, MAX_EVIDENCE_PAGE))
    return {
        "offset": offset,
        "limit": limit,
        "total": len(cards),
        "has_more": offset + limit < len(cards),
        "items": cards[offset:offset + limit],
    }


def looks_record_specific(name: str) -> bool:
    text = name.strip()
    if not text:
        return True
    if re.fullmatch(r"[0-9][0-9,./:\- ]*", text):
        return True
    if re.fullmatch(r"(?i)[0-9a-f]{8}-[0-9a-f-]{27,}", text):
        return True
    compact = re.sub(r"\s+", "", text)
    digits = sum(char.isdigit() for char in compact)
    if len(compact) >= 9 and digits / len(compact) >= 0.5:
        return True
    if len(compact) >= 10 and digits and re.fullmatch(r"[A-Za-z0-9_.:/\-]+", compact):
        return True
    return False


def referenced_files(card: dict[str, Any]) -> set[str]:
    return {str(source.get("file", "")) for source in card.get("sources", []) if source.get("file")}


def evidence_is_adequate(edge_type: str, cards: Sequence[dict[str, Any]]) -> bool:
    kinds = {card.get("kind") for card in cards}
    relation_statement_kinds = {
        "document_relation_statement", "table_relation_statement", "goal_relation_statement",
    }
    if edge_type in {"references", "depends_on", "feeds", "joins_with"} and "field_relationship" in kinds:
        return True
    if edge_type in ORDER_EDGE_TYPES:
        if any(
            card.get("kind") in relation_statement_kinds
            and any(marker in str(card.get("snippet", "")).casefold() for marker in SEQUENCE_MARKERS + BRANCH_MARKERS)
            for card in cards
        ):
            return True
        if sum(card.get("kind") == "goal_relation_statement" for card in cards) >= 2:
            return True
        return "table_process_signal" in kinds and len(cards) >= 2
    if kinds & relation_statement_kinds:
        return True
    strength_score = sum({"direct": 3, "structural": 2, "corroborating": 1}.get(str(card.get("strength")), 0) for card in cards)
    return strength_score >= 4 and any(card.get("strength") in {"direct", "structural"} for card in cards)


def validate_claims(claims: dict[str, Any], card_payload: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    cards_by_id = {str(card.get("id")): card for card in card_payload.get("cards", [])}
    inventory = set(card_payload.get("coverage", {}).get("files", []))
    nodes = claims.get("nodes", [])
    edges = claims.get("edges", [])
    branches = claims.get("branches", [])
    main_chain = claims.get("main_chain", [])
    if not isinstance(nodes, list) or not isinstance(edges, list) or not isinstance(main_chain, list) or not isinstance(branches, list):
        return ["nodes, edges, main_chain, and branches must all be arrays"]
    if len(nodes) > MAX_SCENARIO_NODES:
        errors.append(
            f"Macro scenario graph may contain at most {MAX_SCENARIO_NODES} nodes; "
            "merge field/table-level concepts into business data domains"
        )
    if len(edges) > MAX_SCENARIO_EDGES:
        errors.append(
            f"Macro scenario graph may contain at most {MAX_SCENARIO_EDGES} edges; "
            "keep only relationships needed to explain downstream business reasoning"
        )
    if len(branches) > MAX_SCENARIO_BRANCHES:
        errors.append(f"Macro scenario graph may contain at most {MAX_SCENARIO_BRANCHES} branches")

    node_ids = [str(node.get("id", "")) for node in nodes if isinstance(node, dict)]
    edge_ids = [str(edge.get("id", "")) for edge in edges if isinstance(edge, dict)]
    if len(node_ids) != len(nodes) or any(not value for value in node_ids) or len(set(node_ids)) != len(node_ids):
        errors.append("Every node needs a non-empty unique id")
    if len(edge_ids) != len(edges) or any(not value for value in edge_ids) or len(set(edge_ids)) != len(edge_ids):
        errors.append("Every edge needs a non-empty unique id")
    node_by_id = {str(node.get("id", "")): node for node in nodes if isinstance(node, dict)}

    used_claim_cards: set[str] = set()

    def validate_evidence(owner: str, evidence_ids: Any, edge_type: str | None = None) -> list[dict[str, Any]]:
        if not isinstance(evidence_ids, list) or not evidence_ids:
            errors.append(f"{owner} must cite at least one evidence card")
            return []
        missing = [str(item) for item in evidence_ids if str(item) not in cards_by_id]
        if missing:
            errors.append(f"{owner} cites unknown evidence cards: {', '.join(missing)}")
        selected = [cards_by_id[str(item)] for item in evidence_ids if str(item) in cards_by_id]
        used_claim_cards.update(str(item) for item in evidence_ids if str(item) in cards_by_id)
        if edge_type and selected and not evidence_is_adequate(edge_type, selected):
            errors.append(f"{owner} lacks evidence strong enough for edge type {edge_type}")
        return selected

    for node in nodes:
        if not isinstance(node, dict):
            errors.append("Every node must be an object")
            continue
        identifier = str(node.get("id", ""))
        node_type = str(node.get("type", ""))
        name = str(node.get("name", "")).strip()
        if node_type not in NODE_TYPES:
            errors.append(f"Node {identifier} has unsupported type {node_type}")
        if not 2 <= len(name) <= 80:
            errors.append(f"Node {identifier} name must contain 2-80 characters")
        elif looks_record_specific(name):
            errors.append(f"Node {identifier} name looks like a record value or code: {name}")
        validate_evidence(f"Node {identifier}", node.get("evidence_ids"))

    edge_by_pair: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    adjacency: dict[str, set[str]] = defaultdict(set)
    for edge in edges:
        if not isinstance(edge, dict):
            errors.append("Every edge must be an object")
            continue
        identifier = str(edge.get("id", ""))
        source, target = str(edge.get("source", "")), str(edge.get("target", ""))
        edge_type = str(edge.get("type", ""))
        if source not in node_by_id or target not in node_by_id:
            errors.append(f"Edge {identifier} references an unknown node")
        if source == target:
            errors.append(f"Edge {identifier} cannot be a self-loop")
        if edge_type not in EDGE_TYPES:
            errors.append(f"Edge {identifier} has unsupported type {edge_type}")
        endpoint_types = EDGE_ENDPOINT_TYPES.get(edge_type)
        if endpoint_types and source in node_by_id and target in node_by_id:
            allowed_sources, allowed_targets = endpoint_types
            source_type = str(node_by_id[source].get("type", ""))
            target_type = str(node_by_id[target].get("type", ""))
            if source_type not in allowed_sources or target_type not in allowed_targets:
                errors.append(
                    f"Edge {identifier} has invalid {edge_type} direction: "
                    f"{source_type} -> {target_type}"
                )
        confidence = edge.get("confidence")
        if not isinstance(confidence, (int, float)) or not 0 <= confidence <= 1:
            errors.append(f"Edge {identifier} confidence must be between 0 and 1")
        validate_evidence(f"Edge {identifier}", edge.get("evidence_ids"), edge_type if edge_type in EDGE_TYPES else None)
        edge_by_pair[(source, target)].append(edge)
        adjacency[source].add(target)
        adjacency[target].add(source)

    if len(main_chain) < 3:
        errors.append("main_chain must contain at least three scenario nodes")
    if len(set(main_chain)) != len(main_chain):
        errors.append("main_chain cannot repeat a node")
    for node_id in main_chain:
        if node_id not in node_by_id:
            errors.append(f"main_chain references unknown node {node_id}")
    for source, target in zip(main_chain, main_chain[1:]):
        candidates = [edge for edge in edge_by_pair.get((source, target), []) if edge.get("type") in FLOW_EDGE_TYPES]
        if not candidates:
            errors.append(f"main_chain step {source} -> {target} lacks a directed flow edge")
    if main_chain and main_chain[0] in node_by_id and node_by_id[main_chain[0]].get("type") not in {"trigger", "input", "actor", "system"}:
        errors.append("main_chain must start with a trigger, input, actor, or system")
    if main_chain and main_chain[-1] in node_by_id and node_by_id[main_chain[-1]].get("type") not in {"output", "state", "object"}:
        errors.append("main_chain must end with an output, state, or object")

    branch_ids: set[str] = set()
    for branch in branches:
        if not isinstance(branch, dict):
            continue
        identifier = str(branch.get("id", ""))
        if not identifier or identifier in branch_ids:
            errors.append("Every branch needs a non-empty unique id")
        branch_ids.add(identifier)
    attached_nodes = set(main_chain)
    pending = list(branches)
    while pending:
        progressed = False
        for branch in list(pending):
            identifier = str(branch.get("id", "")) if isinstance(branch, dict) else ""
            if not isinstance(branch, dict):
                errors.append("Every branch must be an object")
                pending.remove(branch)
                continue
            origin = str(branch.get("from", ""))
            path = branch.get("path", [])
            validate_evidence(f"Branch {identifier}", branch.get("evidence_ids"))
            if origin not in attached_nodes:
                continue
            if not isinstance(path, list) or not path:
                errors.append(f"Branch {identifier} path must contain at least one node")
                pending.remove(branch)
                progressed = True
                continue
            if any(node_id not in node_by_id for node_id in path):
                errors.append(f"Branch {identifier} references an unknown node")
            first_edges = edge_by_pair.get((origin, str(path[0])), [])
            if not any(edge.get("type") == "branches_to" for edge in first_edges):
                errors.append(f"Branch {identifier} must start with a branches_to edge from {origin}")
            for source, target in zip(path, path[1:]):
                if not any(edge.get("type") in FLOW_EDGE_TYPES for edge in edge_by_pair.get((str(source), str(target)), [])):
                    errors.append(f"Branch {identifier} step {source} -> {target} lacks a directed flow edge")
            endpoint = str(path[-1])
            endpoint_type = str(node_by_id.get(endpoint, {}).get("type", ""))
            returns_to_attached = any(
                edge.get("type") == "returns_to"
                and str(edge.get("source", "")) == endpoint
                and str(edge.get("target", "")) in attached_nodes
                for edge in edges
                if isinstance(edge, dict)
            )
            if endpoint not in attached_nodes and endpoint_type not in {"output", "state", "object"} and not returns_to_attached:
                errors.append(
                    f"Branch {identifier} must end at output/state/object or return to an attached node"
                )
            attached_nodes.update(str(item) for item in path)
            pending.remove(branch)
            progressed = True
        if not progressed:
            for branch in pending:
                errors.append(f"Branch {branch.get('id', '')} does not attach to the main chain or an earlier branch")
            break

    if node_by_id and main_chain:
        visited: set[str] = set()
        queue: deque[str] = deque([str(main_chain[0])])
        while queue:
            current = queue.popleft()
            if current in visited:
                continue
            visited.add(current)
            queue.extend(adjacency.get(current, set()) - visited)
        disconnected = sorted(set(node_by_id) - visited)
        if disconnected:
            errors.append(f"All nodes must form one connected scenario graph; disconnected: {', '.join(disconnected)}")

    graph_claim_cards = set(used_claim_cards)
    coverage = claims.get("coverage", {})
    included = coverage.get("included_files", []) if isinstance(coverage, dict) else []
    excluded_items = coverage.get("excluded_files", []) if isinstance(coverage, dict) else []
    included_set = {str(item) for item in included} if isinstance(included, list) else set()
    excluded_set: set[str] = set()
    if not isinstance(excluded_items, list):
        errors.append("coverage.excluded_files must be an array")
    else:
        for item in excluded_items:
            if not isinstance(item, dict) or not item.get("file") or not item.get("reason"):
                errors.append("Each excluded file needs file and reason")
                continue
            excluded_file = str(item["file"])
            excluded_set.add(excluded_file)
            selected = validate_evidence(f"Excluded file {excluded_file}", item.get("evidence_ids"))
            if selected and not any(excluded_file in referenced_files(card) for card in selected):
                errors.append(f"Excluded file {excluded_file} must cite evidence located in that file")
    unknown = sorted((included_set | excluded_set) - inventory)
    missing = sorted(inventory - included_set - excluded_set)
    overlap = sorted(included_set & excluded_set)
    if unknown:
        errors.append(f"Coverage names files outside inventory: {', '.join(unknown)}")
    if missing:
        errors.append(f"Coverage must include or explicitly exclude every file: {', '.join(missing)}")
    if overlap:
        errors.append(f"Files cannot be both included and excluded: {', '.join(overlap)}")
    used_files = {
        file_name
        for evidence_id in graph_claim_cards
        for file_name in referenced_files(cards_by_id[evidence_id])
    }
    unsupported_included = sorted(included_set - used_files)
    if unsupported_included:
        errors.append(
            "Every included file must contribute evidence to a node, edge, or branch: "
            + ", ".join(unsupported_included)
        )

    scenario = claims.get("scenario", {})
    if not isinstance(scenario, dict) or not str(scenario.get("name", "")).strip() or not str(scenario.get("purpose", "")).strip():
        errors.append("scenario.name and scenario.purpose are required")
    return list(dict.fromkeys(errors))


def write_scenario_database(result: dict[str, Any], card_payload: dict[str, Any], path: Path) -> None:
    for suffix in ("", "-wal", "-shm"):
        target = Path(str(path) + suffix)
        if target.exists():
            target.unlink()
    cards_by_id = {card["id"]: card for card in card_payload.get("cards", [])}
    connection = sqlite3.connect(path)
    try:
        connection.executescript(
            """
            PRAGMA journal_mode=DELETE;
            CREATE TABLE nodes(id TEXT PRIMARY KEY, name TEXT, type TEXT, description TEXT);
            CREATE TABLE edges(id TEXT PRIMARY KEY, source TEXT, target TEXT, type TEXT, label TEXT, confidence REAL);
            CREATE TABLE evidence_cards(id TEXT PRIMARY KEY, kind TEXT, strength TEXT, statement TEXT, sources_json TEXT, snippet TEXT);
            CREATE TABLE claim_evidence(claim_kind TEXT, claim_id TEXT, evidence_id TEXT);
            """
        )
        used: set[str] = set()
        for node in result["nodes"]:
            connection.execute("INSERT INTO nodes VALUES (?,?,?,?)", (node["id"], node["name"], node["type"], node.get("description", "")))
            for evidence_id in node["evidence_ids"]:
                used.add(evidence_id)
                connection.execute("INSERT INTO claim_evidence VALUES (?,?,?)", ("node", node["id"], evidence_id))
        for edge in result["edges"]:
            connection.execute("INSERT INTO edges VALUES (?,?,?,?,?,?)", (edge["id"], edge["source"], edge["target"], edge["type"], edge.get("label", ""), edge["confidence"]))
            for evidence_id in edge["evidence_ids"]:
                used.add(evidence_id)
                connection.execute("INSERT INTO claim_evidence VALUES (?,?,?)", ("edge", edge["id"], evidence_id))
        for evidence_id in sorted(used):
            card = cards_by_id[evidence_id]
            connection.execute(
                "INSERT INTO evidence_cards VALUES (?,?,?,?,?,?)",
                (evidence_id, card["kind"], card["strength"], card["statement"], json.dumps(card["sources"], ensure_ascii=False), card.get("snippet", "")),
            )
        connection.commit()
    finally:
        connection.close()


def write_report(result: dict[str, Any], cards_by_id: dict[str, dict[str, Any]], path: Path) -> None:
    node_by_id = {node["id"]: node for node in result["nodes"]}
    lines = [
        "# 业务场景宏观数据关系", "",
        f"- 场景：{result['scenario']['name']}",
        f"- 目的：{result['scenario']['purpose']}",
        f"- 节点：{len(result['nodes'])}；关系：{len(result['edges'])}；分支：{len(result['branches'])}", "",
        "## 主数据路径", "",
        " → ".join(node_by_id[node_id]["name"] for node_id in result["main_chain"]), "",
    ]
    if result["branches"]:
        lines.extend(["## 分支", ""])
        for branch in result["branches"]:
            origin = node_by_id[branch["from"]]["name"]
            route = " → ".join(node_by_id[node_id]["name"] for node_id in branch["path"])
            lines.append(f"- {origin} --[{branch.get('condition', '分支')}]→ {route}")
        lines.append("")
    lines.extend(["## 关系及依据", ""])
    for edge in result["edges"]:
        lines.extend([
            f"### {edge['id']} · {edge['type']}", "",
            f"{node_by_id[edge['source']]['name']} → {node_by_id[edge['target']]['name']}", "",
            f"- 说明：{edge.get('label', '')}",
            f"- 置信度：{edge['confidence']:.3f}",
        ])
        for evidence_id in edge["evidence_ids"]:
            card = cards_by_id[evidence_id]
            locations = "；".join(f"{item['file']}#{item['locator']}" for item in card["sources"])
            detail = card.get("snippet") or card["statement"]
            lines.append(f"- `{evidence_id}` [{card['strength']}] {locations}：{detail}")
        lines.append("")
    lines.extend(["## 文件覆盖", "", "已纳入：" + "、".join(f"`{item}`" for item in result["coverage"]["included_files"]), ""])
    for item in result["coverage"].get("excluded_files", []):
        lines.append(f"- 排除 `{item['file']}`：{item['reason']}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def mermaid_escape(value: str) -> str:
    return value.replace('"', "'").replace("\n", " ")[:80]


def write_mermaid(result: dict[str, Any], path: Path) -> None:
    nodes = {node["id"]: node for node in result["nodes"]}
    lines = ["flowchart TB"]
    for node in result["nodes"]:
        lines.append(f'  {node["id"]}["{mermaid_escape(node["name"])}"]')
    main_pairs = set(zip(result["main_chain"], result["main_chain"][1:]))
    branch_pairs = {
        pair
        for branch in result["branches"]
        for pair in zip([branch["from"]] + branch["path"], branch["path"])
    }
    for edge in result["edges"]:
        pair = (edge["source"], edge["target"])
        arrow = "==>" if pair in main_pairs else "-.->" if pair in branch_pairs else "-->"
        lines.append(f'  {edge["source"]} {arrow}|"{mermaid_escape(edge.get("label") or edge["type"])}"| {edge["target"]}')
    lines.extend(["  classDef main fill:#e8f4ea,stroke:#287a3f,stroke-width:2px;", "  classDef branch fill:#fff4db,stroke:#a66a00;"])
    if result["main_chain"]:
        lines.append("  class " + ",".join(result["main_chain"]) + " main;")
    branch_nodes = sorted({node for branch in result["branches"] for node in branch["path"] if node not in result["main_chain"]})
    if branch_nodes:
        lines.append("  class " + ",".join(branch_nodes) + " branch;")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def compact_summary(result: dict[str, Any], offset: int, limit: int) -> dict[str, Any]:
    edges = result.get("edges", [])
    offset = max(0, offset)
    limit = max(1, min(limit, 50))
    return {
        "status": result.get("status", "complete"),
        "scenario": result.get("scenario", {}),
        "node_count": len(result.get("nodes", [])),
        "edge_count": len(edges),
        "main_chain": result.get("main_chain", []),
        "primary_data_path": result.get("primary_data_path", result.get("main_chain", [])),
        "branch_count": len(result.get("branches", [])),
        "edge_page": {
            "offset": offset,
            "limit": limit,
            "has_more": offset + limit < len(edges),
            "items": edges[offset:offset + limit],
        },
        "coverage": result.get("coverage", {}),
        "artifacts": result.get("artifacts", {}),
    }


def _claim_values(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def _load_claims(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("Claims file must contain one JSON object")
    return payload


def _upsert_claim(items: list[dict[str, Any]], item_id: str, patch: dict[str, Any]) -> str:
    current = next((item for item in items if item.get("id") == item_id), None)
    if current is None:
        current = {"id": item_id}
        items.append(current)
        action = "added"
    else:
        action = "updated"
    current.update({key: value for key, value in patch.items() if value is not None})
    return action


def mutate_claims(args: argparse.Namespace) -> dict[str, Any]:
    claims_path = Path(args.claims).resolve()
    command = args.command
    if command == "claims-copy":
        if claims_path.exists() and not args.force:
            raise FileExistsError(
                f"Candidate already exists: {claims_path}. Continue editing or finalize it; do not overwrite it."
            )
        claims = _load_claims(Path(args.source).resolve())
        atomic_json(claims_path, claims)
        return {"status": "copied", "claims": str(claims_path)}
    if command == "claims-init":
        if claims_path.exists() and not args.force:
            raise FileExistsError(
                f"Candidate already exists: {claims_path}. Continue editing or finalize it; do not reinitialize it."
            )
        claims = {
            "schema_version": SCHEMA_VERSION,
            "scenario": {"name": args.name, "purpose": args.purpose},
            "nodes": [],
            "edges": [],
            "main_chain": [],
            "branches": [],
            "coverage": {"included_files": [], "excluded_files": []},
        }
        atomic_json(claims_path, claims)
        return {"status": "initialized", "claims": str(claims_path)}

    claims = _load_claims(claims_path)
    if command == "claims-node":
        action = _upsert_claim(
            claims.setdefault("nodes", []),
            args.id,
            {
                "name": args.name,
                "type": args.node_type,
                "description": args.description,
                "evidence_ids": _claim_values(args.evidence_ids) if args.evidence_ids is not None else None,
            },
        )
        detail = {"kind": "node", "id": args.id, "action": action}
    elif command == "claims-edge":
        action = _upsert_claim(
            claims.setdefault("edges", []),
            args.id,
            {
                "source": args.source,
                "target": args.target,
                "type": args.edge_type,
                "label": args.label,
                "confidence": args.confidence,
                "evidence_ids": _claim_values(args.evidence_ids) if args.evidence_ids is not None else None,
            },
        )
        detail = {"kind": "edge", "id": args.id, "action": action}
    elif command == "claims-chain":
        claims["main_chain"] = _claim_values(args.node_ids)
        detail = {"kind": "main_chain", "count": len(claims["main_chain"]), "action": "updated"}
    elif command == "claims-branch":
        action = _upsert_claim(
            claims.setdefault("branches", []),
            args.id,
            {
                "from": args.from_node,
                "condition": args.condition,
                "path": _claim_values(args.path_ids) if args.path_ids is not None else None,
                "evidence_ids": _claim_values(args.evidence_ids) if args.evidence_ids is not None else None,
            },
        )
        detail = {"kind": "branch", "id": args.id, "action": action}
    elif command == "claims-coverage":
        included = list(args.included_file)
        if args.include_all:
            cards = json.loads(Path(args.cards).resolve().read_text(encoding="utf-8"))
            included.extend(
                file_name
                for card in cards.get("cards", [])
                for file_name in referenced_files(card)
                if file_name
            )
        coverage = claims.setdefault("coverage", {})
        coverage["included_files"] = sorted(set(included))
        coverage.setdefault("excluded_files", [])
        detail = {"kind": "coverage", "count": len(coverage["included_files"]), "action": "updated"}
    elif command == "claims-exclusion":
        coverage = claims.setdefault("coverage", {})
        exclusions = coverage.setdefault("excluded_files", [])
        exclusion = next((item for item in exclusions if item.get("file") == args.file), None)
        if exclusion is None:
            exclusion = {"file": args.file}
            exclusions.append(exclusion)
            action = "added"
        else:
            action = "updated"
        exclusion.update({"reason": args.reason, "evidence_ids": _claim_values(args.evidence_ids)})
        detail = {"kind": "exclusion", "id": args.file, "action": action}
    elif command == "claims-remove":
        key = {"node": "nodes", "edge": "edges", "branch": "branches"}[args.kind]
        before = len(claims.setdefault(key, []))
        claims[key] = [item for item in claims[key] if item.get("id") != args.id]
        detail = {"kind": args.kind, "id": args.id, "action": "removed" if len(claims[key]) < before else "not_found"}
    else:
        raise ValueError(f"Unsupported claims mutation: {command}")
    atomic_json(claims_path, claims)
    return {"status": "success", "claims": str(claims_path), **detail}


def finalize(args: argparse.Namespace) -> tuple[int, dict[str, Any]]:
    output_root = Path(args.output).resolve()
    cards_path = Path(args.cards).resolve() if args.cards else output_root / "evidence-cards.json"
    claims_path = Path(args.claims).resolve()
    card_payload = json.loads(cards_path.read_text(encoding="utf-8"))
    claims = json.loads(claims_path.read_text(encoding="utf-8"))
    canonical_claims_path = output_root / "scenario-claims.json"
    if claims_path != canonical_claims_path:
        atomic_json(canonical_claims_path, claims)
        if (
            claims_path.parent == output_root
            and claims_path.name.startswith("scenario-claims.candidate")
        ):
            claims_path.unlink(missing_ok=True)
    errors = validate_claims(claims, card_payload)
    if errors:
        payload = {"status": "validation_failed", "error_count": len(errors), "errors": errors}
        atomic_json(output_root / "validation-errors.json", payload)
        return 2, payload
    cards_by_id = {card["id"]: card for card in card_payload["cards"]}
    result = {
        "schema_version": SCHEMA_VERSION,
        "status": "complete",
        "generated_at": utc_now(),
        "strategy": "bounded_evidence_semantic_synthesis",
        "scenario": claims["scenario"],
        "nodes": claims["nodes"],
        "edges": claims["edges"],
        "main_chain": claims["main_chain"],
        "primary_data_path": claims["main_chain"],
        "branches": claims["branches"],
        "coverage": {
            **claims["coverage"],
            "guarantee": "One bounded macro data-relationship graph; every node and edge cites validated evidence; fields and record-specific values are forbidden as nodes.",
        },
        "artifacts": {
            "json": str(output_root / "scenario-relationship.json"),
            "compatibility_json": str(output_root / "relations.json"),
            "markdown": str(output_root / "relation-report.md"),
            "mermaid": str(output_root / "relations.mmd"),
            "evidence_index": str(output_root / "evidence.sqlite3"),
            "evidence_cards": str(cards_path),
        },
    }
    atomic_json(output_root / "scenario-relationship.json", result)
    atomic_json(output_root / "relations.json", result)
    write_report(result, cards_by_id, output_root / "relation-report.md")
    write_mermaid(result, output_root / "relations.mmd")
    write_scenario_database(result, card_payload, output_root / "evidence.sqlite3")
    validation_path = output_root / "validation-errors.json"
    if validation_path.exists():
        validation_path.unlink()
    return 0, compact_summary(result, 0, args.summary_limit)


def add_probe_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--input", default="/workspace/data")
    parser.add_argument("--output", default="/workspace/outputs/data-relations")
    parser.add_argument("--goal-file", default="/workspace/description.md")
    parser.add_argument("--field-result", default="")
    parser.add_argument("--ocr-mode", choices=["auto", "always", "never"], default="auto")
    parser.add_argument("--deadline-seconds", type=int, default=780)
    parser.add_argument("--resume", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--seed-cell-budget", type=int, default=100_000)
    parser.add_argument("--seed-file-bytes", type=int, default=8 * 1024 * 1024)
    parser.add_argument("--seed-values-per-column", type=int, default=256)
    parser.add_argument("--max-seed-values", type=int, default=20_000)
    parser.add_argument("--max-text-seeds", type=int, default=2_000)
    parser.add_argument("--profile-size", type=int, default=128)
    parser.add_argument("--frontier-values-per-column", type=int, default=128)
    parser.add_argument("--min-expansion-distinct-ratio", type=float, default=0.01)
    parser.add_argument("--max-matches-per-seed", type=int, default=50)
    parser.add_argument("--bootstrap-rows", type=int, default=1_000)
    parser.add_argument("--checkpoint-rows", type=int, default=50_000)
    parser.add_argument("--document-character-budget", type=int, default=2_000_000)
    parser.add_argument("--document-cards-per-file", type=int, default=40)
    parser.add_argument("--semantic-table-cell-budget", type=int, default=100_000)
    parser.add_argument("--table-character-budget", type=int, default=500_000)
    parser.add_argument("--table-cards-per-file", type=int, default=40)
    parser.add_argument("--max-evidence-cards", type=int, default=1_000)
    parser.add_argument("--summary-limit", type=int, default=20)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Discover one evidence-backed scenario relationship chain")
    commands = parser.add_subparsers(dest="command", required=True)
    analyze = commands.add_parser("analyze")
    add_probe_arguments(analyze)
    evidence = commands.add_parser("evidence")
    evidence.add_argument("--cards", default="/workspace/outputs/data-relations/evidence-cards.json")
    evidence.add_argument("--offset", type=int, default=0)
    evidence.add_argument("--limit", type=int, default=20)
    evidence.add_argument("--kind", action="append", default=[])
    evidence.add_argument("--file", default="")
    evidence.add_argument("--ids", default="")
    brief = commands.add_parser("brief")
    brief.add_argument("--brief", default="/workspace/outputs/data-relations/synthesis-brief.json")
    claims_init = commands.add_parser("claims-init")
    claims_init.add_argument("--claims", required=True)
    claims_init.add_argument("--name", required=True)
    claims_init.add_argument("--purpose", required=True)
    claims_init.add_argument("--force", action="store_true")
    claims_copy = commands.add_parser("claims-copy")
    claims_copy.add_argument("--source", required=True)
    claims_copy.add_argument("--claims", required=True)
    claims_copy.add_argument("--force", action="store_true")
    claims_node = commands.add_parser("claims-node")
    claims_node.add_argument("--claims", required=True)
    claims_node.add_argument("--id", required=True)
    claims_node.add_argument("--name")
    claims_node.add_argument("--node-type", choices=sorted(NODE_TYPES))
    claims_node.add_argument("--description")
    claims_node.add_argument("--evidence-ids")
    claims_edge = commands.add_parser("claims-edge")
    claims_edge.add_argument("--claims", required=True)
    claims_edge.add_argument("--id", required=True)
    claims_edge.add_argument("--source")
    claims_edge.add_argument("--target")
    claims_edge.add_argument("--edge-type", choices=sorted(EDGE_TYPES))
    claims_edge.add_argument("--label")
    claims_edge.add_argument("--confidence", type=float)
    claims_edge.add_argument("--evidence-ids")
    claims_chain = commands.add_parser("claims-chain")
    claims_chain.add_argument("--claims", required=True)
    claims_chain.add_argument("--node-ids", required=True)
    claims_branch = commands.add_parser("claims-branch")
    claims_branch.add_argument("--claims", required=True)
    claims_branch.add_argument("--id", required=True)
    claims_branch.add_argument("--from-node")
    claims_branch.add_argument("--condition")
    claims_branch.add_argument("--path-ids")
    claims_branch.add_argument("--evidence-ids")
    claims_coverage = commands.add_parser("claims-coverage")
    claims_coverage.add_argument("--claims", required=True)
    claims_coverage.add_argument("--cards", default="/workspace/outputs/data-relations/evidence-cards.json")
    claims_coverage.add_argument("--include-all", action="store_true")
    claims_coverage.add_argument("--included-file", action="append", default=[])
    claims_exclusion = commands.add_parser("claims-exclusion")
    claims_exclusion.add_argument("--claims", required=True)
    claims_exclusion.add_argument("--file", required=True)
    claims_exclusion.add_argument("--reason", required=True)
    claims_exclusion.add_argument("--evidence-ids", default="")
    claims_remove = commands.add_parser("claims-remove")
    claims_remove.add_argument("--claims", required=True)
    claims_remove.add_argument("--kind", choices=["node", "edge", "branch"], required=True)
    claims_remove.add_argument("--id", required=True)
    finish = commands.add_parser("finalize")
    finish.add_argument("--claims", required=True)
    finish.add_argument("--cards", default="")
    finish.add_argument("--output", default="/workspace/outputs/data-relations")
    finish.add_argument("--summary-limit", type=int, default=20)
    summary = commands.add_parser("summary")
    summary.add_argument("--result", default="/workspace/outputs/data-relations/scenario-relationship.json")
    summary.add_argument("--offset", type=int, default=0)
    summary.add_argument("--limit", type=int, default=20)
    relation = commands.add_parser("relation")
    relation.add_argument("relation_id")
    relation.add_argument("--result", default="/workspace/outputs/data-relations/scenario-relationship.json")
    chain = commands.add_parser("chain")
    chain.add_argument("--result", default="/workspace/outputs/data-relations/scenario-relationship.json")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")
    args = build_parser().parse_args(argv)
    try:
        if args.command == "analyze":
            payload = prepare_evidence(args)
            print_agent_json(payload)
            return 0
        if args.command == "evidence":
            payload = json.loads(Path(args.cards).read_text(encoding="utf-8"))
            identifiers = {item.strip() for item in args.ids.split(",") if item.strip()}
            print_agent_json({
                "status": "success",
                "evidence_page": evidence_page(
                    payload, args.offset, args.limit, set(args.kind), args.file, identifiers,
                ),
            })
            return 0
        if args.command == "brief":
            payload = json.loads(Path(args.brief).read_text(encoding="utf-8"))
            print_agent_json(payload)
            return 0
        if args.command.startswith("claims-"):
            print_agent_json(mutate_claims(args))
            return 0
        if args.command == "finalize":
            code, payload = finalize(args)
            stream = sys.stderr if code else sys.stdout
            print(json.dumps(payload, ensure_ascii=False, indent=2), file=stream)
            return code
        result = json.loads(Path(args.result).read_text(encoding="utf-8"))
        if args.command == "summary":
            print(json.dumps(compact_summary(result, args.offset, args.limit), ensure_ascii=False, indent=2))
            return 0
        if args.command == "chain":
            payload = {
                "status": "success",
                "scenario": result.get("scenario", {}),
                "main_chain": result.get("main_chain", []),
                "branches": result.get("branches", []),
            }
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return 0
        relation = next((item for item in result.get("edges", []) if item.get("id") == args.relation_id), None)
        if relation is None:
            print(json.dumps({"status": "not_found", "id": args.relation_id}, ensure_ascii=False), file=sys.stderr)
            return 2
        print(json.dumps({"status": "success", "relation": relation}, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:
        print(json.dumps({"status": "error", "message": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
