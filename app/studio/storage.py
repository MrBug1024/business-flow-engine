"""File-backed persistence for AI Business Studio workspaces."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import shutil
import threading
import uuid
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from time import time
from typing import Any

from app.core.config import settings
from app.core.storage_layout import (
    LEGACY_STUDIO_ROOT,
    UNASSIGNED_ACCOUNT,
    account_business_root,
    cleanup_legacy_data_root,
    ensure_storage_layout,
    safe_scope,
)
from app.studio.file_preview import preview_workspace_file
from app.studio.runtime.llm import strip_thinking_markup
from app.studio.models import (
    AIRun,
    BusinessContext,
    BusinessFile,
    BusinessRecord,
    BusinessSummary,
    ChatMessage,
    ChatSession,
    ContextVersion,
    DISTILLATION_PHASES,
    DistillationApproval,
    DistillationPhase,
    DistillationState,
    DistillationStage,
    PackageRecord,
    TableRoleConfirmation,
    TraceAnchorSelector,
    WorkspaceNode,
)
from app.studio.capabilities.registry import installed_skill_names


DESCRIPTION_FILENAME = "description.md"
LEGACY_DESCRIPTION_FILENAME = "scenario.md"


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def now() -> float:
    return time()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(64 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _load_json_artifact(path: Path, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"Phase artifact is not a valid JSON document: {label}.") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"Phase artifact must be a JSON object: {label}.")
    return payload


def _atomic_json_write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _platform_approval_hmac_key() -> bytes:
    value = str(
        os.environ.get("BUSINESS_FLOW_PLATFORM_APPROVAL_HMAC_KEY", "")
        or getattr(settings, "business_flow_platform_approval_hmac_key", "")
    )
    if not value:
        raise ValueError(
            "BUSINESS_FLOW_PLATFORM_APPROVAL_HMAC_KEY is required for platform trace approvals."
        )
    return value.encode("utf-8")


def _platform_approval_signature(payload: dict[str, Any], key: bytes) -> str:
    unsigned = {name: value for name, value in payload.items() if name != "signature"}
    serialized = json.dumps(
        unsigned,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hmac.new(key, serialized, hashlib.sha256).hexdigest()


_TABLE_ROLES = {"input", "result", "rule", "reference", "template", "ignore"}
_TABULAR_ROLE_SUFFIXES = {
    ".csv", ".tsv", ".xlsx", ".xls", ".jsonl", ".ndjson",
    ".parquet", ".sqlite", ".sqlite3", ".db",
}
_CATALOG_PARSER = "workspace_catalog_v1"
_CATALOG_CACHE_FIELDS = (
    "size",
    "parse_status",
    "parser",
    "summary",
    "text",
    "columns",
    "sample_rows",
    "sheets",
    "structured",
    "warnings",
    "source_digest",
    "source_stat",
)
_CATALOG_SCHEMA_VERSION = 1
_CATALOG_COLUMN_LIMIT = 80
_CATALOG_ROW_LIMIT = 20
_CATALOG_CELL_LIMIT = 2_000
_CATALOG_WARNING_LIMIT = 20
_CATALOG_TEXT_LIMIT = 2_000
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_SAFE_DOCUMENT_LOCATOR = re.compile(
    r"^(?:line|paragraph|page|slide):[1-9]\d*$|^ocr:line:[1-9]\d*$"
)
ROLE_MANIFEST_RELATIVE = "outputs/data-relations/approved-role-manifest.json"
TRACE_SAMPLES_RELATIVE = "outputs/data-relations/trace-samples.json"
TRACE_REVIEW_RELATIVE = "outputs/data-relations/trace-review.json"
TRACE_REVIEW_HISTORY_RELATIVE = "outputs/data-relations/trace-review-history"
FIELD_EVIDENCE_RELATIVE = "outputs/data-relations/_field-evidence/relations.json"
PLATFORM_APPROVALS_RELATIVE = "outputs/data-relations/platform-approvals.json"
CAPABILITY_MANIFEST_RELATIVE = "outputs/capability-distillation/capability-manifest.json"
CAPABILITY_RELEASE_MANIFEST_RELATIVE = "outputs/capability-distillation/release/release.json"
CAPABILITY_SKILL_ARCHIVE_RELATIVE = "outputs/capability-distillation/release/artifacts/skill.zip"
CAPABILITY_MCP_ARCHIVE_RELATIVE = "outputs/capability-distillation/release/artifacts/mcp-stdio.zip"
_PHASE_ARTIFACTS: dict[DistillationPhase, tuple[str, set[str] | None]] = {
    "data_lineage": (
        TRACE_REVIEW_RELATIVE,
        {"pending_review", "approved"},
    ),
    "relations": (
        "outputs/data-relations/scenario-relationship.json",
        {"complete", "ready_for_review"},
    ),
    "micro_process": (
        "outputs/data-relations/micro-process.json",
        {"pending_review", "ready_for_review", "draft", "approved"},
    ),
    "business_flow": (
        "outputs/business-flow/business-flow.json",
        {"complete", "ready_for_review"},
    ),
    "capability": (CAPABILITY_MANIFEST_RELATIVE, {"complete", "ready_for_review"}),
    "package": (CAPABILITY_SKILL_ARCHIVE_RELATIVE, None),
}

_DISTILLATION_REVIEW_UI: dict[DistillationPhase, dict[str, str]] = {
    "data_lineage": {
        "label": "数据链路样本",
        "target_kind": "data_catalog",
        "target_label": "查看数据链路样本",
        "target_path": "",
    },
    "relations": {
        "label": "数据关联关系",
        "target_kind": "workspace_file",
        "target_label": "查看关联关系报告",
        "target_path": "outputs/data-relations/relation-report.md",
    },
    "micro_process": {
        "label": "微观业务复现契约",
        "target_kind": "workspace_file",
        "target_label": "查看微观业务复现契约",
        "target_path": "outputs/data-relations/micro-process.json",
    },
    "business_flow": {
        "label": "业务流程",
        "target_kind": "workspace_file",
        "target_label": "查看业务流程报告",
        "target_path": "outputs/business-flow/business-flow-report.md",
    },
    "capability": {
        "label": "能力方案",
        "target_kind": "workspace_file",
        "target_label": "查看能力方案",
        "target_path": CAPABILITY_MANIFEST_RELATIVE,
    },
    "package": {
        "label": "离线能力包",
        "target_kind": "workspace_file",
        "target_label": "查看发布清单",
        "target_path": CAPABILITY_RELEASE_MANIFEST_RELATIVE,
    },
}


def _normalize_trace_correction_path(value: Any, label: str) -> str:
    normalized = str(value or "").strip().replace("\\", "/").strip("/")
    parts = PurePosixPath(normalized).parts
    if (
        not normalized
        or len(normalized) > 500
        or "\x00" in normalized
        or any(part in {"", ".", ".."} for part in parts)
    ):
        raise ValueError(f"Trace correction {label} must be a safe relative source path.")
    return normalized


def _normalize_trace_correction_text(value: Any, label: str, limit: int) -> str:
    normalized = str(value or "").strip()
    if not normalized or len(normalized) > limit or "\x00" in normalized:
        raise ValueError(f"Trace correction {label} is required and must be at most {limit} characters.")
    return normalized


def _trace_correction_signature(correction: dict[str, Any]) -> tuple[Any, ...]:
    pairs = correction.get("key_pairs") if isinstance(correction.get("key_pairs"), list) else []
    return (
        str(correction.get("source_file", "")),
        str(correction.get("source_table", "")),
        str(correction.get("target_file", "")),
        str(correction.get("target_table", "")),
        tuple(
            (str(pair.get("source_field", "")), str(pair.get("target_field", "")))
            for pair in pairs
            if isinstance(pair, dict)
        ),
    )


def _trace_correction_endpoint(correction: dict[str, Any]) -> tuple[str, str, str, str]:
    return (
        str(correction.get("source_file", "")),
        str(correction.get("source_table", "")),
        str(correction.get("target_file", "")),
        str(correction.get("target_table", "")),
    )


def _distillation_phase_index(phase: DistillationPhase) -> int:
    try:
        return DISTILLATION_PHASES.index(phase)
    except ValueError as exc:  # pragma: no cover - protected by Pydantic/API validation
        raise ValueError(f"Unsupported distillation phase: {phase}") from exc


def _ensure_distillation_stages(state: DistillationState) -> None:
    """Fill stage defaults for workspaces saved before the state machine."""

    existing = {item.phase: item for item in state.stages}
    state.stages = [
        existing.get(phase, DistillationStage(phase=phase))
        for phase in DISTILLATION_PHASES
    ]


def _distillation_stage(
    state: DistillationState,
    phase: DistillationPhase,
) -> DistillationStage:
    _ensure_distillation_stages(state)
    return next(item for item in state.stages if item.phase == phase)


def _assert_expected_distillation_revision(
    current_revision: int,
    expected_revision: int | None,
) -> None:
    if expected_revision is not None and expected_revision != current_revision:
        raise ValueError(
            f"Distillation revision conflict: expected {expected_revision}, current {current_revision}."
        )


def _assert_distillation_actor(record: BusinessRecord, actor: str) -> None:
    if not actor.strip():
        raise PermissionError("An authenticated reviewer identity is required.")
    if record.owner_id and record.owner_id != actor:
        raise PermissionError("Only the owner of this business scenario may change distillation truth.")


def _logical_source_path(file: BusinessFile) -> str:
    return (file.workspace_path or file.filename).replace("\\", "/").strip("/")


def _is_tabular_role_source(file: BusinessFile) -> bool:
    catalog = file.structured if isinstance(file.structured, dict) else {}
    if file.parser == _CATALOG_PARSER:
        # A suffix such as .parquet or .jsonl is not enough to manufacture a
        # table scope.  Once cataloged, only an actual table/database parser
        # result is entitled to table and field roles.
        return bool(catalog.get("is_structured"))
    suffix = str(file.suffix or Path(file.filename).suffix).casefold()
    return suffix in _TABULAR_ROLE_SUFFIXES


def expected_role_scopes(file: BusinessFile) -> tuple[str, ...]:
    """Return the factual scopes a current role assignment must cover.

    A structured source is role-scoped to each cataloged worksheet/table.  A
    ``__file__`` assignment is deliberately *not* returned here: it is a
    valid default that covers every returned tabular scope (and is the only
    scope for non-tabular material).  Keeping the factual scopes separate
    lets every caller apply the same coverage rule instead of treating one
    arbitrary role on a workbook as coverage for all of its sheets.
    """

    if not _is_tabular_role_source(file):
        return ("__file__",)

    raw_sheets = file.sheets if isinstance(file.sheets, list) else []
    scopes: list[str] = []
    seen: set[str] = set()
    for sheet in raw_sheets:
        if not isinstance(sheet, dict):
            continue
        name = str(sheet.get("name") or "").strip()
        if not name or name in seen:
            continue
        seen.add(name)
        scopes.append(name)

    # A cataloged table parser should normally supply at least one table.  If
    # a legacy or partially parsed source cannot, fail closed to a file-level
    # default rather than inventing a sheet/table name that the tracing engine
    # cannot verify later.
    return tuple(scopes) if scopes else ("__file__",)


def _catalog_source_stat(source: Path) -> dict[str, int]:
    """Return the cheap change detector stored beside a source digest.

    We deliberately keep the digest as the identity of the cached facts, but
    use the filesystem fingerprint to avoid re-hashing a large, unchanged
    workbook on every catalog request.  A changed fingerprint causes a digest
    check before any parser work is reused.
    """

    stat = source.stat()
    return {
        "size": int(stat.st_size),
        "mtime_ns": int(stat.st_mtime_ns),
    }


def _catalog_short_text(value: Any, limit: int = _CATALOG_TEXT_LIMIT) -> str:
    text = str(value if value is not None else "").strip()
    return text[:limit]


def _catalog_columns(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    columns: list[str] = []
    seen: set[str] = set()
    for raw in value[:_CATALOG_COLUMN_LIMIT]:
        column = _catalog_short_text(raw, 240)
        if not column or column in seen:
            continue
        seen.add(column)
        columns.append(column)
    return columns


def _catalog_cell(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return _catalog_short_text(value, _CATALOG_CELL_LIMIT)


def _catalog_sample_rows(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    rows: list[dict[str, Any]] = []
    for raw in value[:_CATALOG_ROW_LIMIT]:
        if not isinstance(raw, dict):
            continue
        row: dict[str, Any] = {}
        for index, (key, cell) in enumerate(raw.items()):
            if index >= _CATALOG_COLUMN_LIMIT:
                break
            column = _catalog_short_text(key, 240)
            if column:
                row[column] = _catalog_cell(cell)
        rows.append(row)
    return rows


def _catalog_optional_count(value: Any) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number >= 0 else None


def _catalog_sheets(value: Any, fallback_columns: list[str]) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    sheets: list[dict[str, Any]] = []
    seen: set[str] = set()
    # Spreadsheet previews bound field extraction to the first few sheets,
    # but still carry every worksheet name as a role-coverage fact.  Do not
    # trim that identity list here or a later worksheet could escape the
    # approved role manifest.
    for index, raw in enumerate(value):
        if not isinstance(raw, dict):
            continue
        name = _catalog_short_text(raw.get("name"), 240) or "__file__"
        if name in seen:
            continue
        seen.add(name)
        columns = _catalog_columns(raw.get("columns")) or (
            fallback_columns if index == 0 else []
        )
        sample_rows = _catalog_sample_rows(raw.get("sample_rows"))
        sheets.append({
            "name": name,
            "columns": columns,
            "row_count": _catalog_optional_count(raw.get("row_count")),
            "column_count": _catalog_optional_count(raw.get("column_count")) or len(columns),
            "sample_rows": sample_rows,
        })
    return sheets


def _catalog_warnings(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    warnings: list[str] = []
    for raw in value[:_CATALOG_WARNING_LIMIT]:
        warning = _catalog_short_text(raw)
        if warning:
            warnings.append(warning)
    return warnings


def _catalog_metadata_from_preview(source: Path, preview: dict[str, Any]) -> dict[str, Any]:
    """Normalize preview output into bounded file facts safe to persist.

    The catalog intentionally stores schema/sample facts only for parsers that
    identify a real table or database.  A PDF, Word file, image, markdown
    document, archive, or unsupported input remains a file-level material: it
    gets no synthetic table, columns, or rows.
    """

    kind = _catalog_short_text(preview.get("kind") or "unsupported", 80)
    is_structured = kind in {"table", "database"}
    warnings = _catalog_warnings(preview.get("warnings"))
    if not is_structured:
        return {
            "kind": kind,
            "is_structured": False,
            "columns": [],
            "sample_rows": [],
            "sheets": [],
            "warnings": warnings,
        }

    columns = _catalog_columns(preview.get("columns"))
    sample_rows = _catalog_sample_rows(preview.get("sample_rows"))
    sheets = _catalog_sheets(preview.get("sheets"), columns)
    if not sheets:
        sheets = [{
            "name": source.name or "__file__",
            "columns": columns,
            "row_count": None,
            "column_count": len(columns),
            "sample_rows": sample_rows,
        }]
    return {
        "kind": kind,
        "is_structured": True,
        "columns": columns,
        "sample_rows": sample_rows,
        "sheets": sheets,
        "warnings": warnings,
    }


def _catalog_summary(metadata: dict[str, Any]) -> str:
    kind = _catalog_short_text(metadata.get("kind") or "file", 80)
    if metadata.get("is_structured"):
        fields = len(metadata.get("columns") or [])
        tables = len(metadata.get("sheets") or [])
        return f"Cataloged {kind}: {tables} table(s), {fields} field(s)."
    return f"Cataloged {kind} as file-level material."


def _apply_catalog_metadata(
    item: BusinessFile,
    *,
    source_digest: str,
    source_stat: dict[str, int],
    metadata: dict[str, Any],
) -> bool:
    """Update the persisted cache and report whether it changed."""

    warnings = list(metadata.get("warnings") or [])
    parse_status: str = (
        "failed"
        if str(metadata.get("kind", "")).casefold() in {"error", "missing"}
        else "parsed_with_warnings" if warnings else "parsed"
    )
    before = (
        item.size,
        item.parse_status,
        item.parser,
        item.summary,
        item.text,
        item.columns,
        item.sample_rows,
        item.sheets,
        item.structured,
        item.warnings,
        item.source_digest,
        item.source_stat,
    )
    item.size = int(source_stat.get("size", item.size))
    item.parse_status = parse_status  # type: ignore[assignment]
    item.parser = _CATALOG_PARSER
    item.summary = _catalog_summary(metadata)
    # Do not leave a full-text preview from a previous source digest attached
    # to fresh catalog facts.  Opening a document remains an explicit preview
    # action, while the durable catalog stores only bounded metadata.
    item.text = ""
    item.columns = list(metadata.get("columns") or [])
    item.sample_rows = list(metadata.get("sample_rows") or [])
    item.sheets = list(metadata.get("sheets") or [])
    item.structured = {
        "catalog_schema_version": _CATALOG_SCHEMA_VERSION,
        "kind": _catalog_short_text(metadata.get("kind") or "unsupported", 80),
        "is_structured": bool(metadata.get("is_structured")),
    }
    item.warnings = warnings
    item.source_digest = source_digest
    item.source_stat = dict(source_stat)
    after = (
        item.size,
        item.parse_status,
        item.parser,
        item.summary,
        item.text,
        item.columns,
        item.sample_rows,
        item.sheets,
        item.structured,
        item.warnings,
        item.source_digest,
        item.source_stat,
    )
    return before != after


def _apply_missing_catalog_metadata(item: BusinessFile) -> bool:
    """Persist a missing-source result so reads do not retry a dead path."""

    metadata = {
        "kind": "missing",
        "is_structured": False,
        "columns": [],
        "sample_rows": [],
        "sheets": [],
        "warnings": ["The registered source file is no longer available."],
    }
    before = (
        item.parse_status,
        item.parser,
        item.summary,
        item.text,
        item.columns,
        item.sample_rows,
        item.sheets,
        item.structured,
        item.warnings,
        item.source_digest,
        item.source_stat,
    )
    item.parse_status = "failed"
    item.parser = _CATALOG_PARSER
    item.summary = "Catalog source is missing."
    item.text = ""
    item.columns = []
    item.sample_rows = []
    item.sheets = []
    item.structured = {
        "catalog_schema_version": _CATALOG_SCHEMA_VERSION,
        "kind": metadata["kind"],
        "is_structured": False,
    }
    item.warnings = metadata["warnings"]
    item.source_digest = ""
    item.source_stat = {}
    after = (
        item.parse_status,
        item.parser,
        item.summary,
        item.text,
        item.columns,
        item.sample_rows,
        item.sheets,
        item.structured,
        item.warnings,
        item.source_digest,
        item.source_stat,
    )
    return before != after


def _merge_file_catalog(target: BusinessFile, source: BusinessFile) -> bool:
    """Merge parser-owned cache fields without replacing concurrent business state."""

    before = tuple(deepcopy(getattr(target, field)) for field in _CATALOG_CACHE_FIELDS)
    for field in _CATALOG_CACHE_FIELDS:
        setattr(target, field, deepcopy(getattr(source, field)))
    after = tuple(getattr(target, field) for field in _CATALOG_CACHE_FIELDS)
    return before != after


def _is_data_catalog_file(item: BusinessFile) -> bool:
    relative = str(item.workspace_path or item.filename).replace("\\", "/").strip("/")
    return relative == "data" or relative.startswith("data/")


def _source_snapshot_entries(record: BusinessRecord) -> list[dict[str, Any]]:
    """Return stable, content-bound identities for the registered source snapshot."""

    entries: list[dict[str, Any]] = []
    for item in record.files:
        source = Path(item.storage_path)
        if not source.is_file():
            raise ValueError(f"Registered source is missing and cannot be role-approved: {item.filename}")
        logical_path = _logical_source_path(item)
        if not logical_path:
            raise ValueError(f"Registered source has no workspace path: {item.id}")
        aliases = list(dict.fromkeys(value for value in (logical_path, item.filename) if value))
        try:
            current_stat = _catalog_source_stat(source)
        except OSError as exc:
            raise ValueError(
                f"Registered source cannot be inspected for role approval: {item.filename}"
            ) from exc
        source_digest = (
            item.source_digest
            if item.source_digest and item.source_stat == current_stat
            else _sha256_file(source)
        )
        entries.append({
            "file_id": item.id,
            "file": logical_path,
            "aliases": aliases,
            "sha256": source_digest,
            "size": current_stat["size"],
        })
    return sorted(entries, key=lambda item: (str(item["file"]), str(item["file_id"])))


def _source_snapshot_fingerprint(record: BusinessRecord) -> str:
    payload = {
        "source_revision": record.distillation.source_revision,
        "sources": _source_snapshot_entries(record),
    }
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _current_role_entries(record: BusinessRecord) -> list[dict[str, Any]]:
    state = record.distillation
    sources = {str(item["file_id"]): item for item in _source_snapshot_entries(record)}
    roles = []
    for item in state.table_roles:
        # A role is durable metadata attached to a stable file/table scope.
        # Adding or removing another source invalidates the signed manifest,
        # but it must not silently erase the user's role for an unchanged
        # file.  The newly signed manifest below still binds every role to the
        # exact current source digest and source revision.
        if item.status != "confirmed":
            continue
        source = sources.get(item.file_id)
        if source is None:
            # Confirmations for deleted files remain as audit history but do
            # not participate in the current source snapshot.
            continue
        roles.append({
            "file_id": item.file_id,
            "file": source["file"],
            "aliases": source["aliases"],
            "source_sha256": source["sha256"],
            "table": item.table_name,
            "role": item.role,
            "note": item.note,
        })
    return sorted(
        roles,
        key=lambda item: (str(item["file"]), str(item["table"]), str(item["role"])),
    )


def _role_manifest_fingerprint(record: BusinessRecord) -> str:
    """Fingerprint roles together with the exact source snapshot they describe."""

    state = record.distillation
    roles = _current_role_entries(record)
    if not roles:
        return ""
    payload = {
        "source_revision": state.source_revision,
        "source_fingerprint": _source_snapshot_fingerprint(record),
        "roles": [
            {
                "file_id": item["file_id"],
                "file": item["file"],
                "source_sha256": item["source_sha256"],
                "table": item["table"],
                "role": item["role"],
                "note": item["note"],
            }
            for item in roles
        ],
    }
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _has_current_file_roles_approval(state: DistillationState) -> bool:
    return any(
        item.status == "active"
        and item.phase == "file_roles"
        and item.decision == "approved"
        and item.source_revision == state.source_revision
        for item in state.approvals
    )


def _has_valid_platform_approval_envelope(
    workspace: Path,
    *,
    artifact_kind: str,
    artifact_fingerprint: str,
    trace_fingerprint: str,
) -> bool:
    """Return whether the platform ledger authorizes one exact artifact.

    This is intentionally independent of the mutable review JSON.  A caller
    can write ``status: approved`` into that file, but only the platform-held
    HMAC key can mint the receipt that authorizes the next phase.
    """

    path = workspace / PLATFORM_APPROVALS_RELATIVE
    if not path.is_file():
        return False
    try:
        ledger = _load_json_artifact(path, PLATFORM_APPROVALS_RELATIVE)
    except ValueError:
        return False
    if (
        ledger.get("schema_version") != 1
        or ledger.get("kind") != "platform_approval_envelopes"
        or ledger.get("issuer") != "business-flow-platform"
        or not isinstance(ledger.get("approvals"), list)
    ):
        return False
    key = _platform_approval_hmac_key()
    for envelope in ledger["approvals"]:
        if not isinstance(envelope, dict):
            continue
        if (
            envelope.get("schema_version") != 1
            or envelope.get("issuer") != "business-flow-platform"
            or envelope.get("artifact_kind") != artifact_kind
            or envelope.get("decision") != "approved"
            or str(envelope.get("artifact_fingerprint", "")) != artifact_fingerprint
            or str(envelope.get("trace_fingerprint", "")) != trace_fingerprint
            or not str(envelope.get("approval_id", "")).strip()
            or not str(envelope.get("subject", "")).strip()
            or not str(envelope.get("issued_at", "")).strip()
        ):
            continue
        expected_signature = _platform_approval_signature(envelope, key)
        if hmac.compare_digest(str(envelope.get("signature", "")), expected_signature):
            return True
    return False


class StudioStore:
    """Single-node persistence for Studio business workspaces."""

    def __init__(self, root: Path | None = None) -> None:
        self.data_root = root or settings.data_path
        if root is None:
            ensure_storage_layout()
        self.root = self.data_root
        self.business_root = self.data_root / "accounts"
        self.business_root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._locations: dict[str, Path] = {}
        self._migrate_legacy_businesses()
        self._rebuild_location_index()

    def business_dir(self, business_id: str, owner_id: str | None = None) -> Path:
        business = safe_scope(business_id, label="business id")
        if owner_id is not None:
            path = self._account_root(owner_id) / business
            if path.exists():
                self._locations[business] = path
            return path
        cached = self._locations.get(business)
        if cached is not None:
            return cached
        matches = [
            path.parent
            for path in self.business_root.glob(f"*/{business}/business.json")
            if path.is_file()
        ]
        if len(matches) == 1:
            self._locations[business] = matches[0]
            return matches[0]
        if len(matches) > 1:
            raise RuntimeError(f"Business id collision detected for {business!r}.")
        return self._account_root(UNASSIGNED_ACCOUNT) / business

    def workspace_dir(self, business_id: str) -> Path:
        path = self.business_dir(business_id) / "workspace"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def description_markdown_path(self, business_id: str) -> Path:
        return self.workspace_dir(business_id) / DESCRIPTION_FILENAME

    def scenario_markdown_path(self, business_id: str) -> Path:
        """Return the canonical description path for legacy callers."""

        return self.description_markdown_path(business_id)

    def files_dir(self, business_id: str) -> Path:
        path = self.workspace_dir(business_id) / "data"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def context_dir(self, business_id: str) -> Path:
        path = self.workspace_dir(business_id) / "context"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def deliverables_dir(self, business_id: str) -> Path:
        path = self.workspace_dir(business_id) / "deliverables"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def packages_dir(self, business_id: str) -> Path:
        path = self.deliverables_dir(business_id) / "skill-package"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def package_work_dir(self, business_id: str, package_id: str) -> Path:
        path = self.packages_dir(business_id) / package_id
        path.mkdir(parents=True, exist_ok=True)
        return path

    def next_data_file_path(self, business_id: str, filename: str) -> Path:
        target = self.files_dir(business_id) / _safe_filename(filename)
        if not target.exists():
            return target
        stem = target.stem
        suffix = target.suffix
        for index in range(2, 1000):
            candidate = target.with_name(f"{stem}-{index}{suffix}")
            if not candidate.exists():
                return candidate
        return target.with_name(f"{stem}-{new_id('copy')}{suffix}")

    def _meta_file(self, business_id: str, owner_id: str | None = None) -> Path:
        return self.business_dir(business_id, owner_id) / "business.json"

    def create(
        self,
        name: str,
        goal: str = "",
        description: str = "",
        *,
        owner_id: str = "",
    ) -> BusinessRecord:
        with self._lock:
            business_id = new_id("biz")
            ts = now()
            cleaned_goal = goal.strip()
            cleaned_description = description.strip()
            context = BusinessContext(
                business_id=business_id,
                name=name.strip(),
                goal=cleaned_goal or cleaned_description,
                user_requirements=[
                    {
                        "id": new_id("req"),
                        "text": cleaned_description or cleaned_goal,
                        "source": DESCRIPTION_FILENAME,
                        "created_at": ts,
                    }
                ]
                if (cleaned_description or cleaned_goal)
                else [],
            )
            record = BusinessRecord(
                id=business_id,
                owner_id=owner_id,
                name=name.strip(),
                goal=cleaned_goal,
                description=cleaned_description,
                created_at=ts,
                updated_at=ts,
                context=context,
                chat_sessions=[
                    ChatSession(
                        id=new_id("chat"),
                        business_id=business_id,
                        created_at=ts,
                        updated_at=ts,
                    )
                ],
            )
            self._locations[business_id] = self.business_dir(
                business_id,
                owner_id or UNASSIGNED_ACCOUNT,
            )
            self._ensure_workspace(record)
            self.description_markdown_path(business_id).write_text(_description_markdown(record), encoding="utf-8")
            self.create_version(record, "Created business workspace", "create_business")
            self.save(record)
            return record

    def list(self, owner_id: str | None = None) -> list[BusinessSummary]:
        with self._lock:
            items: list[BusinessSummary] = []
            pattern = (
                self._account_root(owner_id).glob("*/business.json")
                if owner_id is not None
                else self.business_root.glob("*/*/business.json")
            )
            for meta in pattern:
                try:
                    record = self._read(meta)
                except Exception:  # noqa: BLE001
                    continue
                if owner_id is not None and record.owner_id != owner_id:
                    continue
                self._locations[record.id] = meta.parent
                items.append(self.to_summary(record))
            items.sort(key=lambda item: item.updated_at, reverse=True)
            return items

    def get(self, business_id: str, owner_id: str | None = None) -> BusinessRecord | None:
        with self._lock:
            meta = self._meta_file(business_id, owner_id)
            if not meta.exists():
                return None
            record = self._read(meta)
            if owner_id is not None and record.owner_id != owner_id:
                return None
            self._locations[record.id] = meta.parent
            changed = _sanitize_legacy_runtime_state(record)
            changed = _ensure_chat_sessions(record) or changed
            changed = self._ensure_workspace(record) or changed
            changed = _migrate_description_sources(record) or changed
            self._write_business_context(record)
            if changed:
                target = self._meta_file(record.id, record.owner_id or UNASSIGNED_ACCOUNT)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(record.model_dump_json(indent=2), encoding="utf-8")
            return record

    def require(self, business_id: str, owner_id: str | None = None) -> BusinessRecord:
        record = self.get(business_id, owner_id)
        if record is None:
            raise KeyError(business_id)
        return record

    def save(self, record: BusinessRecord) -> BusinessRecord:
        with self._lock:
            owner = record.owner_id or UNASSIGNED_ACCOUNT
            business_id = safe_scope(record.id, label="business id")
            expected = self.business_root / safe_scope(owner, label="account id") / business_id
            current = self.business_dir(record.id)
            if current != expected and current.exists():
                if expected.exists():
                    raise FileExistsError(expected)
                expected.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(current), str(expected))
            self._locations[record.id] = expected
            record.updated_at = now()
            record.context.name = record.name
            record.context.goal = record.goal or record.context.goal
            _ensure_chat_sessions(record)
            self._ensure_workspace(record)
            _migrate_description_sources(record)
            self._write_business_context(record)
            target = expected / "business.json"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(record.model_dump_json(indent=2), encoding="utf-8")
            return record

    def ensure_file_catalog(
        self,
        record: BusinessRecord,
        item: BusinessFile,
    ) -> bool:
        """Populate or refresh one file's persisted catalog facts.

        This is intentionally file-fact extraction only.  It does not infer a
        relationship, assign a role, or alter distillation state.  The
        cache is valid only for the exact content digest; the lightweight stat
        fingerprint prevents repeated full hashing and parser calls while the
        file is unchanged.
        """

        with self._lock:
            return self._ensure_file_catalog_snapshot(record, item)

    def _ensure_file_catalog_snapshot(
        self,
        record: BusinessRecord,
        item: BusinessFile,
    ) -> bool:
        """Populate parser facts on a caller-owned record snapshot."""

        try:
            workspace = self.workspace_dir(record.id).resolve()
            source = Path(item.storage_path).resolve()
        except (OSError, ValueError):
            return _apply_missing_catalog_metadata(item)
        if not source.is_file() or workspace not in source.parents:
            return _apply_missing_catalog_metadata(item)

        try:
            source_stat = _catalog_source_stat(source)
        except OSError:
            return _apply_missing_catalog_metadata(item)

        # The normal catalog read path ends here: cached schema facts are
        # already tied to this exact on-disk file fingerprint.
        if (
            item.parser == _CATALOG_PARSER
            and bool(item.source_digest)
            and item.source_stat == source_stat
        ):
            return False

        try:
            source_digest = _sha256_file(source)
        except OSError:
            return _apply_missing_catalog_metadata(item)

        # A timestamp-only change has not changed source bytes.  Keep the
        # existing metadata and simply refresh the stat fingerprint.
        if (
            item.parser == _CATALOG_PARSER
            and item.source_digest == source_digest
        ):
            before = (item.size, item.source_stat)
            item.size = int(source_stat["size"])
            item.source_stat = source_stat
            return before != (item.size, item.source_stat)

        try:
            preview = preview_workspace_file(source)
        except Exception as exc:  # noqa: BLE001 - catalog reads must not reject uploads
            preview = {
                "kind": "error",
                "columns": [],
                "sample_rows": [],
                "sheets": [],
                "warnings": [f"{type(exc).__name__}: {exc}"],
            }
        metadata = _catalog_metadata_from_preview(source, preview)
        return _apply_catalog_metadata(
            item,
            source_digest=source_digest,
            source_stat=source_stat,
            metadata=metadata,
        )

    def prepare_data_catalog(
        self,
        business_id: str,
        owner_id: str,
    ) -> BusinessRecord:
        """Parse catalog facts on a snapshot and merge them into the latest record.

        Previewing legacy files can be slow.  The final write therefore reloads
        the business under the store lock and copies only parser-owned fields,
        preserving chat, role, approval, and run updates made during parsing.
        """

        snapshot = self.require(business_id, owner_id)
        candidates = [item for item in snapshot.files if _is_data_catalog_file(item)]
        for item in candidates:
            self._ensure_file_catalog_snapshot(snapshot, item)

        with self._lock:
            current = self.require(business_id, owner_id)
            current_by_id = {item.id: item for item in current.files}
            changed = False
            for candidate in candidates:
                target = current_by_id.get(candidate.id)
                if target is None or (
                    target.storage_path != candidate.storage_path
                    or target.workspace_path != candidate.workspace_path
                ):
                    continue

                try:
                    source = Path(target.storage_path).resolve()
                    workspace = self.workspace_dir(current.id).resolve()
                except (OSError, ValueError):
                    continue
                if source.is_file() and workspace in source.parents:
                    try:
                        current_stat = _catalog_source_stat(source)
                    except OSError:
                        continue
                    if candidate.source_stat != current_stat or not candidate.source_digest:
                        continue
                    # Another request may already have committed an equally
                    # current cache.  Keep that newer record untouched.
                    if (
                        target.parser == _CATALOG_PARSER
                        and bool(target.source_digest)
                        and target.source_stat == current_stat
                    ):
                        continue
                elif candidate.source_stat:
                    continue

                changed = _merge_file_catalog(target, candidate) or changed

            changed = self.refresh_role_manifest_fingerprint(current) or changed
            if changed:
                self.save(current)
            return current

    def claim_unowned(self, owner_id: str) -> list[str]:
        """Assign legacy workspaces to the first registered Studio account."""

        claimed: list[str] = []
        with self._lock:
            owner_root = self._account_root(owner_id)
            for meta in self.business_root.glob("*/*/business.json"):
                try:
                    record = self._read(meta)
                except Exception:  # noqa: BLE001
                    continue
                if record.owner_id:
                    continue
                record.owner_id = owner_id
                destination = owner_root / record.id
                if destination.exists() and destination != meta.parent:
                    raise FileExistsError(destination)
                if destination != meta.parent:
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(meta.parent), str(destination))
                target = destination / "business.json"
                target.write_text(record.model_dump_json(indent=2), encoding="utf-8")
                self._locations[record.id] = destination
                claimed.append(record.id)
        return claimed

    def delete(self, business_id: str) -> bool:
        with self._lock:
            target = self.business_dir(business_id)
            if not target.exists():
                return False
            shutil.rmtree(target)
            self._locations.pop(business_id, None)
            return True

    def _account_root(self, owner_id: str) -> Path:
        owner = safe_scope(owner_id, label="account id")
        if self.data_root == settings.data_path:
            return account_business_root(owner)
        return self.business_root / owner

    def _migrate_legacy_businesses(self) -> None:
        legacy_roots = [self.data_root / "business_studio" / "businesses"]
        if self.data_root == settings.data_path:
            legacy_roots.append(LEGACY_STUDIO_ROOT / "businesses")
        for legacy_root in dict.fromkeys(legacy_roots):
            if not legacy_root.is_dir():
                continue
            for source in list(legacy_root.iterdir()):
                meta = source / "business.json"
                if not source.is_dir() or not meta.is_file():
                    self._archive_legacy_business(source)
                    continue
                try:
                    payload = json.loads(meta.read_text(encoding="utf-8"))
                    business_id = safe_scope(
                        str(payload.get("id") or source.name),
                        label="business id",
                    )
                    owner_id = safe_scope(
                        str(payload.get("owner_id") or UNASSIGNED_ACCOUNT),
                        label="account id",
                    )
                except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
                    self._archive_legacy_business(source)
                    continue
                destination = self._account_root(owner_id) / business_id
                if destination.exists():
                    if self.data_root == settings.data_path:
                        conflict_root = (
                            settings.system_path
                            / "migrations"
                            / "conflicts"
                            / "businesses"
                        )
                        conflict_root.mkdir(parents=True, exist_ok=True)
                        conflict = conflict_root / source.name
                        index = 2
                        while conflict.exists():
                            conflict = conflict_root / f"{source.name}-{index}"
                            index += 1
                        shutil.move(str(source), str(conflict))
                    continue
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(source), str(destination))
            try:
                legacy_root.rmdir()
            except OSError:
                pass
        if self.data_root == settings.data_path:
            cleanup_legacy_data_root()

    def _archive_legacy_business(self, source: Path) -> None:
        if self.data_root != settings.data_path or not source.exists():
            return
        archive_root = settings.system_path / "legacy" / "unmigrated-businesses"
        archive_root.mkdir(parents=True, exist_ok=True)
        destination = archive_root / source.name
        index = 2
        while destination.exists():
            destination = archive_root / f"{source.name}-{index}"
            index += 1
        shutil.move(str(source), str(destination))

    def _rebuild_location_index(self) -> None:
        self._locations.clear()
        for meta in self.business_root.glob("*/*/business.json"):
            try:
                payload = json.loads(meta.read_text(encoding="utf-8"))
                business_id = safe_scope(
                    str(payload.get("id") or meta.parent.name),
                    label="business id",
                )
            except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
                continue
            previous = self._locations.get(business_id)
            if previous is not None and previous != meta.parent:
                raise RuntimeError(f"Business id collision detected for {business_id!r}.")
            self._locations[business_id] = meta.parent

    def workspace_tree(self, record: BusinessRecord) -> WorkspaceNode:
        self._ensure_workspace(record)
        return _tree_node(self.workspace_dir(record.id), self.workspace_dir(record.id), record.name)

    def read_description_markdown(self, record: BusinessRecord) -> str:
        self._ensure_workspace(record)
        return self.description_markdown_path(record.id).read_text(encoding="utf-8")

    def write_description_markdown(self, record: BusinessRecord, content: str) -> BusinessRecord:
        self._ensure_workspace(record)
        path = self.description_markdown_path(record.id)
        path.write_text(content, encoding="utf-8")
        _clear_workspace_tombstone(record, DESCRIPTION_FILENAME)
        record.description = content[:4000]
        _append_requirement_from_description(record, content)
        self.create_version(record, "Updated description.md", "edit_description_markdown", actor="user")
        return self.save(record)

    def create_version(
        self,
        record: BusinessRecord,
        summary: str,
        trigger: str,
        *,
        actor: str = "system",
        model: str = "local-context-builder",
        evidence_ids: list[str] | None = None,
    ) -> ContextVersion:
        record.current_version += 1
        snapshot = record.context.model_dump(mode="json")
        snapshot["versions"] = []
        version = ContextVersion(
            version=record.current_version,
            summary=summary,
            trigger=trigger,
            created_at=now(),
            actor=actor,
            model=model,
            evidence_ids=evidence_ids or [],
            snapshot=snapshot,
        )
        record.context.versions.append(version)
        return version

    def invalidate_distillation(
        self,
        record: BusinessRecord,
        reason: str,
        *,
        actor: str = "system",
        source_changed: bool = True,
        from_phase: DistillationPhase = "file_roles",
    ) -> None:
        """Start a new durable distillation revision and invalidate dependents.

        This is intentionally owned by storage rather than an Agent or Skill.
        A source snapshot or a confirmed role is an upstream fact: once it
        changes, prior trace/relation/flow/package approvals can no longer be
        reused as though they described the current scenario.
        """

        with self._lock:
            _assert_distillation_actor(record, actor)
            state = record.distillation
            _ensure_distillation_stages(state)
            phase_index = _distillation_phase_index(from_phase)
            timestamp = now()
            state.revision += 1
            if source_changed:
                state.source_revision += 1
                # Source changes invalidate authority, not durable role
                # labels. Rebind active roles to the new digest-bound source
                # snapshot; approval below is still required before tracing.
                try:
                    state.role_manifest_fingerprint = _role_manifest_fingerprint(record)
                except ValueError:
                    state.role_manifest_fingerprint = ""
            if phase_index <= _distillation_phase_index("data_lineage"):
                # An anchor identifies a row in the previous source/role
                # snapshot, so it must never survive either of those changes.
                state.anchor_selector = None
            state.current_phase = from_phase
            state.last_invalidated_at = timestamp
            state.last_invalidation_reason = reason.strip()[:4000]
            state.artifact_contracts = {}

            for stage in state.stages:
                index = _distillation_phase_index(stage.phase)
                if index < phase_index:
                    continue
                stage.revision = state.revision
                stage.updated_at = timestamp
                stage.invalidation_reason = state.last_invalidation_reason
                stage.status = "pending" if index == phase_index else "invalidated"

            for approval in state.approvals:
                if (
                    approval.status == "active"
                    and _distillation_phase_index(approval.phase) >= phase_index
                ):
                    approval.status = "invalidated"
                    approval.invalidated_at = timestamp
                    approval.invalidation_reason = state.last_invalidation_reason

    def set_table_role(
        self,
        record: BusinessRecord,
        *,
        file_id: str,
        table_name: str,
        role: str,
        note: str = "",
        actor: str,
        expected_revision: int | None = None,
    ) -> TableRoleConfirmation:
        """Persist a user-confirmed table role and reset downstream phases."""

        with self._lock:
            _assert_distillation_actor(record, actor)
            state = record.distillation
            _ensure_distillation_stages(state)
            _assert_expected_distillation_revision(state.revision, expected_revision)
            source_file = next((item for item in record.files if item.id == file_id), None)
            if source_file is None:
                raise ValueError("The selected file does not belong to this business scenario.")
            normalized_table = table_name.strip()
            if not normalized_table:
                raise ValueError("A table or sheet name is required for role confirmation.")
            if role not in _TABLE_ROLES:
                raise ValueError("Unsupported table role.")
            # Legacy records may predate upload-time cataloging.  Establish
            # factual file shape before deciding whether a table scope is
            # legal, so a non-tabular parser result cannot be mislabeled as a
            # field-bearing table based only on its filename suffix.
            self.ensure_file_catalog(record, source_file)
            if not _is_tabular_role_source(source_file):
                if normalized_table != "__file__":
                    raise ValueError("Non-tabular material must use the __file__ role scope.")
            else:
                scopes = expected_role_scopes(source_file)
                if normalized_table != "__file__" and normalized_table not in scopes:
                    raise ValueError(
                        "Structured material must use a cataloged table or sheet name, "
                        "or the __file__ default role scope."
                    )

            self.invalidate_distillation(
                record,
                f"Table role changed: {file_id}/{normalized_table}",
                actor=actor,
                source_changed=False,
                from_phase="file_roles",
            )
            timestamp = now()
            for item in state.table_roles:
                if (
                    item.status == "confirmed"
                    and item.file_id == file_id
                    and item.table_name == normalized_table
                ):
                    item.status = "superseded"
                    item.superseded_at = timestamp
            confirmation = TableRoleConfirmation(
                id=new_id("table_role"),
                file_id=file_id,
                table_name=normalized_table,
                role=role,  # type: ignore[arg-type]
                note=note.strip(),
                confirmed_by=actor,
                confirmed_at=timestamp,
                source_revision=state.source_revision,
                revision=state.revision,
            )
            state.table_roles.append(confirmation)
            state.role_manifest_fingerprint = _role_manifest_fingerprint(record)
            roles_stage = _distillation_stage(state, "file_roles")
            roles_stage.status = "ready_for_review"
            roles_stage.revision = state.revision
            roles_stage.updated_at = timestamp
            roles_stage.invalidation_reason = ""
            return confirmation

    def refresh_role_manifest_fingerprint(self, record: BusinessRecord) -> bool:
        """Refresh legacy/stale role state against the current source snapshot."""

        with self._lock:
            fingerprint = _role_manifest_fingerprint(record)
            if record.distillation.role_manifest_fingerprint == fingerprint:
                return False
            if any(item.status == "active" for item in record.distillation.approvals):
                self.invalidate_distillation(
                    record,
                    "Rebound durable file roles to the current source snapshot",
                    actor=record.owner_id or "system",
                    source_changed=False,
                    from_phase="file_roles",
                )
            record.distillation.role_manifest_fingerprint = fingerprint
            return True

    def _write_approved_role_manifest(
        self,
        record: BusinessRecord,
        *,
        approval_id: str,
        actor: str,
    ) -> tuple[str, str]:
        """Write the platform-signed role contract consumed by tracing.

        The manifest intentionally binds role names to the content hash of each
        registered source.  A role label without that binding could be replayed
        against a changed upload and turn an input or rule table into a false
        result anchor.
        """

        state = record.distillation
        # The signed manifest is the final authority consumed by the tracing
        # engine.  Refresh every source's factual shape here too: a caller
        # may have skipped the catalog screen, and a single role on one sheet
        # must never authorize the rest of a workbook by accident.
        for source in record.files:
            self.ensure_file_catalog(record, source)
        source_fingerprint = _source_snapshot_fingerprint(record)
        roles = _current_role_entries(record)
        calculated_role_fingerprint = _role_manifest_fingerprint(record)
        if not roles or not calculated_role_fingerprint:
            raise ValueError("No current source-snapshot table roles are available for approval.")
        role_source_ids = {str(item["file_id"]) for item in roles}
        source_ids = {str(item["file_id"]) for item in _source_snapshot_entries(record)}
        if role_source_ids != source_ids:
            missing = sorted(source_ids - role_source_ids)
            raise ValueError(
                "Every registered source needs a current file or table role before approval; missing: "
                + ", ".join(missing)
            )
        roles_by_source: dict[str, list[dict[str, Any]]] = {}
        for role in roles:
            roles_by_source.setdefault(str(role["file_id"]), []).append(role)
        scope_errors: list[str] = []
        for source in record.files:
            source_roles = roles_by_source.get(source.id, [])
            assigned_scopes = [str(item.get("table") or "").strip() for item in source_roles]
            assigned = {scope for scope in assigned_scopes if scope}
            expected_scopes = set(expected_role_scopes(source))
            allowed_scopes = expected_scopes | {"__file__"}
            unexpected = sorted(assigned - allowed_scopes)
            duplicate_scopes = sorted(
                scope
                for scope in assigned
                if assigned_scopes.count(scope) > 1
            )
            if unexpected:
                scope_errors.append(
                    f"{source.filename}: role scope(s) no longer found in the data catalog: "
                    + ", ".join(unexpected)
                )
            if duplicate_scopes:
                scope_errors.append(
                    f"{source.filename}: more than one current role was assigned to "
                    + ", ".join(duplicate_scopes)
                )
            if "__file__" not in assigned:
                missing_scopes = sorted(expected_scopes - assigned)
                if missing_scopes:
                    scope_errors.append(
                        f"{source.filename}: missing role for table/sheet "
                        + ", ".join(missing_scopes)
                        + " (or set one __file__ default role)"
                    )
        if scope_errors:
            raise ValueError(
                "Every current table or sheet needs a role, unless its source has a __file__ default; "
                + "; ".join(scope_errors)
            )
        if state.role_manifest_fingerprint != calculated_role_fingerprint:
            raise ValueError("The in-memory role manifest does not match the current source snapshot.")
        payload = {
            "schema_version": 1,
            "kind": "approved_role_manifest",
            "status": "approved",
            "issuer": "business-flow-platform",
            "source_revision": state.source_revision,
            "source_fingerprint": source_fingerprint,
            "role_manifest_fingerprint": calculated_role_fingerprint,
            "approval_id": approval_id,
            "approved_by": actor,
            "approved_at": utc_now(),
            "sources": _source_snapshot_entries(record),
            "roles": roles,
        }
        payload["signature"] = _platform_approval_signature(payload, _platform_approval_hmac_key())
        workspace = self.workspace_dir(record.id).resolve()
        path = workspace / ROLE_MANIFEST_RELATIVE
        _atomic_json_write(path, payload)
        return ROLE_MANIFEST_RELATIVE, _sha256_file(path)

    def require_approved_role_manifest(self, record: BusinessRecord) -> Path:
        """Return the canonical current manifest or fail before command injection."""

        with self._lock:
            state = record.distillation
            active = [
                item for item in state.approvals
                if item.status == "active"
                and item.phase == "file_roles"
                and item.decision == "approved"
                and item.source_revision == state.source_revision
                and item.artifact_fingerprint == state.role_manifest_fingerprint
            ]
            if not active:
                raise ValueError("A current approved file-role manifest is required before result tracing.")
            path = self.workspace_dir(record.id).resolve() / ROLE_MANIFEST_RELATIVE
            if not path.is_file():
                raise ValueError("The canonical approved-role-manifest.json is missing.")
            payload = _load_json_artifact(path, ROLE_MANIFEST_RELATIVE)
            expected_source_fingerprint = _source_snapshot_fingerprint(record)
            expected_role_fingerprint = _role_manifest_fingerprint(record)
            if (
                payload.get("schema_version") != 1
                or payload.get("kind") != "approved_role_manifest"
                or payload.get("status") != "approved"
                or payload.get("issuer") != "business-flow-platform"
                or payload.get("source_revision") != state.source_revision
                or payload.get("source_fingerprint") != expected_source_fingerprint
                or payload.get("role_manifest_fingerprint") != expected_role_fingerprint
                or payload.get("roles") != _current_role_entries(record)
                or payload.get("sources") != _source_snapshot_entries(record)
            ):
                raise ValueError("The canonical role manifest is stale or does not match the current source snapshot.")
            expected_signature = _platform_approval_signature(payload, _platform_approval_hmac_key())
            if not hmac.compare_digest(str(payload.get("signature", "")), expected_signature):
                raise ValueError("The canonical role manifest has no valid platform signature.")
            return path

    def _require_current_upstream_approval_chain(
        self,
        record: BusinessRecord,
        phase: DistillationPhase,
    ) -> None:
        """Fail closed when an approval API call skips stale upstream evidence.

        Runtime command gates already prevent the Agent from advancing on a
        changed artifact.  The HTTP approval endpoint is another trust
        boundary, though: a stale workspace file must not become acceptable
        just because a caller bypassed the Agent runtime.  Compare every
        approved predecessor with the bytes that exist *now*.
        """

        state = record.distillation
        phase_index = _distillation_phase_index(phase)
        workspace = self.workspace_dir(record.id).resolve()
        for upstream in DISTILLATION_PHASES[:phase_index]:
            if upstream == "file_roles":
                self.require_approved_role_manifest(record)
                continue
            requirement = _PHASE_ARTIFACTS.get(upstream)
            if requirement is None:  # pragma: no cover - phases are model-owned
                raise ValueError(f"No artifact contract is defined for upstream phase {upstream}.")
            relative, _statuses = requirement
            path = workspace / relative
            if not path.is_file():
                raise ValueError(
                    f"Upstream {upstream} approval is stale because its canonical artifact is missing."
                )
            fingerprint = _sha256_file(path)
            if not any(
                item.status == "active"
                and item.phase == upstream
                and item.decision == "approved"
                and item.source_revision == state.source_revision
                and item.artifact_id == relative
                and item.artifact_fingerprint == fingerprint
                for item in state.approvals
            ):
                raise ValueError(
                    f"Upstream {upstream} approval is stale or does not match the current artifact bytes. "
                    "Return to that Workbench phase for correction and fresh approval."
                )

    def record_distillation_approval(
        self,
        record: BusinessRecord,
        *,
        phase: DistillationPhase,
        decision: str,
        artifact_id: str,
        artifact_fingerprint: str,
        note: str = "",
        actor: str,
        expected_revision: int | None = None,
    ) -> DistillationApproval:
        """Record a login-identity-bound decision for the current phase only."""

        with self._lock:
            _assert_distillation_actor(record, actor)
            state = record.distillation
            _ensure_distillation_stages(state)
            _assert_expected_distillation_revision(state.revision, expected_revision)
            if phase not in DISTILLATION_PHASES:
                raise ValueError("Unsupported distillation phase.")
            if decision not in {"approved", "rejected"}:
                raise ValueError("Approval decision must be approved or rejected.")
            if phase != state.current_phase:
                raise ValueError(
                    f"Only the current distillation phase may be reviewed; current phase is {state.current_phase}."
                )
            normalized_artifact_id = artifact_id.strip()
            normalized_fingerprint = artifact_fingerprint.strip()
            if not normalized_artifact_id or not normalized_fingerprint:
                raise ValueError("An artifact id and fingerprint are required for an approval.")
            if decision == "rejected" and not note.strip():
                raise ValueError("A rejection note is required so the user can correct the candidate.")
            if decision == "approved" and phase != "file_roles":
                self._require_current_upstream_approval_chain(record, phase)
            if (
                phase == "package"
                and decision == "approved"
                and any(
                    item.status == "active"
                    and item.phase == "package"
                    and item.decision == "approved"
                    and item.source_revision == state.source_revision
                    for item in state.approvals
                )
            ):
                raise ValueError(
                    "This capability package has already been platform-approved. "
                    "Reject it first if a new release candidate is needed."
                )
            approval_id = new_id("approval")
            platform_receipt_path = ""
            platform_receipt_fingerprint = ""
            review_artifact_path = ""
            review_artifact_fingerprint = ""
            if phase == "file_roles":
                if normalized_artifact_id != "table-roles":
                    raise ValueError("File-role approval must review the canonical table-roles manifest.")
                if not state.role_manifest_fingerprint:
                    raise ValueError("No current source-snapshot table roles are available for approval.")
                if normalized_fingerprint != state.role_manifest_fingerprint:
                    raise ValueError("The table-role artifact fingerprint is stale or does not match this revision.")
                if decision == "approved":
                    # The analyzer consumes a signed manifest, not a mutable
                    # role label in state.  Refuse to advance when the platform
                    # cannot mint that authority artifact.
                    _platform_approval_hmac_key()
                    platform_receipt_path, platform_receipt_fingerprint = self._write_approved_role_manifest(
                        record,
                        approval_id=approval_id,
                        actor=actor,
                    )
            else:
                normalized_artifact_id, computed_fingerprint, trace_fingerprint = self._validate_distillation_artifact(
                    record,
                    phase,
                    normalized_artifact_id,
                )
                if normalized_fingerprint != computed_fingerprint:
                    raise ValueError(
                        "The artifact fingerprint does not match the current workspace artifact."
                    )
                normalized_fingerprint = computed_fingerprint
                if phase in {"data_lineage", "micro_process"}:
                    # Fail before altering a candidate if the deployment lacks
                    # the key that downstream workers use to verify authority.
                    if decision == "approved":
                        _platform_approval_hmac_key()
                    normalized_fingerprint = self._apply_platform_review_decision(
                        record,
                        phase=phase,
                        expected_artifact_fingerprint=computed_fingerprint,
                        trace_fingerprint=trace_fingerprint,
                        decision=decision,
                        actor=actor,
                        note=note,
                    )
                    review_artifact_path = normalized_artifact_id
                    review_artifact_fingerprint = normalized_fingerprint
                    if decision == "approved":
                        platform_receipt_path, platform_receipt_fingerprint = self._write_platform_approval_envelope(
                            record,
                            approval_id=approval_id,
                            phase=phase,
                            subject=actor,
                            artifact_fingerprint=normalized_fingerprint,
                            trace_fingerprint=trace_fingerprint,
                        )
                elif phase == "package":
                    review_artifact_path = normalized_artifact_id
                    review_artifact_fingerprint = normalized_fingerprint
                    if decision == "approved":
                        (
                            platform_receipt_path,
                            platform_receipt_fingerprint,
                        ) = self._approve_capability_package(
                            record,
                            approval_id=approval_id,
                            actor=actor,
                            skill_archive_fingerprint=normalized_fingerprint,
                        )
                    else:
                        self._mark_capability_package_revision_required(
                            record,
                            actor=actor,
                            note=note,
                        )

            timestamp = now()
            for item in state.approvals:
                if (
                    item.status == "active"
                    and item.phase == phase
                    and item.revision == state.revision
                ):
                    item.status = "superseded"
                    item.superseded_at = timestamp
            approval = DistillationApproval(
                id=approval_id,
                phase=phase,
                decision=decision,  # type: ignore[arg-type]
                artifact_id=normalized_artifact_id,
                artifact_fingerprint=normalized_fingerprint,
                note=note.strip(),
                actor_id=actor,
                revision=state.revision,
                source_revision=state.source_revision,
                created_at=timestamp,
                review_artifact_path=review_artifact_path,
                review_artifact_fingerprint=review_artifact_fingerprint,
                platform_receipt_path=platform_receipt_path,
                platform_receipt_fingerprint=platform_receipt_fingerprint,
            )
            state.approvals.append(approval)
            state.artifact_contracts = {}
            stage = _distillation_stage(state, phase)
            stage.status = decision  # type: ignore[assignment]
            stage.revision = state.revision
            stage.updated_at = timestamp
            stage.invalidation_reason = ""

            phase_index = _distillation_phase_index(phase)
            if decision == "approved" and phase_index + 1 < len(DISTILLATION_PHASES):
                next_phase = DISTILLATION_PHASES[phase_index + 1]
                state.current_phase = next_phase
                next_stage = _distillation_stage(state, next_phase)
                next_stage.status = "pending"
                next_stage.revision = state.revision
                next_stage.updated_at = timestamp
                next_stage.invalidation_reason = ""
            elif decision == "rejected":
                # A rejected final archive must return to capability generation
                # rather than leave the state machine stranded at `package`.
                # The old capability approval cannot authorize a regenerated
                # archive, and an already published manifest is revoked on
                # disk before the release gate can be attempted again.
                repair_phase: DistillationPhase = "capability" if phase == "package" else phase
                state.current_phase = repair_phase
                state.last_invalidated_at = timestamp
                state.last_invalidation_reason = f"{phase} rejected by reviewer"
                if phase == "package":
                    capability_stage = _distillation_stage(state, "capability")
                    capability_stage.status = "rejected"
                    capability_stage.revision = state.revision
                    capability_stage.updated_at = timestamp
                    capability_stage.invalidation_reason = state.last_invalidation_reason
                    for prior_approval in state.approvals:
                        if (
                            prior_approval.status == "active"
                            and prior_approval.phase == "capability"
                        ):
                            prior_approval.status = "invalidated"
                            prior_approval.invalidated_at = timestamp
                            prior_approval.invalidation_reason = state.last_invalidation_reason
                for downstream_phase in DISTILLATION_PHASES[phase_index + 1 :]:
                    downstream = _distillation_stage(state, downstream_phase)
                    downstream.status = "invalidated"
                    downstream.revision = state.revision
                    downstream.updated_at = timestamp
                    downstream.invalidation_reason = state.last_invalidation_reason
                    for prior_approval in state.approvals:
                        if (
                            prior_approval.status == "active"
                            and prior_approval.phase == downstream_phase
                        ):
                            prior_approval.status = "invalidated"
                            prior_approval.invalidated_at = timestamp
                            prior_approval.invalidation_reason = state.last_invalidation_reason
            return approval

    def record_distillation_approval_atomic(
        self,
        *,
        business_id: str,
        owner_id: str,
        phase: DistillationPhase,
        decision: str,
        artifact_id: str,
        artifact_fingerprint: str,
        note: str = "",
        actor: str,
        expected_revision: int | None = None,
    ) -> DistillationApproval:
        """Record a direct API approval against the latest persisted record."""

        with self._lock:
            record = self.require(business_id, owner_id)
            # A reviewable non-role phase always has a platform-authored,
            # signed confirmation action.  Materialize it from the canonical
            # artifact before checking persisted questions so a direct POST
            # cannot race or bypass a GET that has not yet saved the action.
            prior_question = next(
                (
                    item for item in record.context.questions
                    if item.get("source") == "distillation_approval"
                    and item.get("status", "open") == "open"
                ),
                None,
            )
            known_sessions = {item.id for item in record.chat_sessions}
            binding_session_id = (
                str(prior_question.get("session_id") or "")
                if prior_question is not None
                else ""
            )
            if binding_session_id not in known_sessions:
                binding_session_id = ""
            known_runs = {item.id: item for item in record.runs}
            binding_run_id = (
                str(prior_question.get("run_id") or "")
                if prior_question is not None
                else ""
            )
            binding_run = known_runs.get(binding_run_id)
            if (
                binding_run is None
                or binding_run.status != "waiting_for_user"
                or (binding_session_id and binding_run.session_id != binding_session_id)
            ):
                binding_run_id = ""
            binding_checkpoint_id = (
                str(prior_question.get("checkpoint_run_id") or "")
                if prior_question is not None
                else ""
            )
            binding_checkpoint = known_runs.get(binding_checkpoint_id)
            if (
                binding_checkpoint is None
                or (binding_session_id and binding_checkpoint.session_id != binding_session_id)
            ):
                binding_checkpoint_id = binding_run_id
            binding_tool_call_id = (
                str(prior_question.get("tool_call_id") or "")
                if prior_question is not None
                else ""
            )
            question, question_changed = self.ensure_distillation_approval_question(
                record,
                session_id=binding_session_id or None,
                run_id=binding_run_id,
                checkpoint_run_id=binding_checkpoint_id,
                tool_call_id=binding_tool_call_id,
            )
            if question_changed:
                self.save(record)
            if question is not None:
                raise ValueError(
                    "A signed approval dialog is required for this artifact. "
                    "Resolve that action through the confirmations endpoint."
                )
            open_actions = [
                question for question in record.context.questions
                if question.get("source") == "distillation_approval"
                and question.get("status", "open") == "open"
            ]
            if open_actions:
                raise ValueError(
                    "A signed approval dialog is already open. Resolve that action through the confirmations endpoint."
                )
            approval = self.record_distillation_approval(
                record,
                phase=phase,
                decision=decision,
                artifact_id=artifact_id,
                artifact_fingerprint=artifact_fingerprint,
                note=note,
                actor=actor,
                expected_revision=expected_revision,
            )
            self.save(record)
            return approval

    def set_trace_anchor_selector(
        self,
        record: BusinessRecord,
        *,
        file: str,
        table: str,
        row_number: int,
        actor: str,
        expected_revision: int | None = None,
    ) -> TraceAnchorSelector:
        """Persist a reviewer-selected result row for the next trace attempt."""

        with self._lock:
            _assert_distillation_actor(record, actor)
            state = record.distillation
            _ensure_distillation_stages(state)
            _assert_expected_distillation_revision(state.revision, expected_revision)
            if not _has_current_file_roles_approval(state):
                raise ValueError(
                    "A current source-snapshot file-role approval is required before selecting a trace anchor."
                )
            normalized_file = file.strip()
            normalized_table = table.strip()
            if not normalized_file or not normalized_table or row_number <= 0:
                raise ValueError("Anchor selector requires file, table, and a positive row number.")
            manifest_path = self.require_approved_role_manifest(record)
            manifest = _load_json_artifact(manifest_path, ROLE_MANIFEST_RELATIVE)
            normalized_endpoint_file = normalized_file.replace("\\", "/").strip("/")
            matching_sources = [
                item
                for item in manifest.get("sources", [])
                if isinstance(item, dict)
                and normalized_endpoint_file in {
                    str(value).replace("\\", "/").strip("/")
                    for value in [item.get("file", ""), *item.get("aliases", [])]
                    if str(value).strip()
                }
            ]
            if len(matching_sources) != 1:
                raise ValueError(
                    "Anchor selector file is ambiguous or absent in the canonical role manifest."
                )
            source_id = str(matching_sources[0].get("file_id", ""))
            matching_roles = [
                item
                for item in manifest.get("roles", [])
                if isinstance(item, dict)
                and str(item.get("file_id", "")) == source_id
                and str(item.get("table", "")).strip() in {normalized_table, "__file__"}
            ]
            if not matching_roles:
                raise ValueError(
                    "Anchor selector table has no current approved role in the canonical role manifest."
                )
            explicit_roles = [
                item for item in matching_roles
                if str(item.get("table", "")).strip() == normalized_table
            ]
            fallback_roles = [
                item for item in matching_roles
                if str(item.get("table", "")).strip() == "__file__"
            ]
            applicable_roles = explicit_roles or fallback_roles
            if len(applicable_roles) != 1:
                raise ValueError("Anchor selector table has ambiguous role assignments in the canonical role manifest.")
            assigned_role = str(applicable_roles[0].get("role", ""))
            if assigned_role != "result":
                raise ValueError(
                    f"Anchor selector table is approved as {assigned_role or 'unassigned'}; only a current result table may be selected."
                )

            self.invalidate_distillation(
                record,
                f"Trace anchor selected: {normalized_file}/{normalized_table} row {row_number}",
                actor=actor,
                source_changed=False,
                from_phase="data_lineage",
            )
            selector = TraceAnchorSelector(
                file=normalized_file,
                table=normalized_table,
                row_number=row_number,
                selected_by=actor,
                selected_at=now(),
                source_revision=state.source_revision,
                revision=state.revision,
            )
            state.anchor_selector = selector
            return selector

    def apply_trace_review_corrections(
        self,
        record: BusinessRecord,
        *,
        corrections: list[dict[str, Any]],
        replace_existing: bool = False,
        note: str = "",
        actor: str,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        """Persist authenticated key-pair corrections for one canonical trace.

        The API supplies structured table/field names only.  This method
        validates them against the immutable field-evidence snapshot and the
        current reviewed trace, writes an auditable ``revision_required``
        review, and invalidates every downstream phase.  It intentionally
        preserves the already selected historical result anchor so the next
        deterministic run explains the same business instance.
        """

        with self._lock:
            _assert_distillation_actor(record, actor)
            state = record.distillation
            _ensure_distillation_stages(state)
            _assert_expected_distillation_revision(state.revision, expected_revision)
            self.require_approved_role_manifest(record)
            workspace = self.workspace_dir(record.id).resolve()
            review_path = workspace / TRACE_REVIEW_RELATIVE
            review = _load_json_artifact(review_path, TRACE_REVIEW_RELATIVE)
            if review.get("kind") != "trace_review":
                raise ValueError("Key-pair correction requires the canonical trace_review artifact.")
            if str(review.get("status", "")) not in {
                "pending_review",
                "revision_required",
                "approved",
            }:
                raise ValueError("The canonical trace review is not in a correctable state.")
            trace_fingerprint = self._validate_trace_review_payload(record, workspace, review)
            trace_path = workspace / TRACE_SAMPLES_RELATIVE
            trace = _load_json_artifact(trace_path, TRACE_SAMPLES_RELATIVE)
            bundle_id = str(
                review.get("trace", {}).get("bundle_id", "")
                if isinstance(review.get("trace"), dict)
                else ""
            ).strip()
            normalized = self._normalize_trace_review_corrections(
                corrections,
                source_revision=state.source_revision,
            )
            self._validate_trace_review_corrections_against_evidence(
                workspace,
                record,
                trace,
                bundle_id,
                normalized,
            )

            existing = self._active_trace_review_corrections(review)
            if existing:
                # A new correction can be made after a prior corrected trace
                # has been reviewed.  Those prior corrections live in
                # ``applied_corrections`` on the fresh review, not in the
                # template's empty ``corrections`` list.  Revalidate them
                # before carrying them forward so a manually edited review
                # can never become an input to the next trace.
                self._validate_trace_review_corrections_for_retrace(
                    workspace,
                    record,
                    review,
                    existing,
                )
            existing_signatures = {_trace_correction_signature(item) for item in existing}
            incoming_signatures = {_trace_correction_signature(item) for item in normalized}
            if len(incoming_signatures) != len(normalized):
                raise ValueError("The submitted trace corrections contain duplicate key-pair relationships.")
            if existing_signatures & incoming_signatures:
                raise ValueError("This key-pair correction is already active in the canonical trace review.")

            # Validate the server signing capability before mutating the
            # revision.  A missing HMAC configuration must reject the request
            # atomically, not leave the scenario invalidated without a usable
            # correction review.
            signature_key = _platform_approval_hmac_key()
            prior_anchor = state.anchor_selector
            self.invalidate_distillation(
                record,
                "User-confirmed trace key-pair correction",
                actor=actor,
                source_changed=False,
                from_phase="data_lineage",
            )
            state = record.distillation
            if prior_anchor is not None and prior_anchor.source_revision == state.source_revision:
                # A key-pair correction changes link traversal, not which
                # historical outcome the user asked the platform to explain.
                state.anchor_selector = prior_anchor.model_copy(update={"revision": state.revision})

            correction_time = utc_now()
            for item in normalized:
                item.update({
                    "id": new_id("trace_correction"),
                    "confirmed_by": actor,
                    "confirmed_at": correction_time,
                    "source_revision": state.source_revision,
                    "distillation_revision": state.revision,
                })
                item["signature"] = _platform_approval_signature(item, signature_key)
            incoming_endpoints = {_trace_correction_endpoint(item) for item in normalized}
            if replace_existing:
                retained: list[dict[str, Any]] = []
                superseded = [dict(item) for item in existing]
            else:
                retained = [
                    dict(item)
                    for item in existing
                    if _trace_correction_endpoint(item) not in incoming_endpoints
                ]
                superseded = [
                    dict(item)
                    for item in existing
                    if _trace_correction_endpoint(item) in incoming_endpoints
                ]
            history = review.get("correction_history") if isinstance(review.get("correction_history"), list) else []
            history = [dict(item) for item in history if isinstance(item, dict)]
            for item in superseded:
                item.update({
                    "status": "superseded",
                    "superseded_at": correction_time,
                    "superseded_by": [entry["id"] for entry in normalized],
                })
                history.append(item)

            review["corrections"] = [*retained, *normalized]
            # ``applied_corrections`` is a read-only audit surface on a fresh
            # review.  Once the reviewer asks for another retrace, the active
            # corrections above are the only source of truth for that run.
            review["applied_corrections"] = []
            review["correction_history"] = history
            review["status"] = "revision_required"
            review["approval"] = {
                "decision": "revision_required",
                "reviewer": f"platform:{actor}",
                "note": note.strip() or "User-confirmed key-pair correction requires deterministic retracing.",
                "accepted_warnings": [],
                "reviewed_at": correction_time,
            }
            _atomic_json_write(review_path, review)
            return {
                "status": "revision_required",
                "correction_ids": [str(item["id"]) for item in normalized],
                "correction_count": len(review["corrections"]),
                "trace_fingerprint": trace_fingerprint,
                "review_fingerprint": _sha256_file(review_path),
                "revision": state.revision,
            }

    def archive_trace_review_for_retrace(
        self,
        record: BusinessRecord,
    ) -> dict[str, Any] | None:
        """Snapshot the trusted correction review before the Skill rewrites it.

        ``analyze --trace-review`` intentionally creates a fresh pending
        review for the new trace.  Without this archive, that template would
        erase the correction evidence that explains *why* the new trace was
        produced.  The caller receives only server-derived paths and IDs; no
        client-controlled path reaches the sandbox command.
        """

        with self._lock:
            review_path = self.require_trace_review_corrections_for_retrace(record)
            if review_path is None:
                return None
            workspace = self.workspace_dir(record.id).resolve()
            review = _load_json_artifact(review_path, TRACE_REVIEW_RELATIVE)
            trace_path = workspace / TRACE_SAMPLES_RELATIVE
            trace = _load_json_artifact(trace_path, TRACE_SAMPLES_RELATIVE)
            corrections = self._active_trace_review_corrections(review)
            # ``require_*`` has already checked these, but keep the archive
            # operation independently fail-closed if this code is reused.
            self._validate_trace_review_corrections_for_retrace(
                workspace,
                record,
                review,
                corrections,
            )
            archive_id = new_id("trace_review_retrace")
            archive_root = workspace / TRACE_REVIEW_HISTORY_RELATIVE
            archive_root.mkdir(parents=True, exist_ok=True)
            review_archive = archive_root / f"{archive_id}.review.json"
            trace_archive = archive_root / f"{archive_id}.trace.json"
            archived_at = utc_now()
            _atomic_json_write(
                review_archive,
                {
                    **review,
                    "archive": {
                        "id": archive_id,
                        "archived_at": archived_at,
                        "reason": "deterministic_retrace_with_user_confirmed_key_pairs",
                    },
                },
            )
            _atomic_json_write(trace_archive, trace)
            return {
                "id": archive_id,
                "review_archive": f"{TRACE_REVIEW_HISTORY_RELATIVE}/{review_archive.name}",
                "trace_archive": f"{TRACE_REVIEW_HISTORY_RELATIVE}/{trace_archive.name}",
                "review_fingerprint": _sha256_file(review_path),
                "trace_fingerprint": _sha256_file(trace_path),
                "corrections": [dict(item) for item in corrections],
                "correction_ids": [str(item.get("id", "")) for item in corrections],
                "source_revision": record.distillation.source_revision,
                "revision": record.distillation.revision,
                "archived_at": archived_at,
            }

    def finalize_trace_review_retrace(
        self,
        record: BusinessRecord,
        retrace: dict[str, Any],
    ) -> dict[str, Any]:
        """Prove corrections participated, then attach their audit trail.

        A non-empty correction review is not proof that the tracer used it:
        an invalid or irrelevant override could otherwise be silently skipped.
        This method requires every stored correction ID to appear in an actual
        result-anchored trace link before the fresh review becomes reviewable.
        """

        with self._lock:
            workspace = self.workspace_dir(record.id).resolve()
            state = record.distillation
            if (
                int(retrace.get("source_revision", -1)) != state.source_revision
                or int(retrace.get("revision", -1)) != state.revision
            ):
                raise ValueError("Trace correction state changed while deterministic retracing was running.")
            corrections = retrace.get("corrections")
            if not isinstance(corrections, list) or not corrections:
                raise ValueError("Deterministic retracing has no trusted correction set to finalize.")
            review_path = workspace / TRACE_REVIEW_RELATIVE
            review = _load_json_artifact(review_path, TRACE_REVIEW_RELATIVE)
            trace = _load_json_artifact(workspace / TRACE_SAMPLES_RELATIVE, TRACE_SAMPLES_RELATIVE)
            self._validate_trace_correction_application(trace, corrections)
            if str(review.get("status", "")) != "pending_review":
                raise ValueError("Corrected tracing did not produce a fresh pending trace review.")
            if review.get("corrections") not in (None, []):
                raise ValueError("Corrected tracing did not reset the prior correction review before re-review.")
            self._validate_trace_review_payload(record, workspace, review)
            review["applied_corrections"] = [dict(item) for item in corrections]
            review["retrace_of"] = {
                "archive_id": str(retrace.get("id", "")),
                "review_archive": str(retrace.get("review_archive", "")),
                "trace_archive": str(retrace.get("trace_archive", "")),
                "review_fingerprint": str(retrace.get("review_fingerprint", "")),
                "trace_fingerprint": str(retrace.get("trace_fingerprint", "")),
                "correction_ids": [str(item.get("id", "")) for item in corrections],
                "source_revision": state.source_revision,
                "distillation_revision": state.revision,
                "applied_at": utc_now(),
            }
            _atomic_json_write(review_path, review)
            return {
                "status": "applied",
                "correction_ids": [str(item.get("id", "")) for item in corrections],
                "review_fingerprint": _sha256_file(review_path),
                "review_archive": str(retrace.get("review_archive", "")),
            }

    def restore_trace_review_after_failed_retrace(
        self,
        record: BusinessRecord,
        retrace: dict[str, Any],
        *,
        reason: str,
    ) -> bool:
        """Restore the correction gate after an unsuccessful corrected run.

        We deliberately leave concurrent user changes untouched.  When the
        identity still matches, restoring the old trace/review makes the
        correction retryable instead of silently discarding it under the
        Skill's newly generated empty review template.
        """

        with self._lock:
            state = record.distillation
            if (
                int(retrace.get("source_revision", -1)) != state.source_revision
                or int(retrace.get("revision", -1)) != state.revision
            ):
                return False
            workspace = self.workspace_dir(record.id).resolve()
            try:
                review_archive = self._retrace_archive_path(
                    workspace,
                    str(retrace.get("review_archive", "")),
                )
                trace_archive = self._retrace_archive_path(
                    workspace,
                    str(retrace.get("trace_archive", "")),
                )
                review = _load_json_artifact(review_archive, "trace correction review archive")
                trace = _load_json_artifact(trace_archive, "trace correction trace archive")
            except ValueError:
                return False
            review.pop("archive", None)
            failure_note = str(reason or "").replace("\x00", " ").strip()[:4000]
            review["status"] = "revision_required"
            review["approval"] = {
                "decision": "revision_required",
                "reviewer": "platform:retrace-recovery",
                "note": failure_note or "Corrected deterministic trace did not complete.",
                "accepted_warnings": [],
                "reviewed_at": utc_now(),
            }
            _atomic_json_write(workspace / TRACE_SAMPLES_RELATIVE, trace)
            _atomic_json_write(workspace / TRACE_REVIEW_RELATIVE, review)
            return True

    def require_trace_review_corrections_for_retrace(
        self,
        record: BusinessRecord,
    ) -> Path | None:
        """Return the fixed review path only for a current trusted correction.

        A stale or manually written revision-required review must never be
        silently omitted: doing so would rerun the trace while discarding a
        user correction.  A normal pending review is simply not a correction
        and therefore returns ``None``.
        """

        with self._lock:
            workspace = self.workspace_dir(record.id).resolve()
            review_path = workspace / TRACE_REVIEW_RELATIVE
            if not review_path.is_file():
                return None
            review = _load_json_artifact(review_path, TRACE_REVIEW_RELATIVE)
            status = str(review.get("status", ""))
            if review.get("kind") != "trace_review":
                if status == "selection_required":
                    return None
                raise ValueError("The canonical trace review has an unexpected contract kind.")
            if status != "revision_required":
                return None
            self._validate_trace_review_payload(record, workspace, review)
            corrections = review.get("corrections") if isinstance(review.get("corrections"), list) else []
            if not corrections:
                raise ValueError(
                    "The trace review requires revision but has no user-confirmed key-pair corrections to retrace."
                )
            self._validate_trace_review_corrections_for_retrace(
                workspace,
                record,
                review,
                corrections,
            )
            return review_path

    def _active_trace_review_corrections(self, review: dict[str, Any]) -> list[dict[str, Any]]:
        """Return pending corrections, or durable applied corrections on a new review."""

        raw = review.get("corrections")
        if raw is None:
            raw = []
        if not isinstance(raw, list):
            raise ValueError("The current trace review contains an invalid corrections collection.")
        if not raw:
            raw = review.get("applied_corrections") or []
        if not isinstance(raw, list):
            raise ValueError("The current trace review contains an invalid applied corrections collection.")
        entries: list[dict[str, Any]] = []
        for item in raw:
            if not isinstance(item, dict):
                raise ValueError("The current trace review contains an invalid correction entry.")
            entries.append(dict(item))
        return entries

    def _normalize_trace_review_corrections(
        self,
        corrections: list[dict[str, Any]],
        *,
        source_revision: int,
    ) -> list[dict[str, Any]]:
        if not isinstance(corrections, list) or not corrections:
            raise ValueError("At least one structured trace key-pair correction is required.")
        normalized: list[dict[str, Any]] = []
        endpoints: set[tuple[str, str, str, str]] = set()
        for index, raw in enumerate(corrections, 1):
            if not isinstance(raw, dict):
                raise ValueError(f"Trace correction {index} must be an object.")
            source_file = _normalize_trace_correction_path(raw.get("source_file"), "source_file")
            target_file = _normalize_trace_correction_path(raw.get("target_file"), "target_file")
            source_table = _normalize_trace_correction_text(raw.get("source_table"), "source_table", 240)
            target_table = _normalize_trace_correction_text(raw.get("target_table"), "target_table", 240)
            reason = _normalize_trace_correction_text(raw.get("reason"), "reason", 4000)
            raw_pairs = raw.get("key_pairs")
            if not isinstance(raw_pairs, list) or not raw_pairs or len(raw_pairs) > 16:
                raise ValueError(f"Trace correction {index} requires 1-16 key pairs.")
            pairs: list[dict[str, str]] = []
            pair_signatures: set[tuple[str, str]] = set()
            for pair in raw_pairs:
                if not isinstance(pair, dict):
                    raise ValueError(f"Trace correction {index} has an invalid key pair.")
                source_field = _normalize_trace_correction_text(pair.get("source_field"), "source_field", 240)
                target_field = _normalize_trace_correction_text(pair.get("target_field"), "target_field", 240)
                signature = (source_field, target_field)
                if signature in pair_signatures:
                    raise ValueError(f"Trace correction {index} repeats a key pair.")
                pair_signatures.add(signature)
                pairs.append({"source_field": source_field, "target_field": target_field})
            endpoint = (source_file, source_table, target_file, target_table)
            if endpoint in endpoints:
                raise ValueError(
                    "Submit one composite correction per source/target table pair instead of duplicate endpoints."
                )
            endpoints.add(endpoint)
            normalized.append({
                "source_file": source_file,
                "source_table": source_table,
                "target_file": target_file,
                "target_table": target_table,
                "key_pairs": pairs,
                "reason": reason,
                "source_revision": source_revision,
            })
        return normalized

    def _validate_trace_review_corrections_against_evidence(
        self,
        workspace: Path,
        record: BusinessRecord,
        trace: dict[str, Any],
        bundle_id: str,
        corrections: list[dict[str, Any]],
    ) -> None:
        bundle = next(
            (
                item for item in trace.get("bundles", [])
                if isinstance(item, dict) and str(item.get("bundle_id", "")) == bundle_id
            ),
            None,
        )
        if bundle is None:
            raise ValueError("Trace correction cannot find the reviewed trace bundle.")
        # The original sample is evidence, not a closed world. A reviewer may
        # need to add an approved table the heuristic failed to reach; that is
        # why the correction loop exists. Authorize endpoints from the signed
        # role manifest + field evidence, then require the resulting retrace
        # to materialize every correction in the selected result chain.
        manifest_path = self.require_approved_role_manifest(record)
        manifest = _load_json_artifact(manifest_path, ROLE_MANIFEST_RELATIVE)
        sources = manifest.get("sources") if isinstance(manifest.get("sources"), list) else []
        roles = manifest.get("roles") if isinstance(manifest.get("roles"), list) else []
        source_ids_by_alias: dict[str, set[str]] = {}
        roles_by_source: dict[str, list[dict[str, Any]]] = {}
        for source in sources:
            if not isinstance(source, dict):
                continue
            file_id = str(source.get("file_id", "")).strip()
            if not file_id:
                continue
            aliases = {
                _normalize_trace_correction_path(value, "approved source path")
                for value in [
                    source.get("file", ""),
                    *(source.get("aliases") if isinstance(source.get("aliases"), list) else []),
                ]
                if str(value).strip()
            }
            for alias in aliases:
                source_ids_by_alias.setdefault(alias, set()).add(file_id)
        for role in roles:
            if not isinstance(role, dict):
                continue
            file_id = str(role.get("file_id", "")).strip()
            if file_id:
                roles_by_source.setdefault(file_id, []).append(role)
        columns = self._field_evidence_columns(workspace)
        for correction in corrections:
            source_endpoint = (correction["source_file"], correction["source_table"])
            target_endpoint = (correction["target_file"], correction["target_table"])
            def is_approved(endpoint: tuple[str, str]) -> bool:
                path, table = endpoint
                source_ids = source_ids_by_alias.get(path, set())
                if len(source_ids) != 1:
                    return False
                source_roles = roles_by_source.get(next(iter(source_ids)), [])
                explicit = [
                    item for item in source_roles
                    if str(item.get("table", "")).strip() == table
                ]
                applicable = explicit or [
                    item for item in source_roles
                    if str(item.get("table", "")).strip() == "__file__"
                ]
                return (
                    len(applicable) == 1
                    and str(applicable[0].get("role", "")).strip() != "ignore"
                )
            if not is_approved(source_endpoint) or not is_approved(target_endpoint):
                raise ValueError(
                    "Trace correction endpoints must be current non-ignored tables in the signed role manifest."
                )
            source_columns = columns.get(source_endpoint)
            target_columns = columns.get(target_endpoint)
            if source_columns is None or target_columns is None:
                raise ValueError("Trace correction endpoint is absent from current field evidence.")
            for pair in correction["key_pairs"]:
                if pair["source_field"] not in source_columns:
                    raise ValueError(
                        f"Trace correction source field is absent from current field evidence: {pair['source_field']}."
                    )
                if pair["target_field"] not in target_columns:
                    raise ValueError(
                        f"Trace correction target field is absent from current field evidence: {pair['target_field']}."
                    )

    def _field_evidence_columns(self, workspace: Path) -> dict[tuple[str, str], set[str]]:
        field_evidence = _load_json_artifact(
            workspace / FIELD_EVIDENCE_RELATIVE,
            FIELD_EVIDENCE_RELATIVE,
        )
        columns: dict[tuple[str, str], set[str]] = {}
        for file_info in field_evidence.get("files", []):
            if not isinstance(file_info, dict):
                continue
            path = _normalize_trace_correction_path(file_info.get("path"), "field evidence path")
            for table in file_info.get("tables", []):
                if not isinstance(table, dict):
                    continue
                table_name = _normalize_trace_correction_text(
                    table.get("table_name"), "field evidence table", 240,
                )
                raw_columns = table.get("columns") if isinstance(table.get("columns"), list) else []
                names = {
                    str(item.get("name", "")).strip()
                    for item in raw_columns
                    if isinstance(item, dict) and str(item.get("name", "")).strip()
                }
                if not names:
                    raise ValueError(f"Field evidence table has no usable columns: {path}/{table_name}.")
                endpoint = (path, table_name)
                if endpoint in columns:
                    raise ValueError(f"Field evidence has duplicate table endpoint: {path}/{table_name}.")
                columns[endpoint] = names
        if not columns:
            raise ValueError("Current field evidence has no tabular schema for trace correction validation.")
        return columns

    def _validate_trace_review_corrections_for_retrace(
        self,
        workspace: Path,
        record: BusinessRecord,
        review: dict[str, Any],
        corrections: list[Any],
    ) -> None:
        trace_path = workspace / TRACE_SAMPLES_RELATIVE
        trace = _load_json_artifact(trace_path, TRACE_SAMPLES_RELATIVE)
        bundle_id = str(
            review.get("trace", {}).get("bundle_id", "")
            if isinstance(review.get("trace"), dict)
            else ""
        ).strip()
        checked: list[dict[str, Any]] = []
        signature_key = _platform_approval_hmac_key()
        for item in corrections:
            if not isinstance(item, dict):
                raise ValueError("The current trace review contains an invalid correction entry.")
            correction_id = str(item.get("id", "")).strip()
            if (
                not correction_id.startswith("trace_correction_")
                or not str(item.get("confirmed_by", "")).strip()
                or not str(item.get("confirmed_at", "")).strip()
            ):
                raise ValueError(
                    "Revision-required trace corrections must be recorded by the authenticated platform service."
                )
            try:
                correction_source_revision = int(item.get("source_revision"))
                correction_distillation_revision = int(item.get("distillation_revision"))
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    "Revision-required trace corrections have incomplete revision provenance."
                ) from exc
            if correction_source_revision != record.distillation.source_revision:
                raise ValueError(
                    "Revision-required trace corrections belong to a different source snapshot."
                )
            if correction_distillation_revision <= 0:
                raise ValueError(
                    "Revision-required trace corrections have invalid distillation revision provenance."
                )
            signature = str(item.get("signature", "")).strip()
            expected_signature = _platform_approval_signature(item, signature_key)
            if not signature or not hmac.compare_digest(signature, expected_signature):
                raise ValueError(
                    "Revision-required trace corrections do not have a valid platform signature."
                )
            checked.append({
                "source_file": item.get("source_file"),
                "source_table": item.get("source_table"),
                "target_file": item.get("target_file"),
                "target_table": item.get("target_table"),
                "key_pairs": item.get("key_pairs"),
                "reason": item.get("reason"),
            })
        normalized_checked = self._normalize_trace_review_corrections(
            checked,
            source_revision=0,
        )
        self._validate_trace_review_corrections_against_evidence(
            workspace,
            record,
            trace,
            bundle_id,
            normalized_checked,
        )

    def _validate_trace_correction_application(
        self,
        trace: dict[str, Any],
        corrections: list[Any],
    ) -> None:
        """Require every correction to be visible in a materialized trace link."""

        try:
            override_count = int(trace.get("reviewed_relation_override_count", -1))
        except (TypeError, ValueError):
            override_count = -1
        if override_count != len(corrections):
            raise ValueError(
                "Corrected tracing did not accept every user-confirmed key-pair override."
            )
        expected_relation_ids = {
            f"review:{str(item.get('id', '')).strip()}"
            for item in corrections
            if isinstance(item, dict) and str(item.get("id", "")).strip()
        }
        if len(expected_relation_ids) != len(corrections):
            raise ValueError("Corrected tracing has an invalid correction audit identifier.")
        applied_relation_ids: set[str] = set()
        bundles = trace.get("bundles") if isinstance(trace.get("bundles"), list) else []
        for bundle in bundles:
            if not isinstance(bundle, dict):
                continue
            links = bundle.get("links") if isinstance(bundle.get("links"), list) else []
            for link in links:
                if not isinstance(link, dict):
                    continue
                relation_ids = link.get("relation_ids") if isinstance(link.get("relation_ids"), list) else []
                applied_relation_ids.update(str(item) for item in relation_ids)
        missing = sorted(expected_relation_ids - applied_relation_ids)
        if missing:
            raise ValueError(
                "Corrected tracing did not materialize every user-confirmed relationship in the selected result chain: "
                + ", ".join(missing)
            )

    def _retrace_archive_path(self, workspace: Path, relative: str) -> Path:
        normalized = relative.replace("\\", "/").strip("/")
        prefix = f"{TRACE_REVIEW_HISTORY_RELATIVE}/"
        if not normalized.startswith(prefix):
            raise ValueError("Trace retrace archive path is invalid.")
        parts = PurePosixPath(normalized).parts
        if not parts or ".." in parts:
            raise ValueError("Trace retrace archive path is invalid.")
        path = (workspace / Path(*parts)).resolve()
        history_root = (workspace / TRACE_REVIEW_HISTORY_RELATIVE).resolve()
        if history_root not in path.parents or not path.is_file():
            raise ValueError("Trace retrace archive is unavailable.")
        return path

    def _validate_distillation_artifact(
        self,
        record: BusinessRecord,
        phase: DistillationPhase,
        artifact_id: str,
    ) -> tuple[str, str, str]:
        """Resolve and fingerprint the canonical workbench artifact for a phase.

        The client may identify an artifact, but it cannot choose an arbitrary
        file or claim its digest.  This creates a durable link from the human
        approval to the exact bytes in this scenario's workspace.
        """

        requirement = _PHASE_ARTIFACTS.get(phase)
        if requirement is None:
            raise ValueError(f"No workspace artifact contract is defined for phase {phase}.")
        expected_path, allowed_statuses = requirement
        normalized = artifact_id.replace("\\", "/").strip("/")
        if normalized != expected_path:
            raise ValueError(
                f"Phase {phase} must review the canonical artifact {expected_path}."
            )
        workspace, artifact = self._workspace_artifact(record, normalized, expected_path)
        artifact_fingerprint = _sha256_file(artifact)
        if allowed_statuses is None:
            return normalized, artifact_fingerprint, ""
        payload = _load_json_artifact(artifact, expected_path)
        status = str(payload.get("status", ""))
        if status not in allowed_statuses:
            allowed = ", ".join(sorted(allowed_statuses))
            raise ValueError(
                f"Phase artifact {expected_path} has status {status or '<missing>'}; expected {allowed}."
            )
        if phase == "data_lineage":
            trace_fingerprint = self._validate_trace_review_payload(record, workspace, payload)
            return normalized, artifact_fingerprint, trace_fingerprint
        if phase == "micro_process":
            trace_fingerprint = self._validate_micro_process_payload(workspace, payload)
            return normalized, artifact_fingerprint, trace_fingerprint
        return normalized, artifact_fingerprint, ""

    def _workspace_artifact(
        self,
        record: BusinessRecord,
        normalized: str,
        expected_path: str,
    ) -> tuple[Path, Path]:
        workspace = self.workspace_dir(record.id).resolve()
        try:
            artifact = (workspace / normalized).resolve()
        except (OSError, ValueError) as exc:
            raise ValueError("Invalid distillation artifact path.") from exc
        if workspace not in artifact.parents or not artifact.is_file():
            raise ValueError(f"Required phase artifact is missing: {expected_path}.")
        return workspace, artifact

    def _validate_trace_review_payload(
        self,
        record: BusinessRecord,
        workspace: Path,
        review: dict[str, Any],
    ) -> str:
        if review.get("kind") != "trace_review":
            raise ValueError("Data-lineage artifact is not a trace-review contract.")
        trace_path = workspace / "outputs" / "data-relations" / "trace-samples.json"
        if not trace_path.is_file():
            raise ValueError("Data-lineage review has no trace-samples.json to verify.")
        trace = _load_json_artifact(trace_path, "outputs/data-relations/trace-samples.json")
        trace_fingerprint = _sha256_file(trace_path)
        reference = review.get("trace") if isinstance(review.get("trace"), dict) else {}
        if reference.get("fingerprint") != trace_fingerprint:
            raise ValueError("Trace-review is stale: it does not match the current trace-samples artifact.")
        bundle_id = str(reference.get("bundle_id", "")).strip()
        bundles = trace.get("bundles") if isinstance(trace.get("bundles"), list) else []
        if trace.get("status") != "complete" or not bundle_id or not any(
            isinstance(item, dict) and str(item.get("bundle_id", "")) == bundle_id
            for item in bundles
        ):
            raise ValueError(
                "Data-lineage approval requires a current trace with at least one reviewable anchored bundle."
            )
        self._validate_trace_role_authority(record, trace, bundle_id)
        return trace_fingerprint

    def _validate_trace_role_authority(
        self,
        record: BusinessRecord,
        trace: dict[str, Any],
        bundle_id: str,
    ) -> None:
        """Bind a reviewed trace to the current signed result authority.

        The normal runtime injects the canonical role manifest before it runs
        tracing.  This independent API-side check closes the other path: a
        caller must not be able to upload a hand-written trace/review pair and
        obtain a platform approval merely because its JSON shape looks valid.
        """

        manifest_path = self.require_approved_role_manifest(record)
        manifest = _load_json_artifact(manifest_path, ROLE_MANIFEST_RELATIVE)
        trace_manifest = trace.get("role_manifest") if isinstance(trace.get("role_manifest"), dict) else {}
        expected = {
            "artifact_fingerprint": _sha256_file(manifest_path),
            "fingerprint": str(manifest.get("role_manifest_fingerprint", "")),
            "source_revision": record.distillation.source_revision,
            "source_fingerprint": _source_snapshot_fingerprint(record),
        }
        if any(trace_manifest.get(key) != value for key, value in expected.items()):
            raise ValueError(
                "Trace is not bound to the current platform-approved role manifest and source snapshot."
            )

        bundle = next(
            (
                item for item in trace.get("bundles", [])
                if isinstance(item, dict) and str(item.get("bundle_id", "")) == bundle_id
            ),
            None,
        )
        anchor = bundle.get("anchor") if isinstance(bundle, dict) and isinstance(bundle.get("anchor"), dict) else {}
        anchor_file = str(anchor.get("path", "")).replace("\\", "/").strip("/")
        anchor_table = str(anchor.get("table", "")).strip()
        sources = manifest.get("sources") if isinstance(manifest.get("sources"), list) else []
        roles = manifest.get("roles") if isinstance(manifest.get("roles"), list) else []

        def source_aliases(source: dict[str, Any]) -> set[str]:
            values = [
                source.get("file", ""),
                *(source.get("aliases") if isinstance(source.get("aliases"), list) else []),
            ]
            return {
                str(value).replace("\\", "/").strip("/")
                for value in values
                if str(value).strip()
            }

        def matching_sources(path: str) -> list[dict[str, Any]]:
            normalized = str(path).replace("\\", "/").strip("/")
            return [
                item for item in sources
                if isinstance(item, dict) and normalized in source_aliases(item)
            ]

        def applicable_roles(source: dict[str, Any], table: str) -> list[dict[str, Any]]:
            source_id = str(source.get("file_id", ""))
            source_roles = [
                item for item in roles
                if isinstance(item, dict) and str(item.get("file_id", "")) == source_id
            ]
            if not table:
                return [item for item in source_roles if str(item.get("table", "")).strip() == "__file__"]
            explicit = [item for item in source_roles if str(item.get("table", "")).strip() == table]
            return explicit or [
                item for item in source_roles if str(item.get("table", "")).strip() == "__file__"
            ]

        if not anchor_file:
            raise ValueError("Trace review has no concrete result anchor.")
        matched_sources = matching_sources(anchor_file)
        if len(matched_sources) != 1:
            raise ValueError("Trace anchor is absent from or ambiguous in the approved role manifest.")
        anchor_source = matched_sources[0]
        anchor_kind = str(anchor.get("kind", "") or "table_row").strip()

        if anchor_kind == "document_segment":
            locator = str(anchor.get("locator", "")).strip()
            source_digest = str(anchor.get("source_digest", "")).strip().lower()
            segment_digest = str(anchor.get("segment_digest", "")).strip().lower()
            anchor_roles = applicable_roles(anchor_source, "")
            if (
                anchor_table
                or not _SAFE_DOCUMENT_LOCATOR.fullmatch(locator)
                or not _SHA256_PATTERN.fullmatch(source_digest)
                or not _SHA256_PATTERN.fullmatch(segment_digest)
                or source_digest != str(anchor_source.get("sha256", "")).lower()
            ):
                raise ValueError(
                    "Document result anchor is not bound to a safe locator and the approved source digest."
                )
            if len(anchor_roles) != 1 or str(anchor_roles[0].get("role", "")) != "result":
                raise ValueError(
                    "Trace anchor is not a current platform-approved result document."
                )
            if record.distillation.anchor_selector is not None:
                raise ValueError(
                    "A table-row selector cannot authorize a document-segment result anchor."
                )

            coverage = bundle.get("coverage") if isinstance(bundle, dict) and isinstance(bundle.get("coverage"), dict) else {}
            try:
                exact_link_count = int(coverage.get("exact_link_count", 0) or 0)
            except (TypeError, ValueError):
                exact_link_count = 0
            links = bundle.get("links") if isinstance(bundle, dict) and isinstance(bundle.get("links"), list) else []
            verified_document_links = 0
            for link in links:
                if not isinstance(link, dict) or link.get("link_kind") != "exact_value_document_segment":
                    continue
                source_matches_anchor = anchor_file in source_aliases(anchor_source) and str(
                    link.get("source_file", "")
                ).replace("\\", "/").strip("/") in source_aliases(anchor_source)
                target_matches_anchor = str(link.get("target_file", "")).replace(
                    "\\", "/"
                ).strip("/") in source_aliases(anchor_source)
                if source_matches_anchor == target_matches_anchor:
                    continue
                other_file = str(
                    link.get("target_file" if source_matches_anchor else "source_file", "")
                ).replace("\\", "/").strip("/")
                other_table = str(
                    link.get("target_table" if source_matches_anchor else "source_table", "")
                ).strip()
                other_sources = matching_sources(other_file)
                if len(other_sources) != 1:
                    continue
                other_roles = applicable_roles(other_sources[0], other_table)
                pairs = link.get("key_pairs") if isinstance(link.get("key_pairs"), list) else []
                fingerprints = (
                    link.get("key_fingerprints")
                    if isinstance(link.get("key_fingerprints"), list)
                    else []
                )
                valid_pairs = bool(pairs) and all(
                    isinstance(pair, dict)
                    and str(pair.get("source_field", "")).strip()
                    and str(pair.get("target_field", "")).strip()
                    for pair in pairs
                )
                valid_fingerprints = bool(fingerprints) and all(
                    _SHA256_PATTERN.fullmatch(str(value).strip().lower())
                    for value in fingerprints
                )
                if (
                    len(other_roles) == 1
                    and str(other_roles[0].get("role", "")) == "input"
                    and valid_pairs
                    and valid_fingerprints
                    and link.get("search_mode") == "field_evidence_exact_locator_replay"
                    and link.get("matched_row_count") == 1
                    and not bool(link.get("fanout_warning"))
                    and not bool(link.get("materialization_truncated"))
                ):
                    verified_document_links += 1
            if exact_link_count <= 0 or verified_document_links <= 0:
                raise ValueError(
                    "Document result trace has no replayable exact-value link to an approved business input."
                )
            return

        try:
            anchor_row = int(anchor.get("row_number"))
        except (TypeError, ValueError):
            anchor_row = 0
        if anchor_kind not in {"", "table_row"} or not anchor_table or anchor_row <= 0:
            raise ValueError("Trace review has no concrete result-table anchor.")
        anchor_roles = applicable_roles(anchor_source, anchor_table)
        if len(anchor_roles) != 1 or str(anchor_roles[0].get("role", "")) != "result":
            raise ValueError(
                "Trace anchor is not a current platform-approved result table."
            )

        selected = record.distillation.anchor_selector
        if selected is not None:
            if (
                selected.source_revision != record.distillation.source_revision
                or selected.file.replace("\\", "/").strip("/") != anchor_file
                or selected.table != anchor_table
                or selected.row_number != anchor_row
            ):
                raise ValueError(
                    "Trace anchor does not match the reviewer-selected historical result row."
                )

    def _validate_micro_process_payload(self, workspace: Path, payload: dict[str, Any]) -> str:
        if payload.get("kind") != "trace_micro_process":
            raise ValueError("Micro-process artifact has an unexpected contract kind.")
        review_path = workspace / "outputs" / "data-relations" / "trace-review.json"
        trace_path = workspace / "outputs" / "data-relations" / "trace-samples.json"
        if not review_path.is_file() or not trace_path.is_file():
            raise ValueError("Micro-process approval requires current trace-review and trace-samples artifacts.")
        review_fingerprint = _sha256_file(review_path)
        trace_fingerprint = _sha256_file(trace_path)
        source = payload.get("source") if isinstance(payload.get("source"), dict) else {}
        if source.get("trace_review_fingerprint") != review_fingerprint:
            raise ValueError("Micro-process is stale: its trace-review fingerprint no longer matches.")
        review = _load_json_artifact(review_path, "outputs/data-relations/trace-review.json")
        if review.get("status") != "approved":
            raise ValueError("Micro-process approval requires a currently approved trace-review.")
        reference = review.get("trace") if isinstance(review.get("trace"), dict) else {}
        if reference.get("fingerprint") != trace_fingerprint:
            raise ValueError("Trace-review is stale: it does not match the current trace samples.")
        if not _has_valid_platform_approval_envelope(
            workspace,
            artifact_kind="trace_review",
            artifact_fingerprint=review_fingerprint,
            trace_fingerprint=trace_fingerprint,
        ):
            raise ValueError(
                "Micro-process approval requires a valid platform-signed trace-review approval envelope "
                "for the current review and trace artifacts."
            )
        return trace_fingerprint

    def _apply_platform_review_decision(
        self,
        record: BusinessRecord,
        *,
        phase: DistillationPhase,
        expected_artifact_fingerprint: str,
        trace_fingerprint: str,
        decision: str,
        actor: str,
        note: str,
    ) -> str:
        """Write the platform's review decision into legacy review contracts.

        The HMAC envelope is the authority consumed by new workers.  Updating
        these JSON contracts keeps the existing review validators compatible
        without ever accepting an Agent-supplied reviewer identity.
        """

        if phase not in {"data_lineage", "micro_process"}:
            return expected_artifact_fingerprint
        workspace = self.workspace_dir(record.id).resolve()
        relative, _statuses = _PHASE_ARTIFACTS[phase]
        artifact = workspace / relative
        if not artifact.is_file() or _sha256_file(artifact) != expected_artifact_fingerprint:
            raise ValueError("Review artifact changed while the approval was being recorded; refresh and retry.")
        payload = _load_json_artifact(artifact, relative)
        approvable_statuses = (
            {"pending_review", "approved"}
            if phase == "data_lineage"
            else {"pending_review", "ready_for_review", "draft", "approved"}
        )
        if decision == "approved" and payload.get("status") not in approvable_statuses:
            raise ValueError("Only a reviewable candidate can receive a new platform approval.")
        if decision == "rejected" and not note.strip():
            raise ValueError("A rejection note is required so the user can correct and retrace.")

        reviewer = f"platform:{actor}"
        approval = payload.get("approval") if isinstance(payload.get("approval"), dict) else {}
        if decision == "approved":
            payload["status"] = "approved"
            approval.update({
                "decision": "approved",
                "reviewer": reviewer,
                "note": note.strip(),
                "reviewed_at": utc_now(),
            })
            if phase == "data_lineage":
                surface = payload.get("review_surface") if isinstance(payload.get("review_surface"), dict) else {}
                warnings = surface.get("warnings") if isinstance(surface.get("warnings"), list) else []
                approval["accepted_warnings"] = list(dict.fromkeys(str(item) for item in warnings if str(item)))
        else:
            payload["status"] = "revision_required"
            approval.update({
                "decision": "revision_required",
                "reviewer": reviewer,
                "note": note.strip(),
                "reviewed_at": utc_now(),
            })
            if phase == "data_lineage":
                approval["accepted_warnings"] = []
        payload["approval"] = approval
        _atomic_json_write(artifact, payload)
        resulting_fingerprint = _sha256_file(artifact)
        if phase == "data_lineage":
            # Re-verify after the platform write so the envelope always binds
            # the final, displayed review rather than the pre-review draft.
            final_review = _load_json_artifact(artifact, relative)
            if self._validate_trace_review_payload(record, workspace, final_review) != trace_fingerprint:
                raise ValueError("The platform review no longer matches the selected trace.")
        return resulting_fingerprint

    def _write_platform_approval_envelope(
        self,
        record: BusinessRecord,
        *,
        approval_id: str,
        phase: DistillationPhase,
        subject: str,
        artifact_fingerprint: str,
        trace_fingerprint: str,
    ) -> tuple[str, str]:
        key = _platform_approval_hmac_key()
        artifact_kind = "trace_review" if phase == "data_lineage" else "micro_process"
        envelope = {
            "schema_version": 1,
            "issuer": "business-flow-platform",
            "approval_id": approval_id,
            "subject": subject,
            "artifact_kind": artifact_kind,
            "artifact_fingerprint": artifact_fingerprint,
            "trace_fingerprint": trace_fingerprint,
            "decision": "approved",
            "issued_at": utc_now(),
        }
        envelope["signature"] = _platform_approval_signature(envelope, key)

        workspace = self.workspace_dir(record.id).resolve()
        relative = PLATFORM_APPROVALS_RELATIVE
        path = workspace / relative
        if path.is_file():
            payload = _load_json_artifact(path, relative)
            if (
                payload.get("schema_version") != 1
                or payload.get("kind") != "platform_approval_envelopes"
                or payload.get("issuer") != "business-flow-platform"
                or not isinstance(payload.get("approvals"), list)
            ):
                raise ValueError("Existing platform-approvals.json has an invalid contract.")
        else:
            payload = {
                "schema_version": 1,
                "kind": "platform_approval_envelopes",
                "issuer": "business-flow-platform",
                "approvals": [],
            }
        payload["approvals"].append(envelope)
        _atomic_json_write(path, payload)
        return relative, _sha256_file(path)

    def _capability_package_candidate(
        self,
        record: BusinessRecord,
        *,
        skill_archive_fingerprint: str,
    ) -> tuple[Path, dict[str, Any], dict[str, Path]]:
        """Validate the exact package a reviewer is about to release.

        Capability generation creates a candidate only.  Before the platform
        changes it to publishable, bind the final archive to the already
        approved capability manifest, both release modes, and the same
        relation/flow inputs.  This makes the package checkpoint meaningful
        instead of a UI-only acknowledgement of a ZIP filename.
        """

        workspace = self.workspace_dir(record.id).resolve()
        paths = {
            "capability_manifest": workspace / CAPABILITY_MANIFEST_RELATIVE,
            "release_manifest": workspace / CAPABILITY_RELEASE_MANIFEST_RELATIVE,
            "skill_archive": workspace / CAPABILITY_SKILL_ARCHIVE_RELATIVE,
            "mcp_stdio_archive": workspace / CAPABILITY_MCP_ARCHIVE_RELATIVE,
        }
        missing = [name for name, path in paths.items() if not path.is_file()]
        if missing:
            raise ValueError(
                "Capability package review is missing required release artifacts: " + ", ".join(missing)
            )
        if _sha256_file(paths["skill_archive"]) != skill_archive_fingerprint:
            raise ValueError("Capability package changed while it was being reviewed; refresh and retry.")

        manifest_path = paths["capability_manifest"]
        manifest = _load_json_artifact(manifest_path, CAPABILITY_MANIFEST_RELATIVE)
        publication = manifest.get("publication") if isinstance(manifest.get("publication"), dict) else {}
        if (
            manifest.get("status") != "complete"
            # ``verification`` governs whether a compiled recipe may produce
            # a deterministic business conclusion.  It is intentionally
            # allowed to be unverified for an evidence-only capability: the
            # portable executor will then stay on the human-judgment path.
            # Package publication is a separate, human-controlled axis and
            # must use the generator's explicit publication candidate flag.
            or publication.get("verifiable") is not True
            or publication.get("status") != "pending_human_platform_package_approval"
            or publication.get("publishable") is not False
        ):
            raise ValueError(
                "Capability manifest is not a current pending human package-review candidate."
            )

        state = record.distillation
        manifest_fingerprint = _sha256_file(manifest_path)
        if not any(
            item.status == "active"
            and item.phase == "capability"
            and item.decision == "approved"
            and item.source_revision == state.source_revision
            and item.artifact_id == CAPABILITY_MANIFEST_RELATIVE
            and item.artifact_fingerprint == manifest_fingerprint
            for item in state.approvals
        ):
            raise ValueError(
                "Capability package review requires a current approval of this exact capability manifest."
            )

        source = manifest.get("source") if isinstance(manifest.get("source"), dict) else {}
        relation_fingerprint = str(source.get("relation_fingerprint", "")).strip()
        flow_fingerprint = str(source.get("flow_fingerprint", "")).strip()
        relation_path = workspace / "outputs" / "data-relations" / "scenario-relationship.json"
        flow_path = workspace / "outputs" / "business-flow" / "business-flow.json"
        if (
            not relation_fingerprint
            or not flow_fingerprint
            or not relation_path.is_file()
            or not flow_path.is_file()
            or _sha256_file(relation_path) != relation_fingerprint
            or _sha256_file(flow_path) != flow_fingerprint
        ):
            raise ValueError(
                "Capability package candidate no longer matches the current relation/flow artifacts."
            )

        actual_skill = _sha256_file(paths["skill_archive"])
        actual_mcp = _sha256_file(paths["mcp_stdio_archive"])
        release = manifest.get("release") if isinstance(manifest.get("release"), dict) else {}
        release_digests = release.get("artifact_digests") if isinstance(release.get("artifact_digests"), dict) else {}
        artifact_digests = manifest.get("artifact_digests") if isinstance(manifest.get("artifact_digests"), dict) else {}
        release_manifest = _load_json_artifact(paths["release_manifest"], CAPABILITY_RELEASE_MANIFEST_RELATIVE)
        release_document_digests = (
            release_manifest.get("artifact_digests")
            if isinstance(release_manifest.get("artifact_digests"), dict)
            else {}
        )
        if (
            release.get("schema_version") != 1
            or release_digests.get("skill_zip") != actual_skill
            or release_digests.get("mcp_stdio_zip") != actual_mcp
            or artifact_digests.get("skill_archive") != actual_skill
            or artifact_digests.get("mcp_stdio_archive") != actual_mcp
            or release_manifest.get("format") != "portable-business-capability-release"
            or release_document_digests.get("skill_zip") != actual_skill
            or release_document_digests.get("mcp_stdio_zip") != actual_mcp
        ):
            raise ValueError(
                "Capability package release manifests or archive digests do not match the review candidate."
            )
        return manifest_path, manifest, paths

    def _approve_capability_package(
        self,
        record: BusinessRecord,
        *,
        approval_id: str,
        actor: str,
        skill_archive_fingerprint: str,
    ) -> tuple[str, str]:
        """Turn a reviewed candidate into a signed, publishable package."""

        key = _platform_approval_hmac_key()
        manifest_path, manifest, paths = self._capability_package_candidate(
            record,
            skill_archive_fingerprint=skill_archive_fingerprint,
        )
        relative = PLATFORM_APPROVALS_RELATIVE
        ledger_path = self.workspace_dir(record.id).resolve() / relative
        if ledger_path.is_file():
            ledger = _load_json_artifact(ledger_path, relative)
            if (
                ledger.get("schema_version") != 1
                or ledger.get("kind") != "platform_approval_envelopes"
                or ledger.get("issuer") != "business-flow-platform"
                or not isinstance(ledger.get("approvals"), list)
            ):
                raise ValueError("Existing platform-approvals.json has an invalid contract.")
        else:
            ledger = {
                "schema_version": 1,
                "kind": "platform_approval_envelopes",
                "issuer": "business-flow-platform",
                "approvals": [],
            }
        source = manifest["source"]
        published_at = utc_now()
        previous_publication = (
            manifest.get("publication") if isinstance(manifest.get("publication"), dict) else {}
        )
        manifest["publication"] = {
            **previous_publication,
            "status": "approved",
            "verifiable": True,
            "publishable": True,
            "approved_by": actor,
            "approved_at": published_at,
            "approval_id": approval_id,
            "platform_approval": {
                "issuer": "business-flow-platform",
                "artifact_kind": "capability_package",
                "decision": "approved",
                "approval_id": approval_id,
                "subject": actor,
                "issued_at": published_at,
                "ledger": PLATFORM_APPROVALS_RELATIVE,
            },
        }
        _atomic_json_write(manifest_path, manifest)

        package_artifacts = {name: _sha256_file(path) for name, path in paths.items()}
        if package_artifacts["skill_archive"] != skill_archive_fingerprint:
            raise ValueError("Capability skill archive changed while publication approval was being written.")
        envelope = {
            "schema_version": 1,
            "issuer": "business-flow-platform",
            "approval_id": approval_id,
            "subject": actor,
            "artifact_kind": "capability_package",
            "artifact_fingerprint": package_artifacts["skill_archive"],
            "relation_fingerprint": str(source.get("relation_fingerprint", "")),
            "flow_fingerprint": str(source.get("flow_fingerprint", "")),
            "package_artifacts": package_artifacts,
            "decision": "approved",
            "issued_at": published_at,
        }
        envelope["signature"] = _platform_approval_signature(envelope, key)
        ledger["approvals"].append(envelope)
        _atomic_json_write(ledger_path, ledger)
        return relative, _sha256_file(ledger_path)

    def _mark_capability_package_revision_required(
        self,
        record: BusinessRecord,
        *,
        actor: str,
        note: str,
    ) -> None:
        """Revoke any previously publishable manifest when final review fails."""

        manifest_path = self.workspace_dir(record.id).resolve() / CAPABILITY_MANIFEST_RELATIVE
        if not manifest_path.is_file():
            return
        try:
            manifest = _load_json_artifact(manifest_path, CAPABILITY_MANIFEST_RELATIVE)
        except ValueError:
            # The state transition still returns to capability repair; the
            # malformed candidate cannot pass the offline summary gate.
            return
        publication = manifest.get("publication") if isinstance(manifest.get("publication"), dict) else {}
        publication.update({
            "status": "revision_required",
            "verifiable": bool(manifest.get("verification", {}).get("verifiable"))
            if isinstance(manifest.get("verification"), dict) else False,
            "publishable": False,
            "rejected_by": actor,
            "rejected_at": utc_now(),
            "rejection_note": note.strip(),
        })
        manifest["publication"] = publication
        _atomic_json_write(manifest_path, manifest)

    def prepare_business_for_view(
        self,
        business_id: str,
        owner_id: str,
    ) -> BusinessRecord:
        """Load a current business and prepare ephemeral UI approval actions."""

        with self._lock:
            record = self.require(business_id, owner_id)
            if self.refresh_role_manifest_fingerprint(record):
                self.save(record)
            self.ensure_distillation_approval_question(record)
            return record

    def refresh_distillation_artifact_contracts(
        self,
        record: BusinessRecord,
    ) -> dict[str, dict[str, Any]]:
        """Describe canonical review artifacts with server-computed digests.

        This is a read-side helper for the workbench.  The approval endpoint
        recomputes every value again, so this metadata never becomes a trust
        boundary or a time-of-check/time-of-use bypass.
        """

        with self._lock:
            state = record.distillation
            contracts = {
                phase: self._build_distillation_artifact_contract(record, phase)
                for phase in DISTILLATION_PHASES
            }
            state.artifact_contracts = contracts
            return contracts

    def refresh_current_distillation_artifact_contract(
        self,
        record: BusinessRecord,
    ) -> dict[str, Any]:
        """Refresh only the current checkpoint's candidate for ordinary UI reads."""

        with self._lock:
            phase = record.distillation.current_phase
            contract = self._build_distillation_artifact_contract(record, phase)
            record.distillation.artifact_contracts[phase] = contract
            return contract

    def _build_distillation_artifact_contract(
        self,
        record: BusinessRecord,
        phase: DistillationPhase,
    ) -> dict[str, Any]:
        state = record.distillation
        if phase == "file_roles":
            current_roles = [
                item
                for item in state.table_roles
                if item.status == "confirmed"
                and any(source.id == item.file_id for source in record.files)
            ]
            return {
                "artifact_id": "table-roles",
                "fingerprint": state.role_manifest_fingerprint,
                "status": _distillation_stage(state, "file_roles").status,
                "reviewable": bool(current_roles and state.role_manifest_fingerprint),
                "detail": f"{len(current_roles)} active file/table role confirmation(s)",
            }

        relative, allowed_statuses = _PHASE_ARTIFACTS[phase]
        workspace = self.workspace_dir(record.id).resolve()
        artifact = workspace / relative
        contract: dict[str, Any] = {
            "artifact_id": relative,
            "fingerprint": "",
            "status": "missing",
            "reviewable": False,
            "detail": "Artifact has not been generated in this workspace.",
        }
        if not artifact.is_file():
            return contract
        contract["fingerprint"] = _sha256_file(artifact)
        if phase == "package":
            # The final review action must be offered only for the same
            # candidate the approval endpoint can publish.  Checking only
            # that skill.zip exists used to create an approval question for
            # evidence-only packages and then reject it at submission time.
            try:
                self._capability_package_candidate(
                    record,
                    skill_archive_fingerprint=contract["fingerprint"],
                )
            except ValueError as exc:
                contract.update({
                    "status": "not_current_package_review_candidate",
                    "reviewable": False,
                    "detail": str(exc),
                })
            else:
                contract.update({
                    "status": "pending_human_platform_package_approval",
                    "reviewable": True,
                    "detail": "Canonical package candidate is ready for human platform review.",
                })
            return contract
        if allowed_statuses is None:
            contract.update({
                "status": "available",
                "reviewable": True,
                "detail": "Canonical binary release artifact is available.",
            })
            return contract
        try:
            payload = _load_json_artifact(artifact, relative)
            status = str(payload.get("status", ""))
            contract["status"] = status or "unknown"
            if phase == "data_lineage":
                self._validate_trace_review_payload(record, workspace, payload)
            elif phase == "micro_process":
                self._validate_micro_process_payload(workspace, payload)
            contract["reviewable"] = status in allowed_statuses
            contract["detail"] = (
                "Canonical candidate is ready for platform review."
                if contract["reviewable"]
                else "Artifact exists but is not in a reviewable candidate status."
            )
        except ValueError as exc:
            contract["detail"] = str(exc)
        return contract

    def ensure_distillation_approval_question(
        self,
        record: BusinessRecord,
        *,
        session_id: str | None = None,
        run_id: str = "",
        checkpoint_run_id: str = "",
        tool_call_id: str = "",
    ) -> tuple[dict[str, Any] | None, bool]:
        """Materialize the one real approval action for the current phase.

        The question is platform-authored and signed.  It is only created when
        the canonical current artifact passes its server-side validator and
        every upstream approval still matches current bytes.  Chat text can
        therefore explain a gate, but it cannot manufacture an approval UI.
        """

        with self._lock:
            state = record.distillation
            _ensure_distillation_stages(state)
            phase = state.current_phase
            contract = self.refresh_current_distillation_artifact_contract(record)
            changed = False

            def supersede_open_questions() -> None:
                nonlocal changed
                for item in record.context.questions:
                    if (
                        item.get("source") == "distillation_approval"
                        and item.get("status", "open") == "open"
                    ):
                        item["status"] = "superseded"
                        item["superseded_at"] = now()
                        changed = True

            if phase == "file_roles" or not isinstance(contract, dict):
                supersede_open_questions()
                return None, changed

            artifact_id = str(contract.get("artifact_id") or "").strip()
            fingerprint = str(contract.get("fingerprint") or "").strip()
            if not contract.get("reviewable") or not artifact_id or not fingerprint:
                supersede_open_questions()
                return None, changed

            try:
                # Approval questions use the same platform authority key as
                # approval receipts, including phases whose legacy artifact
                # does not itself carry an envelope.
                key = _platform_approval_hmac_key()
                normalized_id, computed_fingerprint, _trace_fingerprint = (
                    self._validate_distillation_artifact(record, phase, artifact_id)
                )
                self._require_current_upstream_approval_chain(record, phase)
            except ValueError as exc:
                contract["reviewable"] = False
                contract["detail"] = str(exc)
                contract["approval_error_code"] = (
                    "approval_key_missing"
                    if "BUSINESS_FLOW_PLATFORM_APPROVAL_HMAC_KEY" in str(exc)
                    else "approval_validation_failed"
                )
                supersede_open_questions()
                return None, changed
            if normalized_id != artifact_id or computed_fingerprint != fingerprint:
                contract["reviewable"] = False
                contract["detail"] = "Canonical artifact changed while preparing its approval action."
                supersede_open_questions()
                return None, changed

            active_decisions = [
                item
                for item in state.approvals
                if item.status == "active"
                and item.phase == phase
                and item.source_revision == state.source_revision
                and item.artifact_id == artifact_id
                and item.artifact_fingerprint == fingerprint
            ]
            if any(item.decision in {"approved", "rejected"} for item in active_decisions):
                supersede_open_questions()
                return None, changed

            ui = _DISTILLATION_REVIEW_UI[phase]
            target_path = ui["target_path"]
            if (
                ui["target_kind"] == "workspace_file"
                and (not target_path or not (self.workspace_dir(record.id) / target_path).is_file())
            ):
                target_path = artifact_id
            review_target = {
                "kind": ui["target_kind"],
                "label": ui["target_label"],
                "path": target_path,
            }
            question_text = f"是否批准当前“{ui['label']}”并进入下一阶段？"
            reason_text = (
                f"平台已校验当前规范产物（状态：{contract.get('status') or 'unknown'}）。"
                "请先查看待审批内容；正确则批准，有问题则退回并说明需要修改的地方。"
            )

            identity = {
                "issuer": "business-flow-platform",
                "approval_schema": 2,
                "business_id": record.id,
                "phase": phase,
                "artifact_id": artifact_id,
                "artifact_fingerprint": fingerprint,
                "expected_revision": state.revision,
                "source_revision": state.source_revision,
            }
            identity_text = json.dumps(
                identity,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            question_id = "q_distill_" + hashlib.sha256(identity_text.encode("utf-8")).hexdigest()[:20]
            decision_options = {
                "approved": f"{question_id}_approve",
                "rejected": f"{question_id}_reject",
            }
            authority = {
                **identity,
                "question_id": question_id,
                "decision_options": decision_options,
                "display_label": ui["label"],
                "review_target": review_target,
            }
            authority["signature"] = _platform_approval_signature(authority, key)

            canonical_options = [
                {
                    "id": decision_options["approved"],
                    "label": "批准当前产物",
                    "description": "锁定当前产物及其指纹，并进入下一蒸馏阶段。",
                    "recommended": False,
                    "value": "approved",
                },
                {
                    "id": decision_options["rejected"],
                    "label": "退回修改",
                    "description": "保留当前阶段，并按你的说明重新生成或追踪。",
                    "recommended": False,
                    "value": "rejected",
                },
            ]
            canonical_approval = {
                "phase": phase,
                "label": ui["label"],
                "artifact_id": artifact_id,
                "artifact_fingerprint": fingerprint,
                "expected_revision": state.revision,
                "source_revision": state.source_revision,
                "artifact_status": contract.get("status"),
                "authority": authority,
            }

            existing: dict[str, Any] | None = None
            for item in record.context.questions:
                if item.get("source") != "distillation_approval" or item.get("status", "open") != "open":
                    continue
                item_approval = item.get("approval") if isinstance(item.get("approval"), dict) else {}
                if item.get("id") == question_id and item_approval.get("authority") == authority:
                    existing = item
                    continue
                item["status"] = "superseded"
                item["superseded_at"] = now()
                changed = True
            if existing is not None:
                if run_id and existing.get("run_id") and existing.get("run_id") != run_id:
                    # A business approval is shared, but only its first waiter
                    # owns the checkpoint.  A second run must receive a normal
                    # gate result instead of becoming an unresumable waiter.
                    return None, changed
                if run_id and not existing.get("run_id"):
                    existing["run_id"] = run_id
                    changed = True
                if checkpoint_run_id and not existing.get("checkpoint_run_id"):
                    existing["checkpoint_run_id"] = checkpoint_run_id
                    changed = True
                if session_id and not existing.get("session_id"):
                    existing["session_id"] = session_id
                    changed = True
                if tool_call_id and not existing.get("tool_call_id"):
                    existing["tool_call_id"] = tool_call_id
                    changed = True
                if existing.get("options") != canonical_options:
                    existing["options"] = canonical_options
                    changed = True
                canonical_display = {
                    "question": question_text,
                    "reason": reason_text,
                    "category": "蒸馏审批",
                    "response_kind": "approval",
                    "approval": canonical_approval,
                    "review_target": dict(review_target),
                    "rejection_note_required": True,
                }
                for name, value in canonical_display.items():
                    if existing.get(name) != value:
                        existing[name] = value
                        changed = True
                return existing, changed

            question = {
                "id": question_id,
                "tool_call_id": tool_call_id,
                "question": question_text,
                "reason": reason_text,
                "category": "蒸馏审批",
                "source": "distillation_approval",
                "response_kind": "approval",
                "options": canonical_options,
                "approval": canonical_approval,
                "review_target": dict(review_target),
                "rejection_note_required": True,
                "status": "open",
                "run_id": run_id,
                "checkpoint_run_id": checkpoint_run_id or run_id,
                "session_id": session_id,
                "created_at": now(),
            }
            record.context.questions.append(question)
            return question, True

    def supersede_micro_process_gate_bypass_questions(
        self,
        record: BusinessRecord,
        *,
        session_id: str = "",
        run_id: str = "",
        checkpoint_run_id: str = "",
    ) -> bool:
        """Retire legacy Agent questions that tried to bypass micro-process review.

        Earlier runtime versions could turn a downstream business-flow gate
        into an ordinary ``source=agent`` clarification.  Such a question has
        no authority to approve a candidate and must not remain visible next
        to the signed platform action.  Scope the cleanup to the current chat
        or run and require an explicit micro-process/business-flow marker so
        unrelated business questions remain untouched.
        """

        if record.distillation.current_phase != "micro_process":
            return False
        bindings = {value for value in (session_id, run_id, checkpoint_run_id) if value}
        changed = False
        timestamp = now()
        for question in record.context.questions:
            if question.get("source") != "agent" or question.get("status", "open") != "open":
                continue
            question_bindings = {
                str(question.get(key) or "")
                for key in ("session_id", "run_id", "checkpoint_run_id")
                if str(question.get(key) or "")
            }
            if bindings and not bindings.intersection(question_bindings):
                continue
            phase = str(question.get("distillation_phase") or "")
            text = " ".join(
                str(question.get(key) or "")
                for key in ("question", "reason", "category", "detail")
            ).casefold()
            is_gate_bypass = (
                phase == "micro_process"
                or (
                    ("micro_process" in text or "微观" in text)
                    and any(marker in text for marker in ("business-flow", "business flow", "业务流程", "业务流"))
                )
            )
            if not is_gate_bypass:
                continue
            question["status"] = "superseded"
            question["superseded_at"] = timestamp
            question["superseded_reason"] = "Replaced by the signed micro-process approval action."
            changed = True
        return changed

    def validate_distillation_approval_question(
        self,
        record: BusinessRecord,
        question: dict[str, Any],
    ) -> tuple[DistillationPhase, str, str, int]:
        """Resolve a signed approval question back to the current contract."""

        with self._lock:
            if (
                question.get("source") != "distillation_approval"
                or question.get("status", "open") != "open"
            ):
                raise ValueError("This distillation approval action is no longer open.")
            approval = question.get("approval") if isinstance(question.get("approval"), dict) else {}
            authority = approval.get("authority") if isinstance(approval.get("authority"), dict) else {}
            signature = str(authority.get("signature") or "")
            expected_signature = _platform_approval_signature(authority, _platform_approval_hmac_key())
            if (
                authority.get("issuer") != "business-flow-platform"
                or authority.get("approval_schema") != 2
                or authority.get("business_id") != record.id
                or authority.get("question_id") != question.get("id")
                or not signature
                or not hmac.compare_digest(signature, expected_signature)
            ):
                raise ValueError("The distillation approval action has no valid platform authority.")
            question_id = str(question.get("id") or "")
            decision_options = authority.get("decision_options")
            if decision_options != {
                "approved": f"{question_id}_approve",
                "rejected": f"{question_id}_reject",
            }:
                raise ValueError("The distillation approval action has an invalid decision contract.")

            state = record.distillation
            phase = str(authority.get("phase") or "")
            if phase not in DISTILLATION_PHASES or phase == "file_roles":
                raise ValueError("The distillation approval action has an invalid phase.")
            if phase != state.current_phase:
                raise ValueError(
                    f"This approval action is stale; the current phase is {state.current_phase}."
                )
            try:
                expected_revision = int(authority.get("expected_revision"))
                source_revision = int(authority.get("source_revision"))
            except (TypeError, ValueError) as exc:
                raise ValueError("The distillation approval action has invalid revision metadata.") from exc
            if expected_revision != state.revision or source_revision != state.source_revision:
                raise ValueError("This approval action is stale; refresh the scenario and review the current candidate.")

            contract = self.refresh_current_distillation_artifact_contract(record)
            if not contract.get("reviewable"):
                raise ValueError("The current phase artifact is not ready for platform review.")
            artifact_id = str(contract.get("artifact_id") or "")
            fingerprint = str(contract.get("fingerprint") or "")
            if (
                authority.get("artifact_id") != artifact_id
                or authority.get("artifact_fingerprint") != fingerprint
            ):
                raise ValueError("This approval action is stale because the reviewed artifact changed.")
            ui = _DISTILLATION_REVIEW_UI[phase]  # type: ignore[index]
            target_path = ui["target_path"]
            if (
                ui["target_kind"] == "workspace_file"
                and (not target_path or not (self.workspace_dir(record.id) / target_path).is_file())
            ):
                target_path = artifact_id
            expected_review_target = {
                "kind": ui["target_kind"],
                "label": ui["target_label"],
                "path": target_path,
            }
            if (
                authority.get("display_label") != ui["label"]
                or authority.get("review_target") != expected_review_target
            ):
                raise ValueError("The distillation approval action has an invalid signed review target.")
            return phase, artifact_id, fingerprint, expected_revision  # type: ignore[return-value]

    def resolve_distillation_approval_question(
        self,
        *,
        business_id: str,
        owner_id: str,
        question_id: str,
        option_id: str,
        answer: str,
        accepted: bool,
        actor: str,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        """Atomically resolve one signed distillation approval action.

        The latest business record is reloaded while holding the store lock so
        two browser submissions cannot both approve the same artifact.  A
        question returned ephemerally by a GET is regenerated from the current
        artifact contract before its signature is validated.
        """

        with self._lock:
            record = self.require(business_id, owner_id)
            _assert_distillation_actor(record, actor)
            if session_id:
                self.require_chat_session(record, session_id)

            prior_question = next(
                (
                    item for item in record.context.questions
                    if str(item.get("id") or "") == question_id
                    and item.get("source") == "distillation_approval"
                    and item.get("status", "open") == "open"
                ),
                None,
            )
            known_sessions = {item.id for item in record.chat_sessions}
            binding_session_id = (
                str(prior_question.get("session_id") or "")
                if prior_question is not None
                else ""
            )
            if binding_session_id not in known_sessions:
                binding_session_id = str(session_id or "")
            known_runs = {item.id: item for item in record.runs}
            binding_run_id = (
                str(prior_question.get("run_id") or "")
                if prior_question is not None
                else ""
            )
            binding_run = known_runs.get(binding_run_id)
            if (
                binding_run is None
                or binding_run.status != "waiting_for_user"
                or (binding_session_id and binding_run.session_id != binding_session_id)
            ):
                binding_run_id = ""
            binding_checkpoint_id = (
                str(prior_question.get("checkpoint_run_id") or "")
                if prior_question is not None
                else ""
            )
            binding_checkpoint = known_runs.get(binding_checkpoint_id)
            if (
                binding_checkpoint is None
                or (binding_session_id and binding_checkpoint.session_id != binding_session_id)
            ):
                binding_checkpoint_id = binding_run_id
            binding_tool_call_id = (
                str(prior_question.get("tool_call_id") or "")
                if prior_question is not None
                else ""
            )

            # Rebuild/supersede the persisted action before matching the
            # submitted id. This repairs rotated HMAC signatures and restores
            # every display field from the current signed contract.
            self.ensure_distillation_approval_question(
                record,
                session_id=binding_session_id or session_id,
                run_id=binding_run_id,
                checkpoint_run_id=binding_checkpoint_id,
                tool_call_id=binding_tool_call_id,
            )
            matching_questions = [
                item for item in record.context.questions
                if str(item.get("id") or "") == question_id
            ]
            question = next(
                (
                    item for item in matching_questions
                    if item.get("source") == "distillation_approval"
                    and item.get("status", "open") == "open"
                ),
                None,
            )
            if question is None:
                if matching_questions:
                    raise ValueError("This distillation approval action is no longer open.")
                if not question_id.startswith("q_distill_"):
                    raise ValueError("This is not a platform distillation approval action.")
                question, _changed = self.ensure_distillation_approval_question(
                    record,
                    session_id=session_id,
                )
                if question is None or str(question.get("id") or "") != question_id:
                    contract = record.distillation.artifact_contracts.get(
                        str(record.distillation.current_phase),
                        {},
                    )
                    detail = str(contract.get("detail") or "").strip()
                    raise ValueError(
                        detail
                        or "This distillation approval action is stale; refresh the scenario and review the current candidate."
                    )

            question_session = str(question.get("session_id") or "")
            if question_session and session_id and question_session != session_id:
                raise ValueError("Question does not belong to this chat session.")
            resolved_session_id = question_session or str(session_id or "") or None

            approval_context = (
                question.get("approval")
                if isinstance(question.get("approval"), dict)
                else {}
            )
            authority_value = approval_context.get("authority")
            authority = authority_value if isinstance(authority_value, dict) else {}
            signed_options = (
                authority.get("decision_options", {})
                if isinstance(authority.get("decision_options"), dict)
                else {}
            )
            decision = next(
                (
                    value for value in ("approved", "rejected")
                    if signed_options.get(value) == option_id
                    and option_id == f"{question_id}_{'approve' if value == 'approved' else 'reject'}"
                ),
                None,
            )
            if decision is None:
                raise ValueError("Select either the signed approve or reject action for this artifact.")
            if accepted != (decision == "approved"):
                raise ValueError("The approval option does not match the submitted decision.")

            note = answer.strip()
            if decision == "rejected" and not note:
                raise ValueError("A specific correction is required when returning an artifact for revision.")

            phase, artifact_id, fingerprint, expected_revision = (
                self.validate_distillation_approval_question(record, question)
            )
            approval = self.record_distillation_approval(
                record,
                phase=phase,
                decision=decision,
                artifact_id=artifact_id,
                artifact_fingerprint=fingerprint,
                note=note,
                actor=actor,
                expected_revision=expected_revision,
            )

            answered_at = now()
            display_run_id = str(question.get("run_id") or "") or None
            checkpoint_run_id = (
                str(question.get("checkpoint_run_id") or "")
                or display_run_id
            )
            confirmation = {
                "id": new_id("confirm"),
                "question_id": question_id,
                "run_id": display_run_id,
                "checkpoint_run_id": checkpoint_run_id,
                "session_id": resolved_session_id,
                "answer": note,
                "accepted": decision == "approved",
                "decision": decision,
                "approval_id": approval.id,
                "source": "distillation_approval",
                "created_at": answered_at,
            }
            record.context.confirmations.append(confirmation)
            question.update({
                "status": "answered",
                "answer": note,
                "decision": decision,
                "approval_id": approval.id,
                "answered_at": answered_at,
            })
            question.pop("continued_at", None)
            question.pop("continuation_run_id", None)
            for other in record.context.questions:
                if (
                    other is not question
                    and other.get("source") == "distillation_approval"
                    and other.get("status", "open") == "open"
                ):
                    other["status"] = "superseded"
                    other["superseded_at"] = answered_at

            next_question: dict[str, Any] | None = None
            if decision == "approved" and checkpoint_run_id is None:
                next_question, _changed = self.ensure_distillation_approval_question(
                    record,
                    session_id=resolved_session_id,
                )
            self.create_version(
                record,
                f"Recorded {phase} distillation {decision} decision",
                "distillation_approval_confirmation",
                actor=actor,
                evidence_ids=[approval.id],
            )
            self.save(record)
            return {
                "record": record,
                "approval": approval,
                "confirmation": confirmation,
                "run_id": display_run_id,
                "checkpoint_run_id": checkpoint_run_id,
                "next_question": next_question,
            }

    def claim_distillation_approval_continuation(
        self,
        *,
        business_id: str,
        owner_id: str,
        confirmation_id: str,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        """Claim one server-owned follow-up for an answered approval.

        Formal approvals are resolved synchronously, while their next action can
        be a long deterministic trace or an Agent run.  Keeping the claim in
        the store prevents a browser retry from starting two repairs for the
        same signed decision.
        """

        with self._lock:
            record = self.require(business_id, owner_id)
            confirmation = next(
                (
                    item
                    for item in record.context.confirmations
                    if str(item.get("id") or "") == confirmation_id
                    and item.get("source") == "distillation_approval"
                ),
                None,
            )
            if confirmation is None:
                raise ValueError("The requested distillation continuation does not exist.")

            confirmation_session_id = str(confirmation.get("session_id") or "")
            if session_id and confirmation_session_id and session_id != confirmation_session_id:
                raise ValueError("This distillation continuation belongs to another chat session.")
            resolved_session_id = confirmation_session_id or str(session_id or "")
            if not resolved_session_id:
                resolved_session_id = self.require_chat_session(record).id
            self.require_chat_session(record, resolved_session_id)

            state = str(confirmation.get("continuation_status") or "")
            if state == "running":
                raise ValueError("This distillation continuation is already running.")
            if state in {"completed", "needs_clarification"}:
                raise ValueError("This distillation continuation has already been handled.")

            approval_id = str(confirmation.get("approval_id") or "")
            approval = next((item for item in record.distillation.approvals if item.id == approval_id), None)
            if approval is None:
                raise ValueError("The signed approval receipt for this continuation is unavailable.")

            source_run_id = (
                str(confirmation.get("checkpoint_run_id") or "")
                or str(confirmation.get("run_id") or "")
            )
            source_run = next((item for item in record.runs if item.id == source_run_id), None)
            if source_run is not None and source_run.status == "waiting_for_user":
                source_run.status = "succeeded"
                source_run.finished_at = now()
                source_run.summary = "Reviewer decision received; platform continuation started."

            continuation_kind = (
                "lineage_retrace"
                if approval.phase == "data_lineage" and approval.decision == "rejected"
                else "agent"
            )
            started_at = now()
            confirmation.update({
                "continuation_status": "running",
                "continuation_kind": continuation_kind,
                "continuation_started_at": started_at,
                "continuation_session_id": resolved_session_id,
            })
            confirmation.pop("continuation_finished_at", None)
            confirmation.pop("continuation_detail", None)
            self.save(record)
            return {
                "record": record,
                "confirmation": dict(confirmation),
                "approval": approval,
                "session_id": resolved_session_id,
                "source_run_id": source_run_id or None,
                "kind": continuation_kind,
            }

    def finish_distillation_approval_continuation(
        self,
        *,
        business_id: str,
        owner_id: str,
        confirmation_id: str,
        status: str,
        detail: str = "",
    ) -> BusinessRecord:
        """Close a claimed continuation with an auditable terminal state."""

        if status not in {"completed", "needs_clarification", "failed"}:
            raise ValueError("Unsupported distillation continuation status.")
        with self._lock:
            record = self.require(business_id, owner_id)
            confirmation = next(
                (
                    item
                    for item in record.context.confirmations
                    if str(item.get("id") or "") == confirmation_id
                    and item.get("source") == "distillation_approval"
                ),
                None,
            )
            if confirmation is None:
                raise ValueError("The requested distillation continuation does not exist.")
            if confirmation.get("continuation_status") != "running":
                raise ValueError("This distillation continuation is no longer running.")
            confirmation.update({
                "continuation_status": status,
                "continuation_finished_at": now(),
                "continuation_detail": detail.strip()[:1000],
            })
            self.save(record)
            return record

    def rollback(self, record: BusinessRecord, version: int) -> BusinessRecord:
        match = next((item for item in record.context.versions if item.version == version), None)
        if match is None:
            raise ValueError(f"version {version} not found")
        restored = dict(match.snapshot)
        previous_versions = record.context.versions
        record.context = BusinessContext.model_validate(restored)
        record.context.versions = previous_versions
        self.create_version(record, f"Rolled back to v{version}", "rollback")
        return self.save(record)

    def list_chat_sessions(self, record: BusinessRecord) -> list[ChatSession]:
        _ensure_chat_sessions(record)
        return sorted(record.chat_sessions, key=lambda item: item.updated_at, reverse=True)

    def get_chat_session(self, record: BusinessRecord, session_id: str) -> ChatSession | None:
        _ensure_chat_sessions(record)
        return next((item for item in record.chat_sessions if item.id == session_id), None)

    def require_chat_session(self, record: BusinessRecord, session_id: str | None = None) -> ChatSession:
        _ensure_chat_sessions(record)
        if session_id:
            session = self.get_chat_session(record, session_id)
            if session is None:
                raise KeyError(session_id)
            return session
        return max(record.chat_sessions, key=lambda item: item.updated_at)

    def create_chat_session(self, record: BusinessRecord, title: str = "") -> ChatSession:
        with self._lock:
            ts = now()
            session = ChatSession(
                id=new_id("chat"),
                business_id=record.id,
                title=title.strip()[:120],
                created_at=ts,
                updated_at=ts,
            )
            record.chat_sessions.append(session)
            self.save(record)
            return session

    @staticmethod
    def _retire_open_questions_for_chat(record: BusinessRecord, session_id: str) -> None:
        """Prevent questions from retaining checkpoints removed with a chat."""

        timestamp = now()
        for question in record.context.questions:
            if (
                str(question.get("session_id") or "") != session_id
                or question.get("status", "open") != "open"
            ):
                continue
            if question.get("source") == "distillation_approval":
                # The decision is business-scoped and remains actionable, but
                # clearing its chat deliberately discards automatic resume.
                question["session_id"] = ""
                question["run_id"] = ""
                question["checkpoint_run_id"] = ""
                question["tool_call_id"] = ""
                question["detached_at"] = timestamp
                continue
            question["status"] = "superseded"
            question["superseded_at"] = timestamp

    def clear_chat_session(self, record: BusinessRecord, session_id: str) -> ChatSession | None:
        with self._lock:
            session = self.get_chat_session(record, session_id)
            if session is None:
                return None
            self._retire_open_questions_for_chat(record, session_id)
            record.messages = [item for item in record.messages if item.session_id != session_id]
            record.runs = [item for item in record.runs if item.session_id != session_id]
            session.updated_at = now()
            self.save(record)
            return session

    def delete_chat_session(self, record: BusinessRecord, session_id: str) -> ChatSession | None:
        with self._lock:
            session = self.get_chat_session(record, session_id)
            if session is None:
                return None
            self._retire_open_questions_for_chat(record, session_id)
            record.chat_sessions = [item for item in record.chat_sessions if item.id != session_id]
            record.messages = [item for item in record.messages if item.session_id != session_id]
            record.runs = [item for item in record.runs if item.session_id != session_id]
            if not record.chat_sessions:
                ts = now()
                record.chat_sessions.append(
                    ChatSession(
                        id=new_id("chat"),
                        business_id=record.id,
                        created_at=ts,
                        updated_at=ts,
                    )
                )
            self.save(record)
            return session

    def append_message(
        self,
        record: BusinessRecord,
        role: str,
        content: str,
        run_id: str | None = None,
        session_id: str | None = None,
        *,
        task_id: str = "",
        kind: str = "standard",
        progress_action: str = "",
        work_item_id: str = "",
        progress: dict[str, Any] | None = None,
        activity_events: list[dict[str, Any]] | None = None,
    ) -> ChatMessage:
        session = self.require_chat_session(record, session_id)
        message = ChatMessage(
            id=new_id("msg"),
            session_id=session.id,
            role=role,  # type: ignore[arg-type]
            content=content,
            created_at=now(),
            run_id=run_id,
            task_id=task_id,
            kind=kind,  # type: ignore[arg-type]
            progress_action=progress_action,
            work_item_id=work_item_id,
            progress=progress or {},
            activity_events=activity_events or [],
        )
        record.messages.append(message)
        session.updated_at = message.created_at
        if role == "user" and not session.title:
            session.title = _chat_session_title(content)
        return message

    def append_run(self, record: BusinessRecord, run: AIRun) -> AIRun:
        if run.session_id:
            session = self.require_chat_session(record, run.session_id)
            session.updated_at = max(session.updated_at, run.started_at)
        record.runs.append(run)
        return run

    def append_package(self, record: BusinessRecord, package: PackageRecord) -> PackageRecord:
        record.packages.append(package)
        try:
            relative = Path(package.storage_path).resolve().relative_to(
                self.workspace_dir(record.id).resolve()
            ).as_posix()
            _clear_workspace_tombstone(record, relative)
        except (OSError, ValueError):
            pass
        return package

    def create_workspace_entry(
        self,
        record: BusinessRecord,
        requested_path: str,
        kind: str,
        *,
        content: str = "",
        actor: str = "user",
    ) -> dict[str, Any]:
        with self._lock:
            workspace, target, relative_path = _workspace_target(
                self.workspace_dir(record.id), requested_path
            )
            if target == workspace:
                raise ValueError("The workspace root cannot be created.")
            if target.exists():
                raise FileExistsError(relative_path)
            if not target.parent.is_dir():
                raise FileNotFoundError(target.parent.relative_to(workspace).as_posix())
            if kind == "folder":
                target.mkdir()
            elif kind == "file":
                target.write_text(content, encoding="utf-8")
            else:
                raise ValueError("Workspace entry kind must be file or folder.")
            _clear_workspace_tombstone(record, relative_path)
            self.create_version(
                record,
                f"Created workspace {kind} {relative_path}",
                f"create_workspace_{kind}",
                actor=actor,
            )
            self.save(record)
            return {
                "path": relative_path,
                "name": target.name,
                "kind": kind,
            }

    def move_workspace_entry(
        self,
        record: BusinessRecord,
        requested_path: str,
        destination: str,
        *,
        actor: str = "user",
    ) -> dict[str, Any]:
        with self._lock:
            workspace, source, relative_path = _workspace_target(
                self.workspace_dir(record.id), requested_path
            )
            _workspace, target, destination_path = _workspace_target(
                workspace, destination
            )
            if source == workspace or target == workspace:
                raise ValueError("The workspace root cannot be moved.")
            if not source.exists():
                raise FileNotFoundError(relative_path)
            if target.exists():
                raise FileExistsError(destination_path)
            if not target.parent.is_dir():
                raise FileNotFoundError(target.parent.relative_to(workspace).as_posix())
            if source.is_dir() and source in target.parents:
                raise ValueError("A directory cannot be moved inside itself.")
            source_was_directory = source.is_dir()
            shutil.move(str(source), str(target))
            _update_registered_workspace_paths(record, source, target, workspace)
            record.workspace_deleted_paths = sorted(
                {*record.workspace_deleted_paths, relative_path}
            )
            _clear_workspace_tombstone(record, destination_path)
            if relative_path == DESCRIPTION_FILENAME:
                record.description = ""
            self.create_version(
                record,
                f"Moved workspace entry {relative_path} to {destination_path}",
                "move_workspace_entry",
                actor=actor,
            )
            self.save(record)
            return {
                "path": relative_path,
                "destination": destination_path,
                "name": target.name,
                "kind": "folder" if source_was_directory else "file",
            }

    def delete_workspace_entry(
        self,
        record: BusinessRecord,
        requested_path: str,
        *,
        recursive: bool = False,
        actor: str = "user",
    ) -> dict[str, Any] | None:
        workspace, target, relative_path = _workspace_target(
            self.workspace_dir(record.id), requested_path
        )
        if target == workspace:
            raise ValueError("The workspace root cannot be deleted.")
        if not target.exists():
            return None
        if target.is_file():
            return self.delete_workspace_file(record, relative_path, actor=actor)
        with self._lock:
            if any(target.iterdir()) and not recursive:
                raise ValueError("Directory is not empty.")
            affected_files = _registered_items_under(record.files, target)
            affected_packages = _registered_items_under(record.packages, target)
            if recursive:
                shutil.rmtree(target)
            else:
                target.rmdir()
            record.files = [item for item in record.files if item not in affected_files]
            record.packages = [item for item in record.packages if item not in affected_packages]
            removed_ids = {item.id for item in affected_files}
            record.context.source_files = [
                item for item in record.context.source_files if item.get("id") not in removed_ids
            ]
            if removed_ids:
                self.invalidate_distillation(
                    record,
                    f"Registered source files deleted from {relative_path}",
                    actor=actor,
                    source_changed=True,
                )
            record.workspace_deleted_paths = sorted(
                {*record.workspace_deleted_paths, relative_path}
            )
            self.create_version(
                record,
                f"Deleted workspace directory {relative_path}",
                "delete_workspace_directory",
                actor=actor,
                evidence_ids=sorted(removed_ids),
            )
            self.save(record)
            return {
                "path": relative_path,
                "filename": target.name,
                "kind": "folder",
                "registered_file_ids": sorted(removed_ids),
            }

    def delete_workspace_file(
        self,
        record: BusinessRecord,
        requested_path: str,
        *,
        actor: str = "user",
    ) -> dict[str, Any] | None:
        with self._lock:
            workspace = self.workspace_dir(record.id).resolve()
            normalized = requested_path.replace("\\", "/").strip("/")
            relative = Path(normalized)
            if not normalized or "\x00" in normalized or relative.is_absolute() or ".." in relative.parts:
                raise ValueError("Invalid workspace file path.")
            try:
                target = (workspace / relative).resolve()
            except (OSError, ValueError) as exc:
                raise ValueError("Invalid workspace file path.") from exc
            if workspace not in target.parents:
                raise ValueError("Invalid workspace file path.")
            if not target.is_file():
                return None

            relative_path = target.relative_to(workspace).as_posix()
            registered = next(
                (
                    item
                    for item in record.files
                    if _same_resolved_file(Path(item.storage_path), target)
                ),
                None,
            )
            package = next(
                (
                    item
                    for item in record.packages
                    if _same_resolved_file(Path(item.storage_path), target)
                ),
                None,
            )
            target.unlink()
            record.workspace_deleted_paths = sorted(
                {*record.workspace_deleted_paths, relative_path}
            )
            if registered is not None:
                record.files = [item for item in record.files if item.id != registered.id]
                record.context.source_files = [
                    item
                    for item in record.context.source_files
                    if item.get("id") != registered.id
                ]
                record.context.tool_usages = [
                    item
                    for item in record.context.tool_usages
                    if item.get("source_file_id") != registered.id
                ]
                self.invalidate_distillation(
                    record,
                    f"Registered source file deleted: {registered.filename}",
                    actor=actor,
                    source_changed=True,
                )
            if package is not None:
                record.packages = [item for item in record.packages if item.id != package.id]
            if relative_path == DESCRIPTION_FILENAME:
                record.description = ""
            evidence_ids = [registered.id] if registered is not None else []
            self.create_version(
                record,
                f"Deleted workspace file {relative_path}",
                "delete_workspace_file",
                actor=actor,
                evidence_ids=evidence_ids,
            )
            self.save(record)
            return {
                "path": relative_path,
                "filename": target.name,
                "registered_file_id": registered.id if registered is not None else None,
                "package_id": package.id if package is not None else None,
            }

    def delete_file(
        self,
        record: BusinessRecord,
        file_id: str,
        *,
        actor: str | None = None,
    ) -> Any | None:
        match = next((item for item in record.files if item.id == file_id), None)
        if match is None:
            return None
        record.files = [item for item in record.files if item.id != file_id]
        self.invalidate_distillation(
            record,
            f"Registered source file deleted: {match.filename}",
            actor=actor or record.owner_id or "system",
            source_changed=True,
        )
        storage_path = Path(match.storage_path)
        try:
            resolved = storage_path.resolve()
            workspace = self.workspace_dir(record.id).resolve()
            if resolved == workspace or workspace not in resolved.parents:
                return match
            if resolved.exists() and resolved.is_file():
                resolved.unlink()
        except OSError:
            pass
        return match

    def find_file(
        self,
        file_id: str,
        owner_id: str | None = None,
    ) -> tuple[BusinessRecord, Any] | None:
        with self._lock:
            for summary in self.list(owner_id):
                record = self.get(summary.id, owner_id)
                if record is None:
                    continue
                match = next((item for item in record.files if item.id == file_id), None)
                if match is not None:
                    return record, match
        return None

    def find_package(
        self,
        package_id: str,
        owner_id: str | None = None,
    ) -> tuple[BusinessRecord, PackageRecord] | None:
        with self._lock:
            for summary in self.list(owner_id):
                record = self.get(summary.id, owner_id)
                if record is None:
                    continue
                match = next((item for item in record.packages if item.id == package_id), None)
                if match is not None:
                    return record, match
        return None

    def to_summary(self, record: BusinessRecord) -> BusinessSummary:
        open_questions = [
            item
            for item in record.context.questions
            if item.get("status", "open") == "open"
        ]
        return BusinessSummary(
            id=record.id,
            name=record.name,
            goal=record.goal,
            description=record.description,
            status=record.status,
            created_at=record.created_at,
            updated_at=record.updated_at,
            current_version=record.current_version,
            file_count=len(record.files),
            open_question_count=len(open_questions),
            package_count=len(record.packages),
        )

    def _read(self, meta_file: Path) -> BusinessRecord:
        data = json.loads(meta_file.read_text(encoding="utf-8"))
        return BusinessRecord.model_validate(data)

    def _ensure_workspace(self, record: BusinessRecord) -> bool:
        workspace = self.workspace_dir(record.id)
        self.files_dir(record.id)
        self.context_dir(record.id)
        changed = _migrate_legacy_description_file(workspace)
        description = self.description_markdown_path(record.id)
        if not description.exists() and not _workspace_path_is_deleted(record, DESCRIPTION_FILENAME):
            description.write_text(_description_markdown(record), encoding="utf-8")
            changed = True
        return changed

    def _write_business_context(self, record: BusinessRecord) -> None:
        context_payload = record.context.model_dump(mode="json")
        # Rollback snapshots remain in the internal business record. The Agent-facing
        # workspace artifact exposes only current state and lean version metadata.
        for version in context_payload.get("versions", []):
            if isinstance(version, dict):
                version.pop("snapshot", None)
        if not _workspace_path_is_deleted(record, "context/business_context.json"):
            (self.context_dir(record.id) / "business_context.json").write_text(
                json.dumps(context_payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )


def _description_markdown(record: BusinessRecord) -> str:
    goal = record.goal or "Describe the outcome this business workspace should produce."
    description = record.description or "Write the business scenario, source context, constraints, and expected skill package here."
    return f"""# {record.name}

## Business Goal

{goal}

## Scenario Description

{description}

## Source Notes

- Add uploaded files under `data/`.
- Ask AI to analyze the workspace after the description is updated.

## Acceptance Criteria

- Business Context is traceable.
- Stage outputs are derived from evidence and written under `outputs/<task>/`.
- Do not create `deliverables/skill-package/` until the complete business scenario is validated and the user requests the final Skill package.
"""


def _append_requirement_from_description(record: BusinessRecord, content: str) -> None:
    text = content.strip()
    if not text:
        return
    existing = [item.get("text") for item in record.context.user_requirements]
    if text not in existing:
        record.context.user_requirements.append(
            {
                "id": new_id("req"),
                "text": text,
                "source": DESCRIPTION_FILENAME,
                "created_at": now(),
            }
        )


def _migrate_legacy_description_file(workspace: Path) -> bool:
    canonical = workspace / DESCRIPTION_FILENAME
    legacy = workspace / LEGACY_DESCRIPTION_FILENAME
    if not legacy.is_file():
        return False
    if not canonical.exists():
        legacy.rename(canonical)
        return True
    if canonical.is_file() and canonical.read_bytes() == legacy.read_bytes():
        legacy.unlink()
        return True
    legacy.rename(_next_legacy_description_backup(workspace))
    return True


def _next_legacy_description_backup(workspace: Path) -> Path:
    candidate = workspace / "scenario.legacy.md"
    index = 2
    while candidate.exists():
        candidate = workspace / f"scenario.legacy-{index}.md"
        index += 1
    return candidate


def _migrate_description_sources(record: BusinessRecord) -> bool:
    payload = record.context.model_dump(mode="python")
    if not _replace_legacy_description_sources(payload):
        return False
    record.context = BusinessContext.model_validate(payload)
    return True


def _replace_legacy_description_sources(value: Any) -> bool:
    changed = False
    if isinstance(value, dict):
        for key, item in value.items():
            if key == "source" and item == LEGACY_DESCRIPTION_FILENAME:
                value[key] = DESCRIPTION_FILENAME
                changed = True
            else:
                changed = _replace_legacy_description_sources(item) or changed
    elif isinstance(value, list):
        for item in value:
            changed = _replace_legacy_description_sources(item) or changed
    return changed


def _same_resolved_file(candidate: Path, target: Path) -> bool:
    try:
        return candidate.resolve() == target
    except OSError:
        return False


def _clear_workspace_tombstone(record: BusinessRecord, relative_path: str) -> None:
    normalized = relative_path.replace("\\", "/").strip("/")
    record.workspace_deleted_paths = [
        item for item in record.workspace_deleted_paths if item != normalized
    ]


def _workspace_path_is_deleted(record: BusinessRecord, relative_path: str) -> bool:
    normalized = relative_path.replace("\\", "/").strip("/")
    return any(
        normalized == item.replace("\\", "/").strip("/")
        or normalized.startswith(item.replace("\\", "/").strip("/") + "/")
        for item in record.workspace_deleted_paths
        if item.replace("\\", "/").strip("/")
    )


def _workspace_target(workspace_root: Path, requested_path: str) -> tuple[Path, Path, str]:
    workspace = workspace_root.resolve()
    normalized = str(requested_path or "").replace("\\", "/").strip("/")
    relative = Path(normalized)
    if "\x00" in normalized or relative.is_absolute() or ".." in relative.parts:
        raise ValueError("Invalid workspace path.")
    target = workspace if not normalized else (workspace / relative).resolve()
    if target != workspace and workspace not in target.parents:
        raise ValueError("Invalid workspace path.")
    relative_path = "" if target == workspace else target.relative_to(workspace).as_posix()
    return workspace, target, relative_path


def _registered_items_under(items: list[Any], root: Path) -> list[Any]:
    registered: list[Any] = []
    resolved_root = root.resolve()
    for item in items:
        try:
            path = Path(item.storage_path).resolve()
        except (OSError, ValueError):
            continue
        if path == resolved_root or resolved_root in path.parents:
            registered.append(item)
    return registered


def _update_registered_workspace_paths(
    record: BusinessRecord,
    source: Path,
    destination: Path,
    workspace: Path,
) -> None:
    resolved_source = source.resolve()
    for item in [*record.files, *record.packages]:
        try:
            current = Path(item.storage_path).resolve()
            relative = current.relative_to(resolved_source)
        except (OSError, ValueError):
            continue
        updated = (destination / relative).resolve()
        item.storage_path = str(updated)
        if hasattr(item, "workspace_path"):
            try:
                item.workspace_path = updated.relative_to(workspace.resolve()).as_posix()
            except (OSError, ValueError):
                item.workspace_path = ""
        if current == resolved_source and hasattr(item, "filename"):
            item.filename = updated.name
        if current == resolved_source and hasattr(item, "suffix"):
            item.suffix = updated.suffix.lower()


def _tree_node(path: Path, base: Path, root_name: str | None = None) -> WorkspaceNode:
    relative = "" if path == base else path.relative_to(base).as_posix()
    if path.is_dir():
        children = [
            _tree_node(child, base)
            for child in sorted(path.iterdir(), key=_sort_key)
            if child.name != "_field-evidence"
        ]
        return WorkspaceNode(
            name=root_name or path.name,
            path=relative,
            kind="folder",
            icon=_folder_icon(path.name),
            children=children,
        )
    return WorkspaceNode(
        name=path.name,
        path=relative,
        kind="file",
        icon=_file_icon(path.suffix.lower(), path.name),
        size=path.stat().st_size,
    )


def _sort_key(path: Path) -> tuple[int, str]:
    return (0 if path.is_dir() else 1, path.name.lower())


def _folder_icon(name: str) -> str:
    return {
        "data": "database",
        "context": "brain",
        "deliverables": "package",
        "skill-package": "package",
    }.get(name, "folder")


def _file_icon(suffix: str, name: str) -> str:
    if name == DESCRIPTION_FILENAME:
        return "scenario"
    if suffix in {".md", ".markdown"}:
        return "markdown"
    if suffix in {".csv", ".tsv", ".xlsx", ".xls", ".parquet"}:
        return "table"
    if suffix in {".json", ".jsonl", ".ndjson", ".yaml", ".yml"}:
        return "json"
    if suffix in {".mmd"}:
        return "graph"
    if suffix in {".zip", ".tar", ".tgz", ".gz", ".bz2", ".xz"}:
        return "package"
    if suffix in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg"}:
        return "image"
    if suffix in {".sqlite", ".sqlite3", ".db"}:
        return "database"
    if suffix in {".mp4", ".webm", ".mov", ".m4v"}:
        return "video"
    if suffix in {".mp3", ".wav", ".ogg", ".m4a", ".flac"}:
        return "audio"
    if suffix in {".pdf", ".docx", ".pptx"}:
        return "document"
    return "file"


def _safe_filename(filename: str) -> str:
    cleaned = filename.replace("\\", "_").replace("/", "_").strip()
    cleaned = re.sub(r"[\x00-\x1f]+", "_", cleaned)
    return cleaned[:180] or "upload.bin"


def _ensure_chat_sessions(record: BusinessRecord) -> bool:
    """Migrate legacy chat history without assigning background analysis runs."""

    changed = False
    sessions_by_id = {item.id: item for item in record.chat_sessions}
    referenced_ids = {
        session_id
        for session_id in [
            *(message.session_id for message in record.messages),
            *(run.session_id for run in record.runs),
        ]
        if session_id
    }
    for session_id in referenced_ids - sessions_by_id.keys():
        timestamps = [
            message.created_at
            for message in record.messages
            if message.session_id == session_id
        ] + [
            run.started_at
            for run in record.runs
            if run.session_id == session_id
        ]
        created_at = min(timestamps) if timestamps else record.created_at
        updated_at = max(timestamps) if timestamps else created_at
        session = ChatSession(
            id=session_id,
            business_id=record.id,
            created_at=created_at,
            updated_at=updated_at,
        )
        record.chat_sessions.append(session)
        sessions_by_id[session_id] = session
        changed = True

    if not record.chat_sessions:
        timestamps = [message.created_at for message in record.messages] + [
            run.started_at for run in record.runs if run.id in {message.run_id for message in record.messages}
        ]
        created_at = min(timestamps) if timestamps else record.created_at
        updated_at = max(timestamps) if timestamps else created_at
        session = ChatSession(
            id=new_id("chat"),
            business_id=record.id,
            created_at=created_at,
            updated_at=updated_at,
        )
        record.chat_sessions.append(session)
        sessions_by_id[session.id] = session
        changed = True

    default_session = min(record.chat_sessions, key=lambda item: item.created_at)
    runs_by_id = {run.id: run for run in record.runs}
    for message in record.messages:
        if message.session_id:
            continue
        linked_run = runs_by_id.get(message.run_id or "")
        message.session_id = linked_run.session_id if linked_run and linked_run.session_id else default_session.id
        changed = True

    message_sessions_by_run = {
        message.run_id: message.session_id
        for message in record.messages
        if message.run_id and message.session_id
    }
    for run in record.runs:
        if run.session_id or run.id not in message_sessions_by_run:
            continue
        run.session_id = message_sessions_by_run[run.id]
        changed = True

    for session in record.chat_sessions:
        if session.business_id != record.id:
            session.business_id = record.id
            changed = True
        messages = [item for item in record.messages if item.session_id == session.id]
        runs = [item for item in record.runs if item.session_id == session.id]
        timestamps = [item.created_at for item in messages] + [item.started_at for item in runs]
        if timestamps:
            created_at = min(timestamps)
            updated_at = max(
                [*timestamps, *(run.finished_at for run in runs if run.finished_at is not None)]
            )
            if created_at < session.created_at:
                session.created_at = created_at
                changed = True
            if updated_at > session.updated_at:
                session.updated_at = updated_at
                changed = True
        if not session.title:
            first_user_message = next((item.content for item in messages if item.role == "user"), "")
            title = _chat_session_title(first_user_message)
            if title:
                session.title = title
                changed = True
    return changed


def _chat_session_title(content: str) -> str:
    return re.sub(r"\s+", " ", content).strip()[:48]


def _sanitize_legacy_runtime_state(record: BusinessRecord) -> bool:
    changed = False
    fake_markers = ("本轮已更新 Business Context", "当前建议先确认这些问题")
    clean_messages = [
        message
        for message in record.messages
        if not (message.role == "assistant" and all(marker in message.content for marker in fake_markers))
    ]
    if len(clean_messages) != len(record.messages):
        record.messages = clean_messages
        changed = True
    for message in record.messages:
        if message.role == "assistant":
            cleaned = strip_thinking_markup(message.content)
            if cleaned != message.content:
                message.content = cleaned
                changed = True

    real_skills = installed_skill_names(record.owner_id)
    clean_skill_refs = [item for item in record.context.skill_references if item.get("name") in real_skills]
    if len(clean_skill_refs) != len(record.context.skill_references):
        record.context.skill_references = clean_skill_refs
        changed = True
    return changed


store = StudioStore()
