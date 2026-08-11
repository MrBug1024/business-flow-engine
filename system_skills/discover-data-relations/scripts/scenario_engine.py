#!/usr/bin/env python3
"""Build and validate one scenario-level relationship graph from bounded evidence.

The progressive engine remains a low-level evidence probe. This module distills its
results plus document statements and table structure into bounded evidence cards.
An Agent synthesizes semantic claims from those cards, and this module validates
that the result is one connected, evidence-backed main chain with attached branches.
"""

from __future__ import annotations

import argparse
from copy import deepcopy
import hashlib
import hmac
import itertools
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
from trace_engine import build_trace_samples, compact_trace_report
from trace_review import (
    ReviewError,
    atomic_json as atomic_review_json,
    load_approved_review,
    load_json as load_review_json,
    make_corrections,
    micro_process_template,
    normalized_overrides,
    review_template,
    validate_review,
)


SCHEMA_VERSION = 1
PLATFORM_APPROVAL_ISSUER = "business-flow-platform"
PLATFORM_APPROVAL_KEY_ENV = "BUSINESS_FLOW_PLATFORM_APPROVAL_HMAC_KEY"
ROLE_MANIFEST_KIND = "approved_role_manifest"
MAX_SNIPPET = 320
MAX_CARD_STATEMENT = 480
MAX_EVIDENCE_PAGE = 20
MAX_BRIEF_CARDS = 40
MAX_BRIEF_STATEMENTS = 8
MAX_SCENARIO_NODES = 10
MAX_SCENARIO_EDGES = 14
MAX_SCENARIO_BRANCHES = 3
LARGE_TABULAR_ROWS = 50_000
MATERIAL_SOURCE_NODE_TYPES = {"actor", "input", "object", "rule", "state", "output"}
EXTERNAL_CAPABILITY_MARKERS = (
    "knowledge base", "knowledge-base", "vector kb", "vector-kb", "external knowledge",
    "web search", "crawler", "scraper", "remote api", "external api",
    "知识库", "外部知识", "药品知识", "政策知识", "规范知识", "网络检索", "爬虫",
    "外部接口", "远程接口", "第三方接口",
)
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
        {"trigger", "input", "object", "activity", "state", "system"},
        {"activity", "decision", "system", "object", "output"},
    ),
    "joins_with": ({"input", "object", "state"}, {"input", "object", "state"}),
    "governs": ({"rule"}, {"activity", "decision", "system", "object", "output"}),
    "derives": (
        {"input", "object", "activity", "decision", "system", "state"},
        {"decision", "object", "state", "output"},
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
    "不得", "禁止", "必须", "应当", "不可", "对应",
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
        "人员", "职工", "操作人", "经办人", "机构", "组织", "部门", "员工",
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


def parse_trace_anchor_selector(raw: Any) -> dict[str, Any] | None:
    """Decode the one explicit result-row selector accepted by tracing.

    The trace engine owns endpoint and row validation because it has the table
    inventory.  This boundary only prevents a CLI/API string from silently
    being ignored or interpreted as an arbitrary object.
    """

    if raw is None:
        return None
    if isinstance(raw, dict):
        return dict(raw)
    text = str(raw).strip()
    if not text:
        return None
    try:
        selector = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(
            "--trace-anchor-selector must be a JSON object such as "
            '{"file":"results.csv","table":"Sheet1","row_number":17}'
        ) from exc
    if not isinstance(selector, dict):
        raise ValueError("--trace-anchor-selector must decode to a JSON object")
    return selector


def trace_anchor_selector_example(selection: dict[str, Any]) -> dict[str, Any]:
    """Return one redacted candidate selector without inventing a row."""

    for candidate in selection.get("candidates", []):
        if not isinstance(candidate, dict):
            continue
        file_name = str(candidate.get("file", "")).strip()
        table_name = str(candidate.get("table", "")).strip()
        rows = candidate.get("preview_rows") if isinstance(candidate.get("preview_rows"), list) else []
        row = next((item for item in rows if isinstance(item, dict)), None)
        row_number = row.get("row_number") if isinstance(row, dict) else None
        if file_name and table_name and isinstance(row_number, int) and row_number > 0:
            return {"file": file_name, "table": table_name, "row_number": row_number}
    return {}


def trace_anchor_selection_review(
    trace_path: Path, trace_report: dict[str, Any],
) -> dict[str, Any]:
    """Create a review-surface handoff when a result row must be selected.

    This is deliberately not a ``trace_review``: no relationship review is
    possible before a single business instance has been designated.
    """

    selection = trace_report.get("anchor_selection")
    if not isinstance(selection, dict) or selection.get("status") != "selection_required":
        raise ValueError("A selection handoff can only be created for selection_required traces")
    selector_example = trace_anchor_selector_example(selection)
    role_resolution_required = selection.get("resolution") == "role_correction_required"
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": "trace_anchor_selection",
        "status": "selection_required",
        "created_at": utc_now(),
        "trace": {
            "artifact": str(trace_path.resolve()),
            "fingerprint": file_sha256(trace_path),
            "field_evidence_fingerprint": str(trace_report.get("field_evidence_fingerprint", "")),
            "strategy": str(trace_report.get("strategy", "")),
        },
        "role_manifest": trace_report.get("role_manifest", {}),
        "anchor_selection": selection,
        "review_surface": {
            "candidates": selection.get("candidates", []),
            "selector_schema": selection.get("selector_schema", {}),
            "instruction": (
                "Correct the approved roles so exactly one physical result source remains. "
                "A document result will then be anchored only at an exact-value-backed segment."
                if role_resolution_required else
                "Choose one result row that represents the business outcome to explain. "
                "Do not combine independently sampled rows from different files."
            ),
        },
        "next_action": {
            "command": "confirm_file_roles" if role_resolution_required else "analyze",
            "cli_option": "" if role_resolution_required else "--trace-anchor-selector",
            "selector_example": {} if role_resolution_required else selector_example,
            "instruction": (
                "Keep exactly one file or table assigned as result, confirm the corrected roles, and rerun tracing."
                if role_resolution_required else
                "Rerun analyze with exactly one JSON selector containing file, table, and row_number. "
                "Only then can relation tracing and review continue."
            ),
        },
    }


def platform_approvals_path(review_path: Path) -> Path:
    """Return the platform-owned approval ledger adjacent to a review."""

    return review_path.resolve().parent / "platform-approvals.json"


def platform_approval_signing_payload(envelope: dict[str, Any]) -> bytes:
    """Canonical bytes signed by the platform, excluding the signature itself."""

    payload = {name: value for name, value in envelope.items() if name != "signature"}
    return json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")


def platform_approval_signature(envelope: dict[str, Any], key: str) -> str:
    """Compute the HMAC used by the server-side platform approval worker."""

    return hmac.new(
        key.encode("utf-8"), platform_approval_signing_payload(envelope), hashlib.sha256,
    ).hexdigest()


def platform_approval_errors(
    review_path: Path, review: dict[str, Any], trace_path: Path,
) -> list[str]:
    """Verify a server-side approval envelope; free-text reviewers never pass.

    The CLI intentionally has no command that can issue this envelope.  In a
    deployed workbench, only the platform approval worker holds the HMAC key.
    If that verifier is absent, the local workflow remains fail-closed.
    """

    ledger_path = platform_approvals_path(review_path)
    if not ledger_path.is_file():
        return [
            "Platform-signed trace approval is required; platform-approvals.json is missing. "
            "A free-text reviewer is not a trusted approval."
        ]
    try:
        ledger = load_review_json(ledger_path)
    except ReviewError as exc:
        return [f"Platform approval ledger is unreadable: {exc}"]
    if (
        ledger.get("schema_version") != SCHEMA_VERSION
        or ledger.get("kind") != "platform_approval_envelopes"
        or ledger.get("issuer") != PLATFORM_APPROVAL_ISSUER
    ):
        return ["Platform approval ledger has an unsupported issuer or schema"]
    approvals = ledger.get("approvals")
    if not isinstance(approvals, list):
        return ["Platform approval ledger must contain an approvals array"]
    key = os.environ.get(PLATFORM_APPROVAL_KEY_ENV, "")
    if not key:
        return [
            f"Platform approval verifier is unavailable: {PLATFORM_APPROVAL_KEY_ENV} is not configured. "
            "Standalone CLI is fail-closed."
        ]
    review_fingerprint = file_sha256(review_path)
    trace_fingerprint = file_sha256(trace_path)
    trace_reference = review.get("trace") if isinstance(review.get("trace"), dict) else {}
    if str(trace_reference.get("fingerprint", "")) != trace_fingerprint:
        return ["Trace review does not bind to the current trace artifact fingerprint"]
    matching = []
    for envelope in approvals:
        if not isinstance(envelope, dict):
            continue
        if (
            envelope.get("schema_version") != SCHEMA_VERSION
            or envelope.get("issuer") != PLATFORM_APPROVAL_ISSUER
            or envelope.get("artifact_kind") != "trace_review"
            or envelope.get("decision") != "approved"
            or str(envelope.get("artifact_fingerprint", "")) != review_fingerprint
            or str(envelope.get("trace_fingerprint", "")) != trace_fingerprint
            or not str(envelope.get("approval_id", "")).strip()
            or not str(envelope.get("subject", "")).strip()
            or not str(envelope.get("issued_at", "")).strip()
        ):
            continue
        signature = str(envelope.get("signature", "")).strip().casefold()
        expected = platform_approval_signature(envelope, key).casefold()
        if hmac.compare_digest(signature, expected):
            matching.append(envelope)
    if not matching:
        return [
            "No valid platform-signed approval matches the current trace-review and trace artifacts; "
            "free-text reviewer values are not accepted."
        ]
    return []


def platform_approval_block(args: argparse.Namespace) -> dict[str, Any]:
    """Explain why standalone commands cannot self-approve a gate."""

    command = str(getattr(args, "command", ""))
    path_value = getattr(args, "review", "") if command == "trace-review-approve" else getattr(args, "micro_process", "")
    artifact = str(Path(path_value).resolve()) if path_value else ""
    return {
        "status": "blocked_platform_approval_required",
        "artifact": artifact,
        "message": (
            "Standalone CLI cannot mark a trace review or micro-process as approved. "
            "Submit the pending artifact to the platform approval service."
        ),
        "next_action": {
            "approval_ledger": str(platform_approvals_path(Path(getattr(args, "review", artifact or ".")).resolve())),
            "issuer": PLATFORM_APPROVAL_ISSUER,
            "verification_key_environment": PLATFORM_APPROVAL_KEY_ENV,
        },
    }


def _normalized_role_path(value: Any) -> str:
    return str(value or "").replace("\\", "/").strip("/")


