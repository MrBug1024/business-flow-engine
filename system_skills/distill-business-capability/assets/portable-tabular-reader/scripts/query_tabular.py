#!/usr/bin/env python3
"""Inspect and query tabular files with bounded, read-only output."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
import sys
import tempfile
from pathlib import Path
from typing import Any, Sequence


SUPPORTED_EXTENSIONS = {
    ".csv",
    ".tsv",
    ".parquet",
    ".json",
    ".jsonl",
    ".ndjson",
    ".xlsx",
    ".xls",
    ".xlsb",
    ".sqlite",
    ".sqlite3",
    ".db",
}
MAX_ROWS = 10_000
MAX_CELL_CHARS = 2_000
MAX_COMPLETE_RULE_RESPONSE_CHARS = 512_000
READ_ONLY_SQL = re.compile(r"^\s*(select|with)\b", re.IGNORECASE)
FORBIDDEN_SQL = re.compile(
    r"\b(insert|update|delete|drop|alter|create|replace|truncate|attach|detach|copy|export|import|install|load|call)\b",
    re.IGNORECASE,
)
EXTERNAL_READ_SQL = re.compile(
    r"\b(read_csv(?:_auto)?|read_parquet|parquet_scan|read_json(?:_auto)?|read_ndjson|"
    r"read_xlsx|sqlite_scan|read_text|read_blob|glob|httpfs)\s*\(",
    re.IGNORECASE,
)


class ReaderError(ValueError):
    pass


def compact(value: Any) -> Any:
    if value is None or isinstance(value, (int, float, bool)):
        return value
    text = str(value)
    return text if len(text) <= MAX_CELL_CHARS else text[: MAX_CELL_CHARS - 1] + "…"


def quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def validate_file(raw: str) -> Path:
    path = Path(raw).expanduser().resolve()
    if not path.is_file():
        raise ReaderError(f"File does not exist: {path}")
    if path.suffix.casefold() not in SUPPORTED_EXTENSIONS:
        raise ReaderError(f"Unsupported tabular extension: {path.suffix or '<none>'}")
    return path


def validate_sql(sql: str) -> str:
    normalized = sql.strip().rstrip(";").strip()
    if not normalized or not READ_ONLY_SQL.search(normalized):
        raise ReaderError("Only read-only SELECT/WITH queries are allowed")
    if FORBIDDEN_SQL.search(normalized) or ";" in normalized:
        raise ReaderError("Mutating, extension-loading, multi-statement, and export SQL are forbidden")
    if EXTERNAL_READ_SQL.search(normalized):
        raise ReaderError("SQL must use only the views registered by this Skill; direct file or URL reads are forbidden")
    return normalized


def import_duckdb() -> Any:
    try:
        import duckdb
    except ImportError as exc:
        raise ReaderError("duckdb is required; install this Skill's requirements.txt") from exc
    return duckdb


def excel_sheets(path: Path) -> list[str]:
    try:
        import fastexcel
    except ImportError as exc:
        raise ReaderError("fastexcel is required for Excel sheet discovery") from exc
    reader = fastexcel.read_excel(str(path))
    return [str(item) for item in reader.sheet_names]


def register_excel_fallback(
    connection: Any, path: Path, sheet: str, *, relation: str = "source", header_row: int = 0,
) -> str:
    try:
        import fastexcel
    except ImportError as exc:
        raise ReaderError("Excel requires DuckDB's excel extension or fastexcel") from exc
    reader = fastexcel.read_excel(str(path))
    selected = sheet or str(reader.sheet_names[0])
    batch = reader.load_sheet_eager(selected, header_row=header_row, dtypes="string", dtype_coercion="coerce")
    connection.register(relation, batch)
    return selected


def register_duckdb_source(connection: Any, path: Path, sheet: str) -> dict[str, Any]:
    extension = path.suffix.casefold()
    escaped_path = str(path).replace("'", "''")
    if extension == ".csv":
        connection.execute(
            f"CREATE VIEW source AS SELECT * FROM read_csv_auto('{escaped_path}', sample_size=20000, ignore_errors=true)"
        )
        return {"engine": "duckdb", "relation": "source"}
    if extension == ".tsv":
        connection.execute(
            f"CREATE VIEW source AS SELECT * FROM read_csv_auto('{escaped_path}', delim='\\t', sample_size=20000, ignore_errors=true)"
        )
        return {"engine": "duckdb", "relation": "source"}
    if extension == ".parquet":
        connection.execute(f"CREATE VIEW source AS SELECT * FROM read_parquet('{escaped_path}')")
        return {"engine": "duckdb", "relation": "source"}
    if extension in {".json", ".jsonl", ".ndjson"}:
        connection.execute(f"CREATE VIEW source AS SELECT * FROM read_json_auto('{escaped_path}', ignore_errors=true)")
        return {"engine": "duckdb", "relation": "source"}
    if extension in {".xlsx", ".xls", ".xlsb"}:
        selected = sheet
        if not selected:
            selected = excel_sheets(path)[0]
        escaped_sheet = selected.replace("'", "''")
        try:
            connection.execute(
                f"CREATE VIEW source AS SELECT * FROM read_xlsx('{escaped_path}', sheet='{escaped_sheet}', "
                "header=true, all_varchar=true, ignore_errors=true)"
            )
            connection.execute("SELECT * FROM source LIMIT 0")
            return {"engine": "duckdb-read_xlsx", "relation": "source", "sheet": selected}
        except Exception:
            selected = register_excel_fallback(connection, path, selected)
            return {
                "engine": "fastexcel-arrow",
                "relation": "source",
                "sheet": selected,
                "warning": "Excel fallback materializes the selected sheet; convert very large workbooks to Parquet first.",
            }
    raise ReaderError("SQLite is handled by its native read-only engine")


def load_contract(raw: str) -> dict[str, Any]:
    path = Path(raw).expanduser().resolve()
    if not path.is_file():
        raise ReaderError(f"Operational data contract does not exist: {path}")
    if path.stat().st_size > 8 * 1024 * 1024:
        raise ReaderError("Operational data contract exceeds 8 MiB")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ReaderError(f"Invalid operational data contract: {exc}") from exc
    if not isinstance(payload, dict) or payload.get("status") != "ready":
        raise ReaderError("Operational data contract is not ready")
    return payload


def parse_source_bindings(values: Sequence[str]) -> dict[str, str]:
    bindings: dict[str, str] = {}
    for raw in values:
        source_id, separator, relative = str(raw).partition("=")
        source_id = source_id.strip()
        relative = relative.strip()
        if not separator or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{1,127}", source_id):
            raise ReaderError(f"Invalid source binding; expected <source-id>=<relative-path>: {raw}")
        path = Path(relative)
        if not relative or path.is_absolute() or ".." in path.parts:
            raise ReaderError(f"Source binding must be a safe path relative to data-root: {raw}")
        if source_id in bindings:
            raise ReaderError(f"Duplicate source binding: {source_id}")
        bindings[source_id] = relative
    return bindings


def source_is_runtime_input(source: dict[str, Any]) -> bool:
    lifecycle = str(source.get("lifecycle", "runtime_input"))
    return lifecycle == "runtime_input" and source.get("runtime_required", True) is not False


def excel_column_name(index: int) -> str:
    if index < 1:
        return "A"
    value = ""
    while index:
        index, remainder = divmod(index - 1, 26)
        value = chr(65 + remainder) + value
    return value


def contract_source_path(
    source: dict[str, Any], data_root: Path, bindings: dict[str, str] | None = None,
) -> Path:
    if not source_is_runtime_input(source):
        raise ReaderError(
            f"Contract source {source.get('source_id', '')} is {source.get('lifecycle', 'design-time-only')} "
            "and is not a runtime query input"
        )
    source_id = str(source.get("source_id", ""))
    relative = Path(str((bindings or {}).get(source_id) or source.get("path", "")))
    if relative.is_absolute():
        raise ReaderError("Contract source paths must be relative")
    resolved_root = data_root.resolve()
    path = (resolved_root / relative).resolve()
    if not path.is_relative_to(resolved_root):
        raise ReaderError(f"Contract source escapes data root: {relative}")
    if not path.is_file():
        raise ReaderError(f"Referenced runtime source does not exist: {source_id} -> {relative}")
    if path.suffix.casefold() not in SUPPORTED_EXTENSIONS:
        raise ReaderError(f"Unsupported runtime source extension for {source_id}: {path.suffix or '<none>'}")
    return path


def query_digest(sql: str) -> str:
    return hashlib.sha256(sql.strip().encode("utf-8")).hexdigest()


def register_contract_source(
    connection: Any, source: dict[str, Any], data_root: Path,
    bindings: dict[str, str] | None = None,
) -> dict[str, Any]:
    path = contract_source_path(source, data_root, bindings)
    extension = path.suffix.casefold()
    relation = str(source.get("view_name", ""))
    if not re.fullmatch(r"[a-z][a-z0-9_]{1,62}", relation):
        raise ReaderError(f"Invalid contract view_name: {relation}")
    quoted_relation = quote_identifier(relation)
    escaped_path = str(path).replace("'", "''")
    table = next((item for item in source.get("tables", []) if isinstance(item, dict)), {})
    header = table.get("header") if isinstance(table.get("header"), dict) else {}
    header_row = max(0, int(header.get("header_row", 0)))
    if extension in {".xlsx", ".xls", ".xlsb"}:
        sheet = str(table.get("sheet_or_table", "")) or excel_sheets(path)[0]
        escaped_sheet = sheet.replace("'", "''")
        try:
            # A design-time row_count/column_count describes the historical sample,
            # not the current batch.  Bounding read_xlsx with those values silently
            # drops newly arrived rows and columns.  Header-row-zero workbooks can be
            # read without a range; for offset headers, derive the physical sheet
            # dimensions from the workbook metadata through fastexcel instead of
            # reusing historical dimensions.
            range_clause = ""
            if header_row:
                try:
                    import fastexcel

                    reader = fastexcel.read_excel(str(path))
                    sheet_reader = reader.load_sheet_by_name(sheet)
                    physical_last_row = max(1, int(sheet_reader.total_height) + 1)
                    physical_width = max(1, int(sheet_reader.width))
                    if header_row + 1 > physical_last_row:
                        raise ReaderError(
                            f"Excel header row {header_row} is outside the runtime sheet: {source.get('source_id', '')}"
                        )
                    range_clause = (
                        f", range='A{header_row + 1}:{excel_column_name(physical_width)}{physical_last_row}'"
                    )
                except ReaderError:
                    raise
                except Exception as exc:
                    raise ReaderError(
                        "Excel with a non-zero header row requires fastexcel workbook dimensions"
                    ) from exc
            connection.execute(
                f"CREATE VIEW {quoted_relation} AS SELECT * FROM read_xlsx('{escaped_path}', "
                f"sheet='{escaped_sheet}'{range_clause}, header=true, all_varchar=true)"
            )
            connection.execute(f"SELECT * FROM {quoted_relation} LIMIT 0")
            return {
                "source_id": source.get("source_id"), "view_name": relation, "path": str(path),
                "engine": "duckdb-read_xlsx", "sheet": sheet, "header_row": header_row,
                "runtime_size_bytes": path.stat().st_size,
                "design_time_content_sha256": source.get("content_sha256", ""),
            }
        except Exception as exc:
            if source.get("is_large"):
                raise ReaderError(
                    f"Large Excel source {source.get('path')} requires DuckDB read_xlsx; "
                    "refusing a full-sheet in-memory fallback"
                ) from exc
            selected = register_excel_fallback(
                connection, path, sheet, relation=relation, header_row=header_row
            )
            return {
                "source_id": source.get("source_id"), "view_name": relation, "path": str(path),
                "engine": "fastexcel-arrow", "sheet": selected, "header_row": header_row,
                "runtime_size_bytes": path.stat().st_size,
                "design_time_content_sha256": source.get("content_sha256", ""),
                "warning": "Small-sheet fallback materialized this Excel sheet in memory.",
            }
    if extension == ".csv":
        connection.execute(
            f"CREATE VIEW {quoted_relation} AS SELECT * FROM read_csv_auto('{escaped_path}', "
            f"skip={header_row}, header=true, sample_size=20000)"
        )
    elif extension == ".tsv":
        connection.execute(
            f"CREATE VIEW {quoted_relation} AS SELECT * FROM read_csv_auto('{escaped_path}', delim='\\t', "
            f"skip={header_row}, header=true, sample_size=20000)"
        )
    elif extension == ".parquet":
        connection.execute(f"CREATE VIEW {quoted_relation} AS SELECT * FROM read_parquet('{escaped_path}')")
    elif extension in {".json", ".jsonl", ".ndjson"}:
        connection.execute(f"CREATE VIEW {quoted_relation} AS SELECT * FROM read_json_auto('{escaped_path}')")
    else:
        raise ReaderError(f"Contract multi-source query does not support {extension}")
    return {
        "source_id": source.get("source_id"), "view_name": relation, "path": str(path), "engine": "duckdb",
        "runtime_size_bytes": path.stat().st_size,
        "design_time_content_sha256": source.get("content_sha256", ""),
    }


def contract_sources(contract: dict[str, Any], source_ids: set[str] | None = None) -> list[dict[str, Any]]:
    values = [
        item for item in contract.get("sources", [])
        if isinstance(item, dict) and item.get("kind") == "tabular"
    ]
    if source_ids is not None:
        values = [item for item in values if str(item.get("source_id", "")) in source_ids]
    return values


def validate_registered_schema(connection: Any, source: dict[str, Any]) -> dict[str, Any]:
    relation = quote_identifier(str(source.get("view_name", "")))
    actual = [str(row[0]) for row in connection.execute(f"DESCRIBE SELECT * FROM {relation}").fetchall()]
    table = next((item for item in source.get("tables", []) if isinstance(item, dict)), {})
    expected = [
        str(item.get("query_name") or item.get("name") or "")
        for item in table.get("columns", [])
        if isinstance(item, dict) and str(item.get("query_name") or item.get("name") or "")
    ]
    actual_casefold = {item.casefold() for item in actual}
    missing = [item for item in expected if item.casefold() not in actual_casefold]
    if missing:
        raise ReaderError(
            f"Runtime source schema is incompatible for {source.get('source_id', '')}; "
            f"missing columns: {', '.join(missing[:20])}"
        )
    return {
        "status": "compatible",
        "expected_column_count": len(expected),
        "actual_column_count": len(actual),
        "extra_columns_allowed": True,
    }


def open_contract_connection(
    contract: dict[str, Any], data_root: Path, source_ids: set[str] | None = None,
    bindings: dict[str, str] | None = None,
) -> tuple[Any, list[dict[str, Any]]]:
    duckdb = import_duckdb()
    connection = duckdb.connect(database=":memory:")
    connection.execute("SET enable_progress_bar = false")
    registrations: list[dict[str, Any]] = []
    try:
        for source in contract_sources(contract, source_ids):
            if not source_is_runtime_input(source):
                raise ReaderError(
                    f"Source {source.get('source_id', '')} is a design-time template/evidence source, not runtime input"
                )
            registration = register_contract_source(connection, source, data_root, bindings)
            registration["schema_validation"] = validate_registered_schema(connection, source)
            registrations.append(registration)
    except Exception:
        connection.close()
        raise
    return connection, registrations


def contract_overview(contract: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": "success",
        "scenario": contract.get("scenario", {}),
        "query_policy": contract.get("query_policy", {}),
        "rule_source_ids": contract.get("rule_source_ids", []),
        "runtime_source_ids": contract.get("runtime_source_ids", []),
        "template_source_ids": contract.get("template_source_ids", []),
        "external_capabilities": contract.get("external_capabilities", []),
        "sources": [
            {
                "source_id": item.get("source_id"), "view_name": item.get("view_name"),
                "path": item.get("path"), "kind": item.get("kind"), "is_large": item.get("is_large"),
                "lifecycle": item.get("lifecycle", "runtime_input"),
                "runtime_required": item.get("runtime_required", True),
                "runtime_binding": item.get("runtime_binding", "required_when_referenced_by_stage_or_query"),
                "template_policy": item.get("template_policy", {}),
                "roles": item.get("roles", []), "tables": item.get("tables", []),
                "content_retrieval": item.get("content_retrieval", {}),
            }
            for item in contract.get("sources", []) if isinstance(item, dict)
        ],
        "links": [
            {
                "link_id": item.get("link_id"), "kind": item.get("kind"),
                "source_id": item.get("source_id"), "target_id": item.get("target_id"),
                "recommended_candidate": item.get("recommended_candidate"),
                "candidate_key_sets": item.get("candidate_key_sets", []),
                "runtime_validation": item.get("runtime_validation", []),
            }
            for item in contract.get("links", [])
            if isinstance(item, dict) and (item.get("recommended_candidate") or item.get("candidate_key_sets"))
        ],
        "semantic_routes": [
            item for item in contract.get("semantic_routes", []) if isinstance(item, dict)
        ],
    }


def preflight_contract(
    contract: dict[str, Any], data_root: Path, source_ids: set[str] | None = None,
    bindings: dict[str, str] | None = None,
) -> dict[str, Any]:
    selected_ids = {str(item) for item in (source_ids or set()) if str(item)}
    if not selected_ids:
        raise ReaderError(
            "Scoped preflight requires at least one --source-id; global validation of every contract source is forbidden"
        )
    known = {str(item.get("source_id", "")) for item in contract_sources(contract)}
    unknown = sorted(selected_ids - known)
    if unknown:
        raise ReaderError("Unknown source_id values: " + ", ".join(unknown))
    registrations: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    for source_id in sorted(selected_ids):
        try:
            connection, current = open_contract_connection(
                contract, data_root, {source_id}, bindings
            )
        except ReaderError as exc:
            errors.append({"source_id": source_id, "message": str(exc)})
            continue
        try:
            registrations.extend(current)
        finally:
            connection.close()
    return {
        "status": "success" if not errors else "blocked_missing_or_incompatible_referenced_sources",
        "requested_source_ids": sorted(selected_ids),
        "registrations": registrations,
        "errors": errors,
        "ignored_design_time_source_ids": [
            str(item.get("source_id", ""))
            for item in contract_sources(contract)
            if not source_is_runtime_input(item)
        ],
        "message": (
            "Referenced runtime sources are available and schema-compatible."
            if not errors
            else "Only the listed referenced runtime sources are blocked; design-time templates are not required."
        ),
    }


def search_contract(
    contract: dict[str, Any], data_root: Path, source_id: str, terms: list[str], max_rows: int,
    bindings: dict[str, str] | None = None,
) -> dict[str, Any]:
    source = next(
        (item for item in contract_sources(contract) if str(item.get("source_id", "")) == source_id), None
    )
    if source is None:
        raise ReaderError(f"Unknown tabular source_id: {source_id}")
    if not source_is_runtime_input(source):
        raise ReaderError(f"Source {source_id} is design-time-only and cannot be searched at runtime")
    if not terms:
        raise ReaderError("At least one non-empty search term is required")
    table = next((item for item in source.get("tables", []) if isinstance(item, dict)), {})
    columns = [str(item.get("query_name", item.get("name", ""))) for item in table.get("columns", []) if isinstance(item, dict)]
    if not columns:
        raise ReaderError(f"Source {source_id} has no usable columns")
    relation = quote_identifier(str(source.get("view_name", "")))
    combined = "concat_ws(' ', " + ", ".join(f"coalesce(cast({quote_identifier(column)} as varchar), '')" for column in columns) + ")"
    predicates = " AND ".join(f"{combined} ILIKE ?" for _ in terms)
    connection, registrations = open_contract_connection(contract, data_root, {source_id}, bindings)
    try:
        cursor = connection.execute(
            f"SELECT * FROM {relation} WHERE {predicates} LIMIT {max_rows + 1}",
            [f"%{term}%" for term in terms],
        )
        return {
            "status": "success", "mode": "complete_matching_rows", "source_id": source_id,
            "registrations": registrations, "query_digest": query_digest(
                f"search:{source_id}:" + "\0".join(terms)
            ), **complete_rule_payload(cursor, max_rows),
        }
    finally:
        connection.close()


def query_contract(
    contract: dict[str, Any], data_root: Path, sql: str, max_rows: int,
    link_ids: Sequence[str] = (), bindings: dict[str, str] | None = None,
) -> dict[str, Any]:
    safe_sql = validate_sql(sql)
    used_source_ids = sql_source_ids(contract, safe_sql)
    if not used_source_ids:
        raise ReaderError("SQL does not reference any contract view")
    validations = validate_query_links(contract, data_root, safe_sql, link_ids, bindings)
    connection, registrations = open_contract_connection(
        contract, data_root, used_source_ids, bindings
    )
    try:
        cursor = connection.execute(f"SELECT * FROM ({safe_sql}) AS bounded_result LIMIT {max_rows + 1}")
        return {
            "status": "success", "registrations": registrations,
            "query_digest": query_digest(safe_sql), "join_validations": validations,
            **cursor_payload(cursor, max_rows),
        }
    finally:
        connection.close()


def sql_source_ids(contract: dict[str, Any], sql: str) -> set[str]:
    used: set[str] = set()
    for source in contract_sources(contract):
        view = str(source.get("view_name", ""))
        if view and re.search(rf"(?<![a-z0-9_]){re.escape(view)}(?![a-z0-9_])", sql, re.IGNORECASE):
            used.add(str(source.get("source_id", "")))
    return used


def validate_query_links(
    contract: dict[str, Any], data_root: Path, sql: str, link_ids: Sequence[str],
    bindings: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    used = sql_source_ids(contract, sql)
    selected = list(dict.fromkeys(str(item) for item in link_ids if str(item)))
    if len(used) <= 1:
        return []
    if not selected:
        raise ReaderError("Multi-source SQL requires one or more --link-id values and fresh runtime validation")
    links = {
        str(item.get("link_id", "")): item
        for item in contract.get("links", []) if isinstance(item, dict)
    }
    adjacency: dict[str, set[str]] = {identifier: set() for identifier in used}
    validations = []
    for link_reference in selected:
        link_id, key_set_index = parse_link_reference(link_reference)
        link = links.get(link_id)
        if link is None:
            raise ReaderError(f"Unknown link_id for query: {link_reference}")
        left = str(link.get("source_id", ""))
        right = str(link.get("target_id", ""))
        if left not in used or right not in used:
            raise ReaderError(f"Validated link {link_id} is not used by this SQL")
        candidate_key_set = contract_key_set(link, key_set_index)
        validate_sql_link_predicates(contract, sql, link, candidate_key_set)
        result = validate_contract_link(contract, data_root, link_id, key_set_index, bindings)
        if result.get("requires_agent_review"):
            raise ReaderError(f"Runtime join validation requires review before query: {link_id}")
        validations.append({**result, "sql_key_predicates_verified": True})
        adjacency[left].add(right)
        adjacency[right].add(left)
    reached: set[str] = set()
    frontier = [next(iter(used))]
    while frontier:
        current = frontier.pop()
        if current in reached:
            continue
        reached.add(current)
        frontier.extend(adjacency.get(current, set()) - reached)
    if reached != used:
        raise ReaderError("Validated links do not connect every contract source referenced by this SQL")
    return validations


def parse_link_reference(reference: str) -> tuple[str, int]:
    link_id, separator, suffix = reference.rpartition("@")
    if separator and link_id and suffix.isdigit():
        return link_id, int(suffix)
    return reference, 0


def sql_relation_alias(sql: str, view_name: str) -> str:
    view_pattern = rf'(?:"{re.escape(view_name)}"|{re.escape(view_name)})'
    match = re.search(
        rf"\b(?:from|join)\s+{view_pattern}(?:\s+(?:as\s+)?([^\s,()]+))?",
        sql,
        re.IGNORECASE,
    )
    if match is None:
        raise ReaderError(f"SQL does not reference contract view {view_name}")
    alias = str(match.group(1) or "").strip('"')
    if not alias or alias.casefold() in {
        "join", "left", "right", "full", "inner", "outer", "cross", "on", "where",
        "group", "order", "limit", "union", "having", "qualify",
    }:
        return view_name
    return alias


def qualified_field_pattern(alias: str, field: str) -> str:
    alias_pattern = rf'(?:"{re.escape(alias)}"|{re.escape(alias)})'
    field_pattern = rf'(?:"{re.escape(field)}"|{re.escape(field)})'
    return rf"{alias_pattern}\s*\.\s*{field_pattern}"


def validate_sql_link_predicates(
    contract: dict[str, Any], sql: str, link: dict[str, Any], candidate_key_set: dict[str, Any],
) -> None:
    source_id = str(link.get("source_id", ""))
    target_id = str(link.get("target_id", ""))
    sources = {
        str(item.get("source_id", "")): item
        for item in contract_sources(contract, {source_id, target_id})
    }
    if set(sources) != {source_id, target_id}:
        raise ReaderError(f"Link {link.get('link_id', '')} does not connect two tabular sources")
    source_view = str(sources[source_id].get("view_name", ""))
    target_view = str(sources[target_id].get("view_name", ""))
    source_alias = sql_relation_alias(sql, source_view)
    target_alias = sql_relation_alias(sql, target_view)
    for pair in candidate_key_set.get("key_pairs", []):
        source_field = str(pair.get("source_field", ""))
        target_field = str(pair.get("target_field", ""))
        left = qualified_field_pattern(source_alias, source_field)
        right = qualified_field_pattern(target_alias, target_field)
        equality = rf"(?:{left}\s*=\s*{right}|{right}\s*=\s*{left})"
        if re.search(equality, sql, re.IGNORECASE):
            continue
        if source_field == target_field:
            using = rf"\busing\s*\([^)]*(?:\"{re.escape(source_field)}\"|{re.escape(source_field)})[^)]*\)"
            if re.search(using, sql, re.IGNORECASE):
                continue
        raise ReaderError(
            f"SQL JOIN does not use validated key pair {source_field}={target_field} "
            f"for link {link.get('link_id', '')}"
        )


def export_contract(
    contract: dict[str, Any], data_root: Path, sql: str, output_raw: str,
    link_ids: Sequence[str] = (), *, overwrite: bool = False,
    bindings: dict[str, str] | None = None,
) -> dict[str, Any]:
    safe_sql = validate_sql(sql)
    used_source_ids = sql_source_ids(contract, safe_sql)
    if not used_source_ids:
        raise ReaderError("SQL does not reference any contract view")
    validations = validate_query_links(contract, data_root, safe_sql, link_ids, bindings)
    output = Path(output_raw).expanduser().resolve()
    extension = output.suffix.casefold()
    if extension not in {".csv", ".parquet"}:
        raise ReaderError("Full result export supports only .csv or .parquet")
    if output.exists() and not overwrite:
        raise ReaderError(f"Export target already exists; pass --overwrite to replace it: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary_handle = tempfile.NamedTemporaryFile(
        prefix=f"{output.name}.", suffix=".tmp", dir=output.parent, delete=False
    )
    temporary = Path(temporary_handle.name)
    temporary_handle.close()
    temporary.unlink()
    connection, registrations = open_contract_connection(
        contract, data_root, used_source_ids, bindings
    )
    try:
        escaped_output = str(temporary).replace("'", "''")
        options = "FORMAT PARQUET" if extension == ".parquet" else "FORMAT CSV, HEADER true"
        cursor = connection.execute(f"COPY ({safe_sql}) TO '{escaped_output}' ({options})")
        row = cursor.fetchone()
        exported_rows = int(row[0]) if row and isinstance(row[0], int) else None
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    finally:
        connection.close()
    temporary.replace(output)
    digest = hashlib.sha256()
    with output.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return {
        "status": "success", "mode": "complete_result_export",
        "output": str(output), "format": extension.lstrip("."),
        "row_count": exported_rows, "size_bytes": output.stat().st_size,
        "output_sha256": digest.hexdigest(),
        "artifact": {
            "kind": "exported_query_result", "path": str(output),
            "format": extension.lstrip("."), "sha256": digest.hexdigest(),
        },
        "query_digest": query_digest(safe_sql),
        "registrations": registrations, "join_validations": validations,
        "agent_context_policy": "Result rows were written to a file and were not loaded into Agent context.",
    }


def contract_key_set(link: dict[str, Any], key_set_index: int) -> dict[str, Any]:
    key_sets = [item for item in link.get("candidate_key_sets", []) if isinstance(item, dict)]
    if key_sets:
        selected = next(
            (item for item in key_sets if int(item.get("key_set_index", -1)) == key_set_index),
            None,
        )
        if selected is None and 0 <= key_set_index < len(key_sets):
            selected = key_sets[key_set_index]
        if selected is None:
            raise ReaderError(f"Unknown key_set_index {key_set_index} for link {link.get('link_id', '')}")
        pairs = selected.get("key_pairs", [])
        if not isinstance(pairs, list) or not pairs:
            raise ReaderError("Candidate key set has no key pairs")
        result = dict(selected)
    else:
        candidate = link.get("recommended_candidate")
        if not isinstance(candidate, dict):
            raise ReaderError(f"Link has no executable key candidate: {link.get('link_id', '')}")
        pairs = candidate.get("key_pairs")
        if not isinstance(pairs, list) or not pairs:
            pairs = [{
                "source_field": candidate.get("source_field", ""),
                "target_field": candidate.get("target_field", ""),
            }]
        result = {
            "key_set_index": 0,
            "key_set_kind": "legacy_or_recommended",
            "score": candidate.get("score"),
            "key_pairs": pairs,
        }
    normalized = []
    for pair in pairs:
        if not isinstance(pair, dict):
            raise ReaderError("Each candidate key pair must be an object")
        source_field = str(pair.get("source_field", "")).strip()
        target_field = str(pair.get("target_field", "")).strip()
        if not source_field or not target_field:
            raise ReaderError("Candidate key pair is missing source_field or target_field")
        normalized.append({"source_field": source_field, "target_field": target_field})
    if len(normalized) > 4:
        raise ReaderError("Runtime validation supports at most four fields in a composite key")
    if len({item["source_field"] for item in normalized}) != len(normalized):
        raise ReaderError("Composite source key repeats a field")
    if len({item["target_field"] for item in normalized}) != len(normalized):
        raise ReaderError("Composite target key repeats a field")
    result["key_pairs"] = normalized
    result["key_set_index"] = int(result.get("key_set_index", key_set_index))
    return result


def validate_contract_link(
    contract: dict[str, Any], data_root: Path, link_id: str, key_set_index: int = 0,
    bindings: dict[str, str] | None = None,
) -> dict[str, Any]:
    link = next(
        (item for item in contract.get("links", []) if isinstance(item, dict) and str(item.get("link_id", "")) == link_id),
        None,
    )
    if link is None:
        raise ReaderError(f"Unknown or unresolved link_id: {link_id}")
    if link.get("runtime_eligible", True) is False:
        raise ReaderError(f"Link {link_id} is design-time trace metadata and cannot be used for runtime SQL")
    candidate_key_set = contract_key_set(link, key_set_index)
    key_pairs = candidate_key_set["key_pairs"]
    source_id = str(link.get("source_id", ""))
    target_id = str(link.get("target_id", ""))
    sources = {str(item.get("source_id", "")): item for item in contract_sources(contract, {source_id, target_id})}
    if set(sources) != {source_id, target_id}:
        raise ReaderError(f"Link {link_id} does not connect two tabular sources")
    left_view = quote_identifier(str(sources[source_id].get("view_name", "")))
    right_view = quote_identifier(str(sources[target_id].get("view_name", "")))
    left_projection = ", ".join(
        f"{quote_identifier(pair['source_field'])} AS key_{index}"
        for index, pair in enumerate(key_pairs)
    )
    right_projection = ", ".join(
        f"{quote_identifier(pair['target_field'])} AS key_{index}"
        for index, pair in enumerate(key_pairs)
    )
    key_names = [f"key_{index}" for index in range(len(key_pairs))]
    left_nonnull = " AND ".join(f"l.{name} IS NOT NULL" for name in key_names)
    right_nonnull = " AND ".join(f"r.{name} IS NOT NULL" for name in key_names)
    raw_nonnull = " AND ".join(f"{name} IS NOT NULL" for name in key_names)
    join_condition = " AND ".join(f"l.{name}=r.{name}" for name in key_names)
    distinct_columns = ", ".join(key_names)
    connection, registrations = open_contract_connection(
        contract, data_root, {source_id, target_id}, bindings
    )
    try:
        connection.execute(
            f"CREATE TEMP TABLE __left_join_keys AS SELECT {left_projection} FROM {left_view}"
        )
        connection.execute(
            f"CREATE TEMP TABLE __right_join_keys AS SELECT {right_projection} FROM {right_view}"
        )
        left = connection.execute(
            "SELECT count(*) AS row_count, "
            f"sum(CASE WHEN {raw_nonnull} THEN 1 ELSE 0 END) AS nonnull_count, "
            f"(SELECT count(*) FROM (SELECT DISTINCT {distinct_columns} FROM __left_join_keys "
            f"WHERE {raw_nonnull})) AS distinct_count FROM __left_join_keys"
        ).fetchone()
        right = connection.execute(
            "SELECT count(*) AS row_count, "
            f"sum(CASE WHEN {raw_nonnull} THEN 1 ELSE 0 END) AS nonnull_count, "
            f"(SELECT count(*) FROM (SELECT DISTINCT {distinct_columns} FROM __right_join_keys "
            f"WHERE {raw_nonnull})) AS distinct_count FROM __right_join_keys"
        ).fetchone()
        joined = connection.execute(
            f"SELECT count(*) FROM __left_join_keys l JOIN __right_join_keys r ON {join_condition}"
        ).fetchone()[0]
        left_unmatched = connection.execute(
            f"SELECT count(*) FROM __left_join_keys l WHERE {left_nonnull} AND NOT EXISTS "
            f"(SELECT 1 FROM __right_join_keys r WHERE {join_condition})"
        ).fetchone()[0]
        right_unmatched = connection.execute(
            f"SELECT count(*) FROM __right_join_keys r WHERE {right_nonnull} AND NOT EXISTS "
            f"(SELECT 1 FROM __left_join_keys l WHERE {join_condition})"
        ).fetchone()[0]
        amplification = joined / max(1, max(int(left[1]), int(right[1])))
        unexplained_many_to_many = int(left[2]) < int(left[1]) and int(right[2]) < int(right[1])
        source_unmatched_rate = left_unmatched / max(1, int(left[1]))
        target_unmatched_rate = right_unmatched / max(1, int(right[1]))
        return {
            "status": "success", "link_id": link_id, "candidate": candidate_key_set,
            "candidate_key_set": candidate_key_set,
            "registrations": registrations,
            "source_profile": {"rows": left[0], "nonnull": left[1], "distinct": left[2]},
            "target_profile": {"rows": right[0], "nonnull": right[1], "distinct": right[2]},
            "joined_rows": joined, "source_unmatched_rows": left_unmatched,
            "target_unmatched_rows": right_unmatched, "join_amplification": round(amplification, 6),
            "source_unmatched_rate": round(source_unmatched_rate, 6),
            "target_unmatched_rate": round(target_unmatched_rate, 6),
            "unexplained_many_to_many": unexplained_many_to_many,
            "requires_agent_review": (
                amplification > 5 or joined == 0 or unexplained_many_to_many
                or source_unmatched_rate > 0.5 or target_unmatched_rate > 0.5
            ),
        }
    finally:
        connection.close()


def cursor_payload(cursor: Any, max_rows: int) -> dict[str, Any]:
    columns = [str(item[0]) for item in cursor.description or []]
    rows = cursor.fetchmany(max_rows + 1)
    truncated = len(rows) > max_rows
    rows = rows[:max_rows]
    return {
        "columns": columns,
        "rows": [[compact(value) for value in row] for row in rows],
        "row_count_returned": len(rows),
        "truncated": truncated,
    }


def complete_rule_payload(cursor: Any, max_rows: int) -> dict[str, Any]:
    columns = [str(item[0]) for item in cursor.description or []]
    rows = cursor.fetchmany(max_rows + 1)
    truncated = len(rows) > max_rows
    rows = rows[:max_rows]
    complete_rows: list[list[Any]] = []
    characters = 0
    for row in rows:
        converted = []
        for value in row:
            item = value if value is None or isinstance(value, (int, float, bool)) else str(value)
            characters += len(str(item)) if item is not None else 0
            if characters > MAX_COMPLETE_RULE_RESPONSE_CHARS:
                raise ReaderError(
                    "Complete matching rule rows exceed the bounded response budget; "
                    "add more search terms to select fewer records"
                )
            converted.append(item)
        complete_rows.append(converted)
    return {
        "columns": columns,
        "rows": complete_rows,
        "row_count_returned": len(complete_rows),
        "truncated": truncated,
        "cell_values_truncated": False,
        "selection_required": truncated or len(complete_rows) != 1,
    }


def duckdb_query(path: Path, sql: str, sheet: str, max_rows: int) -> dict[str, Any]:
    duckdb = import_duckdb()
    connection = duckdb.connect(database=":memory:")
    try:
        source = register_duckdb_source(connection, path, sheet)
        safe_sql = validate_sql(sql)
        cursor = connection.execute(f"SELECT * FROM ({safe_sql}) AS bounded_result LIMIT {max_rows + 1}")
        return {"status": "success", "source": str(path), **source, **cursor_payload(cursor, max_rows)}
    finally:
        connection.close()


def sqlite_tables(connection: sqlite3.Connection) -> list[str]:
    cursor = connection.execute(
        "SELECT name FROM sqlite_master WHERE type IN ('table', 'view') AND name NOT LIKE 'sqlite_%' ORDER BY name"
    )
    return [str(row[0]) for row in cursor.fetchall()]


def sqlite_query(path: Path, sql: str, max_rows: int) -> dict[str, Any]:
    safe_sql = validate_sql(sql)
    uri = f"file:{path.as_posix()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    try:
        connection.execute("PRAGMA query_only = ON")
        cursor = connection.execute(safe_sql)
        return {
            "status": "success",
            "source": str(path),
            "engine": "sqlite-read-only",
            **cursor_payload(cursor, max_rows),
        }
    finally:
        connection.close()


def inspect_source(path: Path, sheet: str) -> dict[str, Any]:
    extension = path.suffix.casefold()
    base = {
        "status": "success",
        "source": str(path),
        "extension": extension,
        "size_bytes": path.stat().st_size,
    }
    if extension in {".sqlite", ".sqlite3", ".db"}:
        connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
        try:
            tables = sqlite_tables(connection)
            schemas = []
            for table in tables[:100]:
                columns = connection.execute(f"PRAGMA table_info({quote_identifier(table)})").fetchall()
                schemas.append(
                    {
                        "table": table,
                        "columns": [{"name": row[1], "type": row[2]} for row in columns[:200]],
                    }
                )
            return {**base, "engine": "sqlite-read-only", "tables": schemas, "truncated": len(tables) > 100}
        finally:
            connection.close()
    duckdb = import_duckdb()
    connection = duckdb.connect(database=":memory:")
    try:
        source = register_duckdb_source(connection, path, sheet)
        cursor = connection.execute("DESCRIBE SELECT * FROM source")
        columns = [
            {"name": row[0], "type": row[1], "nullable": row[2] == "YES"}
            for row in cursor.fetchall()[:500]
        ]
        if extension in {".xlsx", ".xls", ".xlsb"}:
            source["available_sheets"] = excel_sheets(path)[:100]
        return {**base, **source, "columns": columns}
    finally:
        connection.close()


def sample_source(path: Path, sheet: str, limit: int) -> dict[str, Any]:
    if path.suffix.casefold() in {".sqlite", ".sqlite3", ".db"}:
        connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
        try:
            tables = sqlite_tables(connection)
            if not tables:
                raise ReaderError("SQLite file contains no user tables")
            table = tables[0]
        finally:
            connection.close()
        return sqlite_query(path, f"SELECT * FROM {quote_identifier(table)} LIMIT {limit}", limit)
    return duckdb_query(path, f"SELECT * FROM source LIMIT {limit}", sheet, limit)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("inspect", "sample", "query"):
        command = commands.add_parser(name)
        command.add_argument("--input", required=True)
        command.add_argument("--sheet", default="", help="Excel sheet name")
        command.add_argument("--max-rows", type=int, default=100)
        if name == "query":
            command.add_argument("--sql", required=True, help="Read-only SQL; tabular files are exposed as source")
    contract = commands.add_parser("contract", help="Show bounded source, schema, rule, and join metadata")
    contract.add_argument("--contract", required=True)
    preflight_contract_parser = commands.add_parser("preflight-contract")
    preflight_contract_parser.add_argument("--contract", required=True)
    preflight_contract_parser.add_argument("--data-root", required=True)
    preflight_contract_parser.add_argument("--source-id", action="append", required=True)
    preflight_contract_parser.add_argument("--bind", action="append", default=[])
    for name in ("search-contract", "validate-join", "query-contract", "export-contract"):
        command = commands.add_parser(name)
        command.add_argument("--contract", required=True)
        command.add_argument("--data-root", required=True)
        command.add_argument("--bind", action="append", default=[])
        command.add_argument("--max-rows", type=int, default=200)
        if name == "search-contract":
            command.add_argument("--source-id", required=True)
            command.add_argument("--term", action="append", default=[])
        elif name == "validate-join":
            command.add_argument("--link-id", required=True)
            command.add_argument("--key-set-index", type=int, default=0)
        elif name == "query-contract":
            command.add_argument("--sql", required=True)
            command.add_argument("--link-id", action="append", default=[])
        else:
            command.add_argument("--sql", required=True)
            command.add_argument("--link-id", action="append", default=[])
            command.add_argument("--output", required=True)
            command.add_argument("--overwrite", action="store_true")
    return parser


def run(argv: Sequence[str] | None = None) -> dict[str, Any]:
    args = build_parser().parse_args(argv)
    if args.command == "contract":
        return contract_overview(load_contract(args.contract))
    if args.command == "preflight-contract":
        contract = load_contract(args.contract)
        data_root = Path(args.data_root).expanduser().resolve()
        if not data_root.is_dir():
            raise ReaderError(f"Data root does not exist: {data_root}")
        source_ids = {str(item) for item in args.source_id if str(item)} or None
        return preflight_contract(
            contract, data_root, source_ids, parse_source_bindings(args.bind)
        )
    if args.command in {"search-contract", "validate-join", "query-contract", "export-contract"}:
        contract = load_contract(args.contract)
        data_root = Path(args.data_root).expanduser().resolve()
        if not data_root.is_dir():
            raise ReaderError(f"Data root does not exist: {data_root}")
        limit = max(1, min(int(args.max_rows), MAX_ROWS))
        bindings = parse_source_bindings(args.bind)
        if args.command == "search-contract":
            terms = [str(item).strip() for item in args.term if str(item).strip()]
            return search_contract(contract, data_root, args.source_id, terms, limit, bindings)
        if args.command == "validate-join":
            return validate_contract_link(
                contract, data_root, args.link_id, args.key_set_index, bindings
            )
        if args.command == "query-contract":
            return query_contract(
                contract, data_root, args.sql, limit, args.link_id, bindings
            )
        return export_contract(
            contract, data_root, args.sql, args.output, args.link_id,
            overwrite=args.overwrite, bindings=bindings,
        )
    path = validate_file(args.input)
    limit = max(1, min(int(args.max_rows), MAX_ROWS))
    if args.command == "inspect":
        return inspect_source(path, args.sheet)
    if args.command == "sample":
        return sample_source(path, args.sheet, limit)
    if path.suffix.casefold() in {".sqlite", ".sqlite3", ".db"}:
        return sqlite_query(path, args.sql, limit)
    return duckdb_query(path, args.sql, args.sheet, limit)


def main(argv: Sequence[str] | None = None) -> int:
    try:
        payload = run(argv)
        code = 0
    except Exception as exc:
        payload = {"status": "error", "message": str(exc)}
        code = 2
    # stdout crosses a host-platform boundary; ASCII JSON survives both UTF-8
    # and legacy GBK decoders.  Files written by this tool remain UTF-8.
    print(json.dumps(payload, ensure_ascii=True, indent=2))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
