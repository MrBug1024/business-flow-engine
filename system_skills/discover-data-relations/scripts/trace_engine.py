#!/usr/bin/env python3
"""Build bounded, result-anchored row traces from discovered field evidence.

The relationship probe proves that values overlap between columns.  This module
adds the missing instance-level check: one candidate result row is kept as the
anchor while exact, evidence-backed keys are followed through the full sources.
Large tables are searched in full, but only a bounded set of redacted rows is
materialized into the trace artifact.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import re
import unicodedata
from collections import defaultdict
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from progressive_engine import (
    ColumnMeta,
    TableMeta,
    _duckdb,
    _fastexcel,
    _quoted_identifier,
    excel_column_name,
    extract_text_values,
    header_semantic,
    iter_document_segments,
    iter_table_rows,
    safe_preview,
    sha256_file,
    value_fingerprint,
)


TRACE_SCHEMA_VERSION = 1
# One result source and one anchor are the safe default.  A multi-row result
# table is deliberately *not* auto-anchored at its first row: callers must
# provide an explicit (file, table, row) selector before a trace can be
# promoted to evidence.  Wider local probing is only used to build redacted
# selector candidates; it never expands the model-facing output beyond one
# selected chain.
DEFAULT_ANCHOR_CANDIDATES = 1
DEFAULT_RESULT_CANDIDATES = 1
DEFAULT_MAX_ROWS_PER_SOURCE = 8
DEFAULT_MAX_COLUMNS_PER_SOURCE = 48
DEFAULT_MAX_HOPS = 4
MAX_KEYSETS_PER_EDGE = 2
DEFAULT_SELECTION_PREVIEW_ROWS = 8
DOCUMENT_CONTEXT_ROLES = {"template", "rule", "reference"}
DOCUMENT_LINK_ROLES = {"input"}
SAFE_DOCUMENT_LOCATOR = re.compile(
    r"^(?:line|paragraph|page|slide):[1-9]\d*$|^ocr:line:[1-9]\d*$"
)
TABLE_ROW_LOCATOR = re.compile(
    r"^table:([^;]+);row:([1-9]\d*)(?:;column:([^;]+))?$"
)

RESULT_ROLES = {
    "result_or_outcome_record": 7.0,
    "decision_or_validation_record": 5.0,
    "transaction_or_outcome_record": 2.0,
}


def _normalize(value: Any) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", str(value or "")).strip()).casefold()


def _fingerprint(value: Any) -> str:
    return hashlib.sha256(_normalize(value).encode("utf-8")).hexdigest()


def _stable_id(prefix: str, *parts: Any) -> str:
    payload = "\0".join(str(part) for part in parts)
    return prefix + hashlib.sha1(payload.encode("utf-8")).hexdigest()[:12]


def _json_value(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, (str, int, float, bool)):
        return value
    if hasattr(value, "isoformat"):
        try:
            return value.isoformat()
        except (TypeError, ValueError):
            pass
    return str(value)


def _table_from_json(raw: dict[str, Any]) -> TableMeta:
    columns = [
        ColumnMeta(
            str(item.get("name", "")),
            str(item.get("query_name", item.get("name", ""))),
            int(item.get("index", index)),
            str(item.get("kind", "other")),
            str(item.get("base", "")),
        )
        for index, item in enumerate(raw.get("columns", []))
        if isinstance(item, dict)
    ]
    return TableMeta(
        str(raw.get("key", "")),
        int(raw.get("file_id", 0)),
        str(raw.get("file_path", "")),
        str(raw.get("table_name", "")),
        raw.get("row_count") if isinstance(raw.get("row_count"), int) else None,
        int(raw.get("column_count", len(columns))),
        columns,
        str(raw.get("engine", "")),
        int(raw.get("header_row", 0)),
        float(raw.get("header_confidence", 1.0)),
        str(raw.get("header_detection", "default_first_row")),
    )


def _indexes(field_result: dict[str, Any]) -> tuple[
    dict[str, dict[str, Any]], dict[tuple[str, str], TableMeta], dict[tuple[str, str], dict[str, Any]]
]:
    files: dict[str, dict[str, Any]] = {}
    tables: dict[tuple[str, str], TableMeta] = {}
    stats: dict[tuple[str, str], dict[str, Any]] = {}
    for file_info in field_result.get("files", []):
        if not isinstance(file_info, dict):
            continue
        path = str(file_info.get("path", ""))
        files[path] = file_info
        for raw_table in file_info.get("tables", []):
            if not isinstance(raw_table, dict):
                continue
            table = _table_from_json(raw_table)
            tables[(path, table.table_name)] = table
    for item in field_result.get("column_statistics", []):
        if not isinstance(item, dict):
            continue
        key = (str(item.get("file", "")), str(item.get("column", "")))
        current = stats.get(key)
        if current is None or int(item.get("nonempty", 0)) > int(current.get("nonempty", 0)):
            stats[key] = item
    return files, tables, stats


def _file_roles_from_manifest(role_manifest: Mapping[str, Any] | None) -> dict[str, str]:
    """Return digest-bound ``__file__`` roles resolved to real source paths.

    ``scenario_engine`` verifies the signed manifest and writes this small
    path-to-role projection into the trace authority reference.  Keeping it
    distinct from table roles is important: a PDF result is a file-scoped
    business result, not an imaginary one-row table.
    """

    if not isinstance(role_manifest, Mapping):
        return {}
    raw = role_manifest.get("file_roles")
    if not isinstance(raw, Mapping):
        return {}
    return {
        str(path): str(role)
        for path, role in raw.items()
        if str(path) and str(role) in {"input", "result", "rule", "reference", "template", "ignore"}
    }


def _document_result_candidates(
    files: Mapping[str, dict[str, Any]],
    tables: Mapping[tuple[str, str], TableMeta],
    file_roles: Mapping[str, str],
) -> list[dict[str, Any]]:
    table_files = {path for path, _table in tables}
    candidates = [
        {
            "file": path,
            "table": "",
            "anchor_kind": "document_segment",
            "inferred_role": "result",
            "approved_role": "result",
            "row_count": None,
            "score": 1.0,
            "selection_kind": "approved_file_role_manifest",
        }
        for path, file_info in files.items()
        if path not in table_files
        and str(file_info.get("kind", "")) == "document"
        and file_roles.get(path) == "result"
    ]
    return sorted(candidates, key=lambda item: item["file"])


def _side(
    relation: Mapping[str, Any], evidence: Mapping[str, Any], name: str,
) -> dict[str, str]:
    return {
        "file": str(relation.get(name, "")),
        "field": str(relation.get(f"{name}_column", "")),
        "locator": str(evidence.get(f"{name}_locator", "")),
    }


def _segment_digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _read_exact_document_segments(
    input_root: Path,
    requests: Mapping[str, set[str]],
    ocr_mode: str,
) -> dict[tuple[str, str], dict[str, Any]]:
    """Read only evidence-addressed segments and bind them to current bytes."""

    found: dict[tuple[str, str], dict[str, Any]] = {}
    root = input_root.resolve()
    for file_name, locators in sorted(requests.items()):
        safe_locators = {locator for locator in locators if SAFE_DOCUMENT_LOCATOR.fullmatch(locator)}
        if not safe_locators:
            continue
        path = (root / file_name).resolve()
        if root != path and root not in path.parents:
            continue
        if not path.is_file():
            continue
        source_digest = sha256_file(path)
        remaining = set(safe_locators)
        try:
            for locator, text in iter_document_segments(path, ocr_mode):
                if locator not in remaining:
                    continue
                found[(file_name, locator)] = {
                    "path": file_name,
                    "locator": locator,
                    "text": text,
                    "source_digest": source_digest,
                    "segment_digest": _segment_digest(text),
                }
                remaining.remove(locator)
                if not remaining:
                    break
        except Exception:
            # Extraction failures are evidence failures.  They never degrade
            # to filename similarity, OCR guesses, or unlocated snippets.
            continue
    return found


def _verified_document_side(
    side: Mapping[str, str],
    fingerprint: str,
    segments: Mapping[tuple[str, str], dict[str, Any]],
) -> dict[str, Any] | None:
    segment = segments.get((str(side.get("file", "")), str(side.get("locator", ""))))
    if segment is None:
        return None
    matches = [
        value
        for value in extract_text_values(
            str(segment.get("text", "")),
            str(segment.get("locator", "")),
            str(segment.get("path", "")),
        )
        if value.fingerprint == fingerprint and int(value.specificity) >= 3
    ]
    if not matches:
        return None
    field = str(side.get("field", ""))
    aligned = [value for value in matches if not field or _normalize(value.column) == _normalize(field)]
    match = aligned[0] if aligned else matches[0]
    return {
        "kind": "document_segment",
        "path": str(segment["path"]),
        "locator": str(segment["locator"]),
        "field": field or str(match.column),
        "source_digest": str(segment["source_digest"]),
        "segment_digest": str(segment["segment_digest"]),
        "value_fingerprint": fingerprint,
        "value_preview": str(match.preview),
        "specificity": int(match.specificity),
        "snippet": str(segment.get("text", "")).replace("\r", " ").replace("\n", " ")[:320],
    }


def _verified_table_side(
    side: Mapping[str, str],
    fingerprint: str,
    tables: Mapping[tuple[str, str], TableMeta],
    matcher: "TableMatcher",
) -> dict[str, Any] | None:
    locator = str(side.get("locator", ""))
    match = TABLE_ROW_LOCATOR.fullmatch(locator)
    if match is None:
        return None
    table_name, raw_row_number, locator_field = match.groups()
    file_name = str(side.get("file", ""))
    field = str(side.get("field", "")) or str(locator_field or "")
    table = tables.get((file_name, table_name))
    column = next((item for item in table.columns if item.name == field), None) if table else None
    if table is None or column is None:
        return None
    row = matcher.exact_row(table, [column], int(raw_row_number))
    value = row.get("values", {}).get(field) if row else None
    if row is None or value_fingerprint(value) != fingerprint:
        return None
    return {
        "kind": "table_row",
        "path": file_name,
        "table": table_name,
        "row_number": int(raw_row_number),
        "field": field,
        "value_fingerprint": fingerprint,
    }


def _exact_document_proofs(
    input_root: Path,
    field_result: Mapping[str, Any],
    files: Mapping[str, dict[str, Any]],
    tables: Mapping[tuple[str, str], TableMeta],
    matcher: "TableMatcher",
    ocr_mode: str,
) -> list[dict[str, Any]]:
    """Replay confirmed field evidence involving at least one document side."""

    document_files = {
        path for path, info in files.items() if str(info.get("kind", "")) == "document"
    }
    requests: dict[str, set[str]] = defaultdict(set)
    candidates: list[tuple[dict[str, Any], dict[str, Any], dict[str, str], dict[str, str]]] = []
    for relation in field_result.get("relations", []):
        if not isinstance(relation, dict) or relation.get("verdict") != "confirmed":
            continue
        for evidence in relation.get("evidence", []):
            if not isinstance(evidence, dict) or evidence.get("kind") != "exact_value_match":
                continue
            fingerprint = str(evidence.get("fingerprint", "")).casefold()
            if not re.fullmatch(r"[0-9a-f]{64}", fingerprint):
                continue
            source = _side(relation, evidence, "source")
            target = _side(relation, evidence, "target")
            if source["file"] not in document_files and target["file"] not in document_files:
                continue
            for side in (source, target):
                if side["file"] in document_files and SAFE_DOCUMENT_LOCATOR.fullmatch(side["locator"]):
                    requests[side["file"]].add(side["locator"])
            candidates.append((relation, evidence, source, target))
    segments = _read_exact_document_segments(input_root, requests, ocr_mode)
    proofs: list[dict[str, Any]] = []
    for relation, evidence, source, target in candidates:
        fingerprint = str(evidence.get("fingerprint", "")).casefold()
        verified: list[dict[str, Any] | None] = []
        for side in (source, target):
            if side["file"] in document_files:
                verified.append(_verified_document_side(side, fingerprint, segments))
            else:
                verified.append(_verified_table_side(side, fingerprint, tables, matcher))
        if verified[0] is None or verified[1] is None:
            continue
        proofs.append({
            "relation_id": str(relation.get("id", "")),
            "confidence": float(relation.get("confidence", 0.0)),
            "fingerprint": fingerprint,
            "source": verified[0],
            "target": verified[1],
        })
    unique = {
        (
            proof["relation_id"],
            str(proof["source"].get("path", "")),
            str(proof["source"].get("locator", proof["source"].get("row_number", ""))),
            str(proof["target"].get("path", "")),
            str(proof["target"].get("locator", proof["target"].get("row_number", ""))),
            proof["fingerprint"],
        ): proof
        for proof in proofs
    }
    return [unique[key] for key in sorted(unique)]


def _table_for_column(
    tables: dict[tuple[str, str], TableMeta], file_name: str, column: str, locator: str = ""
) -> TableMeta | None:
    table_name = ""
    match = re.search(r"(?:^|;)table:([^;]+)", locator)
    if match:
        table_name = match.group(1)
    exact = tables.get((file_name, table_name)) if table_name else None
    if exact and any(item.name == column for item in exact.columns):
        return exact
    candidates = [
        table for (path, _name), table in tables.items()
        if path == file_name and any(item.name == column for item in table.columns)
    ]
    return min(candidates, key=lambda item: item.row_count or 0) if candidates else None


def _relation_score(
    relation: dict[str, Any], source_column: ColumnMeta, target_column: ColumnMeta,
    stats: dict[tuple[str, str], dict[str, Any]],
) -> float:
    score = float(relation.get("confidence", 0.0)) * 0.55
    if _normalize(source_column.name) == _normalize(target_column.name):
        score += 0.2
    if source_column.kind in {"id", "code", "uuid", "reference"}:
        score += 0.1
    if target_column.kind in {"id", "code", "uuid", "reference"}:
        score += 0.1
    source_stats = stats.get((str(relation.get("source", "")), source_column.name), {})
    target_stats = stats.get((str(relation.get("target", "")), target_column.name), {})
    score += min(
        0.05,
        max(float(source_stats.get("distinct_ratio", 0.0)), float(target_stats.get("distinct_ratio", 0.0))) * 0.05,
    )
    if relation.get("verdict") != "confirmed":
        score -= 0.15
    if int(relation.get("evidence_count", 0)) < 2:
        score -= 0.08
    return round(max(0.0, min(1.0, score)), 4)


def _build_edges(
    field_result: dict[str, Any], tables: dict[tuple[str, str], TableMeta],
    stats: dict[tuple[str, str], dict[str, Any]],
) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for relation in field_result.get("relations", []):
        if not isinstance(relation, dict) or relation.get("verdict") != "confirmed":
            continue
        source_file = str(relation.get("source", ""))
        target_file = str(relation.get("target", ""))
        source_field = str(relation.get("source_column", ""))
        target_field = str(relation.get("target_column", ""))
        evidence = next(
            (item for item in relation.get("evidence", []) if isinstance(item, dict)), {}
        )
        source_table = _table_for_column(
            tables, source_file, source_field, str(evidence.get("source_locator", ""))
        )
        target_table = _table_for_column(
            tables, target_file, target_field, str(evidence.get("target_locator", ""))
        )
        if source_table is None or target_table is None:
            continue
        source_column = next((item for item in source_table.columns if item.name == source_field), None)
        target_column = next((item for item in target_table.columns if item.name == target_field), None)
        if source_column is None or target_column is None:
            continue
        candidate = {
            "relation_id": str(relation.get("id", "")),
            "source_field": source_field,
            "target_field": target_field,
            "confidence": float(relation.get("confidence", 0.0)),
            "evidence_count": int(relation.get("evidence_count", 0)),
            "score": _relation_score(relation, source_column, target_column, stats),
        }
        grouped[(source_file, source_table.table_name, target_file, target_table.table_name)].append(candidate)

    edges: list[dict[str, Any]] = []
    for (source_file, source_table, target_file, target_table), candidates in grouped.items():
        candidates.sort(key=lambda item: (-item["score"], -item["evidence_count"], item["relation_id"]))
        eligible = [item for item in candidates if float(item["score"]) >= 0.62][:6]
        if not eligible:
            continue
        key_sets = [
            {
                "key_set_index": index,
                "pairs": [{"source_field": item["source_field"], "target_field": item["target_field"]}],
                "score": item["score"],
                "relation_ids": [item["relation_id"]],
                "mode": "single_key",
            }
            for index, item in enumerate(eligible)
        ]
        for left, right in itertools.combinations(eligible[:4], 2):
            if left["source_field"] == right["source_field"] or left["target_field"] == right["target_field"]:
                continue
            key_sets.append({
                "key_set_index": len(key_sets),
                "pairs": [
                    {"source_field": left["source_field"], "target_field": left["target_field"]},
                    {"source_field": right["source_field"], "target_field": right["target_field"]},
                ],
                "score": round(min(float(left["score"]), float(right["score"])), 4),
                "relation_ids": [left["relation_id"], right["relation_id"]],
                "mode": "composite_key",
            })
            if len(key_sets) >= 10:
                break
        edges.append({
            "edge_id": _stable_id("trace-edge-", source_file, source_table, target_file, target_table),
            "source_file": source_file,
            "source_table": source_table,
            "target_file": target_file,
            "target_table": target_table,
            "key_sets": key_sets,
        })
    return sorted(edges, key=lambda item: item["edge_id"])


def _apply_reviewed_overrides(
    edges: Sequence[dict[str, Any]], tables: dict[tuple[str, str], TableMeta],
    overrides: Sequence[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Apply reviewer-confirmed key sets without turning values into rules.

    A correction may name a composite business key that statistical discovery
    cannot prove strongly enough on its own.  We still validate the referenced
    files, tables and fields locally.  The override only controls which keys
    are traced; it never supplies a row value, a domain predicate or a result.
    """

    result = [dict(edge) for edge in edges]
    by_pair = {
        (
            str(edge.get("source_file", "")), str(edge.get("source_table", "")),
            str(edge.get("target_file", "")), str(edge.get("target_table", "")),
        ): index
        for index, edge in enumerate(result)
    }
    for index, raw in enumerate(overrides, 1):
        if not isinstance(raw, dict):
            raise ValueError(f"Reviewed relation override {index} must be an object")
        source_file = str(raw.get("source_file", ""))
        source_table = str(raw.get("source_table", ""))
        target_file = str(raw.get("target_file", ""))
        target_table = str(raw.get("target_table", ""))
        source_meta = tables.get((source_file, source_table))
        target_meta = tables.get((target_file, target_table))
        pairs = raw.get("key_pairs") if isinstance(raw.get("key_pairs"), list) else []
        if source_meta is None or target_meta is None or not pairs:
            raise ValueError(
                f"Reviewed relation override {index} does not name two current trace tables and a key set"
            )
        source_columns = {item.name for item in source_meta.columns}
        target_columns = {item.name for item in target_meta.columns}
        normalized_pairs = [
            {"source_field": str(item.get("source_field", "")), "target_field": str(item.get("target_field", ""))}
            for item in pairs if isinstance(item, dict)
        ]
        if not normalized_pairs or any(
            not item["source_field"] or not item["target_field"]
            or item["source_field"] not in source_columns
            or item["target_field"] not in target_columns
            for item in normalized_pairs
        ):
            raise ValueError(
                f"Reviewed relation override {index} references a missing or empty trace field"
            )
        override_key_set = {
            "key_set_index": 0,
            "pairs": normalized_pairs,
            "score": 1.0,
            "relation_ids": ["review:" + str(raw.get("review_correction_id", "user_confirmed"))],
            "mode": "reviewed_" + ("composite_key" if len(normalized_pairs) > 1 else "single_key"),
            "review_confirmed": True,
        }
        key = (source_file, source_table, target_file, target_table)
        existing_index = by_pair.get(key)
        if existing_index is None:
            result.append({
                "edge_id": _stable_id("trace-edge-review-", source_file, source_table, target_file, target_table),
                "source_file": source_file,
                "source_table": source_table,
                "target_file": target_file,
                "target_table": target_table,
                "key_sets": [override_key_set],
                "review_confirmed": True,
            })
            by_pair[key] = len(result) - 1
            continue
        existing = result[existing_index]
        # A confirmed correction says which business key this endpoint must
        # use.  Retaining automatic alternatives would let a failed corrected
        # lookup quietly fall back to the very inferred key the reviewer
        # rejected.  If the confirmed key cannot materialize the selected
        # result chain, the platform reports that failure for another user
        # decision instead of fabricating a successful trace.
        existing["key_sets"] = [override_key_set]
        existing["review_confirmed"] = True
    return sorted(result, key=lambda item: str(item.get("edge_id", "")))