def _role_manifest_source_fingerprint(manifest: dict[str, Any]) -> str:
    payload = {
        "source_revision": manifest.get("source_revision"),
        "sources": manifest.get("sources", []),
    }
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _role_manifest_semantic_fingerprint(manifest: dict[str, Any]) -> str:
    roles = manifest.get("roles") if isinstance(manifest.get("roles"), list) else []
    normalized_roles = []
    for role in roles:
        if not isinstance(role, dict):
            continue
        normalized_roles.append({
            "file_id": role.get("file_id"),
            "file": role.get("file"),
            "source_sha256": role.get("source_sha256"),
            "table": role.get("table"),
            "role": role.get("role"),
            "note": role.get("note"),
        })
    payload = {
        "source_revision": manifest.get("source_revision"),
        "source_fingerprint": manifest.get("source_fingerprint"),
        "roles": normalized_roles,
    }
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def load_authoritative_role_manifest(
    raw_path: str | Path,
    input_root: Path,
    field_result: dict[str, Any],
) -> tuple[dict[str, Any], dict[tuple[str, str], str], dict[str, Any]]:
    """Verify the platform-signed role contract against the actual input bytes.

    A table role is business truth only when its manifest is signed by the
    platform *and* its source snapshot still matches the bytes under analysis.
    This prevents a stale "result" designation from being reused after an
    upload changes, and removes heuristic role guesses from anchor selection.
    """

    path = Path(raw_path).resolve()
    if not path.is_file():
        raise ValueError(f"Approved role manifest is missing: {path}")
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"Approved role manifest is unreadable: {path}") from exc
    if not isinstance(manifest, dict):
        raise ValueError("Approved role manifest must be a JSON object")
    required = (
        manifest.get("schema_version") == SCHEMA_VERSION
        and manifest.get("kind") == ROLE_MANIFEST_KIND
        and manifest.get("status") == "approved"
        and manifest.get("issuer") == PLATFORM_APPROVAL_ISSUER
        and isinstance(manifest.get("source_revision"), int)
        and isinstance(manifest.get("sources"), list)
        and isinstance(manifest.get("roles"), list)
        and bool(str(manifest.get("source_fingerprint", "")))
        and bool(str(manifest.get("role_manifest_fingerprint", "")))
    )
    if not required:
        raise ValueError("Approved role manifest has an unsupported schema or is not approved")
    key = os.environ.get(PLATFORM_APPROVAL_KEY_ENV, "")
    if not key:
        raise ValueError(
            f"Cannot verify approved role manifest: {PLATFORM_APPROVAL_KEY_ENV} is not configured."
        )
    signature = str(manifest.get("signature", "")).strip().casefold()
    expected_signature = platform_approval_signature(manifest, key).casefold()
    if not hmac.compare_digest(signature, expected_signature):
        raise ValueError("Approved role manifest has no valid platform signature")
    if manifest.get("source_fingerprint") != _role_manifest_source_fingerprint(manifest):
        raise ValueError("Approved role manifest source fingerprint is invalid")
    if manifest.get("role_manifest_fingerprint") != _role_manifest_semantic_fingerprint(manifest):
        raise ValueError("Approved role manifest role fingerprint is invalid")

    sources_by_id: dict[str, dict[str, Any]] = {}
    source_by_alias: dict[str, set[str]] = defaultdict(set)
    for source in manifest["sources"]:
        if not isinstance(source, dict):
            raise ValueError("Approved role manifest contains an invalid source entry")
        source_id = str(source.get("file_id", "")).strip()
        source_hash = str(source.get("sha256", "")).strip()
        aliases = source.get("aliases") if isinstance(source.get("aliases"), list) else []
        aliases = [source.get("file", ""), *aliases]
        normalized_aliases = {_normalized_role_path(item) for item in aliases if _normalized_role_path(item)}
        if not source_id or not source_hash or not normalized_aliases or source_id in sources_by_id:
            raise ValueError("Approved role manifest source entries need unique file_id, aliases, and sha256")
        sources_by_id[source_id] = source
        for alias in normalized_aliases:
            source_by_alias[alias].add(source_id)

    field_path_to_source: dict[str, str] = {}
    field_raw_paths: dict[str, str] = {}
    seen_source_ids: set[str] = set()
    for file_info in field_result.get("files", []):
        if not isinstance(file_info, dict):
            continue
        file_path = _normalized_role_path(file_info.get("path", ""))
        if not file_path:
            raise ValueError("Field evidence contains a source without a relative path")
        source_ids = source_by_alias.get(file_path, set())
        if len(source_ids) != 1:
            raise ValueError(
                f"Field evidence source {file_path} is not uniquely bound by the approved role manifest"
            )
        source_id = next(iter(source_ids))
        source = sources_by_id[source_id]
        local_path = (input_root / file_path).resolve()
        if not local_path.is_file():
            raise ValueError(f"Field evidence source is missing under the analysis input root: {file_path}")
        if file_sha256(local_path) != str(source.get("sha256", "")):
            raise ValueError(f"Field evidence source content changed since role approval: {file_path}")
        if file_path in field_raw_paths and field_raw_paths[file_path] != str(file_info.get("path", "")):
            raise ValueError(f"Field evidence contains ambiguous source path aliases: {file_path}")
        field_path_to_source[file_path] = source_id
        field_raw_paths[file_path] = str(file_info.get("path", ""))
        seen_source_ids.add(source_id)
    if seen_source_ids != set(sources_by_id):
        missing = sorted(set(sources_by_id) - seen_source_ids)
        raise ValueError(
            "Approved role manifest and analysis input differ; source ids absent from input: " + ", ".join(missing)
        )

    table_endpoints = {
        (str(file_info.get("path", "")), str(table.get("table_name", "")))
        for file_info in field_result.get("files", [])
        if isinstance(file_info, dict)
        for table in file_info.get("tables", [])
        if isinstance(table, dict)
        and _normalized_role_path(file_info.get("path", ""))
        and str(table.get("table_name", ""))
    }
    explicit_roles: dict[tuple[str, str], str] = {}
    file_default_roles: dict[str, str] = {}
    for role_entry in manifest["roles"]:
        if not isinstance(role_entry, dict):
            raise ValueError("Approved role manifest contains an invalid role entry")
        source_id = str(role_entry.get("file_id", "")).strip()
        table_name = str(role_entry.get("table", "")).strip()
        role = str(role_entry.get("role", "")).strip()
        if source_id not in sources_by_id or not table_name or role not in {
            "input", "result", "rule", "reference", "template", "ignore",
        }:
            raise ValueError("Approved role manifest contains an unsupported role assignment")
        source = sources_by_id[source_id]
        if str(role_entry.get("source_sha256", "")) != str(source.get("sha256", "")):
            raise ValueError("Approved role manifest role entry is not bound to its source hash")
        matching_paths = [
            path for path, matched_source_id in field_path_to_source.items() if matched_source_id == source_id
        ]
        if len(matching_paths) != 1:
            raise ValueError("Approved role manifest source mapping is ambiguous")
        if table_name == "__file__":
            if source_id in file_default_roles:
                raise ValueError("Approved role manifest assigns more than one file-level role to a source")
            file_default_roles[source_id] = role
            continue
        endpoint = (field_raw_paths[matching_paths[0]], table_name)
        if endpoint not in table_endpoints:
            raise ValueError(
                f"Approved role manifest refers to a table not found in current field evidence: {endpoint[0]} / {table_name}"
            )
        if endpoint in explicit_roles:
            raise ValueError(f"Approved role manifest assigns more than one role to {endpoint[0]} / {table_name}")
        explicit_roles[endpoint] = role

    # ``__file__`` is an authoritative role in its own right.  For a tabular
    # source it is also the default for discovered tables, while for a PDF,
    # Word document, Markdown file or image it remains a file-scoped role and
    # must never be converted into an imaginary table endpoint.
    resolved_file_roles: dict[str, str] = {}
    for normalized_path, source_id in field_path_to_source.items():
        role = file_default_roles.get(source_id)
        if role:
            resolved_file_roles[field_raw_paths[normalized_path]] = role
    authoritative_roles: dict[tuple[str, str], str] = {}
    for endpoint in table_endpoints:
        normalized_path = _normalized_role_path(endpoint[0])
        source_id = field_path_to_source.get(normalized_path, "")
        role = explicit_roles.get(endpoint) or file_default_roles.get(source_id)
        if role:
            authoritative_roles[endpoint] = role
    unassigned = sorted(table_endpoints - set(authoritative_roles))
    if unassigned:
        rendered = ", ".join(f"{path} / {table}" for path, table in unassigned[:8])
        raise ValueError(f"Every discovered table needs a current approved role; missing: {rendered}")
    table_files = {path for path, _table in table_endpoints}
    non_tabular_paths = {
        str(file_info.get("path", ""))
        for file_info in field_result.get("files", [])
        if isinstance(file_info, dict)
        and str(file_info.get("path", ""))
        and str(file_info.get("path", "")) not in table_files
    }
    missing_file_roles = sorted(non_tabular_paths - set(resolved_file_roles))
    if missing_file_roles:
        raise ValueError(
            "Every non-tabular source needs a current approved __file__ role; missing: "
            + ", ".join(missing_file_roles[:8])
        )
    non_tabular_result_files = {
        path for path in non_tabular_paths if resolved_file_roles.get(path) == "result"
    }
    if "result" not in set(authoritative_roles.values()) and not non_tabular_result_files:
        raise ValueError("Approved role manifest has no current table or non-tabular file assigned the result role")
    reference = {
        "artifact": str(path),
        "artifact_fingerprint": file_sha256(path),
        "fingerprint": str(manifest.get("role_manifest_fingerprint", "")),
        "source_revision": manifest.get("source_revision"),
        "source_fingerprint": str(manifest.get("source_fingerprint", "")),
        "file_roles": dict(sorted(resolved_file_roles.items())),
    }
    return manifest, authoritative_roles, reference


def reusable_trace_role_authority_errors(
    trace_report: dict[str, Any],
    role_manifest_reference: dict[str, Any],
    authoritative_roles: dict[tuple[str, str], str],
) -> list[str]:
    """Reject a reusable trace unless it was made under this role authority.

    ``--trace-file`` is a performance/resume feature, never permission to
    replay an old heuristic trace.  The anchor inside a reusable trace must
    still be a table that the current signed manifest calls ``result``.
    """

    bound_manifest = trace_report.get("role_manifest")
    if not isinstance(bound_manifest, dict):
        return ["Reusable trace does not bind to a current approved role manifest"]
    bound_keys = (
        "fingerprint",
        "artifact_fingerprint",
        "source_revision",
        "source_fingerprint",
    )
    if any(
        bound_manifest.get(key) != role_manifest_reference.get(key)
        for key in bound_keys
    ):
        return ["Reusable trace role manifest does not match the current approved source snapshot"]
    approved_file_roles = (
        role_manifest_reference.get("file_roles")
        if isinstance(role_manifest_reference.get("file_roles"), dict)
        else {}
    )
    for bundle in trace_report.get("bundles", []):
        if not isinstance(bundle, dict):
            continue
        anchor = bundle.get("anchor") if isinstance(bundle.get("anchor"), dict) else {}
        endpoint = (str(anchor.get("path", "")), str(anchor.get("table", "")))
        approved_role = (
            str(approved_file_roles.get(endpoint[0], ""))
            if anchor.get("kind") == "document_segment"
            else authoritative_roles.get(endpoint)
        )
        if approved_role != "result":
            return [
                "Reusable trace anchor is approved as "
                f"{approved_role or 'unassigned'}; only a current result table or document may anchor tracing"
            ]
    return []


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