def _role_by_table(cards: Sequence[dict[str, Any]]) -> dict[tuple[str, str], str]:
    result: dict[tuple[str, str], str] = {}
    for card in cards:
        if not isinstance(card, dict) or card.get("kind") != "table_schema":
            continue
        sources = card.get("sources") if isinstance(card.get("sources"), list) else []
        source = sources[0] if sources and isinstance(sources[0], dict) else {}
        facts = card.get("facts") if isinstance(card.get("facts"), dict) else {}
        result[(str(source.get("file", "")), str(facts.get("table", "")))] = str(
            facts.get("inferred_material_role", "")
        )
    return result


def _result_candidates(
    files: dict[str, dict[str, Any]], tables: dict[tuple[str, str], TableMeta],
    edges: Sequence[dict[str, Any]], cards: Sequence[dict[str, Any]], limit: int,
    authoritative_roles: Mapping[tuple[str, str], str] | None = None,
) -> list[dict[str, Any]]:
    if authoritative_roles is not None:
        candidates = []
        for (file_name, table_name), table in tables.items():
            if authoritative_roles.get((file_name, table_name)) != "result":
                continue
            candidates.append({
                "file": file_name,
                "table": table_name,
                "inferred_role": "result",
                "approved_role": "result",
                "row_count": table.row_count,
                "score": 1.0,
                "selection_kind": "approved_role_manifest",
            })
        candidates.sort(key=lambda item: (item.get("row_count") or 0, item["file"], item["table"]))
        # User-confirmed result roles are not a ranking hint.  If there is
        # more than one, hiding all but the first would recreate exactly the
        # arbitrary-result-table failure this trace contract prevents.  Keep
        # every identity so the Workbench can require an explicit endpoint.
        return candidates
    roles = _role_by_table(cards)
    degree: dict[str, set[str]] = defaultdict(set)
    for edge in edges:
        degree[edge["source_file"]].add(edge["target_file"])
        degree[edge["target_file"]].add(edge["source_file"])
    candidates = []
    for (file_name, table_name), table in tables.items():
        role = roles.get((file_name, table_name), "")
        if role == "rule_or_policy_material":
            continue
        row_count = table.row_count or 0
        score = RESULT_ROLES.get(role, 0.0)
        score += min(3.0, len(degree.get(file_name, set())) * 0.75)
        if 0 < row_count <= 1_000:
            score += 2.5
        elif row_count <= 10_000:
            score += 1.0
        if files.get(file_name, {}).get("role_score", 0) and role not in RESULT_ROLES:
            score -= 1.0
        candidates.append({
            "file": file_name,
            "table": table_name,
            "inferred_role": role or "unknown",
            "row_count": table.row_count,
            "score": round(score, 4),
        })
    candidates.sort(key=lambda item: (-float(item["score"]), item.get("row_count") or 0, item["file"]))
    return candidates[: max(1, limit)]


def _incident_columns(file_name: str, table_name: str, edges: Sequence[dict[str, Any]]) -> set[str]:
    names: set[str] = set()
    for edge in edges:
        if edge["source_file"] == file_name and edge["source_table"] == table_name:
            names.update(
                str(pair["source_field"])
                for key_set in edge["key_sets"] for pair in key_set["pairs"]
            )
        if edge["target_file"] == file_name and edge["target_table"] == table_name:
            names.update(
                str(pair["target_field"])
                for key_set in edge["key_sets"] for pair in key_set["pairs"]
            )
    return names


def _selected_columns(table: TableMeta, mandatory: Iterable[str], limit: int) -> list[ColumnMeta]:
    required = set(mandatory)
    selected: list[ColumnMeta] = []

    def add(items: Iterable[ColumnMeta]) -> None:
        known = {item.name for item in selected}
        for item in items:
            if item.name not in known and len(selected) < limit:
                selected.append(item)
                known.add(item.name)

    add(item for item in table.columns if item.name in required)
    add(table.columns[: min(24, limit)])
    add(item for item in table.columns if item.kind in {"id", "code", "name", "reference", "uuid"})
    remaining = [item for item in table.columns if item.name not in {value.name for value in selected}]
    if remaining and len(selected) < limit:
        slots = limit - len(selected)
        positions = sorted({min(len(remaining) - 1, int(index * len(remaining) / slots)) for index in range(slots)})
        add(remaining[index] for index in positions)
    return sorted(selected, key=lambda item: item.index)


class TableMatcher:
    def __init__(self, input_root: Path, max_rows: int):
        self.input_root = input_root
        self.max_rows = max_rows
        self._arrow_columns: dict[tuple[str, str, int], Any] = {}

    def first_rows(
        self, table: TableMeta, columns: Sequence[ColumnMeta], row_limit: int,
    ) -> list[dict[str, Any]]:
        path = self.input_root / table.file_path
        result = []
        for row_number, values in iter_table_rows(table, path, columns, row_limit=row_limit):
            if any(_normalize(value) for value in values.values()):
                result.append({"row_number": row_number, "values": values})
        return result

    def exact_row(
        self, table: TableMeta, columns: Sequence[ColumnMeta], row_number: int,
    ) -> dict[str, Any] | None:
        offset = row_number - table.header_row - 2
        if offset < 0:
            return None
        rows = list(iter_table_rows(
            table, self.input_root / table.file_path, columns, start_offset=offset, row_limit=1
        ))
        if not rows:
            return None
        actual, values = rows[0]
        return {"row_number": actual, "values": values}

    def match(
        self, table: TableMeta, columns: Sequence[ColumnMeta], pairs: Sequence[tuple[str, Any]],
        *, materialize: bool = True,
    ) -> tuple[list[dict[str, Any]], int, str]:
        if not pairs or any(not _normalize(value) for _column, value in pairs):
            return [], 0, "empty_key"
        path = self.input_root / table.file_path
        if path.suffix.casefold() == ".xlsx" and _fastexcel() is not None:
            try:
                return self._match_xlsx_arrow(table, path, columns, pairs, materialize=materialize)
            except (ImportError, OSError, RuntimeError, TypeError, ValueError):
                if (table.row_count or 0) >= 50_000:
                    return [], 0, "accelerated_search_unavailable"
        matches: list[dict[str, Any]] = []
        total = 0
        pair_map = {column: _normalize(value) for column, value in pairs}
        for row_number, values in iter_table_rows(table, path, columns):
            if all(_normalize(values.get(column)) == expected for column, expected in pair_map.items()):
                total += 1
                if len(matches) < self.max_rows:
                    matches.append({"row_number": row_number, "values": values})
        return matches, total, "streaming_full_scan"

    def _match_xlsx_arrow(
        self, table: TableMeta, path: Path, columns: Sequence[ColumnMeta], pairs: Sequence[tuple[str, Any]],
        *, materialize: bool,
    ) -> tuple[list[dict[str, Any]], int, str]:
        import pyarrow as pa
        import pyarrow.compute as pc

        by_name = {column.name: column for column in table.columns}
        reader = _fastexcel().read_excel(str(path))

        resolved_path = str(path.resolve())

        def ensure_arrow_columns(metas: Sequence[ColumnMeta]) -> None:
            missing = [
                meta for meta in metas
                if (resolved_path, table.table_name, meta.index) not in self._arrow_columns
            ]
            if not missing:
                return
            key_batch = reader.load_sheet_eager(
                table.table_name,
                header_row=table.header_row,
                use_columns=[meta.index for meta in missing],
                dtypes="string",
                dtype_coercion="coerce",
            )
            for index, meta in enumerate(missing):
                self._arrow_columns[(resolved_path, table.table_name, meta.index)] = key_batch.column(index)

        def arrow_column(meta: ColumnMeta) -> Any:
            ensure_arrow_columns([meta])
            return self._arrow_columns[(resolved_path, table.table_name, meta.index)]

        # A logical composite key must be read as one physical projection.
        # Calling ``load_sheet_eager`` once per key field turns a three-field
        # lookup on a large XLSX into three full ZIP/XML passes.  That is not
        # an AI-context problem, but it makes deterministic tracing slow
        # enough that callers are tempted to fall back to partial samples.
        # Load every predicate column together, then reuse the cached Arrow
        # arrays for the mask.  Display columns remain lazy unless the caller
        # asks to materialize the bounded matching rows.
        key_metas: list[ColumnMeta] = []
        for column, _value in pairs:
            meta = by_name.get(column)
            if meta is None:
                return [], 0, "missing_key_column"
            key_metas.append(meta)
        ensure_arrow_columns(key_metas)
        if not materialize:
            ensure_arrow_columns(columns)

        mask = None
        for column, value in pairs:
            meta = by_name[column]
            array = arrow_column(meta)
            array = pc.cast(array, pa.string(), safe=False)
            normalized = pc.fill_null(array, "")
            if hasattr(pc, "utf8_normalize"):
                normalized = pc.utf8_normalize(normalized, form="NFKC")
            normalized = pc.utf8_lower(normalized)
            normalized = pc.replace_substring_regex(normalized, pattern=r"\s+", replacement="")
            current = pc.equal(normalized, _normalize(value))
            mask = current if mask is None else pc.and_(mask, current)
        indexes = pc.indices_nonzero(pc.fill_null(mask, False))
        total = len(indexes)
        bounded = indexes.slice(0, self.max_rows)
        physical_indexes = [int(item.as_py()) for item in bounded]
        if materialize:
            result = self._materialize_xlsx_rows(
                table, path, columns, physical_indexes, reader
            )
        else:
            arrays = [(column, arrow_column(column)) for column in columns]
            result = [
                {
                    "row_number": index + table.header_row + 2,
                    "values": {
                        column.name: array[index].as_py()
                        for column, array in arrays
                    },
                }
                for index in physical_indexes
            ]
        return result, total, "fastexcel_pyarrow_full_scan"

    @staticmethod
    def _materialize_xlsx_rows(
        table: TableMeta, path: Path, columns: Sequence[ColumnMeta],
        physical_indexes: Sequence[int], reader: Any,
    ) -> list[dict[str, Any]]:
        if not physical_indexes:
            return []
        duckdb = _duckdb()
        if duckdb is not None:
            connection = duckdb.connect()
            try:
                projection = ", ".join(_quoted_identifier(column.query_name) for column in columns)
                cell_range = (
                    f"A{table.header_row + 1}:"
                    f"{excel_column_name(max(1, table.column_count))}1048576"
                )
                offsets = ", ".join(str(max(0, int(value))) for value in physical_indexes)
                query = (
                    "WITH numbered AS ("
                    f"SELECT row_number() OVER () - 1 AS __offset, {projection} "
                    "FROM read_xlsx(?, sheet=?, range=?, header=true, all_varchar=true, ignore_errors=true)"
                    ") "
                    f"SELECT __offset + {table.header_row + 2} AS __row_number, {projection} "
                    f"FROM numbered WHERE __offset IN ({offsets}) ORDER BY __offset"
                )
                rows = connection.execute(
                    query, [str(path), table.table_name, cell_range]
                ).fetchall()
                return [
                    {
                        "row_number": int(row[0]),
                        "values": {
                            column.name: row[index + 1]
                            for index, column in enumerate(columns)
                        },
                    }
                    for row in rows
                ]
            except Exception:
                pass
            finally:
                connection.close()
        display_batch = reader.load_sheet_eager(
            table.table_name,
            header_row=table.header_row,
            use_columns=[item.index for item in columns],
            dtypes="string",
            dtype_coercion="coerce",
        )
        records = display_batch.take(list(physical_indexes)).to_pylist()
        return [
            {
                "row_number": physical_index + table.header_row + 2,
                "values": {
                    column.name: record.get(display_batch.schema.names[index])
                    for index, column in enumerate(columns)
                },
            }
            for physical_index, record in zip(physical_indexes, records)
        ]