def table_cards(
    field_result: dict[str, Any], authoritative_roles: dict[tuple[str, str], str] | None = None,
) -> list[dict[str, Any]]:
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
            approved_role = (
                authoritative_roles.get((path, str(table.get("table_name", ""))))
                if authoritative_roles is not None else ""
            )
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
                (
                    f"{path} / {table.get('table_name', '')} is approved as {approved_role}; "
                    f"its structural profile resembles {role}."
                    if approved_role else
                    f"{path} / {table.get('table_name', '')} structurally resembles {role}; "
                    "column roles are grouped without reading data rows."
                ),
                [{"file": path, "locator": locator}],
                {
                    "table": table.get("table_name", ""),
                    "estimated_rows": table.get("row_count"),
                    "column_count": table.get("column_count", 0),
                    "inferred_material_role": role,
                    "approved_role": approved_role,
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
                        "approved_role": approved_role,
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
        int(table.get("header_row", 0)),
        float(table.get("header_confidence", 1.0)),
        str(table.get("header_detection", "default_first_row")),
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
    return tokens - {"同时", "收取", "规则", "结果"}


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


def trace_evidence_cards(trace_report: dict[str, Any]) -> list[dict[str, Any]]:
    """Expose each coherent trace bundle as one bounded, citable evidence card."""

    cards: list[dict[str, Any]] = []
    for bundle in trace_report.get("bundles", []):
        if not isinstance(bundle, dict):
            continue
        sources = []
        bounded_rows = []
        bounded_segments = []
        for source in bundle.get("sources", []):
            if not isinstance(source, dict):
                continue
            rows = [item for item in source.get("rows", []) if isinstance(item, dict)]
            first = rows[0] if rows else {}
            segment = source.get("segment") if isinstance(source.get("segment"), dict) else {}
            locator = str(segment.get("locator", "")) if segment else (
                f"table:{source.get('table', '')};row:{first.get('row_number', '')}"
                if first else f"table:{source.get('table', '')}"
            )
            sources.append({"file": str(source.get("path", "")), "locator": locator})
            if first:
                bounded_rows.append({
                    "file": str(source.get("path", "")),
                    "table": str(source.get("table", "")),
                    "role": str(source.get("role", "")),
                    "row_number": first.get("row_number"),
                    "values": dict(list(first.get("values", {}).items())[:12]),
                })
            if segment:
                bounded_segments.append({
                    "file": str(source.get("path", "")),
                    "role": str(source.get("role", "")),
                    "locator": str(segment.get("locator", "")),
                    "source_digest": str(segment.get("source_digest", "")),
                    "segment_digest": str(segment.get("segment_digest", "")),
                    "value_fingerprint": str(segment.get("value_fingerprint", "")),
                    "value_preview": str(segment.get("value_preview", "")),
                    "snippet": compact_text(segment.get("snippet", ""), 320),
                })
        links = [item for item in bundle.get("links", []) if isinstance(item, dict)]
        anchor = bundle.get("anchor") if isinstance(bundle.get("anchor"), dict) else {}
        cards.append(make_card(
            "record_trace",
            "direct",
            (
                f"One result-anchored business instance from {anchor.get('path', '')} was traced "
                f"to {max(0, len(sources) - 1)} related source(s) with {len(links)} exact key link(s)."
            ),
            sources,
            {
                "bundle_id": str(bundle.get("bundle_id", "")),
                "anchor_file": str(anchor.get("path", "")),
                "anchor_table": str(anchor.get("table", "")),
                "anchor_kind": str(anchor.get("kind", "table_row")),
                "anchor_locator": str(anchor.get("locator", "")),
                "anchor_source_digest": str(anchor.get("source_digest", "")),
                "anchor_segment_digest": str(anchor.get("segment_digest", "")),
                "source_files": [item["file"] for item in sources],
                "key_paths": [
                    {
                        "source_file": item.get("source_file", ""),
                        "target_file": item.get("target_file", ""),
                        "key_pairs": item.get("key_pairs", []),
                        "relation_ids": item.get("relation_ids", []),
                        "confidence": item.get("confidence", 0),
                        "matched_row_count": item.get("matched_row_count", 0),
                    }
                    for item in links
                ],
                "bounded_rows": bounded_rows,
                "bounded_segments": bounded_segments,
                "semantic_context": [
                    {
                        "path": str(item.get("path", "")),
                        "locator": str(item.get("locator", "")),
                        "approved_role": str(item.get("approved_role", "")),
                        "evidence_kind": str(item.get("evidence_kind", "")),
                        "source_digest": str(item.get("source_digest", "")),
                        "segment_digest": str(item.get("segment_digest", "")),
                        "snippet": compact_text(item.get("snippet", ""), 240),
                    }
                    for item in bundle.get("semantic_evidence", [])[:8]
                    if isinstance(item, dict)
                ],
                "coverage": bundle.get("coverage", {}),
            },
        ))
    return cards


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

    role_manifest_arg = str(getattr(args, "role_manifest", "")).strip()
    if not role_manifest_arg:
        raise ValueError("--role-manifest is required; tracing cannot infer an authoritative result table")
    role_manifest, authoritative_roles, role_manifest_reference = load_authoritative_role_manifest(
        role_manifest_arg, input_root, field_result,
    )
    trace_role_manifest = {**role_manifest, **role_manifest_reference}
    cards: list[dict[str, Any]] = []
    cards.extend(goal_card(Path(args.goal_file) if args.goal_file else None))
    cards.extend(table_cards(field_result, authoritative_roles))
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
    anchor_selector = parse_trace_anchor_selector(getattr(args, "trace_anchor_selector", ""))
    if args.trace_file and anchor_selector is not None:
        raise ValueError(
            "--trace-anchor-selector cannot be combined with --trace-file; omit --trace-file so tracing can "
            "generate the explicitly selected result anchor"
        )
    expected_trace_fingerprint = hashlib.sha256(
        json.dumps(field_result, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    trace_path = Path(args.trace_file).resolve() if args.trace_file else output_root / "trace-samples.json"
    review_path = output_root / "trace-review.json"
    reviewed_overrides: list[dict[str, Any]] = []
    if args.trace_review:
        correction_review_path = Path(args.trace_review).resolve()
        correction_review = load_review_json(correction_review_path)
        correction_errors = validate_review(correction_review)
        if correction_errors:
            raise ValueError("链路纠偏审阅无效：" + "；".join(correction_errors))
        if correction_review.get("status") != "revision_required":
            raise ValueError("只有标记为 revision_required 的审阅可以驱动重新追踪")
        reviewed_overrides = normalized_overrides(correction_review)
        if not reviewed_overrides:
            raise ValueError("重新追踪需要至少一条经说明的字段/复合键纠偏")
    if args.trace_file:
        trace_report = json.loads(trace_path.read_text(encoding="utf-8"))
        if trace_report.get("field_evidence_fingerprint") != expected_trace_fingerprint:
            raise ValueError("Reusable trace does not match the current field evidence; rerun result-anchored tracing")
        role_authority_errors = reusable_trace_role_authority_errors(
            trace_report,
            role_manifest_reference,
            authoritative_roles,
        )
        if role_authority_errors:
            raise ValueError("; ".join(role_authority_errors))
    else:
        trace_report = build_trace_samples(
            input_root,
            field_result,
            cards,
            result_candidate_limit=args.trace_result_candidates,
            anchor_candidates=args.trace_anchor_candidates,
            max_rows_per_source=args.trace_rows_per_source,
            max_columns_per_source=args.trace_columns_per_source,
            max_hops=args.trace_max_hops,
            relation_overrides=reviewed_overrides,
            anchor_selector=anchor_selector,
            auto_first_valid_result_row=bool(
                getattr(args, "auto_first_valid_result_row", False)
            ),
            authoritative_roles=authoritative_roles,
            role_manifest=trace_role_manifest,
            document_ocr_mode=args.ocr_mode,
        )
        atomic_json(trace_path, trace_report)
    # A trace is never silently accepted.  Keep a review file next to it so
    # the workbench can show exactly which sources, fields and warnings need
    # human/AI confirmation before relationship synthesis is finalized.
    existing_review: dict[str, Any] | None = None
    if review_path.is_file():
        try:
            existing_review = load_review_json(review_path)
        except ReviewError:
            existing_review = None
    current_fingerprint = file_sha256(trace_path)
    selection_handoff: dict[str, Any] | None = None
    if trace_report.get("status") == "selection_required":
        selection_handoff = trace_anchor_selection_review(trace_path, trace_report)
        atomic_json(review_path, selection_handoff)
        existing_review = None
    elif trace_report.get("status") == "complete" and len(trace_report.get("bundles", [])) == 1:
        if not (
            existing_review
            and existing_review.get("kind") == "trace_review"
            and isinstance(existing_review.get("trace"), dict)
            and existing_review["trace"].get("fingerprint") == current_fingerprint
        ):
            generated_review = review_template(trace_path, trace_report)
            generated_review["role_manifest"] = trace_report.get("role_manifest", {})
            atomic_review_json(review_path, generated_review)
            existing_review = load_review_json(review_path)
    else:
        existing_review = None
    cards.extend(trace_evidence_cards(trace_report))
    warnings = (
        table_warnings
        + warnings
        + list(trace_report.get("quality_gates", {}).get("warnings", []))
        + list(trace_report.get("quality_gates", {}).get("blockers", []))
    )
    cards = deduplicate_and_bound(cards, args.max_evidence_cards)
    files = [str(item.get("path", "")) for item in field_result.get("files", [])]
    cards_path = output_root / "evidence-cards.json"
    card_payload = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": utc_now(),
        "input_root": str(input_root),
        "field_evidence": str(field_result_path),
        "role_manifest": role_manifest_reference,
        "trace_samples": {
            "status": trace_report.get("status", "blocked"),
            "artifact": str(trace_path),
            "fingerprint": file_sha256(trace_path),
            "compact": compact_trace_report(trace_report),
            "anchor_selection": trace_report.get("anchor_selection", {}),
        },
        "trace_review": {
            "status": (
                "selection_required" if selection_handoff else
                existing_review.get("status", "not_reviewable") if existing_review else "not_reviewable"
            ),
            "artifact": str(review_path) if (selection_handoff or existing_review) else "",
            "fingerprint": file_sha256(review_path) if (selection_handoff or existing_review) else "",
            "trace_fingerprint": current_fingerprint,
        },
        "platform_approval": {
            "artifact": str(platform_approvals_path(review_path)),
            "issuer": PLATFORM_APPROVAL_ISSUER,
            "status": "required_after_trace_review" if trace_report.get("status") == "complete" else "not_applicable",
        },
        "card_count": len(cards),
        "cards": cards,
        "warnings": warnings,
        "coverage": {
            "files": files,
            "file_count": len(files),
            "card_kinds": dict(sorted(Counter(card["kind"] for card in cards).items())),
            "guarantee": (
                "Cards contain bounded schema, localized statements, aggregated fingerprints, and only "
                "redacted rows from coherent result-anchored traces; complete tables and documents are never exposed."
            ),
        },
    }
    atomic_json(cards_path, card_payload)
    template_path = output_root / "scenario-claims.template.json"
    atomic_json(template_path, claims_template(cards_path, files))
    brief = synthesis_brief(card_payload)
    brief_path = output_root / "synthesis-brief.json"
    atomic_json(brief_path, brief)
    if trace_report.get("status") == "selection_required":
        selection = trace_report.get("anchor_selection", {})
        role_resolution_required = (
            isinstance(selection, dict)
            and selection.get("resolution") == "role_correction_required"
        )
        result = {
            "schema_version": SCHEMA_VERSION,
            "status": "selection_required",
            "ready_for_synthesis": False,
            "generated_at": utc_now(),
            "card_count": len(cards),
            "warnings": warnings,
            "anchor_selection": selection,
            "artifacts": {
                "evidence_cards": str(cards_path),
                "synthesis_brief": str(brief_path),
                "claims_template": str(template_path),
                "field_evidence": str(field_result_path),
                "trace_samples": str(trace_path),
                "trace_review": str(review_path),
            },
            "synthesis_brief": brief,
            "next_action": selection_handoff.get("next_action", {}) if role_resolution_required and selection_handoff else {
                "cli_option": "--trace-anchor-selector",
                "selector_schema": selection.get("selector_schema", {}) if isinstance(selection, dict) else {},
                "selector_example": trace_anchor_selector_example(selection) if isinstance(selection, dict) else {},
                "instruction": (
                    "Choose one candidate result row in trace-review.json, then rerun analyze with an explicit "
                    "file/table/row_number selector. Preflight and finalize are blocked until then."
                ),
            },
            "message": (
                "More than one physical result source is approved. Correct and reconfirm file roles so exactly "
                "one result remains; document results will then use only exact-value-backed segment anchors."
                if role_resolution_required else
                "A historical result table has multiple rows. No random leading-row trace was generated; "
                "one result anchor must be selected before downstream synthesis."
            ),
        }
        atomic_json(output_root / "prepare-status.json", result)
        return result
    if brief.get("status") != "ready_for_synthesis":
        result = {
            "schema_version": SCHEMA_VERSION,
            "status": "blocked_trace_required",
            "ready_for_synthesis": False,
            "generated_at": utc_now(),
            "card_count": len(cards),
            "warnings": warnings,
            "artifacts": {
                "evidence_cards": str(cards_path),
                "synthesis_brief": str(brief_path),
                "claims_template": str(template_path),
                "field_evidence": str(field_result_path),
                "trace_samples": str(trace_path),
                "trace_review": str(review_path),
            },
            "synthesis_brief": brief,
            "message": "No single result-anchored trace is available. Downstream model synthesis is forbidden until one coherent trace is established.",
        }
        atomic_json(output_root / "prepare-status.json", result)
        return result
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
            "trace_samples": str(trace_path),
            "trace_review": str(review_path),
        },
        "synthesis_brief": brief,
        "next_gate": "请审阅 trace-review.json；确认样本、复合键和警示后再生成关系 claims。",
    }
    atomic_json(output_root / "prepare-status.json", result)
    return result


def compact_evidence_card(card: dict[str, Any]) -> dict[str, Any]:
    kind = str(card.get("kind", ""))
    facts = card.get("facts") if isinstance(card.get("facts"), dict) else {}
    compact_facts: dict[str, Any]
    if kind == "scenario_goal":
        compact_facts = {"description": compact_text(facts.get("description", ""), 600)}
    elif kind == "table_schema":
        columns = facts.get("columns_by_role") if isinstance(facts.get("columns_by_role"), dict) else {}
        compact_facts = {
            "table": facts.get("table", ""),
            "estimated_rows": facts.get("estimated_rows"),
            "column_count": facts.get("column_count"),
            "inferred_material_role": facts.get("inferred_material_role", ""),
            "approved_role": facts.get("approved_role", ""),
            "columns_by_role": {
                str(role): [compact_text(value, 48) for value in values[:3]]
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
                for item in correspondences[:4]
                if isinstance(item, dict)
            ],
            "omitted_correspondence_count": int(facts.get("omitted_correspondence_count", 0))
            + max(0, len(correspondences) - 4),
            "confidence": facts.get("confidence", 0),
            "evidence_count": facts.get("evidence_count", 0),
        }
    elif kind == "record_trace":
        compact_facts = {
            "bundle_id": facts.get("bundle_id", ""),
            "anchor_file": facts.get("anchor_file", ""),
            "anchor_kind": facts.get("anchor_kind", "table_row"),
            "anchor_locator": facts.get("anchor_locator", ""),
            "anchor_source_digest": facts.get("anchor_source_digest", ""),
            "anchor_segment_digest": facts.get("anchor_segment_digest", ""),
            "source_files": list(facts.get("source_files", []))[:12],
            "key_paths": list(facts.get("key_paths", []))[:8],
            "bounded_rows": [
                {
                    **{key: row.get(key) for key in ("file", "table", "role", "row_number")},
                    "values": dict(list(row.get("values", {}).items())[:10]),
                }
                for row in facts.get("bounded_rows", [])[:8]
                if isinstance(row, dict)
            ],
            "bounded_segments": [
                {
                    key: segment.get(key)
                    for key in (
                        "file", "role", "locator", "source_digest", "segment_digest",
                        "value_fingerprint", "value_preview", "snippet",
                    )
                }
                for segment in facts.get("bounded_segments", [])[:8]
                if isinstance(segment, dict)
            ],
            "semantic_context": [
                {
                    key: context.get(key)
                    for key in (
                        "path", "locator", "approved_role", "evidence_kind",
                        "source_digest", "segment_digest", "snippet",
                    )
                }
                for context in facts.get("semantic_context", [])[:8]
                if isinstance(context, dict)
            ],
            "coverage": facts.get("coverage", {}),
        }
    elif kind == "file_structure":
        compact_facts = {
            "extension": facts.get("extension", ""),
            "table_count": facts.get("table_count", 0),
            "inventory_status": facts.get("inventory_status", "ok"),
        }
    elif kind == "table_process_signal":
        compact_facts = {
            "table": compact_text(facts.get("table", ""), 80),
            "co_located_roles": list(facts.get("co_located_roles", []))[:8],
            "inferred_material_role": facts.get("inferred_material_role", ""),
        }
    elif kind in {"goal_relation_statement", "document_relation_statement", "table_relation_statement", "material_topic_alignment"}:
        compact_facts = {
            "relation_markers": list(facts.get("relation_markers", []))[:6],
            "mentioned_files": list(facts.get("mentioned_files", []))[:4],
        }
    else:
        compact_facts = {}
    return {
        "id": card.get("id", ""),
        "kind": kind,
        "strength": card.get("strength", ""),
        "statement": compact_text(card.get("statement", ""), 280),
        "sources": list(card.get("sources", []))[:2],
        "facts": compact_facts,
        "snippet": compact_text(card.get("snippet", ""), 240),
    }