def _display_value(column: ColumnMeta, value: Any) -> Any:
    value = _json_value(value)
    if value is None or value == "":
        return value
    normalized_header = _normalize(column.name)
    sensitive_person_name = any(
        marker in normalized_header
        for marker in ("人员姓名", "患者姓名", "客户姓名", "联系人姓名", "patientname", "customername")
    )
    if column.kind in {"id", "uuid", "email", "phone"} or sensitive_person_name:
        return safe_preview(column.kind if column.kind != "other" else "id", value)
    text = str(value).replace("\r", " ").replace("\n", " ")
    return text[:160] + ("…" if len(text) > 160 else "")


def _bounded_row(table: TableMeta, raw: dict[str, Any]) -> dict[str, Any]:
    columns = {item.name: item for item in table.columns}
    return {
        "row_number": int(raw["row_number"]),
        "values": {
            name: _display_value(columns.get(name, ColumnMeta(name, name, 0, "other", "")), value)
            for name, value in raw.get("values", {}).items()
        },
    }


def _endpoint(edge: dict[str, Any], side: str) -> tuple[str, str]:
    return str(edge[f"{side}_file"]), str(edge[f"{side}_table"])


def _orient_key_set(
    edge: dict[str, Any], known_endpoint: tuple[str, str], key_set: dict[str, Any],
) -> tuple[str, str, list[tuple[str, str]]]:
    """Orient a key set relative to one exact table endpoint.

    File paths are not sufficient identities: a workbook can contain several
    business tables with the same columns.  Keeping the table in the endpoint
    also makes same-file, cross-sheet links traceable rather than looking like
    self-loops.
    """
    if known_endpoint == _endpoint(edge, "source"):
        return edge["target_file"], edge["target_table"], [
            (str(item["source_field"]), str(item["target_field"])) for item in key_set["pairs"]
        ]
    if known_endpoint == _endpoint(edge, "target"):
        return edge["source_file"], edge["source_table"], [
            (str(item["target_field"]), str(item["source_field"])) for item in key_set["pairs"]
        ]
    raise ValueError("Known endpoint is not an endpoint of this trace edge")


def _keyset_probe_priority(
    edge: dict[str, Any], known_endpoint: tuple[str, str], key_set: dict[str, Any],
    tables: dict[tuple[str, str], TableMeta],
) -> tuple[int, float, float, int, tuple[str, ...]]:
    """Prefer high-specificity, evidence-backed keys before broad attributes.

    This ranking is deliberately domain-neutral: it depends only on field
    semantics already inferred from headers and on relation evidence.  It
    keeps result tracing from repeatedly full-scanning a large table once for
    every name/category-like field that happened to overlap.
    """
    target_file, target_table_name, mapping = _orient_key_set(edge, known_endpoint, key_set)
    known_file, known_table_name = known_endpoint
    known_columns = {
        item.name: item for item in tables.get((known_file, known_table_name), TableMeta("", 0, "", "", None, 0, [], "", 0, 0.0, "")).columns
    }
    target_columns = {
        item.name: item for item in tables.get((target_file, target_table_name), TableMeta("", 0, "", "", None, 0, [], "", 0, 0.0, "")).columns
    }
    kind_score = {"uuid": 6.0, "id": 6.0, "code": 5.0, "reference": 5.0, "email": 4.0, "phone": 4.0, "name": 1.0}
    specificity = 0.0
    headers_equal = 0
    for known_column, target_column in mapping:
        specificity += kind_score.get(known_columns.get(known_column, ColumnMeta("", "", 0, "other", "")).kind, 0.0)
        specificity += kind_score.get(target_columns.get(target_column, ColumnMeta("", "", 0, "other", "")).kind, 0.0)
        headers_equal += int(_normalize(known_column) == _normalize(target_column))
    # Composite keys are desirable only after their field-level specificity;
    # they are capped below to avoid unbounded retries.
    specificity += len(mapping) * 0.5 + headers_equal * 0.75
    return (
        0 if key_set.get("review_confirmed") else 1,
        -specificity,
        -float(key_set.get("score", 0.0)),
        -len(mapping),
        tuple(f"{left}\0{right}" for left, right in mapping),
    )


def _trace_anchor(
    candidate: dict[str, Any], anchor: dict[str, Any], tables: dict[tuple[str, str], TableMeta],
    edges: Sequence[dict[str, Any]], matcher: TableMatcher, max_columns: int, max_hops: int,
    *, materialize_rows: bool,
) -> dict[str, Any]:
    anchor_key = (candidate["file"], candidate["table"])
    anchor_table = tables[anchor_key]
    raw_samples: dict[tuple[str, str], dict[str, Any]] = {
        anchor_key: {"table": anchor_table, "rows": [anchor], "role": "result_anchor"}
    }
    traced_links: list[dict[str, Any]] = []
    attempted: set[tuple[str, tuple[str, str]]] = set()
    warnings: list[str] = []
    for _hop in range(max_hops):
        progress = False
        for edge in sorted(
            edges,
            key=lambda item: -max(float(value.get("score", 0.0)) for value in item["key_sets"]),
        ):
            known_endpoints = [
                endpoint
                for endpoint in (_endpoint(edge, "source"), _endpoint(edge, "target"))
                if endpoint in raw_samples
            ]
            if len(known_endpoints) != 1:
                continue
            known_endpoint = known_endpoints[0]
            target_file, target_table_name, _mapping = _orient_key_set(
                edge, known_endpoint, edge["key_sets"][0]
            )
            target_endpoint = (target_file, target_table_name)
            if target_endpoint in raw_samples or (edge["edge_id"], known_endpoint) in attempted:
                continue
            attempted.add((edge["edge_id"], known_endpoint))
            target_table = tables.get((target_file, target_table_name))
            if target_table is None:
                continue
            incident = _incident_columns(target_file, target_table_name, edges)
            target_columns = _selected_columns(target_table, incident, max_columns)
            probe_columns = [column for column in target_table.columns if column.name in incident]
            best_match: tuple[int, float, dict[str, Any], list[dict[str, Any]], str, list[tuple[str, Any]]] | None = None
            ordered_key_sets = sorted(
                edge["key_sets"],
                key=lambda item: _keyset_probe_priority(edge, known_endpoint, item, tables),
            )[:MAX_KEYSETS_PER_EDGE]
            for key_set in ordered_key_sets:
                target_file, target_table_name, mapping = _orient_key_set(edge, known_endpoint, key_set)
                for known_row in raw_samples[known_endpoint]["rows"]:
                    pairs = [
                        (target_column, known_row["values"].get(known_column))
                        for known_column, target_column in mapping
                    ]
                    if any(value is None or not _normalize(value) for _column, value in pairs):
                        continue
                    rows, total, engine = matcher.match(
                        target_table, probe_columns, pairs, materialize=False
                    )
                    if not rows:
                        if engine == "accelerated_search_unavailable":
                            warnings.append(
                                f"{target_file} / {target_table_name} requires accelerated full-table search"
                            )
                        continue
                    choice = (total, -float(key_set["score"]), key_set, rows, engine, pairs)
                    if best_match is None or choice[:2] < best_match[:2]:
                        best_match = choice
                    # A unique, high-specificity match is already the best
                    # causal continuation.  Avoid another complete scan just
                    # to compare weaker alternatives.
                    if total == 1:
                        break
                if best_match is not None and best_match[0] == 1:
                    break
            if best_match is not None:
                total, _negative_score, key_set, probe_rows, engine, pairs = best_match
                rows = probe_rows
                if materialize_rows:
                    rows, materialized_total, materialized_engine = matcher.match(
                        target_table, target_columns, pairs, materialize=True
                    )
                    if not rows:
                        continue
                    total = materialized_total
                    engine = materialized_engine
                raw_samples[(target_file, target_table_name)] = {
                    "table": target_table,
                    "rows": rows,
                    "role": "linked_source",
                }
                traced_links.append({
                    "edge_id": edge["edge_id"],
                    "source_file": edge["source_file"],
                    "source_table": edge["source_table"],
                    "target_file": edge["target_file"],
                    "target_table": edge["target_table"],
                    "key_set_index": key_set["key_set_index"],
                    "key_pairs": key_set["pairs"],
                    "relation_ids": key_set["relation_ids"],
                    "confidence": key_set["score"],
                    "matched_row_count": total,
                    "materialized_row_count": len(rows),
                    "fanout_warning": total > 1,
                    "materialization_truncated": total > len(rows),
                    "search_mode": engine,
                    "key_fingerprints": [_fingerprint(value) for _column, value in pairs],
                })
                progress = True
        if not progress:
            break

    sources = []
    for (path, table_name), sample in raw_samples.items():
        table = sample["table"]
        rows = [_bounded_row(table, row) for row in sample["rows"]]
        sources.append({
            "path": path,
            "table": table_name,
            "endpoint": {"file": path, "table": table_name},
            "role": sample["role"],
            "selected_columns": list(rows[0]["values"]) if rows else [],
            "rows": rows,
        })
    source_count = len(sources)
    fanout_penalty = sum(1 for item in traced_links if item["fanout_warning"])
    if fanout_penalty:
        warnings.append(
            f"{fanout_penalty} trace link(s) matched multiple rows; runtime execution must revalidate cardinality"
        )
    score = float(candidate["score"]) + source_count * 10 + len(traced_links) * 4 - fanout_penalty
    return {
        "bundle_id": _stable_id(
            "trace-", candidate["file"], candidate["table"], anchor["row_number"]
        ),
        "score": round(score, 4),
        "anchor": {
            "path": candidate["file"],
            "table": candidate["table"],
            "row_number": anchor["row_number"],
            "inferred_role": candidate["inferred_role"],
        },
        "sources": sorted(
            sources,
            key=lambda item: (item["role"] != "result_anchor", item["path"], item["table"]),
        ),
        "links": traced_links,
        "coverage": {
            "source_count": source_count,
            "exact_link_count": len(traced_links),
            "fanout_warning_count": fanout_penalty,
        },
        "warnings": sorted(set(warnings)),
    }


def _approved_side_role(
    side: Mapping[str, Any],
    authoritative_roles: Mapping[tuple[str, str], str] | None,
    file_roles: Mapping[str, str],
) -> str:
    if str(side.get("kind", "")) == "table_row":
        endpoint = (str(side.get("path", "")), str(side.get("table", "")))
        return str((authoritative_roles or {}).get(endpoint) or file_roles.get(endpoint[0], ""))
    return str(file_roles.get(str(side.get("path", "")), ""))


def _locator_order(locator: str) -> tuple[int, int, str]:
    if locator.startswith("line:"):
        rank = 0
    elif locator.startswith("paragraph:"):
        rank = 1
    elif locator.startswith("page:"):
        rank = 2
    elif locator.startswith("slide:"):
        rank = 3
    else:
        rank = 4
    number = next((int(item) for item in re.findall(r"\d+", locator)), 0)
    return rank, number, locator


def _document_source(side: Mapping[str, Any], role: str) -> dict[str, Any]:
    segment = {
        "locator": str(side.get("locator", "")),
        "source_digest": str(side.get("source_digest", "")),
        "segment_digest": str(side.get("segment_digest", "")),
        "snippet": str(side.get("snippet", ""))[:320],
        "value_fingerprint": str(side.get("value_fingerprint", "")),
        "value_preview": str(side.get("value_preview", "")),
        "specificity": int(side.get("specificity", 0)),
    }
    return {
        "path": str(side.get("path", "")),
        "table": "",
        "endpoint": {
            "file": str(side.get("path", "")),
            "kind": "document_segment",
            "locator": str(side.get("locator", "")),
        },
        "role": role,
        "selected_columns": [],
        "rows": [],
        "segment": segment,
    }


def _document_semantic_evidence(
    side: Mapping[str, Any], role: str, relation_id: str,
) -> dict[str, Any]:
    return {
        "evidence_card_id": "",
        "relation_id": relation_id,
        "path": str(side.get("path", "")),
        "locator": str(side.get("locator", "")),
        "approved_role": role,
        "evidence_kind": "exact_value_document_segment",
        "source_digest": str(side.get("source_digest", "")),
        "segment_digest": str(side.get("segment_digest", "")),
        "value_fingerprint": str(side.get("value_fingerprint", "")),
        "value_preview": str(side.get("value_preview", "")),
        "specificity": int(side.get("specificity", 0)),
        "snippet": str(side.get("snippet", ""))[:320],
    }


def _document_result_bundle(
    candidate: Mapping[str, Any],
    proofs: Sequence[dict[str, Any]],
    tables: Mapping[tuple[str, str], TableMeta],
    edges: Sequence[dict[str, Any]],
    matcher: TableMatcher,
    max_columns: int,
    authoritative_roles: Mapping[tuple[str, str], str] | None,
    file_roles: Mapping[str, str],
) -> dict[str, Any] | None:
    """Build one document-segment result trace from exact value evidence only."""

    result_file = str(candidate.get("file", ""))
    eligible: list[tuple[dict[str, Any], dict[str, Any], dict[str, Any]]] = []
    for proof in proofs:
        source = proof.get("source") if isinstance(proof.get("source"), dict) else {}
        target = proof.get("target") if isinstance(proof.get("target"), dict) else {}
        if source.get("kind") == "document_segment" and source.get("path") == result_file:
            anchor_side, other_side = source, target
        elif target.get("kind") == "document_segment" and target.get("path") == result_file:
            anchor_side, other_side = target, source
        else:
            continue
        if _approved_side_role(other_side, authoritative_roles, file_roles) not in DOCUMENT_LINK_ROLES:
            continue
        eligible.append((proof, anchor_side, other_side))
    if not eligible:
        return None
    anchor_locator = min(
        (str(anchor.get("locator", "")) for _proof, anchor, _other in eligible),
        key=_locator_order,
    )
    selected = [item for item in eligible if str(item[1].get("locator", "")) == anchor_locator]
    anchor_side = selected[0][1]
    sources: list[dict[str, Any]] = [_document_source(anchor_side, "result_anchor")]
    source_keys: set[tuple[Any, ...]] = {(result_file, "document_segment", anchor_locator)}
    semantic_evidence = [
        _document_semantic_evidence(
            anchor_side, "result", str(selected[0][0].get("relation_id", ""))
        )
    ]
    links: list[dict[str, Any]] = []
    for proof, _anchor, other in selected:
        source = proof["source"]
        target = proof["target"]
        relation_id = str(proof.get("relation_id", ""))
        other_role = _approved_side_role(other, authoritative_roles, file_roles)
        if other.get("kind") == "document_segment":
            key = (str(other.get("path", "")), "document_segment", str(other.get("locator", "")))
            if key not in source_keys:
                sources.append(_document_source(other, "linked_document_input"))
                semantic_evidence.append(
                    _document_semantic_evidence(other, other_role, relation_id)
                )
                source_keys.add(key)
        else:
            endpoint = (str(other.get("path", "")), str(other.get("table", "")))
            table = tables.get(endpoint)
            if table is None:
                continue
            columns = _selected_columns(
                table, _incident_columns(endpoint[0], endpoint[1], edges), max_columns
            )
            row = matcher.exact_row(table, columns, int(other.get("row_number", 0)))
            if row is None:
                continue
            key = (endpoint[0], endpoint[1], int(other.get("row_number", 0)))
            if key not in source_keys:
                bounded = _bounded_row(table, row)
                sources.append({
                    "path": endpoint[0],
                    "table": endpoint[1],
                    "endpoint": {"file": endpoint[0], "table": endpoint[1]},
                    "role": "linked_source",
                    "selected_columns": list(bounded.get("values", {})),
                    "rows": [bounded],
                })
                source_keys.add(key)
        links.append({
            "edge_id": relation_id,
            "link_kind": "exact_value_document_segment",
            "source_file": str(source.get("path", "")),
            "source_table": str(source.get("table", "")),
            "target_file": str(target.get("path", "")),
            "target_table": str(target.get("table", "")),
            "key_set_index": 0,
            "key_pairs": [{
                "source_field": str(source.get("field", "")),
                "target_field": str(target.get("field", "")),
            }],
            "relation_ids": [relation_id],
            "confidence": float(proof.get("confidence", 0.0)),
            "matched_row_count": 1,
            "materialized_row_count": 1,
            "fanout_warning": False,
            "materialization_truncated": False,
            "search_mode": "field_evidence_exact_locator_replay",
            "key_fingerprints": [str(proof.get("fingerprint", ""))],
        })
    if not links:
        return None
    return {
        "bundle_id": _stable_id("trace-document-", result_file, anchor_locator, anchor_side["segment_digest"]),
        "score": round(10 + len(sources) * 10 + len(links) * 4, 4),
        "anchor": {
            "path": result_file,
            "table": "",
            "kind": "document_segment",
            "locator": anchor_locator,
            "source_digest": str(anchor_side.get("source_digest", "")),
            "segment_digest": str(anchor_side.get("segment_digest", "")),
            "inferred_role": "result",
        },
        "sources": sources,
        "links": links,
        "semantic_evidence": semantic_evidence,
        "coverage": {
            "source_count": len(sources),
            "exact_link_count": len(links),
            "document_exact_link_count": len(links),
            "fanout_warning_count": 0,
            "semantic_evidence_count": len(semantic_evidence),
        },
        "warnings": [],
    }