def synthesis_brief(payload: dict[str, Any]) -> dict[str, Any]:
    """Build the only evidence payload intended for model reasoning.

    Full data is searched locally to prove a path, but raw values from only
    one selected result-anchored trace may cross the model boundary.  Its
    anchor may be a table row or one digest-bound document segment.  Other
    source material is represented by bounded schema or role-scoped context.
    """
    cards = [card for card in payload.get("cards", []) if isinstance(card, dict)]

    def source_key(card: dict[str, Any]) -> tuple[str, str]:
        sources = card.get("sources") if isinstance(card.get("sources"), list) else []
        first = sources[0] if sources and isinstance(sources[0], dict) else {}
        return (str(first.get("file", "")), str(card.get("id", "")))

    trace_cards = sorted(
        (card for card in cards if card.get("kind") == "record_trace"),
        key=source_key,
    )
    if len(trace_cards) != 1:
        return {
            "schema_version": SCHEMA_VERSION,
            "status": "blocked_trace_required",
            "card_count": len(cards),
            "selected_card_count": 0,
            "omitted_card_count": len(cards),
            "reason": "Exactly one executable result-anchored record trace is required before model synthesis.",
            "next_action": "Repair or explicitly designate a result anchor, then rerun relationship tracing. Do not provide independent source rows to the model.",
        }

    trace_card = trace_cards[0]
    trace_facts = trace_card.get("facts") if isinstance(trace_card.get("facts"), dict) else {}
    trace_files = {
        str(item) for item in trace_facts.get("source_files", []) if str(item)
    }
    trace_relation_ids = {
        str(relation_id)
        for path in trace_facts.get("key_paths", []) if isinstance(path, dict)
        for relation_id in path.get("relation_ids", []) if str(relation_id)
    }
    if not trace_files:
        return {
            "schema_version": SCHEMA_VERSION,
            "status": "blocked_trace_required",
            "card_count": len(cards),
            "selected_card_count": 0,
            "omitted_card_count": len(cards),
            "reason": "The selected result trace does not identify its source scope.",
            "next_action": "Regenerate the result-anchored trace; do not fall back to independent table samples.",
        }

    def first_per_file(kind: str, source_files: set[str], limit: int) -> list[dict[str, Any]]:
        selected: list[dict[str, Any]] = []
        seen_files: set[str] = set()
        for card in sorted((item for item in cards if item.get("kind") == kind), key=source_key):
            file_name = source_key(card)[0]
            if file_name not in source_files:
                continue
            if file_name in seen_files:
                continue
            selected.append(card)
            seen_files.add(file_name)
            if len(selected) >= limit:
                break
        return selected

    trace_relationships = [
        card for card in cards
        if card.get("kind") == "field_relationship"
        and trace_relation_ids.intersection(
            str(item)
            for item in (card.get("facts", {}) if isinstance(card.get("facts"), dict) else {}).get("field_relation_ids", [])
        )
    ]
    rule_schema_cards = [
        card for card in cards
        if card.get("kind") == "table_schema"
        and str((card.get("facts", {}) if isinstance(card.get("facts"), dict) else {}).get("inferred_material_role", ""))
        == "rule_or_policy_material"
    ]
    rule_files = {source_key(card)[0] for card in rule_schema_cards if source_key(card)[0]}
    candidates = (
        sorted((card for card in cards if card.get("kind") == "scenario_goal"), key=source_key)[:1]
        + [trace_card]
        + first_per_file("file_structure", trace_files, MAX_BRIEF_CARDS)
        + first_per_file("table_schema", trace_files, len(trace_files))
        + sorted(trace_relationships, key=source_key)
        # Rule sources are a side dependency, not a historical data trace.
        # Keep schema-only facts so the downstream package can resolve a live
        # governing record without exposing any rule rows to the model here.
        + first_per_file("file_structure", rule_files, len(rule_files))
        + first_per_file("table_schema", rule_files, len(rule_files))
    )
    selected: list[dict[str, Any]] = []
    selected_ids: set[str] = set()
    for card in candidates:
        card_id = str(card.get("id", ""))
        if not card_id or card_id in selected_ids:
            continue
        selected.append(card)
        selected_ids.add(card_id)
        if len(selected) >= MAX_BRIEF_CARDS:
            break
    files = list(payload.get("coverage", {}).get("files", []))
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "ready_for_synthesis",
        "ai_input_policy": {
            "raw_value_scope": "one_selected_result_anchored_trace",
            "selected_trace_card_id": trace_card.get("id", ""),
            "selected_trace_files": sorted(trace_files),
            "permitted_non_trace_content": [
                "scenario_goal",
                "schema_metadata",
                "rule_source_schema_metadata",
                "trace_key_relationship_metadata",
                "digest_bound_rule_template_reference_context",
            ],
            "forbidden": [
                "independent_table_samples",
                "random_samples",
                "rows_from_untraced_sources",
                "raw_values_from_alternate_result_candidates",
                "semantic_similarity_as_an_exact_data_link",
            ],
        },
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
        "trace_evidence": payload.get("trace_samples", {}).get("compact", {}),
        "next_action": (
            "Write one bounded scenario-claims.candidate.json from the selected result trace and its linked "
            "schema/key metadata only. Do not query evidence cards outside the trace scope or load independent "
            "source rows; run preflight and finalize only after it is valid."
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
        selected_evidence = validate_evidence(f"Node {identifier}", node.get("evidence_ids"))
        if is_external_capability_node(node):
            if node_type != "system":
                errors.append(
                    f"Node {identifier} describes an external knowledge/API/crawler capability and must use type system"
                )
            local_structure_kinds = {
                "file_structure", "table_schema", "table_process_signal", "field_relationship",
            }
            if any(str(card.get("kind", "")) in local_structure_kinds for card in selected_evidence):
                errors.append(
                    f"Node {identifier} is an external capability and must not assign local data files as its role evidence"
                )

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
    operational = result.get("operational_contract", {})
    gates = operational.get("quality_gates", {}) if isinstance(operational, dict) else {}
    lines.extend([
        "", "## 后续执行就绪度", "",
        f"- 数据执行契约：`{operational.get('status', 'missing')}`",
        f"- 证据支持的字段链路：{gates.get('evidence_backed_link_count', 0)}",
        f"- 结果反向追踪链路：{gates.get('result_trace_link_count', 0)}",
        f"- 同锚点全量追踪样本包：{gates.get('validated_trace_bundle_count', 0)}",
    ])
    for blocker in gates.get("blockers", []):
        lines.append(f"- 阻塞：{blocker}")
    for warning in gates.get("warnings", []):
        lines.append(f"- 边界：{warning}")
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
        "operational_contract": result.get("operational_contract", {}),
        "artifacts": result.get("artifacts", {}),
    }


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(64 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def stable_source_id(path: str) -> str:
    return "src-" + hashlib.sha1(path.encode("utf-8")).hexdigest()[:10]


def is_external_capability_node(node: dict[str, Any]) -> bool:
    text = " ".join(str(node.get(key, "")) for key in ("name", "description", "type")).casefold()
    return any(marker.casefold() in text for marker in EXTERNAL_CAPABILITY_MARKERS)


def direct_node_files(node: dict[str, Any], cards_by_id: dict[str, dict[str, Any]]) -> set[str]:
    preferred_kinds = {
        "file_structure", "table_schema", "table_process_signal", "table_relation_statement",
        "document_relation_statement",
    }
    preferred: set[str] = set()
    fallback: set[str] = set()
    for evidence_id in node.get("evidence_ids", []):
        card = cards_by_id.get(str(evidence_id), {})
        if card.get("kind") == "record_trace":
            facts = card.get("facts") if isinstance(card.get("facts"), dict) else {}
            anchor_file = str(facts.get("anchor_file", ""))
            trace_files = {str(item) for item in facts.get("source_files", []) if str(item)}
            if node.get("type") == "output" and anchor_file:
                fallback.add(anchor_file)
            else:
                fallback.update(trace_files - {anchor_file})
            continue
        files = {
            str(item.get("file", ""))
            for item in card.get("sources", [])
            if isinstance(item, dict) and item.get("file")
        }
        fallback.update(files)
        if card.get("kind") in preferred_kinds:
            preferred.update(files)
    return preferred or fallback


def source_runtime_contract(roles: list[dict[str, str]]) -> dict[str, Any]:
    role_types = {str(item.get("node_type", "")) for item in roles}
    runtime_roles = role_types.intersection({"actor", "input", "object", "rule", "state"})
    if runtime_roles:
        return {
            "lifecycle": "runtime_input",
            "runtime_required": True,
            "runtime_binding": "required_when_referenced_by_stage_or_query",
            "integrity_policy": "schema_compatible_runtime_binding",
        }
    if "output" in role_types:
        return {
            "lifecycle": "design_time_template",
            "runtime_required": False,
            "runtime_binding": "not_required",
            "integrity_policy": "design_fingerprint_only",
            "template_policy": {
                "retained": ["format", "table_or_section", "header", "columns", "types", "locators"],
                "example_data": "optional_deidentified_bounded_example_only",
                "original_file_required_at_runtime": False,
            },
        }
    return {
        "lifecycle": "design_time_evidence",
        "runtime_required": False,
        "runtime_binding": "not_required",
        "integrity_policy": "design_fingerprint_only",
    }


def inferred_rule_role(path: str, file_info: dict[str, Any]) -> dict[str, str] | None:
    """Recover an omitted rule role from structural evidence.

    A user may upload a rule table without creating a dedicated relation node.
    That omission must not silently turn a searchable policy source into
    design-time evidence.  This fallback is deliberately conservative and
    domain neutral: it relies on the same material-role classifier used by
    evidence cards, rather than on a business-specific field name.
    """
    for table in file_info.get("tables", []):
        if not isinstance(table, dict):
            continue
        role_columns: dict[str, list[str]] = defaultdict(list)
        for column in table.get("columns", []):
            if not isinstance(column, dict):
                continue
            name = str(column.get("name") or column.get("query_name") or "")
            if name:
                role_columns[classify_header(name)].append(name)
        role_counts = Counter({role: len(names) for role, names in role_columns.items()})
        inferred_material_role = table_role(
            role_counts, str(file_info.get("path", path)), str(table.get("table_name", ""))
        )
        if inferred_material_role != "rule_or_policy_material":
            continue
        table_name = str(table.get("table_name", "")) or Path(path).stem
        digest = hashlib.sha1(f"inferred-rule\0{path}".encode("utf-8")).hexdigest()[:12]
        return {
            "node_id": f"inferred_rule_{digest}",
            "node_name": table_name,
            "node_type": "rule",
            "inference": "structural_material_role_profile",
            "material_role": inferred_material_role,
        }
    return None


def header_is_usable(table: dict[str, Any]) -> tuple[bool, dict[str, Any]]:
    columns = [str(item.get("name", "")) for item in table.get("columns", []) if isinstance(item, dict)]
    generic = [name for name in columns if re.fullmatch(r"(?:column_\d+|__unnamed__\d+)", name, re.IGNORECASE)]
    ratio = len(generic) / len(columns) if columns else 1.0
    confidence = float(table.get("header_confidence", 1.0))
    usable = len(columns) >= 2 and ratio <= 0.4 and confidence >= 0.5
    return usable, {
        "column_count": len(columns),
        "generic_column_ratio": round(ratio, 4),
        "header_row": int(table.get("header_row", 0)),
        "header_confidence": confidence,
        "header_detection": str(table.get("header_detection", "default_first_row")),
    }


def join_candidate_score(candidate: dict[str, Any], stats: dict[tuple[str, str], dict[str, Any]]) -> float:
    source_column = str(candidate.get("source_field", ""))
    target_column = str(candidate.get("target_field", ""))
    normalized_source = normalize_name(source_column)
    normalized_target = normalize_name(target_column)
    combined = (source_column + target_column).casefold()
    score = float(candidate.get("confidence", 0.0)) * 0.45
    if normalized_source and normalized_source == normalized_target:
        score += 0.16
    source_kind, source_base = header_semantic(source_column)
    target_kind, target_base = header_semantic(target_column)
    if source_kind in {"id", "code"} and target_kind in {"id", "code"}:
        score += 0.14
    if source_base and source_base == target_base:
        score += 0.08
    if any(term in combined for term in ("结算id", "就诊id", "医药机构结算id", "数据唯一记录号", "流水号")):
        score += 0.18
    elif any(term in combined for term in ("人员编号", "人员参保关系id", "证件号码")):
        score += 0.07
    if any(term in combined for term in ("创建人", "经办人", "创建机构", "经办机构", "统筹区", "单位编号", "病种")):
        score -= 0.2
    source_stats = stats.get((str(candidate.get("source_file", "")), source_column), {})
    target_stats = stats.get((str(candidate.get("target_file", "")), target_column), {})
    uniqueness = max(float(source_stats.get("distinct_ratio", 0.0)), float(target_stats.get("distinct_ratio", 0.0)))
    score += min(0.12, uniqueness * 0.12)
    return round(max(0.0, min(1.0, score)), 4)


def load_trace_evidence(card_payload: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Load the trace artifact only when its prepare-time fingerprint still matches."""

    claim = card_payload.get("trace_samples") if isinstance(card_payload.get("trace_samples"), dict) else {}
    if not claim:
        return {}, ["当前证据包由旧版本生成，缺少结果锚定追踪；建议重新运行 analyze"]
    try:
        path = Path(str(claim.get("artifact", ""))).resolve()
    except OSError:
        return {}, ["结果锚定追踪产物路径无效"]
    if not path.is_file():
        return {}, ["结果锚定追踪产物缺失；请重新运行 analyze"]
    if file_sha256(path) != claim.get("fingerprint"):
        return {}, ["结果锚定追踪产物 fingerprint 已变化；请重新运行 analyze"]
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}, ["结果锚定追踪产物无法读取"]
    if payload.get("status") not in {"complete", "blocked"}:
        return {}, ["结果锚定追踪产物状态无效"]
    return payload, []


def trace_review_context(
    card_payload: dict[str, Any], trace_report: dict[str, Any],
) -> tuple[dict[str, Any], list[str]]:
    """Require an explicit review that is bound to this exact trace artifact."""

    claim = card_payload.get("trace_review") if isinstance(card_payload.get("trace_review"), dict) else {}
    if not claim:
        return {}, ["缺少链路样本审阅；请先在表格与字段中确认或修正 trace-review.json"]
    try:
        review_path = Path(str(claim.get("artifact", ""))).resolve()
        trace_path = Path(str(card_payload.get("trace_samples", {}).get("artifact", ""))).resolve()
    except OSError:
        return {}, ["链路样本审阅产物路径无效"]
    if not review_path.is_file():
        return {}, ["链路样本审阅产物缺失；请重新审阅当前追踪样本"]
    actual_trace_fingerprint = file_sha256(trace_path) if trace_path.is_file() else ""
    if claim.get("trace_fingerprint") and claim.get("trace_fingerprint") != actual_trace_fingerprint:
        return {}, ["链路样本审阅声明绑定的 trace fingerprint 已变化；请重新运行 analyze"]
    try:
        review = load_approved_review(review_path, trace_path)
    except ReviewError as exc:
        return {}, [str(exc)]
    approval_errors = platform_approval_errors(review_path, review, trace_path)
    if approval_errors:
        return {}, approval_errors
    bundle_id = str(review.get("trace", {}).get("bundle_id", ""))
    if not any(str(item.get("bundle_id", "")) == bundle_id for item in trace_report.get("bundles", []) if isinstance(item, dict)):
        return {}, ["已审阅的链路样本不属于当前 trace-samples.json"]
    trace_roles = trace_report.get("role_manifest") if isinstance(trace_report.get("role_manifest"), dict) else {}
    if trace_roles:
        review_roles = review.get("role_manifest") if isinstance(review.get("role_manifest"), dict) else {}
        card_roles = card_payload.get("role_manifest") if isinstance(card_payload.get("role_manifest"), dict) else {}
        role_keys = ("fingerprint", "artifact_fingerprint", "source_revision", "source_fingerprint")
        if any(review_roles.get(key) != trace_roles.get(key) for key in role_keys):
            return {}, ["Trace review does not bind to the current approved role manifest"]
        if card_roles and any(card_roles.get(key) != trace_roles.get(key) for key in role_keys):
            return {}, ["Evidence cards do not bind to the current approved role manifest"]
    return {
        "status": "approved",
        "artifact": str(review_path),
        "fingerprint": file_sha256(review_path),
        "trace_bundle_id": bundle_id,
        "role_manifest": trace_roles,
        "accepted_warnings": review.get("approval", {}).get("accepted_warnings", []),
    }, []


def operational_trace_evidence(
    trace_report: dict[str, Any], source_by_path: dict[str, dict[str, Any]],
    output_files: set[str], links: Sequence[dict[str, Any]],
) -> dict[str, Any]:
    link_by_pair = {
        frozenset((str(item.get("source_file", "")), str(item.get("target_file", "")))): str(item.get("link_id", ""))
        for item in links if isinstance(item, dict)
    }
    bundles = []
    for bundle in trace_report.get("bundles", []):
        if not isinstance(bundle, dict):
            continue
        anchor = bundle.get("anchor") if isinstance(bundle.get("anchor"), dict) else {}
        if str(anchor.get("path", "")) not in output_files:
            continue
        normalized_sources = []
        for source in bundle.get("sources", []):
            if not isinstance(source, dict):
                continue
            path = str(source.get("path", ""))
            normalized_sources.append({
                **source,
                "source_id": source_by_path.get(path, {}).get("source_id", stable_source_id(path)),
            })
        normalized_links = []
        for item in bundle.get("links", []):
            if not isinstance(item, dict):
                continue
            source_file = str(item.get("source_file", ""))
            target_file = str(item.get("target_file", ""))
            relation_ids = "\0".join(str(value) for value in item.get("relation_ids", []) if str(value))
            link_id = link_by_pair.get(frozenset((source_file, target_file)), "") or (
                "trace-link-" + hashlib.sha1(
                    f"{source_file}\0{target_file}\0{relation_ids}".encode("utf-8")
                ).hexdigest()[:10]
            )
            normalized_links.append({
                **item,
                "link_id": link_id,
                "source_id": source_by_path.get(source_file, {}).get("source_id", stable_source_id(source_file)),
                "target_id": source_by_path.get(target_file, {}).get("source_id", stable_source_id(target_file)),
            })
        bundles.append({
            **bundle,
            "anchor": {
                **anchor,
                "source_id": source_by_path.get(str(anchor.get("path", "")), {}).get(
                    "source_id", stable_source_id(str(anchor.get("path", "")))
                ),
            },
            "sources": normalized_sources,
            "links": normalized_links,
        })
    return {
        "schema_version": trace_report.get("schema_version", 1),
        "status": "validated" if bundles else "no_validated_result_bundle",
        "strategy": trace_report.get("strategy", ""),
        "role_manifest": trace_report.get("role_manifest", {}),
        "bundles": bundles,
        "quality_gates": trace_report.get("quality_gates", {}),
    }


def build_operational_contract(
    claims: dict[str, Any], card_payload: dict[str, Any], cards_by_id: dict[str, dict[str, Any]],
    field_result: dict[str, Any], field_result_path: Path,
) -> dict[str, Any]:
    nodes = [item for item in claims.get("nodes", []) if isinstance(item, dict)]
    roles_by_file: dict[str, list[dict[str, str]]] = defaultdict(list)
    for node in nodes:
        if str(node.get("type", "")) not in MATERIAL_SOURCE_NODE_TYPES:
            continue
        for file_name in direct_node_files(node, cards_by_id):
            roles_by_file[file_name].append({
                "node_id": str(node.get("id", "")),
                "node_name": str(node.get("name", "")),
                "node_type": str(node.get("type", "")),
            })

    sources: list[dict[str, Any]] = []
    source_by_path: dict[str, dict[str, Any]] = {}
    table_by_file_column: dict[tuple[str, str], dict[str, Any]] = {}
    blockers: list[str] = []
    warnings: list[str] = []
    trace_report, trace_warnings = load_trace_evidence(card_payload)
    warnings.extend(trace_warnings)
    warnings.extend(
        str(item)
        for item in trace_report.get("quality_gates", {}).get("warnings", [])
        if str(item)
    )
    inferred_rule_paths: list[str] = []
    for file_info in field_result.get("files", []):
        if not isinstance(file_info, dict):
            continue
        path = str(file_info.get("path", ""))
        if not path or any(
            str(item.get("node_type", "")) in {"rule", "output"}
            for item in roles_by_file.get(path, [])
        ):
            continue
        inferred = inferred_rule_role(path, file_info)
        if inferred:
            roles_by_file[path].append(inferred)
            inferred_rule_paths.append(path)
    if inferred_rule_paths:
        warnings.append(
            "规则来源未显式挂接关系节点，已依据文件/表头结构恢复为可运行规则源："
            + "、".join(sorted(inferred_rule_paths))
        )
    coverage = claims.get("coverage") if isinstance(claims.get("coverage"), dict) else {}
    included_files = {
        str(item) for item in coverage.get("included_files", []) if str(item)
    } if isinstance(coverage.get("included_files"), list) else set()
    for index, file_info in enumerate(field_result.get("files", []), 1):
        if not isinstance(file_info, dict):
            continue
        path = str(file_info.get("path", ""))
        if included_files and path not in included_files:
            continue
        tables = []
        for table in file_info.get("tables", []):
            if not isinstance(table, dict):
                continue
            usable, header_quality = header_is_usable(table)
            columns = [
                {
                    "name": str(column.get("name", "")),
                    "query_name": str(column.get("query_name", column.get("name", ""))),
                    "kind": str(column.get("kind", "other")),
                    "base": str(column.get("base", "")),
                }
                for column in table.get("columns", [])
                if isinstance(column, dict)
            ]
            table_entry = {
                "table_id": str(table.get("key", "")),
                "sheet_or_table": str(table.get("table_name", "")),
                "row_count": table.get("row_count"),
                "column_count": len(columns),
                "columns": columns,
                "header": header_quality,
                "schema_usable": usable,
            }
            role_columns: dict[str, list[str]] = defaultdict(list)
            for column in columns:
                role_columns[classify_header(column["name"])].append(column["name"])
            role_counts = Counter({role: len(names) for role, names in role_columns.items()})
            table_entry["inferred_material_role"] = table_role(
                role_counts, path, table_entry["sheet_or_table"]
            )
            table_entry["semantic_profile"] = {
                role: names[:40] for role, names in sorted(role_columns.items()) if names
            }
            tables.append(table_entry)
            for column in columns:
                table_by_file_column.setdefault((path, column["name"]), table_entry)
            if file_info.get("kind") == "tabular" and not usable:
                blockers.append(f"表头无法可靠识别：{path} / {table_entry['sheet_or_table']}")
        row_counts = [int(item["row_count"]) for item in tables if isinstance(item.get("row_count"), int)]
        extension = str(file_info.get("extension", "")).casefold()
        kind = str(file_info.get("kind", "binary"))
        if kind == "tabular":
            retrieval = {
                "mode": "schema_bound_read_only_sql",
                "locator_scheme": "table/sheet + row + column",
                "required_output_provenance": ["source_id", "table_id", "column names", "query digest"],
            }
        elif extension in {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".tif", ".tiff", ".webp"}:
            retrieval = {
                "mode": "ocr_then_chunk_index",
                "locator_scheme": "image + OCR block/page + chunk",
                "ocr_policy": "required",
                "required_output_provenance": ["source_id", "source digest", "locator", "chunk id", "text digest"],
            }
        else:
            retrieval = {
                "mode": "parse_then_chunk_index",
                "locator_scheme": "page/paragraph/slide/line + chunk",
                "ocr_policy": "fallback_when_text_layer_is_sparse" if extension == ".pdf" else "not_applicable",
                "required_output_provenance": ["source_id", "source digest", "locator", "chunk id", "text digest"],
            }
        entry = {
            "source_id": stable_source_id(path),
            "view_name": f"source_{index}",
            "path": path,
            "extension": extension,
            "kind": kind,
            "size_bytes": int(file_info.get("size", 0)),
            "content_sha256": str(file_info.get("sha256", "")),
            "is_large": bool(
                max(row_counts, default=0) >= LARGE_TABULAR_ROWS
                or (kind != "tabular" and int(file_info.get("size", 0)) >= 20 * 1024 * 1024)
            ),
            "roles": sorted(roles_by_file.get(path, []), key=lambda item: (item["node_type"], item["node_id"])),
            "tables": tables,
            "material_roles": sorted({
                str(table.get("inferred_material_role", ""))
                for table in tables
                if str(table.get("inferred_material_role", ""))
            }),
            "access_policy": "bounded_sql_only" if file_info.get("kind") == "tabular" else "bounded_extract_or_ocr",
            "content_retrieval": retrieval,
            "agent_must_not_open_directly": True,
            **source_runtime_contract(roles_by_file.get(path, [])),
        }
        sources.append(entry)
        source_by_path[path] = entry

    stats_index: dict[tuple[str, str], dict[str, Any]] = {}
    for item in field_result.get("column_statistics", []):
        if not isinstance(item, dict):
            continue
        key = (str(item.get("file", "")), str(item.get("column", "")))
        current = stats_index.get(key)
        if current is None or int(item.get("nonempty", 0)) > int(current.get("nonempty", 0)):
            stats_index[key] = item

    macro_edges_by_evidence: dict[str, list[str]] = defaultdict(list)
    for edge in claims.get("edges", []):
        if not isinstance(edge, dict) or edge.get("type") != "joins_with":
            continue
        for evidence_id in edge.get("evidence_ids", []):
            macro_edges_by_evidence[str(evidence_id)].append(str(edge.get("id", "")))

    links: list[dict[str, Any]] = []
    output_files = {
        file_name
        for node in nodes if node.get("type") == "output"
        for file_name in direct_node_files(node, cards_by_id)
    }
    for card in card_payload.get("cards", []):
        if not isinstance(card, dict) or card.get("kind") != "field_relationship":
            continue
        facts = card.get("facts") if isinstance(card.get("facts"), dict) else {}
        source_file = str(facts.get("source_file", ""))
        target_file = str(facts.get("target_file", ""))
        if source_file not in source_by_path or target_file not in source_by_path:
            continue
        if (
            source_by_path[source_file].get("kind") != "tabular"
            or source_by_path[target_file].get("kind") != "tabular"
        ):
            # Exact document links live in the validated trace bundle with
            # segment provenance.  They are not SQL join candidates and must
            # not acquire empty synthetic table names here.
            continue
        candidates = []
        for raw in facts.get("correspondences", []):
            if not isinstance(raw, dict):
                continue
            candidate = {
                **raw,
                "source_file": source_file,
                "target_file": target_file,
                "source_table": table_by_file_column.get((source_file, str(raw.get("source_field", ""))), {}).get("sheet_or_table", ""),
                "target_table": table_by_file_column.get((target_file, str(raw.get("target_field", ""))), {}).get("sheet_or_table", ""),
                "source_statistics": stats_index.get((source_file, str(raw.get("source_field", ""))), {}),
                "target_statistics": stats_index.get((target_file, str(raw.get("target_field", ""))), {}),
            }
            candidate["score"] = join_candidate_score(candidate, stats_index)
            candidates.append(candidate)
        candidates.sort(key=lambda item: (-float(item["score"]), -int(item.get("evidence_count", 0))))
        recommended = None
        candidate_key_sets = []
        eligible = [item for item in candidates[:6] if float(item.get("score", 0)) >= 0.55]
        for key_index, candidate in enumerate(eligible):
            candidate_key_sets.append({
                "key_set_index": key_index,
                "key_pairs": [{
                    "source_field": candidate["source_field"],
                    "target_field": candidate["target_field"],
                }],
                "score": candidate["score"],
                "mode": "single_key",
                "runtime_selection_required": key_index != 0,
            })
        for left, right in itertools.combinations(eligible[:4], 2):
            if (
                left["source_field"] == right["source_field"]
                or left["target_field"] == right["target_field"]
            ):
                continue
            candidate_key_sets.append({
                "key_set_index": len(candidate_key_sets),
                "key_pairs": [
                    {"source_field": left["source_field"], "target_field": left["target_field"]},
                    {"source_field": right["source_field"], "target_field": right["target_field"]},
                ],
                "score": round(min(float(left["score"]), float(right["score"])), 4),
                "mode": "composite_key_runtime_candidate",
                "runtime_selection_required": True,
            })
            if len(candidate_key_sets) >= 10:
                break
        if candidates and float(candidates[0]["score"]) >= 0.65:
            recommended = {
                **candidates[0],
                "key_pairs": [{
                    "source_field": candidates[0]["source_field"],
                    "target_field": candidates[0]["target_field"],
                }],
            }
        link_kind = "result_trace" if source_file in output_files or target_file in output_files else "cross_source_join"
        runtime_eligible = bool(
            source_by_path.get(source_file, {}).get("runtime_required")
            and source_by_path.get(target_file, {}).get("runtime_required")
        )
        links.append({
            "link_id": "link-" + hashlib.sha1(f"{source_file}\0{target_file}".encode("utf-8")).hexdigest()[:10],
            "kind": link_kind,
            "source_id": source_by_path.get(source_file, {}).get("source_id", stable_source_id(source_file)),
            "target_id": source_by_path.get(target_file, {}).get("source_id", stable_source_id(target_file)),
            "source_file": source_file,
            "target_file": target_file,
            "macro_edge_ids": sorted(macro_edges_by_evidence.get(str(card.get("id", "")), [])),
            "evidence_card_id": str(card.get("id", "")),
            "recommended_candidate": recommended,
            "candidate_key_sets": candidate_key_sets,
            "candidate_count": len(candidates),
            "candidates": candidates[:12],
            "runtime_validation": [
                "check null rate on both keys",
                "check unmatched rate in both directions",
                "check join fanout and duplicate amplification",
                "reject cartesian or unexplained many-to-many expansion",
            ],
            "runtime_eligible": runtime_eligible,
        })

    semantic_routes: list[dict[str, Any]] = []
    route_keys: set[tuple[str, str, str]] = set()
    for edge in claims.get("edges", []):
        if not isinstance(edge, dict):
            continue
        source_node = next((item for item in nodes if str(item.get("id", "")) == str(edge.get("source", ""))), {})
        target_node = next((item for item in nodes if str(item.get("id", "")) == str(edge.get("target", ""))), {})
        source_files = direct_node_files(source_node, cards_by_id)
        target_files = direct_node_files(target_node, cards_by_id)
        for source_file in sorted(source_files):
            for target_file in sorted(target_files):
                if not source_file or not target_file or source_file == target_file:
                    continue
                source_entry = source_by_path.get(source_file, {})
                target_entry = source_by_path.get(target_file, {})
                if not source_entry or not target_entry:
                    continue
                if source_entry.get("kind") == target_entry.get("kind") == "tabular":
                    continue
                ordered = tuple(sorted((source_file, target_file)))
                route_key = (ordered[0], ordered[1], str(edge.get("id", "")))
                if route_key in route_keys:
                    continue
                route_keys.add(route_key)
                evidence_ids = [str(item) for item in edge.get("evidence_ids", []) if str(item)]
                evidence_locators = []
                for evidence_id in evidence_ids:
                    card = cards_by_id.get(evidence_id, {})
                    evidence_locators.extend(
                        {
                            "evidence_id": evidence_id,
                            "file": str(item.get("file", "")),
                            "locator": str(item.get("locator", "")),
                        }
                        for item in card.get("sources", [])
                        if isinstance(item, dict)
                    )
                semantic_routes.append({
                    "route_id": "route-" + hashlib.sha1(
                        f"{source_file}\0{target_file}\0{edge.get('id', '')}".encode("utf-8")
                    ).hexdigest()[:10],
                    "mode": "provenance_preserving_semantic_retrieval",
                    "source_id": source_entry.get("source_id", stable_source_id(source_file)),
                    "target_id": target_entry.get("source_id", stable_source_id(target_file)),
                    "macro_edge_id": str(edge.get("id", "")),
                    "relation_type": str(edge.get("type", "")),
                    "evidence_ids": evidence_ids,
                    "evidence_locators": evidence_locators[:12],
                    "runtime_validation": [
                        "derive search terms from the user request and the complete selected rule record",
                        "return only bounded hits with source, locator, chunk id, and content digest",
                        "treat OCR confidence or sparse text as uncertainty, never as an invented fact",
                        "require an explicit business key before joining a semantic hit to a structured row",
                    ],
                })

    trace_evidence = operational_trace_evidence(
        trace_report, source_by_path, output_files, links
    ) if trace_report else {
        "schema_version": 1,
        "status": "missing",
        "strategy": "",
        "bundles": [],
        "quality_gates": {},
    }
    trace_review, trace_review_errors = trace_review_context(card_payload, trace_report)
    blockers.extend(trace_review_errors)

    input_files = {
        path
        for path, roles in roles_by_file.items()
        if any(item["node_type"] in {"input", "object"} for item in roles)
    }
    structured_input_files = {
        path for path in input_files if source_by_path.get(path, {}).get("kind") == "tabular"
    }
    unstructured_input_files = input_files - structured_input_files
    adjacency: dict[str, set[str]] = defaultdict(set)
    for link in links:
        if (
            link.get("recommended_candidate")
            and link["source_file"] in structured_input_files
            and link["target_file"] in structured_input_files
        ):
            adjacency[link["source_file"]].add(link["target_file"])
            adjacency[link["target_file"]].add(link["source_file"])
    if len(structured_input_files) > 1:
        reached: set[str] = set()
        frontier = [next(iter(structured_input_files))]
        while frontier:
            current = frontier.pop()
            if current in reached:
                continue
            reached.add(current)
            frontier.extend(adjacency.get(current, set()) - reached)
        if reached != structured_input_files:
            blockers.append("多个结构化业务输入源之间没有形成完整的字段级可执行关联网络")

    rule_sources = [
        source["source_id"]
        for source in sources
        if any(role["node_type"] == "rule" for role in source["roles"])
    ]
    if any(node.get("type") == "rule" for node in nodes) and not rule_sources:
        blockers.append("规则节点没有映射到可查询或可提取的规则来源")
    result_source_ids = [source["source_id"] for source in sources if source["path"] in output_files]
    structured_result_ids = {
        source["source_id"] for source in sources
        if source["source_id"] in result_source_ids and source.get("kind") == "tabular"
    }
    unstructured_result_ids = set(result_source_ids) - structured_result_ids
    field_result_trace = {
        endpoint
        for link in links
        if link["kind"] == "result_trace" and link.get("recommended_candidate")
        for endpoint in (link.get("source_id"), link.get("target_id"))
    }
    traced_result_ids = {
        str(item.get("anchor", {}).get("source_id", ""))
        for item in trace_evidence.get("bundles", [])
        if isinstance(item, dict)
        and int(item.get("coverage", {}).get("exact_link_count", 0)) > 0
    }
    # A structured result may link to either another table or an exact-value
    # located input document. A non-tabular result must itself be the
    # digest-bound document anchor of the validated trace. Semantic routes
    # remain useful context, but never satisfy either exact lineage gate.
    if structured_result_ids - (field_result_trace | traced_result_ids):
        blockers.append("结构化结果样例无法通过字段级证据链路反向追踪到业务来源")
    if card_payload.get("trace_samples") and structured_result_ids - traced_result_ids:
        blockers.append("结构化结果样例没有通过同一锚点的全量数据反向追踪验证")
    if unstructured_result_ids - traced_result_ids:
        blockers.append(
            "非结构化结果样例必须具有摘要绑定的文档片段锚点和可重放的精确值链路；"
            "仅靠语义相似度不能通过验收"
        )
    if not result_source_ids:
        warnings.append("没有物理结果样例；结果结构只能由业务契约定义，不能执行反向对账")

    runtime_source_ids = [
        source["source_id"] for source in sources if source.get("runtime_required") is True
    ]
    template_source_ids = [
        source["source_id"] for source in sources if source.get("lifecycle") == "design_time_template"
    ]
    external_capabilities = [
        {
            "node_id": str(node.get("id", "")),
            "name": str(node.get("name", "")),
            "description": str(node.get("description", "")),
            "lifecycle": "optional_enrichment",
            "runtime_required": "agent_decides_from_user_request_and_complete_rule_record",
            "activation": "agent_determines_from_user_request_and_complete_rule_record",
            "failure_policy": "manual_intervention_required_when_mandatory_and_unavailable",
        }
        for node in nodes
        if str(node.get("type", "")) == "system" and is_external_capability_node(node)
    ]
    return {
        "schema_version": 3,
        "status": "ready" if not blockers else "blocked",
        "generated_at": utc_now(),
        "scenario": claims.get("scenario", {}),
        "trace_review": trace_review,
        "source": {
            "field_evidence": str(field_result_path),
            "field_evidence_fingerprint": file_sha256(field_result_path),
        },
        "sources": sources,
        "links": links,
        "semantic_routes": semantic_routes,
        "trace_evidence": trace_evidence,
        "rule_source_ids": rule_sources,
        "result_source_ids": result_source_ids,
        "runtime_source_ids": runtime_source_ids,
        "template_source_ids": template_source_ids,
        "external_capabilities": external_capabilities,
        "query_policy": {
            "rule_record_mode": "return_complete_selected_rule_record",
            "structured_rule_record_mode": "return_complete_selected_row",
            "unstructured_rule_record_mode": "return_complete_located_section_with_provenance",
            "large_table_threshold_rows": LARGE_TABULAR_ROWS,
            "large_sources_must_use_sql": True,
            "agent_must_not_open_source_files": True,
            "register_only_sources_referenced_by_the_current_operation": True,
            "runtime_data_validation": "schema_compatibility_not_design_time_content_identity",
            "design_time_templates_are_not_runtime_dependencies": True,
            "required_sequence": [
                "locate complete rule record with bounded query",
                "derive structured predicates and unstructured retrieval terms from the complete rule record",
                "use the validated result trace blueprint to select source roles, projected fields, and join keys",
                "index and search non-tabular sources with provenance-preserving chunks when required",
                "validate recommended join keys and fanout",
                "execute bounded read-only SQL only for structured sources that participate in the operation",
                "resolve non-tabular inputs and outputs through digest-bound document/OCR locators",
                "reconcile structured rows with document/OCR evidence locators without semantic-only joins",
            ],
        },
        "quality_gates": {
            "status": "passed" if not blockers else "failed",
            "blockers": sorted(set(blockers)),
            "warnings": sorted(set(warnings)),
            "input_source_count": len(input_files),
            "structured_input_source_count": len(structured_input_files),
            "unstructured_input_source_count": len(unstructured_input_files),
            "evidence_backed_link_count": sum(bool(item.get("recommended_candidate")) for item in links),
            "semantic_retrieval_route_count": len(semantic_routes),
            "result_trace_link_count": sum(item["kind"] == "result_trace" and bool(item.get("recommended_candidate")) for item in links),
            "validated_trace_bundle_count": len(trace_evidence.get("bundles", [])),
        },
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


def _claim_projection(payload: dict[str, Any]) -> dict[str, Any]:
    """Return only the editable claim fields from a relationship artifact.

    A failed ``finalize`` can leave a partially assembled
    ``scenario-relationship.json`` behind.  It is not a final artifact and it
    must not be edited in place: copying only the claims surface gives recovery
    a canonical candidate to preflight without preserving accidental result
    metadata such as ``status`` or stale artifact paths.
    """

    schema_version = payload.get("schema_version")
    if not isinstance(schema_version, int) or schema_version <= 0:
        schema_version = SCHEMA_VERSION
    scenario = payload.get("scenario") if isinstance(payload.get("scenario"), dict) else {}
    coverage = payload.get("coverage") if isinstance(payload.get("coverage"), dict) else {}
    return {
        "schema_version": schema_version,
        "scenario": deepcopy(scenario),
        "nodes": deepcopy(payload.get("nodes") if isinstance(payload.get("nodes"), list) else []),
        "edges": deepcopy(payload.get("edges") if isinstance(payload.get("edges"), list) else []),
        "main_chain": deepcopy(payload.get("main_chain") if isinstance(payload.get("main_chain"), list) else []),
        "branches": deepcopy(payload.get("branches") if isinstance(payload.get("branches"), list) else []),
        "coverage": {
            "included_files": deepcopy(
                coverage.get("included_files") if isinstance(coverage.get("included_files"), list) else []
            ),
            "excluded_files": deepcopy(
                coverage.get("excluded_files") if isinstance(coverage.get("excluded_files"), list) else []
            ),
        },
    }


def _claim_evidence_score(claims: dict[str, Any], cards_by_id: dict[str, dict[str, Any]]) -> tuple[int, int]:
    """Rank invalid drafts only; valid candidates are always preserved.

    The score never decides which *valid* graph is better.  It merely prevents
    replacing an invalid candidate with an equally sparse partial artifact when
    an interrupted Agent accidentally wrote to the final result path.
    """

    evidence_count = 0
    claim_count = 0
    for collection in ("nodes", "edges", "branches"):
        for item in claims.get(collection, []):
            if not isinstance(item, dict):
                continue
            claim_count += 1
            evidence_ids = item.get("evidence_ids")
            if not isinstance(evidence_ids, list):
                continue
            evidence_count += sum(
                1
                for evidence_id in evidence_ids
                if str(evidence_id) in cards_by_id
            )
    return evidence_count, claim_count


def _card_facts(card: dict[str, Any]) -> dict[str, Any]:
    facts = card.get("facts")
    return facts if isinstance(facts, dict) else {}


def _result_recovery_evidence(cards: Sequence[dict[str, Any]]) -> tuple[list[str], list[str]]:
    """Select the uniquely anchored output evidence, or fail closed.

    This is intentionally narrow.  Recovery may fill an omitted output citation
    only when the evidence package has exactly one selected result trace and a
    schema card for that trace's approved result source.  It never guesses an
    output from file names or broad semantic similarity.
    """

    trace_cards = sorted(
        (card for card in cards if card.get("kind") == "record_trace" and str(card.get("id", ""))),
        key=lambda card: str(card["id"]),
    )
    if len(trace_cards) != 1:
        return [], []
    trace_card = trace_cards[0]
    anchor_file = str(_card_facts(trace_card).get("anchor_file", "")).strip()
    if not anchor_file:
        return [], []
    result_schema_cards = sorted(
        (
            card
            for card in cards
            if card.get("kind") == "table_schema"
            and str(card.get("id", ""))
            and str(_card_facts(card).get("approved_role", "")) == "result"
            and any(
                str(source.get("file", "")) == anchor_file
                for source in card.get("sources", [])
                if isinstance(source, dict)
            )
        ),
        key=lambda card: str(card["id"]),
    )
    if len(result_schema_cards) != 1:
        return [], []
    return [str(trace_card["id"]), str(result_schema_cards[0]["id"])], [str(trace_card["id"])]


def _output_statement_evidence(cards: Sequence[dict[str, Any]]) -> list[str]:
    """Find one direct statement that actually declares an output relationship."""

    output_markers = {
        "output", "outputs", "produce", "produces", "generate", "generates", "result",
        "输出", "产生", "生成", "结果",
    }
    matches: list[str] = []
    for card in cards:
        if card.get("kind") not in {
            "goal_relation_statement", "table_relation_statement", "document_relation_statement",
        }:
            continue
        identifier = str(card.get("id", "")).strip()
        if not identifier:
            continue
        facts = _card_facts(card)
        markers = {
            str(value).casefold()
            for value in facts.get("relation_markers", [])
            if str(value).strip()
        }
        text = " ".join(
            str(card.get(key, "")) for key in ("statement", "snippet")
        ).casefold()
        if markers.intersection(output_markers) or any(marker in text for marker in output_markers):
            matches.append(identifier)
    return sorted(set(matches))[:1]


def _unproven_self_branch(branch: dict[str, Any], edges: Sequence[dict[str, Any]]) -> bool:
    """Recognize the one branch shape which carries no independent claim.

    A branch that starts and ends on its source, has no citation, and has no
    ``branches_to`` edge is a failed intermediate edit, not an evidence-backed
    business branch.  Other branches are deliberately retained for preflight to
    report rather than being silently discarded.
    """

    evidence_ids = branch.get("evidence_ids")
    if isinstance(evidence_ids, list) and evidence_ids:
        return False
    origin = str(branch.get("from", ""))
    path = branch.get("path")
    if not origin or not isinstance(path, list) or len(path) != 1 or str(path[0]) != origin:
        return False
    return not any(
        str(edge.get("source", "")) == origin
        and str(edge.get("target", "")) == origin
        and str(edge.get("type", "")) == "branches_to"
        for edge in edges
        if isinstance(edge, dict)
    )


def reconcile_partial_relationship_claims(
    partial: dict[str, Any], card_payload: dict[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Repair omitted schema fields in an interrupted partial relationship graph.

    The operation is a deliberately small, evidence-constrained reconciliation:
    it does not redesign the graph, reverse edges, or replace existing citations.
    It can only add citations for a uniquely traced output, add a conservative
    confidence when an otherwise supported output edge omitted it, and remove a
    citation-free self-branch that cannot represent a real branch.
    """

    claims = _claim_projection(partial)
    cards = [card for card in card_payload.get("cards", []) if isinstance(card, dict)]
    cards_by_id = {str(card.get("id", "")): card for card in cards if str(card.get("id", ""))}
    actions: list[dict[str, Any]] = [{
        "action": "canonicalized_partial_relationship",
        "reason": "Copied only editable claim fields; status and generated artifact metadata were not promoted.",
    }]

    original_coverage = partial.get("coverage") if isinstance(partial.get("coverage"), dict) else {}
    if any(key not in {"included_files", "excluded_files"} for key in original_coverage):
        actions.append({
            "action": "removed_non_claim_coverage_metadata",
            "reason": "Final-result coverage metadata is not part of a candidate claim.",
        })

    retained_branches: list[dict[str, Any]] = []
    for branch in claims["branches"]:
        if isinstance(branch, dict) and _unproven_self_branch(branch, claims["edges"]):
            actions.append({
                "action": "removed_unproven_self_branch",
                "id": str(branch.get("id", "")),
                "reason": "The branch cited no evidence, had no branches_to edge, and returned to its own origin.",
            })
            continue
        retained_branches.append(branch)
    claims["branches"] = retained_branches

    output_evidence, trace_evidence = _result_recovery_evidence(cards)
    for node in claims["nodes"]:
        if not isinstance(node, dict) or str(node.get("type", "")) != "output":
            continue
        evidence_ids = node.get("evidence_ids")
        if isinstance(evidence_ids, list) and evidence_ids:
            continue
        if output_evidence:
            node["evidence_ids"] = output_evidence
            actions.append({
                "action": "filled_output_evidence",
                "id": str(node.get("id", "")),
                "evidence_ids": output_evidence,
                "reason": "A single selected result trace and its approved result schema identify this output.",
            })

    node_by_id = {
        str(node.get("id", "")): node
        for node in claims["nodes"]
        if isinstance(node, dict)
    }
    statement_evidence = _output_statement_evidence(cards)
    for edge in claims["edges"]:
        if not isinstance(edge, dict) or str(edge.get("type", "")) != "produces":
            continue
        source = node_by_id.get(str(edge.get("source", "")), {})
        target = node_by_id.get(str(edge.get("target", "")), {})
        if source.get("type") != "decision" or target.get("type") != "output":
            continue
        evidence_ids = edge.get("evidence_ids")
        if (not isinstance(evidence_ids, list) or not evidence_ids) and statement_evidence and trace_evidence:
            chosen = [*statement_evidence, *trace_evidence]
            edge["evidence_ids"] = chosen
            actions.append({
                "action": "filled_output_edge_evidence",
                "id": str(edge.get("id", "")),
                "evidence_ids": chosen,
                "reason": "A direct output statement plus the selected result trace support the decision-to-output edge.",
            })
        if not isinstance(edge.get("confidence"), (int, float)):
            if isinstance(edge.get("evidence_ids"), list) and edge["evidence_ids"]:
                edge["confidence"] = 0.8
                actions.append({
                    "action": "filled_output_edge_confidence",
                    "id": str(edge.get("id", "")),
                    "confidence": 0.8,
                    "reason": "The interrupted claim omitted a required confidence; recovery uses a conservative fixed value rather than inventing precision.",
                })

    # ``cards_by_id`` is intentionally materialized above so callers can see
    # that recovery is tied to a validated evidence package, even though the
    # actual validation remains the ordinary preflight below.
    if not cards_by_id:
        actions.append({
            "action": "evidence_unavailable",
            "reason": "No evidence cards were available, so omitted citations could not be reconstructed.",
        })
    return claims, actions


def recover_claims(args: argparse.Namespace) -> dict[str, Any]:
    """Reconcile a stale candidate with an incomplete final-path graph.

    This command is intentionally explicit.  It never finalizes artifacts and
    never overwrites a structurally valid candidate; callers must still execute
    the normal ``preflight`` and ``finalize`` steps after recovery.
    """

    claims_path = Path(args.claims).resolve()
    output_root = Path(args.output).resolve()
    cards_path = Path(args.cards).resolve() if args.cards else output_root / "evidence-cards.json"
    partial_path = Path(args.partial).resolve() if args.partial else output_root / "scenario-relationship.json"
    card_payload = json.loads(cards_path.read_text(encoding="utf-8"))
    if not isinstance(card_payload, dict):
        raise ValueError("Evidence cards must contain one JSON object")
    cards_by_id = {
        str(card.get("id", "")): card
        for card in card_payload.get("cards", [])
        if isinstance(card, dict) and str(card.get("id", ""))
    }

    if claims_path.is_file():
        existing = _load_claims(claims_path)
        existing_errors = validate_claims(existing, card_payload)
        if not existing_errors:
            return {
                "status": "candidate_preserved",
                "claims": str(claims_path),
                "recovery": [],
                "next_action": "Run ordinary preflight on this unchanged evidence-backed candidate.",
            }
    else:
        existing = None
        existing_errors = ["Candidate does not exist"]

    if not partial_path.is_file():
        return {
            "status": "recovery_blocked",
            "claims": str(claims_path),
            "partial": str(partial_path),
            "errors": ["Incomplete scenario-relationship.json is missing; no recovery source is available."],
            "recovery": [],
        }
    partial = _load_claims(partial_path)
    if str(partial.get("status", "")) == "complete":
        return {
            "status": "recovery_blocked",
            "claims": str(claims_path),
            "partial": str(partial_path),
            "errors": ["The supplied scenario-relationship.json is complete and must not be converted back into a candidate."],
            "recovery": [],
        }

    projected = _claim_projection(partial)
    if existing is not None:
        existing_score = _claim_evidence_score(existing, cards_by_id)
        partial_score = _claim_evidence_score(projected, cards_by_id)
        if partial_score <= existing_score:
            return {
                "status": "recovery_blocked",
                "claims": str(claims_path),
                "partial": str(partial_path),
                "errors": [
                    "The incomplete relationship graph is not more evidence-backed than the current invalid candidate; "
                    "preserving the candidate avoids discarding unresolved claims."
                ],
                "candidate_errors": existing_errors,
                "recovery": [{
                    "action": "candidate_preserved",
                    "reason": "Partial score %s is not greater than candidate score %s." % (partial_score, existing_score),
                }],
            }

    recovered, actions = reconcile_partial_relationship_claims(partial, card_payload)
    errors = validate_claims(recovered, card_payload)
    if errors:
        return {
            "status": "recovery_blocked",
            "claims": str(claims_path),
            "partial": str(partial_path),
            "errors": errors,
            "candidate_errors": existing_errors,
            "recovery": actions,
            "next_action": "Resolve the listed evidence or graph errors manually; recovery did not overwrite the candidate.",
        }

    atomic_json(claims_path, recovered)
    return {
        "status": "recovered",
        "claims": str(claims_path),
        "partial": str(partial_path),
        "recovery": actions,
        "next_action": "Run ordinary preflight on this recovered candidate before finalize.",
    }


def mutate_claims(args: argparse.Namespace) -> dict[str, Any]:
    claims_path = Path(args.claims).resolve()
    command = args.command
    if command == "claims-recover":
        return recover_claims(args)
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


def compatible_edge_types(source_type: str, target_type: str) -> list[str]:
    return sorted(
        edge_type
        for edge_type, (allowed_sources, allowed_targets) in EDGE_ENDPOINT_TYPES.items()
        if source_type in allowed_sources and target_type in allowed_targets
    )


def trace_preflight_gate_errors(card_payload: dict[str, Any]) -> list[str]:
    """Keep claims operations behind result selection and trusted trace review."""

    claim = card_payload.get("trace_samples")
    if not isinstance(claim, dict) or not claim:
        # Compatibility for evidence packages created before trace artifacts
        # existed. Newly prepared evidence always carries this claim.
        return []
    status = str(claim.get("status", ""))
    if status == "selection_required":
        selection = claim.get("anchor_selection") if isinstance(claim.get("anchor_selection"), dict) else {}
        if selection.get("resolution") == "role_correction_required":
            return [
                "Result role correction is required. Keep exactly one physical file or table assigned as "
                "result, confirm the corrected roles, and rerun tracing before preflight or finalize."
            ]
        return [
            "Result anchor selection is required. Rerun analyze with --trace-anchor-selector "
            "containing file, table, and row_number before preflight or finalize."
        ]
    if status != "complete":
        return [
            f"Result-anchored trace is not executable (status={status or 'missing'}); "
            "preflight and finalize are blocked."
        ]
    trace_report, trace_errors = load_trace_evidence(card_payload)
    if trace_errors:
        return trace_errors
    _review, review_errors = trace_review_context(card_payload, trace_report)
    return review_errors


def validation_payload(
    claims: dict[str, Any],
    card_payload: dict[str, Any],
    claims_path: Path,
) -> dict[str, Any]:
    errors = trace_preflight_gate_errors(card_payload) + validate_claims(claims, card_payload)
    if not errors:
        return {
            "status": "valid",
            "error_count": 0,
            "claims": str(claims_path),
            "node_count": len(claims.get("nodes", [])),
            "edge_count": len(claims.get("edges", [])),
            "branch_count": len(claims.get("branches", [])),
            "next_action": "Run finalize with this exact claims path.",
        }

    node_by_id = {
        str(node.get("id", "")): node
        for node in claims.get("nodes", [])
        if isinstance(node, dict)
    }
    repair_hints: list[dict[str, Any]] = []
    for edge in claims.get("edges", []):
        if not isinstance(edge, dict):
            continue
        source = str(edge.get("source", ""))
        target = str(edge.get("target", ""))
        edge_type = str(edge.get("type", ""))
        if source not in node_by_id or target not in node_by_id or edge_type not in EDGE_ENDPOINT_TYPES:
            continue
        source_type = str(node_by_id[source].get("type", ""))
        target_type = str(node_by_id[target].get("type", ""))
        allowed_sources, allowed_targets = EDGE_ENDPOINT_TYPES[edge_type]
        if source_type in allowed_sources and target_type in allowed_targets:
            continue
        compatible = compatible_edge_types(source_type, target_type)
        repair_hints.append({
            "claim": "edge",
            "id": str(edge.get("id", "")),
            "problem": "invalid_direction",
            "current_type": edge_type,
            "source_type": source_type,
            "target_type": target_type,
            "compatible_types": compatible,
            "repair": (
                "Choose a compatible type only when it preserves the evidence-backed meaning; "
                "otherwise reverse, remove, or restructure this edge."
            ),
        })
    return {
        "status": "validation_failed",
        "error_count": len(errors),
        "errors": errors,
        "claims": str(claims_path),
        "repair_target": str(claims_path),
        "candidate_preserved": True,
        "repair_hints": repair_hints,
        "next_action": (
            "Fix all errors on repair_target in one pass. Prefer the documented claims-* commands for "
            "small edits, then rerun preflight. Do not inspect validator source or create helper scripts."
        ),
    }


def preflight(args: argparse.Namespace) -> tuple[int, dict[str, Any]]:
    output_root = Path(args.output).resolve()
    cards_path = Path(args.cards).resolve() if args.cards else output_root / "evidence-cards.json"
    claims_path = Path(args.claims).resolve()
    card_payload = json.loads(cards_path.read_text(encoding="utf-8"))
    claims = _load_claims(claims_path)
    payload = validation_payload(claims, card_payload, claims_path)
    validation_path = output_root / "validation-errors.json"
    if payload["status"] == "validation_failed":
        atomic_json(validation_path, payload)
        return 2, payload
    validation_path.unlink(missing_ok=True)
    return 0, payload


def finalize(args: argparse.Namespace) -> tuple[int, dict[str, Any]]:
    output_root = Path(args.output).resolve()
    cards_path = Path(args.cards).resolve() if args.cards else output_root / "evidence-cards.json"
    claims_path = Path(args.claims).resolve()
    card_payload = json.loads(cards_path.read_text(encoding="utf-8"))
    claims = _load_claims(claims_path)
    validation = validation_payload(claims, card_payload, claims_path)
    if validation["status"] == "validation_failed":
        payload = validation
        atomic_json(output_root / "validation-errors.json", payload)
        return 2, payload
    canonical_claims_path = output_root / "scenario-claims.json"
    if claims_path != canonical_claims_path:
        atomic_json(canonical_claims_path, claims)
        if (
            claims_path.parent == output_root
            and claims_path.name.startswith("scenario-claims.candidate")
        ):
            claims_path.unlink(missing_ok=True)
    cards_by_id = {card["id"]: card for card in card_payload["cards"]}
    field_result_path = Path(str(card_payload.get("field_evidence", ""))).resolve()
    if not field_result_path.is_file():
        raise ValueError("Field evidence artifact is missing; rerun analyze before finalize")
    field_result = json.loads(field_result_path.read_text(encoding="utf-8"))
    operational_contract = build_operational_contract(
        claims, card_payload, cards_by_id, field_result, field_result_path
    )
    operational_contract_path = output_root / "operational-data-contract.json"
    atomic_json(operational_contract_path, operational_contract)
    operational_contract_claim = {
        "status": operational_contract["status"],
        "artifact": str(operational_contract_path),
        "fingerprint": file_sha256(operational_contract_path),
        "quality_gates": operational_contract["quality_gates"],
    }
    operational_ready = operational_contract.get("status") == "ready"
    result = {
        "schema_version": SCHEMA_VERSION,
        "status": "complete" if operational_ready else "blocked_operational_contract",
        "generated_at": utc_now(),
        "strategy": "bounded_evidence_semantic_synthesis",
        "scenario": claims["scenario"],
        "nodes": claims["nodes"],
        "edges": claims["edges"],
        "main_chain": claims["main_chain"],
        "primary_data_path": claims["main_chain"],
        "branches": claims["branches"],
        "operational_contract": operational_contract_claim,
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
            "operational_data_contract": str(operational_contract_path),
            "trace_samples": str(card_payload.get("trace_samples", {}).get("artifact", "")),
        },
    }
    atomic_json(output_root / "scenario-relationship.json", result)
    atomic_json(output_root / "relations.json", result)
    write_report(result, cards_by_id, output_root / "relation-report.md")
    write_mermaid(result, output_root / "relations.mmd")
    write_scenario_database(result, card_payload, output_root / "evidence.sqlite3")
    validation_path = output_root / "validation-errors.json"
    if not operational_ready:
        blocked = {
            "status": "blocked_operational_contract",
            "errors": operational_contract.get("quality_gates", {}).get("blockers", []),
            "warnings": operational_contract.get("quality_gates", {}).get("warnings", []),
            "operational_data_contract": str(operational_contract_path),
            "next_action": (
                "Repair the field-level source network, header detection, rule source, or result trace in "
                "discover-data-relations, then finalize again. Downstream flow and Skill distillation are forbidden."
            ),
        }
        atomic_json(validation_path, blocked)
        return 2, {**compact_summary(result, 0, args.summary_limit), **blocked}
    validation_path.unlink(missing_ok=True)
    return 0, compact_summary(result, 0, args.summary_limit)


def trace_review_command(args: argparse.Namespace) -> dict[str, Any]:
    """Mutate explicit review gates, never relationship claims or source data."""

    command = args.command
    if command in {"trace-review-approve", "micro-process-approve"}:
        # Approval is an authority boundary, not a free-form CLI field.  The
        # platform service must atomically update the review and emit a signed
        # envelope into platform-approvals.json.
        return platform_approval_block(args)
    if command == "trace-review-init":
        trace_path = Path(args.trace).resolve()
        review_path = Path(args.review).resolve()
        if review_path.exists() and not args.force:
            raise FileExistsError("审阅文件已存在；请继续审阅、修正或显式使用 --force")
        trace_report = load_review_json(trace_path)
        review = review_template(trace_path, trace_report)
        review["role_manifest"] = trace_report.get("role_manifest", {})
        atomic_review_json(review_path, review)
        return {
            "status": review["status"], "review": str(review_path),
            "next_action": "核对表、字段、复合键和警示；确认后批准，发现问题则登记纠偏并重新追踪。",
        }

    review_path = Path(args.review).resolve()
    review = load_review_json(review_path)
    trace_ref = review.get("trace") if isinstance(review.get("trace"), dict) else {}
    trace_path = Path(str(trace_ref.get("artifact", ""))).resolve()
    if command == "trace-review-status":
        return {
            "status": review.get("status", "invalid"),
            "review": str(review_path),
            "errors": validate_review(review, trace_path),
            "review_surface": review.get("review_surface", {}),
            "next_action": "先纠正或批准链路样本；不得直接推导关联关系。",
        }
    if command == "trace-review-correct":
        corrections = make_corrections(load_review_json(Path(args.corrections).resolve()))
        existing = review.get("corrections", []) if isinstance(review.get("corrections"), list) else []
        combined = corrections if args.replace else [*existing, *corrections]
        errors = validate_review({**review, "corrections": combined})
        if errors:
            raise ReviewError("；".join(errors))
        review["corrections"] = combined
        review["status"] = "revision_required"
        review["approval"] = {
            "decision": "revision_required",
            "reviewer": str(args.reviewer or "AI/user"),
            "note": str(args.note or "链路样本需要按已确认关联重新追踪"),
            "accepted_warnings": [],
            "reviewed_at": utc_now(),
        }
        atomic_review_json(review_path, review)
        return {
            "status": "revision_required", "review": str(review_path),
            "correction_count": len(combined),
            "next_action": "使用 analyze --trace-review 指向此审阅文件重新追踪；新链路必须重新审阅。",
        }
    if command == "micro-process-draft":
        approved = load_approved_review(review_path, trace_path)
        approval_errors = platform_approval_errors(review_path, approved, trace_path)
        if approval_errors:
            return {
                "status": "blocked_platform_approval_required",
                "review": str(review_path),
                "errors": approval_errors,
                "next_action": "Obtain a platform-signed trace approval before drafting the micro-process candidate.",
            }
        output_path = Path(args.output).resolve()
        if output_path.exists() and not args.force:
            raise FileExistsError("微观复现候选已存在；请审阅现有候选或显式使用 --force")
        atomic_review_json(output_path, micro_process_template(review_path, approved, trace_path))
        return {
            "status": "pending_review", "micro_process": str(output_path),
            "next_action": "确认该复现契约描述的是参数化处理原理、而非样本值后，再批准微观复现。",
        }
    if command == "micro-process-summary":
        micro = load_review_json(Path(args.micro_process).resolve())
        return {
            "status": micro.get("status", "invalid"),
            "micro_process": str(Path(args.micro_process).resolve()),
            "reconstruction": micro.get("sample_reconstruction", {}),
            "generalization_contract": micro.get("generalization_contract", {}),
            "open_questions": micro.get("open_questions", []),
        }
    raise ValueError(f"Unsupported trace review command: {command}")


def add_probe_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--input", default="/workspace/data")
    parser.add_argument("--output", default="/workspace/outputs/data-relations")
    parser.add_argument("--goal-file", default="/workspace/description.md")
    parser.add_argument("--field-result", default="")
    parser.add_argument(
        "--role-manifest",
        required=True,
        help=(
            "Platform-signed approved_role_manifest.json for the current source snapshot. "
            "Its file/table roles are authoritative for result-anchor selection."
        ),
    )
    parser.add_argument("--ocr-mode", choices=["auto", "always", "never"], default="auto")
    parser.add_argument("--deadline-seconds", type=int, default=780)
    parser.add_argument("--resume", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--seed-cell-budget", type=int, default=100_000)
    parser.add_argument("--seed-file-bytes", type=int, default=8 * 1024 * 1024)
    parser.add_argument("--seed-values-per-column", type=int, default=256)
    parser.add_argument("--max-seed-values", type=int, default=20_000)
    parser.add_argument("--max-text-seeds", type=int, default=2_000)
    parser.add_argument("--profile-size", type=int, default=64)
    parser.add_argument("--frontier-values-per-column", type=int, default=64)
    parser.add_argument("--min-expansion-distinct-ratio", type=float, default=0.01)
    parser.add_argument("--max-matches-per-seed", type=int, default=50)
    parser.add_argument("--max-probe-columns", type=int, default=64)
    parser.add_argument("--fallback-probe-columns", type=int, default=16)
    parser.add_argument("--profile-sample-rows", type=int, default=512)
    parser.add_argument("--max-matched-rows-per-table", type=int, default=100_000)
    parser.add_argument("--xlsx-python-fallback-cell-budget", type=int, default=250_000)
    parser.add_argument("--bootstrap-rows", type=int, default=1_000)
    parser.add_argument("--checkpoint-rows", type=int, default=100_000)
    parser.add_argument("--document-character-budget", type=int, default=2_000_000)
    parser.add_argument("--document-cards-per-file", type=int, default=40)
    parser.add_argument("--semantic-table-cell-budget", type=int, default=100_000)
    parser.add_argument("--table-character-budget", type=int, default=500_000)
    parser.add_argument("--table-cards-per-file", type=int, default=40)
    parser.add_argument("--max-evidence-cards", type=int, default=1_000)
    parser.add_argument("--trace-result-candidates", type=int, default=1)
    parser.add_argument("--trace-anchor-candidates", type=int, default=1)
    parser.add_argument("--trace-rows-per-source", type=int, default=8)
    parser.add_argument("--trace-columns-per-source", type=int, default=48)
    parser.add_argument("--trace-max-hops", type=int, default=4)
    parser.add_argument("--trace-file", default="")
    parser.add_argument("--trace-review", default="")
    parser.add_argument(
        "--auto-first-valid-result-row",
        action="store_true",
        help=(
            "Use the first non-empty row of one approved result table as a reproducible "
            "inspection anchor. Reserved for the server-owned chat trace action; generic "
            "tracing still requires an explicit selector for multi-row results."
        ),
    )
    parser.add_argument(
        "--trace-anchor-selector",
        default="",
        help=(
            "JSON result-row selector: {\"file\":\"results.csv\",\"table\":\"Sheet1\",\"row_number\":17}. "
            "Required when a candidate result table has multiple rows."
        ),
    )
    parser.add_argument("--summary-limit", type=int, default=20)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Discover one evidence-backed scenario relationship chain")
    commands = parser.add_subparsers(dest="command", required=True)
    analyze = commands.add_parser("analyze")
    add_probe_arguments(analyze)
    review_init = commands.add_parser("trace-review-init")
    review_init.add_argument("--trace", default="/workspace/outputs/data-relations/trace-samples.json")
    review_init.add_argument("--review", default="/workspace/outputs/data-relations/trace-review.json")
    review_init.add_argument("--force", action="store_true")
    review_status = commands.add_parser("trace-review-status")
    review_status.add_argument("--review", default="/workspace/outputs/data-relations/trace-review.json")
    review_correct = commands.add_parser("trace-review-correct")
    review_correct.add_argument("--review", default="/workspace/outputs/data-relations/trace-review.json")
    review_correct.add_argument("--corrections", required=True)
    review_correct.add_argument("--reviewer", default="")
    review_correct.add_argument("--note", default="")
    review_correct.add_argument("--replace", action="store_true")
    review_approve = commands.add_parser("trace-review-approve")
    review_approve.add_argument("--review", default="/workspace/outputs/data-relations/trace-review.json")
    micro_draft = commands.add_parser("micro-process-draft")
    micro_draft.add_argument("--review", default="/workspace/outputs/data-relations/trace-review.json")
    micro_draft.add_argument("--output", default="/workspace/outputs/data-relations/micro-process.json")
    micro_draft.add_argument("--force", action="store_true")
    micro_approve = commands.add_parser("micro-process-approve")
    micro_approve.add_argument("--review", default="/workspace/outputs/data-relations/trace-review.json")
    micro_approve.add_argument("--micro-process", default="/workspace/outputs/data-relations/micro-process.json")
    micro_summary = commands.add_parser("micro-process-summary")
    micro_summary.add_argument("--review", default="/workspace/outputs/data-relations/trace-review.json")
    micro_summary.add_argument("--micro-process", default="/workspace/outputs/data-relations/micro-process.json")
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
    claims_recover = commands.add_parser("claims-recover")
    claims_recover.add_argument("--claims", required=True)
    claims_recover.add_argument("--partial", default="")
    claims_recover.add_argument("--cards", default="")
    claims_recover.add_argument("--output", default="/workspace/outputs/data-relations")
    check = commands.add_parser("preflight")
    check.add_argument("--claims", required=True)
    check.add_argument("--cards", default="")
    check.add_argument("--output", default="/workspace/outputs/data-relations")
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
            code = 2 if payload.get("status") in {"selection_required", "blocked_trace_required"} else 0
            print_agent_json(payload, stream=sys.stderr if code else sys.stdout)
            return code
        if args.command.startswith("trace-review-") or args.command.startswith("micro-process-"):
            payload = trace_review_command(args)
            code = 2 if payload.get("status") == "blocked_platform_approval_required" else 0
            print_agent_json(payload, stream=sys.stderr if code else sys.stdout)
            return code
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
            payload = mutate_claims(args)
            code = 2 if payload.get("status") == "recovery_blocked" else 0
            print_agent_json(payload, stream=sys.stderr if code else sys.stdout)
            return code
        if args.command == "preflight":
            code, payload = preflight(args)
            print_agent_json(payload, stream=sys.stderr if code else sys.stdout)
            return code
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