def _attach_document_inputs(
    bundle: dict[str, Any],
    proofs: Sequence[dict[str, Any]],
    authoritative_roles: Mapping[tuple[str, str], str] | None,
    file_roles: Mapping[str, str],
) -> None:
    """Attach file-scoped input documents to traced table rows by exact value."""

    traced_rows = {
        (
            str(source.get("path", "")),
            str(source.get("table", "")),
            int(row.get("row_number", 0)),
        )
        for source in bundle.get("sources", [])
        if isinstance(source, dict)
        for row in source.get("rows", [])
        if isinstance(row, dict)
    }
    existing_segments = {
        (str(source.get("path", "")), str(source.get("segment", {}).get("locator", "")))
        for source in bundle.get("sources", [])
        if isinstance(source, dict) and isinstance(source.get("segment"), dict)
    }
    existing_links = {
        (
            tuple(item.get("relation_ids", [])),
            tuple(item.get("key_fingerprints", [])),
        )
        for item in bundle.get("links", []) if isinstance(item, dict)
    }
    added = 0
    for proof in proofs:
        source = proof.get("source") if isinstance(proof.get("source"), dict) else {}
        target = proof.get("target") if isinstance(proof.get("target"), dict) else {}
        if source.get("kind") == "document_segment" and target.get("kind") == "table_row":
            document, table_side = source, target
        elif target.get("kind") == "document_segment" and source.get("kind") == "table_row":
            document, table_side = target, source
        else:
            continue
        if _approved_side_role(document, authoritative_roles, file_roles) != "input":
            continue
        table_identity = (
            str(table_side.get("path", "")),
            str(table_side.get("table", "")),
            int(table_side.get("row_number", 0)),
        )
        if table_identity not in traced_rows:
            continue
        relation_id = str(proof.get("relation_id", ""))
        link_identity = ((relation_id,), (str(proof.get("fingerprint", "")),))
        if link_identity in existing_links:
            continue
        segment_identity = (str(document.get("path", "")), str(document.get("locator", "")))
        if segment_identity not in existing_segments:
            bundle.setdefault("sources", []).append(
                _document_source(document, "linked_document_input")
            )
            existing_segments.add(segment_identity)
        bundle.setdefault("semantic_evidence", []).append(
            _document_semantic_evidence(document, "input", relation_id)
        )
        bundle.setdefault("links", []).append({
            "edge_id": relation_id,
            "link_kind": "exact_value_document_segment",
            "source_file": str(source.get("path", "")),
            "source_table": str(source.get("table", "")),
            "target_file": str(target.get("path", "")),
            "target_table": str(target.get("table", "")),
            "key_set_index": 0,
            "key_pairs": [{
                "source_field": str(source.get("field", "")),
                "target_field": str(target.get("field", "")),
            }],
            "relation_ids": [relation_id],
            "confidence": float(proof.get("confidence", 0.0)),
            "matched_row_count": 1,
            "materialized_row_count": 1,
            "fanout_warning": False,
            "materialization_truncated": False,
            "search_mode": "field_evidence_exact_locator_replay",
            "key_fingerprints": [str(proof.get("fingerprint", ""))],
        })
        existing_links.add(link_identity)
        added += 1
    if added:
        coverage = bundle.setdefault("coverage", {})
        coverage["exact_link_count"] = int(coverage.get("exact_link_count", 0)) + added
        coverage["document_exact_link_count"] = int(
            coverage.get("document_exact_link_count", 0)
        ) + added
        coverage["source_count"] = len(bundle.get("sources", []))
        coverage["semantic_evidence_count"] = len(bundle.get("semantic_evidence", []))


def _semantic_context(
    bundle: dict[str, Any], cards: Sequence[dict[str, Any]], tables: dict[tuple[str, str], TableMeta],
    matcher: TableMatcher, edges: Sequence[dict[str, Any]], max_columns: int,
    authoritative_roles: Mapping[tuple[str, str], str] | None,
    file_roles: Mapping[str, str],
    ocr_mode: str,
) -> None:
    anchor_file = str(bundle.get("anchor", {}).get("path", ""))
    existing = {str(item.get("path", "")) for item in bundle.get("sources", [])}
    anchor_source = next(
        (item for item in bundle.get("sources", []) if item.get("role") == "result_anchor"), {}
    )
    anchor_values = [
        _normalize(value)
        for row in anchor_source.get("rows", [])[:1]
        for value in row.get("values", {}).values()
        if 2 <= len(_normalize(value)) <= 160
    ]
    anchor_stem = _normalize(Path(anchor_file).stem)
    selected_cards: list[tuple[float, dict[str, Any]]] = []
    for card in cards:
        if not isinstance(card, dict) or card.get("kind") not in {
            "table_relation_statement", "document_relation_statement", "material_topic_alignment"
        }:
            continue
        facts = card.get("facts") if isinstance(card.get("facts"), dict) else {}
        mentioned = {str(item) for item in facts.get("mentioned_files", [])}
        if anchor_file not in mentioned:
            continue
        snippet = _normalize(card.get("snippet", ""))
        lexical = SequenceMatcher(None, anchor_stem, snippet).ratio() if snippet else 0.0
        value_hits = sum(1 for value in anchor_values if value in snippet or snippet in value)
        selected_cards.append((lexical + value_hits * 2.0, card))
    selected_cards.sort(key=lambda item: (-item[0], str(item[1].get("id", ""))))
    best_by_file: list[dict[str, Any]] = []
    seen_semantic_files: set[str] = set()
    for _score, card in selected_cards:
        source = next((item for item in card.get("sources", []) if isinstance(item, dict)), {})
        file_name = str(source.get("file", ""))
        if file_name in seen_semantic_files:
            continue
        locator = str(source.get("locator", ""))
        table_match = re.search(r"(?:^|;)table:([^;]+)", locator)
        context_role = (
            str((authoritative_roles or {}).get((file_name, table_match.group(1)), ""))
            if table_match else ""
        ) or str(file_roles.get(file_name, ""))
        if context_role not in DOCUMENT_CONTEXT_ROLES:
            continue
        best_by_file.append(card)
        seen_semantic_files.add(file_name)
        if len(best_by_file) >= 3:
            break
    for card in best_by_file:
        source = next(
            (item for item in card.get("sources", []) if isinstance(item, dict)), {}
        )
        file_name = str(source.get("file", ""))
        locator = str(source.get("locator", ""))
        table_match = re.search(r"(?:^|;)table:([^;]+)", locator)
        context_role = (
            str((authoritative_roles or {}).get((file_name, table_match.group(1)), ""))
            if table_match else ""
        ) or str(file_roles.get(file_name, ""))
        context: dict[str, Any] = {
            "evidence_card_id": str(card.get("id", "")),
            "path": file_name,
            "locator": locator,
            "approved_role": context_role,
            "evidence_kind": "bounded_semantic_context",
            "snippet": str(card.get("snippet", ""))[:320],
        }
        if SAFE_DOCUMENT_LOCATOR.fullmatch(locator):
            exact_segment = _read_exact_document_segments(
                matcher.input_root, {file_name: {locator}}, ocr_mode
            ).get((file_name, locator))
            if exact_segment:
                context.update({
                    "source_digest": str(exact_segment.get("source_digest", "")),
                    "segment_digest": str(exact_segment.get("segment_digest", "")),
                    "snippet": str(exact_segment.get("text", "")).replace("\r", " ").replace("\n", " ")[:320],
                })
        match = re.search(r"table:([^;]+);row:(\d+)", locator)
        table = tables.get((file_name, match.group(1))) if match else None
        if table is not None:
            columns = _selected_columns(
                table, _incident_columns(file_name, table.table_name, edges), max_columns
            )
            row = matcher.exact_row(table, columns, int(match.group(2)))
            if row:
                context["table"] = table.table_name
                context["row"] = _bounded_row(table, row)
                if file_name not in existing:
                    bundle["sources"].append({
                        "path": file_name,
                        "table": table.table_name,
                        "role": "localized_semantic_evidence",
                        "selected_columns": list(context["row"]["values"]),
                        "rows": [context["row"]],
                    })
                    existing.add(file_name)
        bundle.setdefault("semantic_evidence", []).append(context)
    bundle["coverage"]["source_count"] = len(bundle.get("sources", []))
    bundle["coverage"]["semantic_evidence_count"] = len(bundle.get("semantic_evidence", []))


def compact_trace_report(report: dict[str, Any], *, max_bundles: int = 1) -> dict[str, Any]:
    bundles = []
    for bundle in report.get("bundles", [])[:max_bundles]:
        bundles.append({
            "bundle_id": bundle.get("bundle_id"),
            "anchor": bundle.get("anchor", {}),
            "coverage": bundle.get("coverage", {}),
            "links": [
                {
                    "source_file": item.get("source_file"),
                    "target_file": item.get("target_file"),
                    "link_kind": item.get("link_kind", "table_key"),
                    "key_pairs": item.get("key_pairs", []),
                    "relation_ids": item.get("relation_ids", []),
                    "confidence": item.get("confidence"),
                    "matched_row_count": item.get("matched_row_count"),
                    "fanout_warning": item.get("fanout_warning"),
                    "materialization_truncated": item.get("materialization_truncated"),
                }
                for item in bundle.get("links", [])
            ],
            "sources": [
                {
                    "path": source.get("path"),
                    "table": source.get("table"),
                    "role": source.get("role"),
                    "segment": source.get("segment", {}),
                    "rows": [
                        {
                            "row_number": row.get("row_number"),
                            "values": dict(list(row.get("values", {}).items())[:16]),
                        }
                        for row in source.get("rows", [])[:2]
                    ],
                }
                for source in bundle.get("sources", [])
            ],
            "semantic_evidence": bundle.get("semantic_evidence", [])[:2],
            "warnings": bundle.get("warnings", []),
        })
    return {
        "schema_version": report.get("schema_version", TRACE_SCHEMA_VERSION),
        "status": report.get("status", "missing"),
        "strategy": report.get("strategy", ""),
        "candidate_result_sources": report.get("candidate_result_sources", []),
        "anchor_selection": report.get("anchor_selection", {}),
        "role_manifest": report.get("role_manifest", {}),
        "bundles": bundles,
        "quality_gates": report.get("quality_gates", {}),
    }


def _redacted_anchor_candidates(
    candidate: dict[str, Any],
    tables: dict[tuple[str, str], TableMeta],
    edges: Sequence[dict[str, Any]],
    matcher: TableMatcher,
    max_columns: int,
    *,
    limit: int = DEFAULT_SELECTION_PREVIEW_ROWS,
) -> dict[str, Any]:
    """Build display-only candidates for a required anchor-selection gate.

    These rows are never passed to the trace builder.  They are stable,
    bounded locators which let a reviewer choose a historical result instance
    without the engine silently treating a leading row as truth.
    """
    endpoint = (str(candidate["file"]), str(candidate["table"]))
    table = tables[endpoint]
    columns = _selected_columns(
        table, _incident_columns(endpoint[0], endpoint[1], edges), max_columns
    )
    row_count = table.row_count
    raw_rows: list[dict[str, Any]] = []
    if isinstance(row_count, int) and row_count > 0:
        preview_count = min(max(1, limit), row_count)
        if preview_count == 1:
            offsets = [0]
        else:
            offsets = sorted({
                round(index * (row_count - 1) / (preview_count - 1))
                for index in range(preview_count)
            })
        for offset in offsets:
            row = matcher.exact_row(table, columns, table.header_row + 2 + offset)
            if row and any(_normalize(value) for value in row.get("values", {}).values()):
                raw_rows.append(row)
    else:
        raw_rows = matcher.first_rows(table, columns, row_limit=max(1, limit))
    return {
        "file": endpoint[0],
        "table": endpoint[1],
        "inferred_role": candidate.get("inferred_role", "unknown"),
        "row_count": row_count,
        "selector": {
            "file": endpoint[0],
            "table": endpoint[1],
            "row_number": "<choose one of preview_rows or another valid row locator>",
        },
        "preview_strategy": "deterministic_evenly_spaced_redacted_rows",
        "preview_rows": [_bounded_row(table, row) for row in raw_rows],
    }


def _redacted_document_candidate(
    candidate: Mapping[str, Any], proofs: Sequence[dict[str, Any]],
) -> dict[str, Any]:
    """Expose only replayable document locators for a result-role decision."""

    file_name = str(candidate.get("file", ""))
    segments: dict[tuple[str, str], dict[str, Any]] = {}
    for proof in proofs:
        for side in (proof.get("source"), proof.get("target")):
            if not isinstance(side, dict):
                continue
            if side.get("kind") != "document_segment" or side.get("path") != file_name:
                continue
            key = (str(side.get("locator", "")), str(side.get("segment_digest", "")))
            segments[key] = {
                "locator": key[0],
                "source_digest": str(side.get("source_digest", "")),
                "segment_digest": key[1],
                "value_preview": str(side.get("value_preview", "")),
                "specificity": int(side.get("specificity", 0)),
            }
    return {
        "file": file_name,
        "table": "",
        "anchor_kind": "document_segment",
        "inferred_role": "result",
        "selection_resolution": (
            "Keep exactly one file or table assigned as result, then rerun tracing. "
            "Document anchors are selected only from exact-value evidence locators."
        ),
        "verified_segments": [
            segments[key]
            for key in sorted(segments, key=lambda item: _locator_order(item[0]))[:DEFAULT_SELECTION_PREVIEW_ROWS]
        ],
    }


def _explicit_anchor(
    selector: Any,
    tables: dict[tuple[str, str], TableMeta],
    roles: dict[tuple[str, str], str],
    edges: Sequence[dict[str, Any]],
    matcher: TableMatcher,
    max_columns: int,
    authoritative_roles: Mapping[tuple[str, str], str] | None = None,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None, list[str]]:
    """Resolve exactly one reviewer-selected result row, without fallback."""
    if not isinstance(selector, dict):
        return None, None, ["anchor_selector must be an object with file, table, and row_number"]
    file_name = str(selector.get("file", "")).strip()
    table_name = str(selector.get("table", "")).strip()
    raw_row_number = selector.get("row_number")
    try:
        row_number = int(raw_row_number)
    except (TypeError, ValueError):
        row_number = 0
    if not file_name or not table_name or row_number <= 0:
        return None, None, ["anchor_selector must declare non-empty file, table, and positive row_number"]
    endpoint = (file_name, table_name)
    table = tables.get(endpoint)
    if table is None:
        return None, None, ["anchor_selector does not identify a discovered table endpoint"]
    if authoritative_roles is not None:
        approved_role = authoritative_roles.get(endpoint)
        if approved_role != "result":
            return None, None, [
                f"anchor_selector targets a table approved as {approved_role or 'unassigned'}; "
                "only a current approved result table may be selected"
            ]
    columns = _selected_columns(
        table, _incident_columns(file_name, table_name, edges), max_columns
    )
    row = matcher.exact_row(table, columns, row_number)
    if row is None or not any(_normalize(value) for value in row.get("values", {}).values()):
        return None, None, ["anchor_selector row is missing or empty in the selected table"]
    candidate = {
        "file": file_name,
        "table": table_name,
        "inferred_role": "result" if authoritative_roles is not None else roles.get(endpoint, "user_selected_result_anchor"),
        "approved_role": "result" if authoritative_roles is not None else "",
        "row_count": table.row_count,
        "score": 0.0,
        "selection_kind": "explicit_selector_with_approved_role" if authoritative_roles is not None else "explicit_selector",
    }
    return candidate, row, []


def build_trace_samples(
    input_root: Path,
    field_result: dict[str, Any],
    cards: Sequence[dict[str, Any]],
    *,
    result_candidate_limit: int = DEFAULT_RESULT_CANDIDATES,
    anchor_candidates: int = DEFAULT_ANCHOR_CANDIDATES,
    max_rows_per_source: int = DEFAULT_MAX_ROWS_PER_SOURCE,
    max_columns_per_source: int = DEFAULT_MAX_COLUMNS_PER_SOURCE,
    max_hops: int = DEFAULT_MAX_HOPS,
    relation_overrides: Sequence[dict[str, Any]] = (),
    anchor_selector: dict[str, Any] | None = None,
    auto_first_valid_result_row: bool = False,
    authoritative_roles: Mapping[tuple[str, str], str] | None = None,
    role_manifest: dict[str, Any] | None = None,
    document_ocr_mode: str = "auto",
) -> dict[str, Any]:
    """Return exactly one causally coherent, result-anchored trace bundle.

    Candidate result tables and candidate rows are an implementation detail of
    deterministic local search.  They are scored locally so that the final
    model-facing artifact contains rows from one selected result instance
    only; combining rows from several anchors would fabricate a business
    chain that never occurred.
    """
    files, tables, stats = _indexes(field_result)
    edges = _apply_reviewed_overrides(
        _build_edges(field_result, tables, stats), tables, relation_overrides,
    )
    roles = _role_by_table(cards)
    file_roles = _file_roles_from_manifest(role_manifest)
    table_candidates = _result_candidates(
        files, tables, edges, cards, result_candidate_limit, authoritative_roles,
    )
    document_candidates = _document_result_candidates(files, tables, file_roles)
    candidates = table_candidates + document_candidates
    matcher = TableMatcher(input_root, max_rows_per_source)
    document_proofs = _exact_document_proofs(
        input_root, field_result, files, tables, matcher, document_ocr_mode
    ) if file_roles else []
    warnings: list[str] = []
    probes: list[tuple[dict[str, Any], dict[str, Any], dict[str, Any]]] = []
    selection: dict[str, Any] = {
        "status": "auto_selected_single_row" if anchor_selector is None else "explicit_selector",
        "selector": dict(anchor_selector) if isinstance(anchor_selector, dict) else None,
    }
    selected_candidates: list[tuple[dict[str, Any], dict[str, Any]]] = []
    selected_document_bundle: dict[str, Any] | None = None
    selected_document_candidate: dict[str, Any] | None = None
    blockers: list[str] = []

    if anchor_selector is not None:
        candidate, anchor, selector_errors = _explicit_anchor(
            anchor_selector, tables, roles, edges, matcher, max_columns_per_source, authoritative_roles,
        )
        if selector_errors:
            blockers.extend(selector_errors)
            selection["status"] = "invalid_selector"
            selection["errors"] = selector_errors
        elif candidate is not None and anchor is not None:
            selected_candidates.append((candidate, anchor))
    elif not candidates:
        if authoritative_roles is not None or file_roles:
            blockers.append("No current approved result table or document is available in the role manifest")
            selection["status"] = "missing_approved_result_role"
        else:
            blockers.append("No structured candidate result source could be inferred")
            selection["status"] = "missing_result_source"
    else:
        # More than one user-approved result table is an ambiguity, not an
        # invitation to choose the alphabetically first / smallest table.
        # The selector names both table and row, so it resolves this endpoint
        # decision in the same user-auditable action as a multi-row result.
        if (authoritative_roles is not None or file_roles) and len(candidates) != 1:
            includes_document_result = any(
                candidate.get("anchor_kind") == "document_segment" for candidate in candidates
            )
            selection = {
                "status": "selection_required",
                "reason": (
                    "More than one file or table is approved as a historical result; keep exactly "
                    "one result source before tracing."
                    if includes_document_result else
                    "More than one table is approved as a historical result; choose the exact "
                    "result table and row to explain."
                ),
                "candidates": [
                    _redacted_document_candidate(candidate, document_proofs)
                    if candidate.get("anchor_kind") == "document_segment"
                    else _redacted_anchor_candidates(candidate, tables, edges, matcher, max_columns_per_source)
                    for candidate in candidates
                ],
            }
            if includes_document_result:
                selection["resolution"] = "role_correction_required"
                blockers.append("Exactly one approved result file or table is required before document tracing")
            else:
                selection["selector_schema"] = {
                    "file": "string", "table": "string", "row_number": "positive integer"
                }
                blockers.append(
                    "A result table and row selector is required before tracing multiple approved result tables"
                )
        # A multi-row historical result is not a deterministic business
        # instance by itself.  Most callers must stop here and request a
        # reviewer decision.  The Studio chat action is the one explicit
        # exception: it asks for a reproducible *first non-empty result row*
        # as its default inspection sample.  Keep that behaviour opt-in so a
        # generic Skill invocation can never silently promote a leading row.
        elif (auto_candidate := candidates[0]) is not None:
            if auto_candidate.get("anchor_kind") == "document_segment":
                selected_document_bundle = _document_result_bundle(
                    auto_candidate,
                    document_proofs,
                    tables,
                    edges,
                    matcher,
                    max_columns_per_source,
                    authoritative_roles,
                    file_roles,
                )
                if selected_document_bundle is None:
                    blockers.append(
                        "The approved document result has no segment whose extracted high-specificity "
                        "value exactly matches current field evidence in an approved input source; "
                        "semantic similarity alone is not an executable link"
                    )
                    selection = {
                        "status": "missing_exact_document_link",
                        "reason": (
                            "A document result cannot be completed from semantic similarity. "
                            "It needs one safely located segment with a replayable exact-value link."
                        ),
                    }
                else:
                    selected_document_candidate = auto_candidate
                    selection = {
                        "status": "auto_selected_first_exact_document_segment",
                        "selector": {
                            "file": auto_candidate["file"],
                            "kind": "document_segment",
                            "locator": selected_document_bundle["anchor"]["locator"],
                        },
                        "source_digest": selected_document_bundle["anchor"]["source_digest"],
                        "segment_digest": selected_document_bundle["anchor"]["segment_digest"],
                        "reason": (
                            "First safely located result segment with a replayed high-specificity "
                            "exact-value link to an approved input source."
                        ),
                    }
                auto_candidate = None
            if auto_candidate is None:
                pass
            else:
                row_count = auto_candidate.get("row_count")
                if (
                    not auto_first_valid_result_row
                    and (not isinstance(row_count, int) or row_count != 1)
                ):
                    selection = {
                        "status": "selection_required",
                        "reason": (
                            "The candidate result table has multiple or unknown rows; "
                            "choose one business result row explicitly before tracing."
                        ),
                        "selector_schema": {"file": "string", "table": "string", "row_number": "positive integer"},
                        "candidates": [
                            _redacted_anchor_candidates(
                                candidate, tables, edges, matcher, max_columns_per_source
                            )
                            for candidate in table_candidates
                        ],
                    }
                    blockers.append("A result anchor selector is required before tracing a multi-row result table")
                else:
                    table = tables[(auto_candidate["file"], auto_candidate["table"])]
                    selected = _selected_columns(
                        table,
                        _incident_columns(auto_candidate["file"], auto_candidate["table"], edges),
                        max_columns_per_source,
                    )
                    anchors = matcher.first_rows(table, selected, row_limit=1)
                    if not anchors:
                        blockers.append(
                            f"No non-empty anchor rows found in {auto_candidate['file']} / {auto_candidate['table']}"
                        )
                    else:
                        if auto_first_valid_result_row and (
                            not isinstance(row_count, int) or row_count != 1
                        ):
                            selection = {
                                "status": "auto_selected_first_valid_result_row",
                                "selector": {
                                    "file": auto_candidate["file"],
                                    "table": auto_candidate["table"],
                                    "row_number": anchors[0]["row_number"],
                                },
                                "reason": (
                                    "Studio chat default: first non-empty row in the single "
                                    "approved result table. The reviewer can ask for a corrected "
                                    "relationship trace in chat."
                                ),
                            }
                        selected_candidates.append((auto_candidate, anchors[0]))

    for candidate, anchor in selected_candidates:
        probe = _trace_anchor(
            candidate, anchor, tables, edges, matcher, max_columns_per_source, max_hops,
            materialize_rows=False,
        )
        probes.append((probe, candidate, anchor))

    def probe_sort_key(item: tuple[dict[str, Any], dict[str, Any], dict[str, Any]]) -> tuple[Any, ...]:
        probe, candidate, _anchor = item
        coverage = probe.get("coverage", {}) if isinstance(probe.get("coverage"), dict) else {}
        return (
            -int(coverage.get("exact_link_count", 0)),
            -float(probe.get("score", 0.0)),
            int(coverage.get("fanout_warning_count", 0)),
            str(candidate.get("file", "")),
            str(candidate.get("table", "")),
            int(probe.get("anchor", {}).get("row_number", 0)),
        )

    # At most one candidate is eligible here: automatic tracing is restricted
    # to a one-row result table, while an explicit selector names one exact
    # endpoint and row.  Retain the sort as a defensive invariant.
    selected_probe = min(probes, key=probe_sort_key) if probes else None
    selected_candidate: dict[str, Any] | None = None
    bundles: list[dict[str, Any]] = []
    if selected_probe is not None:
        _probe, selected_candidate, selected_anchor = selected_probe
        selected = _trace_anchor(
            selected_candidate, selected_anchor, tables, edges, matcher, max_columns_per_source,
            max_hops, materialize_rows=True,
        )
        _attach_document_inputs(
            selected, document_proofs, authoritative_roles, file_roles
        )
        bundles = [selected]
    elif selected_document_bundle is not None:
        selected_candidate = selected_document_candidate
        bundles = [selected_document_bundle]
    for bundle in bundles:
        _semantic_context(
            bundle,
            cards,
            tables,
            matcher,
            edges,
            max_columns_per_source,
            authoritative_roles,
            file_roles,
            document_ocr_mode,
        )
    executable = [item for item in bundles if item.get("coverage", {}).get("exact_link_count", 0) > 0]
    if not blockers and not executable:
        blockers.append("No candidate result row could be traced to another source with an exact evidence-backed key")
    status = (
        "selection_required"
        if selection.get("status") == "selection_required"
        else "complete" if executable and not blockers else "blocked"
    )
    return {
        "schema_version": TRACE_SCHEMA_VERSION,
        "status": status,
        "strategy": "single_result_anchor_full_scan_bounded_materialization",
        "field_evidence_fingerprint": hashlib.sha256(
            json.dumps(field_result, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest(),
        "candidate_result_sources": [selected_candidate] if selected_candidate is not None else candidates,
        "anchor_selection": selection,
        "role_manifest": {
            "artifact": str(role_manifest.get("artifact", "")),
            "artifact_fingerprint": str(role_manifest.get("artifact_fingerprint", "")),
            "fingerprint": str(role_manifest.get("role_manifest_fingerprint", "")),
            "source_revision": role_manifest.get("source_revision"),
            "source_fingerprint": str(role_manifest.get("source_fingerprint", "")),
            "approved_role_count": len(file_roles) + sum(
                1 for path, _table in (authoritative_roles or {}) if path not in file_roles
            ),
            "approved_table_role_count": len(authoritative_roles or {}),
            "approved_file_role_count": len(file_roles),
            "file_roles": dict(sorted(file_roles.items())),
        } if isinstance(role_manifest, dict) else {},
        "reviewed_relation_override_count": sum(1 for item in relation_overrides if isinstance(item, dict)),
        "bundles": bundles,
        "quality_gates": {
            "status": "passed" if status == "complete" else "failed",
            "blockers": blockers,
            "warnings": sorted(set(warnings + [warning for item in bundles for warning in item.get("warnings", [])])),
            "candidate_result_source_count": len(candidates),
            "probed_anchor_count": len(probes),
            "selected_trace_count": len(bundles),
            "trace_bundle_count": len(bundles),
            "executable_trace_bundle_count": len(executable),
        },
    }


__all__ = ["build_trace_samples", "compact_trace_report"]
