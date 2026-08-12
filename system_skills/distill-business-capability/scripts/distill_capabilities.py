#!/usr/bin/env python3
"""Distill accepted relation and flow artifacts into portable multi-Skill source."""

from __future__ import annotations

import argparse
import ast
import hashlib
import hmac
import json
import os
import re
import shutil
import sys
import zipfile
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence


SCHEMA_VERSION = 1
GENERATOR_CONTRACT_VERSION = 3
RELEASE_CONTRACT_VERSION = 1
RELATION_CAPABILITY = "discover-data-relations"
FLOW_CAPABILITY = "derive-business-flow"
PLATFORM_APPROVAL_ISSUER = "business-flow-platform"
PLATFORM_APPROVAL_KEY_ENV = "BUSINESS_FLOW_PLATFORM_APPROVAL_HMAC_KEY"
MAX_SOURCE_BYTES = 2 * 1024 * 1024
MAX_EVIDENCE_BYTES = 8 * 1024 * 1024
MAX_OPERATIONAL_BYTES = 8 * 1024 * 1024
MAX_PLATFORM_APPROVAL_BYTES = 2 * 1024 * 1024
MAX_CANDIDATE_BYTES = 512 * 1024
MAX_FILES = 500
MAX_STAGE_SKILLS = 12
MAX_PROCEDURE_STEPS = 10
SHA256_DIGEST = re.compile(r"^[0-9a-f]{64}$")

TABULAR_EXTENSIONS = {
    ".csv", ".tsv", ".xlsx", ".xls", ".xlsb", ".parquet", ".jsonl", ".ndjson",
    ".sqlite", ".sqlite3", ".db",
}
DOCUMENT_EXTENSIONS = {".txt", ".md", ".html", ".htm", ".xml", ".json", ".yaml", ".yml", ".docx", ".pptx", ".pdf"}
OCR_EXTENSIONS = {".pdf", ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".tif", ".tiff", ".webp"}
FOUNDATION_SPECS = {
    "tabular": {
        "id": "foundation-tabular",
        "template": "portable-tabular-reader",
        "engine": "duckdb",
    },
    "document": {
        "id": "foundation-document",
        "template": "portable-document-reader",
        "engine": "portable-document-extractors",
    },
    "ocr": {
        "id": "foundation-ocr",
        "template": "adapted-ocr-parser",
        "engine": "httpx-ocr-client",
        "source_skill": "ocr-parser",
        "credential_bindings": {
            "OCR_API_KEY": ("config/defaults.json", "OCR_API_KEY"),
        },
    },
    "knowledge": {
        "id": "foundation-knowledge",
        "template": "adapted-vector-kb",
        "engine": "vector-kb-http-client",
        "source_skill": "vector-kb",
        "credential_bindings": {
            "VECTOR_KB_API_KEY": ("config/defaults.json", "api_key"),
        },
    },
}
KNOWLEDGE_MARKERS = (
    "knowledge base", "knowledge-base", "vector kb", "vector-kb", "external knowledge",
    "知识库", "外部知识", "药品知识", "政策知识", "规范知识",
)
ID_PATTERN = re.compile(r"^[a-z][a-z0-9_-]{1,63}$")
SKILL_NAME_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
FORBIDDEN_PORTABLE_TEXT = {
    "/workspace": "platform workspace path",
    "/skills/": "platform Skill mount path",
    "report_task_progress": "platform progress Tool",
    "discover_studio_capabilities": "Studio capability discovery",
    "call_tool": "Studio Tool gateway",
    "call_mcp": "Studio MCP gateway",
    "outputs/data-relations": "internal upstream output path",
    "outputs/business-flow": "internal upstream output path",
}
RESOLVED_QUESTION_STATUSES = {"resolved", "closed", "answered", "已解决", "已关闭", "已回答"}
RUNTIME_EXCEPTION_STATUSES = {"runtime_exception", "exception", "waived", "运行时例外", "例外"}
APPROVED_EXCEPTION_STATUSES = {"approved", "accepted", "允许", "批准", "同意"}
USER_APPROVAL_ACTORS = {"user", "business_user", "customer", "用户", "业务用户"}
USER_APPROVAL_DECISIONS = {"approved", "accepted", "允许", "批准", "同意"}
CRITICAL_QUESTION_MARKERS = {
    "critical", "blocker", "p0", "p1", "high", "关键", "严重", "重大", "高风险", "高影响",
}


class ContractError(ValueError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def atomic_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)


def atomic_json(path: Path, payload: dict[str, Any]) -> None:
    atomic_text(path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


def load_json(path: Path, max_bytes: int) -> dict[str, Any]:
    if not path.is_file():
        raise ContractError(f"缺少文件：{path}")
    size = path.stat().st_size
    if size > max_bytes:
        raise ContractError(f"文件超过有界读取上限（{size} > {max_bytes} bytes）：{path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ContractError(f"无法读取合法 JSON：{path}：{exc}") from exc
    if not isinstance(payload, dict):
        raise ContractError(f"顶层 JSON 必须是对象：{path}")
    return payload


def load_json_with_digest(path: Path, max_bytes: int) -> tuple[str, dict[str, Any]]:
    """Read, hash and parse one byte sequence to avoid a check/use race."""

    if not path.is_file():
        raise ContractError(f"缺少文件：{path}")
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise ContractError(f"无法读取文件：{path}：{exc}") from exc
    if size > max_bytes:
        raise ContractError(f"文件超过有界读取上限（{size} > {max_bytes} bytes）：{path}")
    try:
        raw = path.read_bytes()
        if len(raw) > max_bytes:
            raise ContractError(f"文件超过有界读取上限：{path}")
        payload = json.loads(raw.decode("utf-8"))
    except ContractError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ContractError(f"无法读取合法 JSON：{path}：{exc}") from exc
    if not isinstance(payload, dict):
        raise ContractError(f"顶层 JSON 必须是对象：{path}")
    return hashlib.sha256(raw).hexdigest(), payload


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(64 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def is_sha256_digest(value: Any) -> bool:
    """Return whether a value is a normalized SHA-256 digest string."""

    return bool(SHA256_DIGEST.fullmatch(str(value or "").strip().casefold()))


def platform_approval_signing_payload(envelope: dict[str, Any]) -> bytes:
    """Use the same UTF-8 canonical HMAC payload as the platform workbench."""

    unsigned = {name: value for name, value in envelope.items() if name != "signature"}
    return json.dumps(
        unsigned, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")


def platform_approval_signature(envelope: dict[str, Any], key: str) -> str:
    return hmac.new(
        key.encode("utf-8"), platform_approval_signing_payload(envelope), hashlib.sha256,
    ).hexdigest()


def platform_approvals_path(relation_root: Path) -> Path:
    return relation_root.resolve() / "platform-approvals.json"


def _same_resolved_path(value: Any, expected: Path) -> bool:
    try:
        return Path(str(value)).resolve() == expected.resolve()
    except OSError:
        return False


def platform_evidence_receipt_errors(relation_root: Path) -> list[str]:
    """Verify the signed trace-review and micro-process evidence chain.

    Capability distillation must not turn a free-text review or an old flow
    into a portable package.  It rechecks the canonical data-relations
    evidence at consumption time, rather than trusting a prior process's
    success status.
    """

    key = os.environ.get(PLATFORM_APPROVAL_KEY_ENV, "")
    if not key:
        return [
            f"Platform approval verifier is unavailable: {PLATFORM_APPROVAL_KEY_ENV} is not configured. "
            "Capability distillation is fail-closed."
        ]

    root = relation_root.resolve()
    trace_path = root / "trace-samples.json"
    review_path = root / "trace-review.json"
    micro_path = root / "micro-process.json"
    ledger_path = platform_approvals_path(root)
    for label, path in (
        ("trace-samples", trace_path),
        ("trace-review", review_path),
        ("micro-process", micro_path),
        ("platform approval ledger", ledger_path),
    ):
        if not path.is_file():
            return [f"Canonical {label} artifact is missing: {path}"]

    try:
        trace = load_json(trace_path, MAX_OPERATIONAL_BYTES)
        review = load_json(review_path, MAX_OPERATIONAL_BYTES)
        micro = load_json(micro_path, MAX_OPERATIONAL_BYTES)
        ledger = load_json(ledger_path, MAX_PLATFORM_APPROVAL_BYTES)
    except ContractError as exc:
        return [f"Platform approval evidence cannot be read: {exc}"]

    errors: list[str] = []
    trace_fingerprint = sha256_file(trace_path)
    review_fingerprint = sha256_file(review_path)
    micro_fingerprint = sha256_file(micro_path)
    if trace.get("status") != "complete":
        errors.append("Canonical trace-samples.json is not complete")

    trace_reference = review.get("trace") if isinstance(review.get("trace"), dict) else {}
    if review.get("schema_version") != SCHEMA_VERSION or review.get("kind") != "trace_review":
        errors.append("Canonical trace-review.json is not a supported trace_review contract")
    if review.get("status") != "approved":
        errors.append("Canonical trace-review.json is not approved")
    if not _same_resolved_path(trace_reference.get("artifact"), trace_path):
        errors.append("Trace review does not reference canonical trace-samples.json")
    if str(trace_reference.get("fingerprint", "")) != trace_fingerprint:
        errors.append("Trace review does not bind the current trace-samples fingerprint")
    if not str(trace_reference.get("bundle_id", "")).strip():
        errors.append("Trace review does not bind a trace bundle")
    review_approval = review.get("approval") if isinstance(review.get("approval"), dict) else {}
    if review_approval.get("decision") != "approved":
        errors.append("Trace review approval decision is not approved")

    micro_source = micro.get("source") if isinstance(micro.get("source"), dict) else {}
    if micro.get("schema_version") != SCHEMA_VERSION or micro.get("kind") != "trace_micro_process":
        errors.append("Canonical micro-process.json is not a supported trace_micro_process contract")
    if micro.get("status") != "approved":
        errors.append("Canonical micro-process.json is not approved")
    if not _same_resolved_path(micro_source.get("trace_review"), review_path):
        errors.append("Micro-process does not reference canonical trace-review.json")
    if str(micro_source.get("trace_review_fingerprint", "")) != review_fingerprint:
        errors.append("Micro-process does not bind the current trace-review fingerprint")
    micro_approval = micro.get("approval") if isinstance(micro.get("approval"), dict) else {}
    if micro_approval.get("decision") != "approved":
        errors.append("Micro-process approval decision is not approved")

    if (
        ledger.get("schema_version") != SCHEMA_VERSION
        or ledger.get("kind") != "platform_approval_envelopes"
        or ledger.get("issuer") != PLATFORM_APPROVAL_ISSUER
    ):
        errors.append("Platform approval ledger has an unsupported issuer or schema")
        return errors
    approvals = ledger.get("approvals")
    if not isinstance(approvals, list):
        return errors + ["Platform approval ledger must contain an approvals array"]

    def has_receipt(kind: str, artifact_fingerprint: str) -> bool:
        for envelope in approvals:
            if not isinstance(envelope, dict):
                continue
            if (
                envelope.get("schema_version") != SCHEMA_VERSION
                or envelope.get("issuer") != PLATFORM_APPROVAL_ISSUER
                or envelope.get("artifact_kind") != kind
                or envelope.get("decision") != "approved"
                or str(envelope.get("artifact_fingerprint", "")) != artifact_fingerprint
                or str(envelope.get("trace_fingerprint", "")) != trace_fingerprint
                or not str(envelope.get("approval_id", "")).strip()
                or not str(envelope.get("subject", "")).strip()
                or not str(envelope.get("issued_at", "")).strip()
            ):
                continue
            signature = str(envelope.get("signature", "")).strip().casefold()
            expected = platform_approval_signature(envelope, key).casefold()
            if hmac.compare_digest(signature, expected):
                return True
        return False

    if not has_receipt("trace_review", review_fingerprint):
        errors.append(
            "No valid platform-signed trace_review receipt matches the current trace-review and trace-samples artifacts"
        )
    if not has_receipt("micro_process", micro_fingerprint):
        errors.append(
            "No valid platform-signed micro_process receipt matches the current micro-process and trace-samples artifacts"
        )
    return errors


def platform_package_receipt_errors(
    relation_root: Path,
    manifest_path: Path,
    manifest: dict[str, Any],
) -> list[str]:
    """Verify the platform's independent approval of the release archive.

    ``capability-manifest.json`` is generated by the distillation process and
    therefore cannot, by itself, be the authority that releases the package.
    The platform appends a separately HMAC-signed approval envelope to the
    data-relations ledger after a human reviews the concrete ``skill.zip``.
    Bind that receipt to every release-critical artifact plus the exact
    upstream relation/flow fingerprints so an old approval cannot publish a
    regenerated or partially modified package.
    """

    key = os.environ.get(PLATFORM_APPROVAL_KEY_ENV, "")
    if not key:
        return [
            f"Platform approval verifier is unavailable: {PLATFORM_APPROVAL_KEY_ENV} is not configured. "
            "Capability package publication is fail-closed."
        ]

    source = manifest.get("source") if isinstance(manifest.get("source"), dict) else {}
    relation_fingerprint = str(source.get("relation_fingerprint", "")).strip()
    flow_fingerprint = str(source.get("flow_fingerprint", "")).strip()
    if not relation_fingerprint or not flow_fingerprint:
        return [
            "Capability manifest is missing its relation_fingerprint or flow_fingerprint; "
            "a package approval receipt cannot be bound safely."
        ]

    release_root = manifest_path.resolve().parent
    expected_artifact_paths = {
        "capability_manifest": manifest_path.resolve(),
        "release_manifest": release_root / "release" / "release.json",
        "skill_archive": release_root / "release" / "artifacts" / "skill.zip",
        "mcp_stdio_archive": release_root / "release" / "artifacts" / "mcp-stdio.zip",
    }
    missing_artifacts = [
        f"{name} ({path})" for name, path in expected_artifact_paths.items() if not path.is_file()
    ]
    if missing_artifacts:
        return [
            "Capability release artifacts are missing; a platform package receipt cannot be verified: "
            + ", ".join(missing_artifacts)
        ]
    package_artifacts = {
        name: sha256_file(path) for name, path in expected_artifact_paths.items()
    }
    artifact_fingerprint = package_artifacts["skill_archive"]

    ledger_path = platform_approvals_path(relation_root)
    if not ledger_path.is_file():
        return [
            "Platform-signed capability_package approval is required; platform-approvals.json is missing."
        ]
    try:
        ledger = load_json(ledger_path, MAX_PLATFORM_APPROVAL_BYTES)
    except ContractError as exc:
        return [f"Platform package approval ledger cannot be read: {exc}"]

    if (
        ledger.get("schema_version") != SCHEMA_VERSION
        or ledger.get("kind") != "platform_approval_envelopes"
        or ledger.get("issuer") != PLATFORM_APPROVAL_ISSUER
    ):
        return ["Platform package approval ledger has an unsupported issuer or schema"]
    approvals = ledger.get("approvals")
    if not isinstance(approvals, list):
        return ["Platform package approval ledger must contain an approvals array"]

    package_receipts = 0
    invalid_matching_receipts: list[str] = []
    mismatch_reasons: set[str] = set()
    for index, envelope in enumerate(approvals):
        if not isinstance(envelope, dict) or envelope.get("artifact_kind") != "capability_package":
            continue
        package_receipts += 1

        receipt_artifact = str(envelope.get("artifact_fingerprint", "")).strip()
        receipt_relation = str(envelope.get("relation_fingerprint", "")).strip()
        receipt_flow = str(envelope.get("flow_fingerprint", "")).strip()
        receipt_artifacts = envelope.get("package_artifacts")
        receipt_mismatches: list[str] = []
        if receipt_artifact != artifact_fingerprint:
            receipt_mismatches.append("skill.zip fingerprint")
        if receipt_relation != relation_fingerprint:
            receipt_mismatches.append("relation fingerprint")
        if receipt_flow != flow_fingerprint:
            receipt_mismatches.append("flow fingerprint")
        if not isinstance(receipt_artifacts, dict):
            receipt_mismatches.append("package_artifacts")
        else:
            for name, expected_fingerprint in package_artifacts.items():
                if str(receipt_artifacts.get(name, "")).strip() != expected_fingerprint:
                    receipt_mismatches.append(f"package_artifacts.{name}")
        if receipt_mismatches:
            mismatch_reasons.update(receipt_mismatches)
            continue

        missing_fields = [
            field for field in ("approval_id", "subject", "issued_at", "signature")
            if not str(envelope.get(field, "")).strip()
        ]
        if (
            envelope.get("schema_version") != SCHEMA_VERSION
            or envelope.get("issuer") != PLATFORM_APPROVAL_ISSUER
            or envelope.get("decision") != "approved"
            or missing_fields
        ):
            invalid_matching_receipts.append(
                f"receipt #{index} has an invalid contract"
                + (f" (missing {', '.join(missing_fields)})" if missing_fields else "")
            )
            continue

        signature = str(envelope.get("signature", "")).strip().casefold()
        expected = platform_approval_signature(envelope, key).casefold()
        if not hmac.compare_digest(signature, expected):
            invalid_matching_receipts.append(f"receipt #{index} has an invalid platform signature")
            continue
        return []

    if invalid_matching_receipts:
        return [
            "Capability package approval receipt is malformed or tampered: "
            + "; ".join(invalid_matching_receipts)
        ]
    if package_receipts:
        detail = ", ".join(sorted(mismatch_reasons)) or "required receipt fields"
        return [
            "No valid platform-signed capability_package receipt matches the current "
            f"release and upstream fingerprints ({detail} differs)."
        ]
    return [
        "No platform-signed capability_package receipt is present for the current release archive."
    ]


def compact(value: Any, limit: int = 360) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def unique_strings(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return list(dict.fromkeys(str(item) for item in value if str(item)))


def normalized_question_value(value: Any) -> str:
    return re.sub(r"\s+", "", str(value or "").casefold())


def question_is_critical(question: dict[str, Any]) -> bool:
    if question.get("critical") is True:
        return True
    for key in ("severity", "priority", "impact", "risk"):
        value = normalized_question_value(question.get(key))
        if value in CRITICAL_QUESTION_MARKERS or any(marker in value for marker in CRITICAL_QUESTION_MARKERS):
            return True
    return False


def runtime_exception_errors(question: dict[str, Any], identifier: str) -> list[str]:
    """Require an explicit, user-backed waiver before packaging an uncertainty."""

    exception = question.get("runtime_exception")
    if not isinstance(exception, dict):
        return [
            f"open_question {identifier} 需要显式 runtime_exception，且必须附带用户批准证据"
        ]
    errors: list[str] = []
    if normalized_question_value(exception.get("status")) not in APPROVED_EXCEPTION_STATUSES:
        errors.append(f"open_question {identifier}.runtime_exception.status 必须为 approved")
    if len(str(exception.get("reason", "")).strip()) < 8:
        errors.append(f"open_question {identifier}.runtime_exception.reason 必须说明运行时例外原因")
    if len(str(exception.get("scope", "")).strip()) < 4:
        errors.append(f"open_question {identifier}.runtime_exception.scope 必须说明例外适用范围")
    approval = exception.get("user_approval")
    if not isinstance(approval, dict):
        errors.append(f"open_question {identifier}.runtime_exception 必须包含 user_approval 用户批准证据")
        return errors
    if normalized_question_value(approval.get("actor")) not in USER_APPROVAL_ACTORS:
        errors.append(f"open_question {identifier}.runtime_exception.user_approval.actor 必须标识为用户")
    if normalized_question_value(approval.get("decision")) not in USER_APPROVAL_DECISIONS:
        errors.append(f"open_question {identifier}.runtime_exception.user_approval.decision 必须为 approved")
    if not (
        str(approval.get("user_id", "")).strip()
        or str(approval.get("actor_id", "")).strip()
    ):
        errors.append(f"open_question {identifier}.runtime_exception.user_approval 缺少 user_id")
    if not (
        str(approval.get("evidence_ref", "")).strip()
        or str(approval.get("evidence_id", "")).strip()
    ):
        errors.append(f"open_question {identifier}.runtime_exception.user_approval 缺少 evidence_ref")
    if len(str(approval.get("approved_at", "")).strip()) < 8:
        errors.append(f"open_question {identifier}.runtime_exception.user_approval 缺少 approved_at")
    return errors


def open_question_gate_errors(open_questions: Any) -> list[str]:
    """Return release blockers for unresolved or critical business questions."""

    if not isinstance(open_questions, list):
        return []
    errors: list[str] = []
    for index, question in enumerate(open_questions):
        if not isinstance(question, dict):
            continue
        identifier = str(question.get("id") or f"index-{index}")
        exception = question.get("runtime_exception")
        if exception is not None:
            exception_errors = runtime_exception_errors(question, identifier)
            if not exception_errors:
                continue
            errors.extend(exception_errors)
            continue
        status = normalized_question_value(question.get("status") or "open")
        if status in RUNTIME_EXCEPTION_STATUSES:
            errors.extend(runtime_exception_errors(question, identifier))
            continue
        if question_is_critical(question):
            errors.append(
                f"critical open_question {identifier} 阻断 finalize；解决后应移出 open_questions，"
                "或提供已批准的 runtime_exception"
            )
            continue
        if status in RESOLVED_QUESTION_STATUSES:
            if len(str(question.get("resolution", "")).strip()) < 4:
                errors.append(f"resolved open_question {identifier} 缺少可审计的 resolution")
            continue
        errors.append(
            f"unresolved open_question {identifier} 阻断 finalize；先记录 resolution，"
            "或提供带用户批准证据的 runtime_exception"
        )
    return errors


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


def normalized_text(value: Any) -> str:
    return re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", str(value or "").casefold())


def infer_column_semantic_role(column: dict[str, Any]) -> str:
    """Assign a portable semantic role from schema facts only.

    This is intentionally a small vocabulary.  It describes how a field can
    participate in a workflow, not what business domain the field belongs to.
    """
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


def source_column_descriptors(source: dict[str, Any]) -> list[dict[str, Any]]:
    descriptors: list[dict[str, Any]] = []
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
            descriptors.append({
                "source_id": str(source.get("source_id", "")),
                "table": table_name,
                "column": name,
                "kind": str(column.get("kind", "other")),
                "semantic_role": infer_column_semantic_role(column),
            })
    return descriptors


def is_external_knowledge_node(node: dict[str, Any]) -> bool:
    text = " ".join(str(node.get(key, "")) for key in ("name", "description", "type")).casefold()
    return any(marker.casefold() in text for marker in KNOWLEDGE_MARKERS) or any(
        marker in text
        for marker in ("crawler", "scraper", "web search", "remote api", "external api", "爬虫", "网络检索", "外部接口", "远程接口")
    )


def looks_like_structured_rule_source(source: dict[str, Any], result_source_ids: set[str]) -> bool:
    """Recover a missing rule role from a generic structural profile.

    The preferred source is ``material_roles`` emitted by relation discovery.
    The fallback is retained only for legacy artifacts and uses portable
    vocabulary (rule/policy, narrative, selector, identifier), never a
    scenario-specific field name.
    """
    source_id = str(source.get("source_id", ""))
    if source_id in result_source_ids:
        return False
    roles = source.get("roles") if isinstance(source.get("roles"), list) else []
    if any(str(role.get("node_type", "")) == "rule" for role in roles if isinstance(role, dict)):
        return True
    material_roles = {
        str(value) for value in source.get("material_roles", [])
        if str(value)
    }
    if "rule_or_policy_material" in material_roles:
        return True
    table_roles = {
        str(table.get("inferred_material_role", ""))
        for table in source.get("tables", []) if isinstance(table, dict)
    }
    if "rule_or_policy_material" in table_roles:
        return True
    text = " ".join(str(source.get(key, "")) for key in ("path", "view_name", "table_name")).casefold()
    if any(marker in text for marker in ("rule", "policy", "规则", "政策", "规范")):
        return True
    descriptors = source_column_descriptors(source)
    # A small policy table can legitimately lack an explicit relation node;
    # broad transactional/event tables must not be promoted merely because
    # they happen to contain a description and a status field.
    if len(descriptors) > 20:
        return False
    role_counts = defaultdict(int)
    for item in descriptors:
        role_counts[str(item.get("semantic_role", ""))] += 1
    return (
        role_counts["narrative"] >= 1
        and role_counts["selector"] >= 1
        and role_counts["identifier"] >= 1
    )


def normalize_operational_runtime_contract(
    relations: dict[str, Any], operational: dict[str, Any],
) -> dict[str, Any]:
    """Upgrade legacy design-time contracts into portable runtime bindings."""

    normalized = json.loads(json.dumps(operational, ensure_ascii=False))
    external_node_ids = {
        str(node.get("id", ""))
        for node in relations.get("nodes", [])
        if isinstance(node, dict) and is_external_knowledge_node(node)
    }
    result_source_ids = set(unique_strings(normalized.get("result_source_ids")))
    inferred_rule_source_ids: list[str] = []
    runtime_source_ids: list[str] = []
    template_source_ids: list[str] = []
    for source in normalized.get("sources", []):
        if not isinstance(source, dict):
            continue
        source["roles"] = [
            role
            for role in source.get("roles", [])
            if isinstance(role, dict) and str(role.get("node_id", "")) not in external_node_ids
        ]
        if looks_like_structured_rule_source(source, result_source_ids) and not any(
            str(role.get("node_type", "")) == "rule"
            for role in source["roles"] if isinstance(role, dict)
        ):
            digest = hashlib.sha1(
                f"inferred-rule\0{source.get('source_id', '')}".encode("utf-8")
            ).hexdigest()[:12]
            source["roles"].append({
                "node_id": f"inferred_rule_{digest}",
                "node_name": Path(str(source.get("path", ""))).stem or str(source.get("source_id", "")),
                "node_type": "rule",
                "inference": "distillation_structural_fallback",
            })
            source.setdefault("material_roles", []).append("rule_or_policy_material")
            inferred_rule_source_ids.append(str(source.get("source_id", "")))
        source["material_roles"] = list(dict.fromkeys(
            str(value) for value in source.get("material_roles", []) if str(value)
        ))
        if "rule_or_policy_material" in source["material_roles"]:
            source["material_role"] = "rule_or_policy_material"
        elif source.get("material_roles"):
            source["material_role"] = str(source["material_roles"][0])
        role_types = {str(role.get("node_type", "")) for role in source["roles"]}
        source_id = str(source.get("source_id", ""))
        runtime_roles = role_types.intersection({"actor", "input", "object", "rule", "state"})
        if runtime_roles:
            source.update({
                "lifecycle": "runtime_input",
                "runtime_required": True,
                "runtime_binding": "required_when_referenced_by_stage_or_query",
                "integrity_policy": "schema_compatible_runtime_binding",
            })
            runtime_source_ids.append(source_id)
        elif source_id in result_source_ids or "output" in role_types:
            source.update({
                "lifecycle": "design_time_template",
                "runtime_required": False,
                "runtime_binding": "not_required",
                "integrity_policy": "design_fingerprint_only",
                "template_policy": {
                    "retained": ["format", "table_or_section", "header", "columns", "types", "locators"],
                    "example_data": "optional_deidentified_bounded_example_only",
                    "original_file_required_at_runtime": False,
                },
            })
            template_source_ids.append(source_id)
        else:
            source.update({
                "lifecycle": "design_time_evidence",
                "runtime_required": False,
                "runtime_binding": "not_required",
                "integrity_policy": "design_fingerprint_only",
            })
    runtime_ids = set(runtime_source_ids)
    for link in normalized.get("links", []):
        if isinstance(link, dict):
            link["runtime_eligible"] = (
                str(link.get("source_id", "")) in runtime_ids
                and str(link.get("target_id", "")) in runtime_ids
            )
    for route in normalized.get("semantic_routes", []):
        if isinstance(route, dict):
            route["runtime_eligible"] = (
                str(route.get("source_id", "")) in runtime_ids
                and str(route.get("target_id", "")) in runtime_ids
            )
    normalized["schema_version"] = max(2, int(normalized.get("schema_version", 1)))
    normalized["runtime_source_ids"] = runtime_source_ids
    normalized["template_source_ids"] = template_source_ids
    normalized["rule_source_ids"] = [
        source_id
        for source_id in unique_strings(normalized.get("rule_source_ids"))
        if source_id in runtime_ids
    ]
    normalized["rule_source_ids"] = list(dict.fromkeys([
        *normalized["rule_source_ids"],
        *[source_id for source_id in inferred_rule_source_ids if source_id in runtime_ids],
    ]))
    normalized["rule_source_inference"] = (
        "explicit_or_structural_contract"
        if normalized["rule_source_ids"] else "missing"
    )
    normalized["external_capabilities"] = [
        {
            "node_id": str(node.get("id", "")),
            "name": str(node.get("name", "")),
            "description": str(node.get("description", "")),
            "lifecycle": "optional_enrichment",
            "runtime_required": "agent_decides_from_user_request_and_complete_rule_record",
            "activation": "agent_determines_from_user_request_and_complete_rule_record",
            "failure_policy": "manual_intervention_required_when_mandatory_and_unavailable",
        }
        for node in relations.get("nodes", [])
        if isinstance(node, dict) and str(node.get("id", "")) in external_node_ids
    ]
    policy = normalized.get("query_policy") if isinstance(normalized.get("query_policy"), dict) else {}
    normalized["query_policy"] = {
        **policy,
        "register_only_sources_referenced_by_the_current_operation": True,
        "runtime_data_validation": "schema_compatibility_not_design_time_content_identity",
        "design_time_templates_are_not_runtime_dependencies": True,
    }
    return normalized


def compact_trace_evidence(operational: dict[str, Any], *, include_rows: bool) -> dict[str, Any]:
    trace = operational.get("trace_evidence") if isinstance(operational.get("trace_evidence"), dict) else {}
    bundles = []
    for bundle in trace.get("bundles", [])[:2]:
        if not isinstance(bundle, dict):
            continue
        sources = []
        for source in bundle.get("sources", [])[:12]:
            if not isinstance(source, dict):
                continue
            item = {
                "source_id": source.get("source_id"),
                "path": source.get("path"),
                "table": source.get("table"),
                "role": source.get("role"),
                "selected_columns": source.get("selected_columns", []),
            }
            if include_rows:
                item["rows"] = [
                    {
                        "row_number": row.get("row_number"),
                        "values": dict(list(row.get("values", {}).items())[:16]),
                    }
                    for row in source.get("rows", [])[:2]
                    if isinstance(row, dict)
                ]
            sources.append(item)
        bundles.append({
            "bundle_id": bundle.get("bundle_id"),
            "anchor": bundle.get("anchor", {}),
            "coverage": bundle.get("coverage", {}),
            "sources": sources,
            "links": [
                {
                    "link_id": item.get("link_id"),
                    "source_id": item.get("source_id"),
                    "target_id": item.get("target_id"),
                    "key_pairs": item.get("key_pairs", []),
                    "confidence": item.get("confidence"),
                    "matched_row_count": item.get("matched_row_count"),
                    "fanout_warning": item.get("fanout_warning"),
                }
                for item in bundle.get("links", [])[:12]
                if isinstance(item, dict)
            ],
            "semantic_evidence": bundle.get("semantic_evidence", [])[:2] if include_rows else [],
        })
    return {
        "status": trace.get("status", "missing"),
        "strategy": trace.get("strategy", ""),
        "bundles": bundles,
    }


def validate_identifier(owner: str, value: Any, errors: list[str]) -> str:
    identifier = str(value or "")
    if not ID_PATTERN.fullmatch(identifier):
        errors.append(f"{owner} id 必须是简短稳定的 ASCII 标识：{identifier or '<empty>'}")
    return identifier


def validate_skill_name(owner: str, value: Any, errors: list[str]) -> str:
    name = str(value or "")
    if not SKILL_NAME_PATTERN.fullmatch(name) or len(name) > 63:
        errors.append(f"{owner} skill_name 必须是 63 字符内的小写 kebab-case：{name or '<empty>'}")
    return name


def validate_string_list(owner: str, value: Any, errors: list[str], *, minimum: int = 0) -> list[str]:
    if not isinstance(value, list):
        errors.append(f"{owner} 必须是数组")
        return []
    items = [str(item).strip() for item in value if str(item).strip()]
    if len(items) != len(set(items)):
        errors.append(f"{owner} 不能包含重复项")
    if len(items) < minimum:
        errors.append(f"{owner} 至少需要 {minimum} 项")
    return items


def validate_relations(path: Path) -> tuple[dict[str, Any], list[str]]:
    errors: list[str] = []
    try:
        payload = load_json(path, MAX_SOURCE_BYTES)
    except ContractError as exc:
        return {}, [str(exc)]
    if payload.get("status") != "complete":
        errors.append("scenario-relationship.json 的 status 必须为 complete")
    if not isinstance(payload.get("nodes"), list) or not payload.get("nodes"):
        errors.append("关系产物缺少非空 nodes")
    if not isinstance(payload.get("edges"), list) or not payload.get("edges"):
        errors.append("关系产物缺少非空 edges")
    if not isinstance(payload.get("scenario"), dict) or not str(payload.get("scenario", {}).get("name", "")).strip():
        errors.append("关系产物缺少 scenario.name")
    for required in ("relations.mmd", "relation-report.md"):
        if not (path.parent / required).is_file():
            errors.append(f"关系产物缺少 {required}")
    if (path.parent / "validation-errors.json").exists():
        errors.append("关系产物目录仍存在 validation-errors.json")
    _, _, operational_errors = operational_context(payload, path)
    errors.extend(operational_errors)
    return payload, errors


def operational_context(
    relations: dict[str, Any], relation_path: Path,
) -> tuple[Path, dict[str, Any], list[str]]:
    errors: list[str] = []
    claim = relations.get("operational_contract") if isinstance(relations.get("operational_contract"), dict) else {}
    path = relation_path.parent / "operational-data-contract.json"
    try:
        claimed_path = Path(str(claim.get("artifact", ""))).resolve()
    except OSError:
        claimed_path = Path("__invalid__")
    if claimed_path != path.resolve():
        errors.append("关系产物未精确引用 operational-data-contract.json")
    try:
        payload = load_json(path, MAX_OPERATIONAL_BYTES)
    except ContractError as exc:
        return path, {}, errors + [str(exc)]
    fingerprint = sha256_file(path)
    if claim.get("fingerprint") != fingerprint:
        errors.append("operational-data-contract.json fingerprint 已过期")
    if claim.get("status") != "ready" or payload.get("status") != "ready":
        blockers = payload.get("quality_gates", {}).get("blockers", [])
        errors.append("数据执行契约未通过质量门禁：" + "；".join(str(item) for item in blockers))
    return path, normalize_operational_runtime_contract(relations, payload), errors


def validate_flow(path: Path, relation_path: Path, relation_fingerprint: str) -> tuple[dict[str, Any], list[str]]:
    errors: list[str] = []
    try:
        payload = load_json(path, MAX_SOURCE_BYTES)
    except ContractError as exc:
        return {}, [str(exc)]
    if payload.get("status") != "complete":
        errors.append("business-flow.json 的 status 必须为 complete")
    source = payload.get("source") if isinstance(payload.get("source"), dict) else {}
    if source.get("capability") != RELATION_CAPABILITY:
        errors.append("流程产物 source.capability 必须为 discover-data-relations")
    try:
        source_path = Path(str(source.get("artifact", ""))).resolve()
    except OSError:
        source_path = Path("__invalid__")
    if source_path != relation_path:
        errors.append("流程产物没有引用当前关系产物")
    if source.get("fingerprint") != relation_fingerprint:
        errors.append("流程产物引用的关系 fingerprint 已过期")
    operational_path = relation_path.parent / "operational-data-contract.json"
    operational_claim = source.get("operational_data_contract") if isinstance(source.get("operational_data_contract"), dict) else {}
    try:
        claimed_operational_path = Path(str(operational_claim.get("artifact", ""))).resolve()
    except OSError:
        claimed_operational_path = Path("__invalid__")
    if claimed_operational_path != operational_path.resolve():
        errors.append("流程产物没有引用当前 operational-data-contract.json")
    if operational_path.is_file() and operational_claim.get("fingerprint") != sha256_file(operational_path):
        errors.append("流程产物引用的数据执行契约 fingerprint 已过期")
    micro_path = relation_path.parent / "micro-process.json"
    micro_claim = source.get("micro_process") if isinstance(source.get("micro_process"), dict) else {}
    try:
        claimed_micro_path = Path(str(micro_claim.get("artifact", ""))).resolve()
    except OSError:
        claimed_micro_path = Path("__invalid__")
    if claimed_micro_path != micro_path.resolve():
        errors.append("流程产物没有引用当前已批准的 micro-process.json")
    elif not micro_path.is_file():
        errors.append("当前关系目录缺少 micro-process.json")
    elif micro_claim.get("fingerprint") != sha256_file(micro_path):
        errors.append("流程产物引用的 micro-process fingerprint 已过期")
    else:
        try:
            micro = load_json(micro_path, MAX_OPERATIONAL_BYTES)
        except ContractError as exc:
            errors.append(str(exc))
            micro = {}
        if micro.get("kind") != "trace_micro_process" or micro.get("status") != "approved":
            errors.append("micro-process.json 必须是已批准的样本无关复现契约")
        elif micro_claim.get("status") != "approved":
            errors.append("流程产物未声明已批准的 micro-process 状态")

    # Re-verify the platform authority at the consumption boundary.  A flow
    # may have been produced before an approval was revoked, the trace may
    # have been regenerated, or a caller may be trying to bypass derive's
    # validation by supplying a hand-written flow artifact.
    errors.extend(platform_evidence_receipt_errors(relation_path.parent))

    expected_execution_policy = {
        "rule_resolution": "complete_rule_record_before_bulk_query",
        "bulk_data_access": "bounded_read_only_sql",
        "join_policy": "evidence_backed_keys_with_runtime_fanout_validation",
        "agent_direct_file_read": False,
        "unstructured_access": "parse_or_ocr_then_provenance_chunk_search",
    }
    # The flow contract owns this object and may add new, machine-verifiable
    # safeguards (for example the selected trace policy).  Rejecting an
    # otherwise valid flow merely because it carries an additive safeguard
    # silently breaks forward compatibility between the two platform skills.
    execution_policy = payload.get("execution_policy")
    if not isinstance(execution_policy, dict) or any(
        execution_policy.get(key) != value
        for key, value in expected_execution_policy.items()
    ):
        errors.append("流程产物缺少规则优先、只读 SQL 和关联校验执行策略")
    stages = payload.get("stages")
    if not isinstance(stages, list) or not 1 <= len(stages) <= MAX_STAGE_SKILLS:
        errors.append(f"流程产物必须包含 1-{MAX_STAGE_SKILLS} 个 stages")
    stage_ids = [str(item.get("id", "")) for item in stages or [] if isinstance(item, dict)]
    if len(stage_ids) != len(stages or []) or any(not item for item in stage_ids) or len(set(stage_ids)) != len(stage_ids):
        errors.append("流程阶段必须具有非空且唯一的 id")
    main_flow = payload.get("main_flow")
    if not isinstance(main_flow, list) or not main_flow or any(str(item) not in set(stage_ids) for item in main_flow):
        errors.append("流程产物缺少合法 main_flow")
    for required in ("business-flow.mmd", "business-flow-report.md", "flow-claims.json"):
        if not (path.parent / required).is_file():
            errors.append(f"流程产物缺少 {required}")
    if (path.parent / "validation-errors.json").exists():
        errors.append("流程产物目录仍存在 validation-errors.json")
    return payload, errors


def extension_for(path_text: str, explicit: str = "") -> str:
    value = explicit.strip().casefold()
    if value and not value.startswith("."):
        value = "." + value
    return value or Path(path_text).suffix.casefold()


def categories_for(extension: str) -> list[str]:
    categories = []
    if extension in TABULAR_EXTENSIONS:
        categories.append("tabular")
    if extension in DOCUMENT_EXTENSIONS:
        categories.append("document")
    if extension in OCR_EXTENSIONS:
        if "document" not in categories:
            categories.append("document")
        categories.append("ocr")
    return categories


def evidence_context(relation_path: Path) -> tuple[dict[str, set[str]], dict[str, str], dict[str, dict[str, Any]]]:
    cards_path = relation_path.parent / "evidence-cards.json"
    if not cards_path.is_file():
        return {}, {}, {}
    try:
        payload = load_json(cards_path, MAX_EVIDENCE_BYTES)
    except ContractError:
        return {}, {}, {}
    files_by_evidence: dict[str, set[str]] = defaultdict(set)
    extension_by_file: dict[str, str] = {}
    card_by_id: dict[str, dict[str, Any]] = {}
    for card in payload.get("cards", []):
        if not isinstance(card, dict):
            continue
        card_id = str(card.get("id", ""))
        if card_id:
            card_by_id[card_id] = card
        sources = card.get("sources") if isinstance(card.get("sources"), list) else []
        source_files = {str(item.get("file", "")) for item in sources if isinstance(item, dict) and item.get("file")}
        files_by_evidence[card_id].update(source_files)
        if card.get("kind") == "file_structure":
            facts = card.get("facts") if isinstance(card.get("facts"), dict) else {}
            for source_file in source_files:
                extension_by_file[source_file] = extension_for(source_file, str(facts.get("extension", "")))
    return files_by_evidence, extension_by_file, card_by_id


def source_indexes(relations: dict[str, Any]) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    nodes = {
        str(item.get("id")): item
        for item in relations.get("nodes", [])
        if isinstance(item, dict) and item.get("id")
    }
    edges = {
        str(item.get("id")): item
        for item in relations.get("edges", [])
        if isinstance(item, dict) and item.get("id")
    }
    return nodes, edges


def build_inventory(relations: dict[str, Any], flow: dict[str, Any], relation_path: Path) -> list[dict[str, Any]]:
    _, operational, operational_errors = operational_context(relations, relation_path)
    if not operational_errors and operational.get("sources"):
        stages_by_node: dict[str, set[str]] = defaultdict(set)
        for stage in flow.get("stages", []):
            if not isinstance(stage, dict):
                continue
            for node_id in unique_strings(stage.get("input_node_ids")) + unique_strings(stage.get("output_node_ids")):
                stages_by_node[node_id].add(str(stage.get("name", stage.get("id", ""))))
        inventory = []
        for source in operational.get("sources", [])[:MAX_FILES]:
            if not isinstance(source, dict):
                continue
            roles = source.get("roles") if isinstance(source.get("roles"), list) else []
            extension = extension_for(str(source.get("path", "")), str(source.get("extension", "")))
            inventory.append({
                "path": str(source.get("path", "")),
                "extension": extension,
                "categories": categories_for(extension),
                "relation_roles": sorted({str(role.get("node_name", "")) for role in roles if isinstance(role, dict)}),
                "stage_roles": sorted({
                    stage_name
                    for role in roles if isinstance(role, dict)
                    for stage_name in stages_by_node.get(str(role.get("node_id", "")), set())
                }),
                "source_id": str(source.get("source_id", "")),
                "view_name": str(source.get("view_name", "")),
                "source_kind": str(source.get("kind", "")),
                "is_large": bool(source.get("is_large")),
                "content_retrieval": source.get("content_retrieval", {}),
                "agent_must_not_open_directly": bool(source.get("agent_must_not_open_directly", True)),
                "lifecycle": str(source.get("lifecycle", "runtime_input")),
                "runtime_required": bool(source.get("runtime_required", True)),
                "runtime_binding": str(source.get("runtime_binding", "required_when_referenced_by_stage_or_query")),
            })
        return inventory
    files_by_evidence, extension_by_file, _ = evidence_context(relation_path)
    node_by_id, _ = source_indexes(relations)
    all_files: set[str] = set()
    coverage = relations.get("coverage") if isinstance(relations.get("coverage"), dict) else {}
    all_files.update(unique_strings(coverage.get("included_files")))
    for values in files_by_evidence.values():
        all_files.update(values)
    if not all_files:
        all_files.update(unique_strings(coverage.get("files")))
    relation_roles: dict[str, set[str]] = defaultdict(set)
    for node_id, node in node_by_id.items():
        for evidence_id in unique_strings(node.get("evidence_ids")):
            for file_name in files_by_evidence.get(evidence_id, set()):
                relation_roles[file_name].add(str(node.get("name", node_id)))
    stage_roles: dict[str, set[str]] = defaultdict(set)
    for stage in flow.get("stages", []):
        if not isinstance(stage, dict):
            continue
        stage_files: set[str] = set()
        for node_id in unique_strings(stage.get("input_node_ids")) + unique_strings(stage.get("output_node_ids")):
            node = node_by_id.get(node_id, {})
            for evidence_id in unique_strings(node.get("evidence_ids")):
                stage_files.update(files_by_evidence.get(evidence_id, set()))
        support = stage.get("support") if isinstance(stage.get("support"), dict) else {}
        for evidence_id in unique_strings(support.get("evidence_ids")):
            stage_files.update(files_by_evidence.get(evidence_id, set()))
        for file_name in stage_files:
            stage_roles[file_name].add(str(stage.get("name", stage.get("id", ""))))
    inventory = []
    for file_name in sorted(all_files)[:MAX_FILES]:
        extension = extension_by_file.get(file_name) or extension_for(file_name)
        inventory.append({
            "path": file_name,
            "extension": extension,
            "categories": categories_for(extension),
            "relation_roles": sorted(relation_roles.get(file_name, set())),
            "stage_roles": sorted(stage_roles.get(file_name, set())),
        })
    return inventory


def format_requirements(inventory: list[dict[str, Any]]) -> dict[str, list[str]]:
    result: dict[str, set[str]] = defaultdict(set)
    for item in inventory:
        if item.get("runtime_required") is False:
            continue
        extension = str(item.get("extension", ""))
        for category in item.get("categories", []):
            result[str(category)].add(extension)
    return {key: sorted(value) for key, value in sorted(result.items()) if value}


def files_for_node(node: dict[str, Any], files_by_evidence: dict[str, set[str]]) -> set[str]:
    files: set[str] = set()
    for evidence_id in unique_strings(node.get("evidence_ids")):
        files.update(files_by_evidence.get(evidence_id, set()))
    return files


def stage_format_map(
    flow: dict[str, Any], relations: dict[str, Any], relation_path: Path, inventory: list[dict[str, Any]]
) -> dict[str, dict[str, list[str]]]:
    files_by_evidence, _, _ = evidence_context(relation_path)
    node_by_id, _ = source_indexes(relations)
    extension_by_path = {str(item["path"]): str(item["extension"]) for item in inventory}
    result: dict[str, dict[str, list[str]]] = {}
    for stage in flow.get("stages", []):
        if not isinstance(stage, dict):
            continue
        input_map: dict[str, list[str]] = {}
        output_map: dict[str, list[str]] = {}
        for field, target in (("input_node_ids", input_map), ("output_node_ids", output_map)):
            for node_id in unique_strings(stage.get(field)):
                node = node_by_id.get(node_id, {})
                if is_external_knowledge_node(node):
                    target[node_id] = []
                    continue
                extensions = {
                    extension_by_path[file_name]
                    for file_name in files_for_node(node, files_by_evidence)
                    if file_name in extension_by_path and extension_by_path[file_name]
                }
                target[node_id] = sorted(extensions)
        result[str(stage.get("id", ""))] = {
            "inputs": input_map,
            "outputs": output_map,
        }
    return result


def applicable_control_ids(flow: dict[str, Any], stage_id: str) -> list[str]:
    return [
        str(control.get("id"))
        for control in flow.get("controls", [])
        if isinstance(control, dict) and stage_id in unique_strings(control.get("applies_to"))
    ]


def related_question_ids(flow: dict[str, Any], stage_id: str) -> list[str]:
    return [
        str(item.get("id"))
        for item in flow.get("open_questions", [])
        if isinstance(item, dict) and stage_id in unique_strings(item.get("related_stage_ids"))
    ]


def predecessor_ids(flow: dict[str, Any], stage_id: str) -> list[str]:
    return [
        str(item.get("source"))
        for item in flow.get("transitions", [])
        if isinstance(item, dict) and str(item.get("target", "")) == stage_id
    ]


def successor_ids(flow: dict[str, Any], stage_id: str) -> list[str]:
    return [
        str(item.get("target"))
        for item in flow.get("transitions", [])
        if isinstance(item, dict) and str(item.get("source", "")) == stage_id
    ]


def expected_foundation_ids_for_formats(formats: Iterable[str], requirements: dict[str, list[str]]) -> list[str]:
    values = set(formats)
    result = []
    for kind, required_formats in requirements.items():
        if values.intersection(required_formats):
            result.append(str(FOUNDATION_SPECS[kind]["id"]))
    return result


def knowledge_system_roles(relations: dict[str, Any], flow: dict[str, Any]) -> list[dict[str, Any]]:
    """Return explicit external-knowledge nodes and the stages that consume them."""
    stages_by_node: dict[str, set[str]] = defaultdict(set)
    for stage in flow.get("stages", []):
        if not isinstance(stage, dict):
            continue
        for node_id in unique_strings(stage.get("input_node_ids")) + unique_strings(stage.get("output_node_ids")):
            stages_by_node[node_id].add(str(stage.get("id", "")))
    roles: list[dict[str, Any]] = []
    for node in relations.get("nodes", []):
        if not isinstance(node, dict):
            continue
        node_id = str(node.get("id", ""))
        if not stages_by_node.get(node_id) or not is_external_knowledge_node(node):
            continue
        roles.append({
            "node_id": node_id,
            "name": str(node.get("name", node_id)),
            "description": str(node.get("description", "")),
            "stage_ids": sorted(stages_by_node[node_id]),
        })
    return roles


def expected_foundation_ids_for_stage(
    formats: Iterable[str], requirements: dict[str, list[str]], node_ids: Iterable[str],
    knowledge_roles: list[dict[str, Any]],
) -> list[str]:
    result = expected_foundation_ids_for_formats(formats, requirements)
    knowledge_nodes = {str(item.get("node_id", "")) for item in knowledge_roles}
    if set(node_ids).intersection(knowledge_nodes):
        result.append(str(FOUNDATION_SPECS["knowledge"]["id"]))
    return list(dict.fromkeys(result))


def input_contracts(
    stage: dict[str, Any], stage_formats: dict[str, dict[str, list[str]]], node_by_id: dict[str, dict[str, Any]]
) -> list[dict[str, Any]]:
    stage_id = str(stage.get("id", ""))
    result = []
    for node_id in unique_strings(stage.get("input_node_ids")):
        node = node_by_id.get(node_id, {})
        result.append({
            "name": str(node.get("name", node_id)),
            "relation_node_ids": [node_id],
            "accepted_formats": stage_formats.get(stage_id, {}).get("inputs", {}).get(node_id, []),
            "required": True,
            "description": compact(node.get("description", ""), 240),
        })
    return result


def output_contracts(
    stage: dict[str, Any], stage_formats: dict[str, dict[str, list[str]]], node_by_id: dict[str, dict[str, Any]]
) -> list[dict[str, Any]]:
    stage_id = str(stage.get("id", ""))
    result = []
    for node_id in unique_strings(stage.get("output_node_ids")):
        node = node_by_id.get(node_id, {})
        result.append({
            "name": str(node.get("name", node_id)),
            "relation_node_ids": [node_id],
            "formats": stage_formats.get(stage_id, {}).get("outputs", {}).get(node_id, []),
            "required": True,
            "description": compact(node.get("description", ""), 240),
        })
    return result


def stage_execution_contract(stage: dict[str, Any], operational: dict[str, Any]) -> dict[str, Any]:
    node_ids = set(unique_strings(stage.get("input_node_ids")) + unique_strings(stage.get("output_node_ids")))
    source_ids = []
    large_source_ids = []
    document_source_ids = []
    for source in operational.get("sources", []):
        if not isinstance(source, dict):
            continue
        role_node_ids = {
            str(role.get("node_id", ""))
            for role in source.get("roles", [])
            if isinstance(role, dict)
        }
        if not node_ids.intersection(role_node_ids):
            continue
        source_id = str(source.get("source_id", ""))
        source_ids.append(source_id)
        if source.get("is_large"):
            large_source_ids.append(source_id)
        if source.get("kind") != "tabular":
            document_source_ids.append(source_id)
    link_ids = [
        str(link.get("link_id", ""))
        for link in operational.get("links", [])
        if isinstance(link, dict)
        and (link.get("recommended_candidate") or link.get("candidate_key_sets"))
        and (
            str(link.get("source_id", "")) in source_ids
            or str(link.get("target_id", "")) in source_ids
        )
    ]
    rule_source_ids = sorted(set(source_ids).intersection(unique_strings(operational.get("rule_source_ids"))))
    structured_rule_source_ids = sorted(
        source_id for source_id in rule_source_ids
        if next((item for item in operational.get("sources", []) if item.get("source_id") == source_id), {}).get("kind") == "tabular"
    )
    document_rule_source_ids = sorted(set(rule_source_ids) - set(structured_rule_source_ids))
    semantic_route_ids = [
        str(route.get("route_id", ""))
        for route in operational.get("semantic_routes", [])
        if isinstance(route, dict)
        and (
            str(route.get("source_id", "")) in source_ids
            or str(route.get("target_id", "")) in source_ids
        )
    ]
    trace_bundle_ids = [
        str(bundle.get("bundle_id", ""))
        for bundle in operational.get("trace_evidence", {}).get("bundles", [])
        if isinstance(bundle, dict)
        and any(
            str(source.get("source_id", "")) in source_ids
            for source in bundle.get("sources", [])
            if isinstance(source, dict)
        )
    ]
    return {
        "source_ids": sorted(set(source_ids)),
        "large_source_ids": sorted(set(large_source_ids)),
        "rule_source_ids": rule_source_ids,
        "structured_rule_source_ids": structured_rule_source_ids,
        "document_rule_source_ids": document_rule_source_ids,
        "link_ids": sorted(set(link_ids)),
        "document_source_ids": sorted(set(document_source_ids)),
        "semantic_route_ids": sorted(set(semantic_route_ids)),
        "trace_bundle_ids": sorted(set(trace_bundle_ids)),
        "trace_guidance": "validated_design_time_blueprint_revalidate_on_runtime_data" if trace_bundle_ids else "not_available",
        "agent_direct_file_read": False,
        "large_data_access": "bounded_read_only_sql" if large_source_ids else "bounded_tool_access",
        "rule_record_mode": "complete_selected_record" if rule_source_ids else "not_applicable",
        "document_access": "parse_or_ocr_then_search_chunk_index" if document_source_ids else "not_applicable",
        "evidence_provenance_required": True,
    }


def compact_flow(flow: dict[str, Any]) -> dict[str, Any]:
    return {
        "scenario": flow.get("scenario", {}),
        "execution_policy": flow.get("execution_policy", {}),
        "main_flow": flow.get("main_flow", []),
        "stages": [
            {
                "id": item.get("id"),
                "name": item.get("name"),
                "stage_type": item.get("stage_type"),
                "objective": item.get("objective"),
                "outcome": item.get("outcome"),
                "owner_role": item.get("owner_role", ""),
                "input_node_ids": item.get("input_node_ids", []),
                "output_node_ids": item.get("output_node_ids", []),
                "inference": item.get("inference", {}),
            }
            for item in flow.get("stages", [])
            if isinstance(item, dict)
        ],
        "transitions": flow.get("transitions", []),
        "states": flow.get("states", []),
        "controls": flow.get("controls", []),
        "open_questions": flow.get("open_questions", []),
    }


def build_brief(
    relations: dict[str, Any],
    flow: dict[str, Any],
    relation_path: Path,
    flow_path: Path,
    relation_fingerprint: str,
    flow_fingerprint: str,
) -> dict[str, Any]:
    operational_path, operational, operational_errors = operational_context(relations, relation_path)
    if operational_errors:
        raise ContractError("；".join(operational_errors))
    inventory = build_inventory(relations, flow, relation_path)
    requirements = format_requirements(inventory)
    knowledge_roles = knowledge_system_roles(relations, flow)
    node_by_id, edge_by_id = source_indexes(relations)
    return {
        "schema_version": SCHEMA_VERSION,
        "generator_contract_version": GENERATOR_CONTRACT_VERSION,
        "status": "ready_for_distillation",
        "source": {
            "relations": {"artifact": str(relation_path), "fingerprint": relation_fingerprint},
            "flow": {"artifact": str(flow_path), "fingerprint": flow_fingerprint},
            "operational_data_contract": {
                "artifact": str(operational_path),
                "fingerprint": sha256_file(operational_path),
            },
        },
        "scenario": flow.get("scenario", {}),
        "file_inventory": inventory,
        "required_foundations": [
            {
                "kind": kind,
                "id": FOUNDATION_SPECS[kind]["id"],
                "template": FOUNDATION_SPECS[kind]["template"],
                "engine": FOUNDATION_SPECS[kind]["engine"],
                "formats": formats,
            }
            for kind, formats in requirements.items()
        ] + ([{
            "kind": "knowledge",
            "id": FOUNDATION_SPECS["knowledge"]["id"],
            "template": FOUNDATION_SPECS["knowledge"]["template"],
            "engine": FOUNDATION_SPECS["knowledge"]["engine"],
            "formats": [],
            "system_roles": knowledge_roles,
        }] if knowledge_roles else []),
        "relation_nodes": [
            {
                "id": node_id,
                "name": node.get("name", ""),
                "type": node.get("type", ""),
                "description": compact(node.get("description", ""), 240),
            }
            for node_id, node in node_by_id.items()
        ],
        "relation_edges": [
            {
                "id": edge_id,
                "source": edge.get("source", ""),
                "target": edge.get("target", ""),
                "type": edge.get("type", ""),
                "label": compact(edge.get("label", ""), 160),
            }
            for edge_id, edge in edge_by_id.items()
        ],
        "flow": compact_flow(flow),
        "operational_execution": {
            "query_policy": operational.get("query_policy", {}),
            "rule_source_ids": operational.get("rule_source_ids", []),
            "sources": [
                {
                    "source_id": item.get("source_id"),
                    "view_name": item.get("view_name"),
                    "path": item.get("path"),
                    "kind": item.get("kind"),
                    "extension": item.get("extension"),
                    "size_bytes": item.get("size_bytes"),
                    "content_sha256": item.get("content_sha256"),
                    "is_large": item.get("is_large"),
                    "lifecycle": item.get("lifecycle"),
                    "runtime_required": item.get("runtime_required"),
                    "runtime_binding": item.get("runtime_binding"),
                    "template_policy": item.get("template_policy", {}),
                    "roles": item.get("roles", []),
                    "content_retrieval": item.get("content_retrieval", {}),
                    "tables": [
                        {
                            "sheet_or_table": table.get("sheet_or_table"),
                            "row_count": table.get("row_count"),
                            "header": table.get("header", {}),
                            "columns": table.get("columns", []),
                        }
                        for table in item.get("tables", [])
                    ],
                }
                for item in operational.get("sources", [])
            ],
            "links": [
                {
                    "link_id": item.get("link_id"),
                    "kind": item.get("kind"),
                    "source_id": item.get("source_id"),
                    "target_id": item.get("target_id"),
                    "recommended_candidate": item.get("recommended_candidate"),
                    "candidate_key_sets": item.get("candidate_key_sets", []),
                    "runtime_validation": item.get("runtime_validation", []),
                }
                for item in operational.get("links", [])
                if item.get("recommended_candidate") or item.get("candidate_key_sets")
            ],
            "semantic_routes": [
                {
                    "route_id": item.get("route_id"),
                    "mode": item.get("mode"),
                    "source_id": item.get("source_id"),
                    "target_id": item.get("target_id"),
                    "relation_type": item.get("relation_type"),
                    "evidence_locators": item.get("evidence_locators", []),
                    "runtime_validation": item.get("runtime_validation", []),
                }
                for item in operational.get("semantic_routes", [])
            ],
            "trace_evidence": compact_trace_evidence(operational, include_rows=True),
        },
        "distillation_policy": {
            "required_outputs": [
                "one scenario orchestrator Skill",
                "exactly one stage Skill per accepted flow stage",
                "only the format foundations required by the current inventory",
            ],
            "portability": "Generated Skills use relative resources and stable scripts; customized system Skills preserve configured values and support environment overrides; no Studio paths or Tools.",
            "evidence_boundary": "Do not invent micro procedures. Unsupported details remain runtime contracts or open questions.",
            "foundation_boundary": "Customize descriptions, triggers, file roles, and scenario guidance; never copy a generic SKILL.md unchanged.",
        },
        "next_action": "Complete capability-plan.candidate.json, run preflight, then finalize the portable Skill source tree.",
    }


def claims_template(
    relations: dict[str, Any],
    flow: dict[str, Any],
    relation_path: Path,
    flow_path: Path,
    relation_fingerprint: str,
    flow_fingerprint: str,
) -> dict[str, Any]:
    operational_path, operational, operational_errors = operational_context(relations, relation_path)
    if operational_errors:
        raise ContractError("；".join(operational_errors))
    inventory = build_inventory(relations, flow, relation_path)
    requirements = format_requirements(inventory)
    knowledge_roles = knowledge_system_roles(relations, flow)
    stage_formats = stage_format_map(flow, relations, relation_path, inventory)
    node_by_id, _ = source_indexes(relations)
    foundations = []
    for kind, formats in requirements.items():
        spec = FOUNDATION_SPECS[kind]
        role_items = []
        for extension in formats:
            matching = [
                item for item in inventory
                if item.get("extension") == extension and item.get("runtime_required") is not False
            ]
            role_items.append({
                "extension": extension,
                "business_roles": sorted({role for item in matching for role in item.get("relation_roles", [])}),
                "stage_roles": sorted({role for item in matching for role in item.get("stage_roles", [])}),
            })
        foundations.append({
            "id": spec["id"],
            "kind": kind,
            "skill_name": "",
            "display_name": "",
            "description": "",
            "formats": formats,
            "engine": spec["engine"],
            "source_template": spec["template"],
            "file_roles": role_items,
            "when_to_use": [],
            "scenario_instructions": [],
            "non_goals": [],
        })
    if knowledge_roles:
        spec = FOUNDATION_SPECS["knowledge"]
        foundations.append({
            "id": spec["id"],
            "kind": "knowledge",
            "skill_name": "",
            "display_name": "",
            "description": "",
            "formats": [],
            "engine": spec["engine"],
            "source_template": spec["template"],
            "file_roles": [],
            "system_roles": knowledge_roles,
            "when_to_use": [],
            "scenario_instructions": [],
            "non_goals": [],
        })
    stage_skills = []
    for stage in flow.get("stages", []):
        if not isinstance(stage, dict):
            continue
        stage_id = str(stage.get("id", ""))
        inputs = input_contracts(stage, stage_formats, node_by_id)
        outputs = output_contracts(stage, stage_formats, node_by_id)
        formats = sorted({fmt for item in inputs for fmt in item.get("accepted_formats", [])})
        stage_node_ids = unique_strings(stage.get("input_node_ids")) + unique_strings(stage.get("output_node_ids"))
        stage_skills.append({
            "id": f"cap-{stage_id}",
            "stage_id": stage_id,
            "skill_name": "",
            "display_name": str(stage.get("name", "")),
            "description": "",
            "objective": str(stage.get("objective", "")),
            "outcome": str(stage.get("outcome", "")),
            "invocation_triggers": [],
            "foundation_ids": expected_foundation_ids_for_stage(
                formats, requirements, stage_node_ids, knowledge_roles
            ),
            "predecessor_stage_ids": predecessor_ids(flow, stage_id),
            "successor_stage_ids": successor_ids(flow, stage_id),
            "input_contract": inputs,
            "output_contract": outputs,
            "control_ids": applicable_control_ids(flow, stage_id),
            "procedure": [],
            "non_goals": [],
            "open_question_ids": related_question_ids(flow, stage_id),
            "execution_contract": stage_execution_contract(stage, operational),
        })
    unknown_formats = sorted({str(item.get("extension", "")) for item in inventory if not item.get("categories")})
    return {
        "schema_version": SCHEMA_VERSION,
        "generator_contract_version": GENERATOR_CONTRACT_VERSION,
        "source": {
            "relations": {
                "capability": RELATION_CAPABILITY,
                "artifact": str(relation_path),
                "fingerprint": relation_fingerprint,
            },
            "flow": {
                "capability": FLOW_CAPABILITY,
                "artifact": str(flow_path),
                "fingerprint": flow_fingerprint,
            },
            "operational_data_contract": {
                "capability": RELATION_CAPABILITY,
                "artifact": str(operational_path),
                "fingerprint": sha256_file(operational_path),
            },
        },
        "scenario": flow.get("scenario", {}),
        "bundle": {
            "name": "",
            "description": "",
            "target_agents": "third_party_agents",
        },
        "portability": {
            "platform_independent": True,
            "python_requirement": ">=3.10",
            "resource_paths": "relative_to_each_skill",
            "credentials": "preserve_public_defaults_externalize_credentials",
        },
        "file_inventory": inventory,
        "foundation_skills": foundations,
        "stage_skills": stage_skills,
        "orchestrator": {
            "id": "scenario-orchestrator",
            "skill_name": "",
            "display_name": f"{flow.get('scenario', {}).get('name', '')}业务场景编排",
            "description": "",
            "invocation_triggers": [],
            "foundation_ids": [item["id"] for item in foundations],
            "main_flow": flow.get("main_flow", []),
            "routing": [
                {"stage_id": item["stage_id"], "capability_id": item["id"]}
                for item in stage_skills
            ],
            "failure_policy": [],
            "non_goals": [],
        },
        "unsupported_formats": [
            {"extension": extension, "reason": ""} for extension in unknown_formats
        ],
    }


def prepare(args: argparse.Namespace) -> tuple[int, dict[str, Any]]:
    relation_path = Path(args.relations).resolve()
    flow_path = Path(args.flow).resolve()
    output_root = Path(args.output).resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    relations, relation_errors = validate_relations(relation_path)
    relation_fingerprint = sha256_file(relation_path) if relation_path.is_file() else ""
    flow, flow_errors = validate_flow(flow_path, relation_path, relation_fingerprint)
    errors = relation_errors + flow_errors
    if relations and flow:
        relation_name = str(relations.get("scenario", {}).get("name", "")).strip()
        flow_name = str(flow.get("scenario", {}).get("name", "")).strip()
        if relation_name != flow_name:
            errors.append("关系与流程产物的 scenario.name 不一致")
    if errors:
        payload = {
            "schema_version": SCHEMA_VERSION,
            "status": "blocked_missing_or_invalid_upstream",
            "errors": errors,
            "next_action": "先由 discover-data-relations 和 derive-business-flow 修复并完成上游；不得直接从原始数据或旧产物蒸馏。",
        }
        atomic_json(output_root / "prepare-status.json", payload)
        return 2, payload
    flow_fingerprint = sha256_file(flow_path)
    brief = build_brief(relations, flow, relation_path, flow_path, relation_fingerprint, flow_fingerprint)
    template = claims_template(relations, flow, relation_path, flow_path, relation_fingerprint, flow_fingerprint)
    _, operational, operational_errors = operational_context(relations, relation_path)
    if operational_errors:
        # validate_flow already guards this dependency, but keep prepare's
        # replay template honest if a legacy upstream artifact slips through.
        replay_contract = {
            "schema_version": 1,
            "kind": "compiled_recipe_replay_report",
            "status": "blocked_missing_operational_contract",
            "errors": operational_errors,
        }
    else:
        replay_contract = refresh_recipe_replay_contract(output_root, flow, operational)
    atomic_json(output_root / "distillation-brief.json", brief)
    atomic_json(output_root / "capability-plan.template.json", template)
    atomic_json(output_root / "recipe-replay-contract.json", replay_contract)
    payload = {
        "schema_version": SCHEMA_VERSION,
        "generator_contract_version": GENERATOR_CONTRACT_VERSION,
        "status": "ready_for_distillation",
        "relation_fingerprint": relation_fingerprint,
        "flow_fingerprint": flow_fingerprint,
        "stage_count": len(flow.get("stages", [])),
        "foundation_count": len(template["foundation_skills"]),
        "inventory_count": len(template["file_inventory"]),
        "brief": str(output_root / "distillation-brief.json"),
        "template": str(output_root / "capability-plan.template.json"),
        "recipe_replay_contract": str(output_root / "recipe-replay-contract.json"),
        "next_action": (
            "Complete capability-plan.candidate.json, then run preflight. Preflight materializes "
            "recipe-replay-runtime.json; use that exact runtime with the private signed replay fixture before "
            "writing recipe-replay-report.json. Without it, finalize remains evidence-only."
        ),
    }
    atomic_json(output_root / "prepare-status.json", payload)
    return 0, {**payload, "distillation_brief": brief}


def baseline_candidate(template: dict[str, Any]) -> dict[str, Any]:
    """Fill only reusable plan prose from accepted contracts.

    This deliberately does not inspect source rows, infer a domain rule, or
    create a business-specific decision procedure.  It gives every scenario a
    valid, reviewable capability-plan baseline so the distillation flow itself
    does not consume an Agent turn just to restate accepted flow contracts.
    """
    claims = json.loads(json.dumps(template, ensure_ascii=False))
    scenario = claims.get("scenario") if isinstance(claims.get("scenario"), dict) else {}
    scenario_name = str(scenario.get("name", "business scenario")).strip() or "business scenario"
    scenario_token = hashlib.sha256(scenario_name.encode("utf-8")).hexdigest()[:10]
    prefix = f"scenario-{scenario_token}"
    bundle_name = f"{prefix}-capabilities"
    claims["bundle"]["name"] = bundle_name
    claims["bundle"]["description"] = (
        f"Portable capability source for the {scenario_name} scenario, for third-party Agents to use "
        "accepted data contracts, stage responsibilities, and flow routing without direct raw-file access."
    )
    for foundation in claims.get("foundation_skills", []):
        if not isinstance(foundation, dict):
            continue
        kind = str(foundation.get("kind", "reader"))
        formats = ", ".join(unique_strings(foundation.get("formats"))) or "declared source files"
        foundation["skill_name"] = f"{prefix}-{kind}-reader"
        foundation["display_name"] = f"{scenario_name} {kind} data reader"
        foundation["description"] = (
            f"Use this read-only {kind} foundation when {scenario_name} receives {formats} inputs "
            "required by an accepted stage contract; it returns bounded, provenance-bearing data and never makes a business decision."
        )
        foundation["when_to_use"] = [
            f"A {scenario_name} request includes a declared {formats} runtime source.",
            "A stage input contract requires a bounded, read-only source lookup or verification.",
        ]
        foundation["scenario_instructions"] = [
            "Use only the declared file roles and runtime bindings; preserve source identifiers and query provenance.",
        ]
        foundation["non_goals"] = [
            "Do not infer a business rule, decide an outcome, modify a source file, or load a full large source into Agent context.",
        ]
    for stage in claims.get("stage_skills", []):
        if not isinstance(stage, dict):
            continue
        stage_id = str(stage.get("stage_id", "stage"))
        stage_slug = re.sub(r"[^a-z0-9]+", "-", stage_id.casefold().replace("_", "-")).strip("-") or "stage"
        stage["skill_name"] = f"{prefix}-{stage_slug}"[:63].rstrip("-")
        display_name = str(stage.get("display_name", stage_id)).strip() or stage_id
        outcome = str(stage.get("outcome", "the accepted stage outcome")).strip()
        stage["description"] = (
            f"Use this skill when a {scenario_name} request reaches the {display_name} stage or the prior accepted handoff is available. "
            f"It produces {outcome} under the stage input/output contracts and does not perform adjacent-stage responsibilities."
        )
        stage["invocation_triggers"] = [
            f"The user explicitly requests the {display_name} responsibility in {scenario_name}.",
            "The accepted predecessor handoff satisfies this stage input contract.",
        ]
        input_ids = list(dict.fromkeys(
            node_id
            for contract in stage.get("input_contract", []) if isinstance(contract, dict)
            for node_id in unique_strings(contract.get("relation_node_ids"))
        ))
        procedure = [
            {
                "action": "Verify only the declared stage inputs and keep evidence bounded to the accepted contract.",
                "basis": "input_contract" if input_ids else "flow_stage",
                "source_ids": input_ids[:2] if input_ids else [stage_id],
            },
            {
                "action": "Produce the accepted stage outcome and a traceable handoff without extending the business procedure.",
                "basis": "flow_stage",
                "source_ids": [stage_id],
            },
        ]
        controls = unique_strings(stage.get("control_ids"))
        if controls:
            procedure.insert(1, {
                "action": "Apply the accepted stage control before making a decision; do not derive rules from historical examples.",
                "basis": "control",
                "source_ids": controls,
            })
        stage["procedure"] = procedure
        stage["non_goals"] = [
            "Do not execute predecessor or successor stages, and do not invent unsupported branches, rules, approvals, or exceptions.",
        ]
    orchestrator = claims.get("orchestrator") if isinstance(claims.get("orchestrator"), dict) else {}
    orchestrator["skill_name"] = f"{prefix}-orchestrator"
    orchestrator["description"] = (
        f"Use this orchestrator for an end-to-end {scenario_name} request or a request spanning multiple stages. "
        "It routes only through the accepted main flow, preserves handoffs and boundaries, and does not replace stage-level business execution."
    )
    orchestrator["invocation_triggers"] = [
        f"The user requests an end-to-end {scenario_name} outcome.",
        "The request spans two or more accepted stages and requires controlled routing.",
    ]
    orchestrator["failure_policy"] = [
        "Stop the affected stage and report the missing input, rule, source compatibility, or validation evidence.",
        "Keep unresolved branches as explicit questions; never choose them from historical frequency or fabricated facts.",
    ]
    orchestrator["non_goals"] = [
        "Do not replace a stage Skill, invent a business decision, publish the package, or bypass the accepted main flow.",
    ]
    for item in claims.get("unsupported_formats", []):
        if isinstance(item, dict):
            item["reason"] = "No portable parser is bundled for this format; convert it to a declared supported format before execution."
    return claims


def draft(args: argparse.Namespace) -> tuple[int, dict[str, Any]]:
    output_root, relation_path, flow_path, relations, flow, relation_fingerprint, flow_fingerprint = source_context(args)
    candidate_path = Path(args.claims).resolve() if getattr(args, "claims", "") else output_root / "capability-plan.candidate.json"
    if candidate_path.exists():
        return 2, {
            "schema_version": SCHEMA_VERSION,
            "status": "blocked_candidate_exists",
            "claims": str(candidate_path),
            "next_action": "Review or rename the existing candidate; the draft command never overwrites a human-authored plan.",
        }
    template = claims_template(relations, flow, relation_path, flow_path, relation_fingerprint, flow_fingerprint)
    candidate = baseline_candidate(template)
    atomic_json(candidate_path, candidate)
    return 0, {
        "schema_version": SCHEMA_VERSION,
        "status": "candidate_created",
        "claims": str(candidate_path),
        "semantic_origin": "accepted-contract-baseline",
        "raw_data_access": "none",
        "next_action": "Run preflight; refine only bounded business wording if a reviewer has accepted additional semantics.",
    }


def source_context(args: argparse.Namespace) -> tuple[Path, Path, Path, dict[str, Any], dict[str, Any], str, str]:
    relation_path = Path(args.relations).resolve()
    flow_path = Path(args.flow).resolve()
    relations, relation_errors = validate_relations(relation_path)
    relation_fingerprint = sha256_file(relation_path) if relation_path.is_file() else ""
    flow, flow_errors = validate_flow(flow_path, relation_path, relation_fingerprint)
    if relation_errors or flow_errors:
        raise ContractError("；".join(relation_errors + flow_errors))
    relation_name = str(relations.get("scenario", {}).get("name", "")).strip()
    flow_name = str(flow.get("scenario", {}).get("name", "")).strip()
    if relation_name != flow_name:
        raise ContractError("关系与流程产物的 scenario.name 不一致")
    return (
        Path(args.output).resolve(), relation_path, flow_path, relations, flow,
        relation_fingerprint, sha256_file(flow_path),
    )


def validate_source_claim(
    claims: dict[str, Any], relation_path: Path, flow_path: Path, relation_fingerprint: str, flow_fingerprint: str,
    errors: list[str],
) -> None:
    source = claims.get("source") if isinstance(claims.get("source"), dict) else {}
    operational_path = relation_path.parent / "operational-data-contract.json"
    expected = {
        "relations": (RELATION_CAPABILITY, relation_path, relation_fingerprint),
        "flow": (FLOW_CAPABILITY, flow_path, flow_fingerprint),
        "operational_data_contract": (
            RELATION_CAPABILITY,
            operational_path,
            sha256_file(operational_path) if operational_path.is_file() else "",
        ),
    }
    for key, (capability, path, fingerprint) in expected.items():
        item = source.get(key) if isinstance(source.get(key), dict) else {}
        if item.get("capability") != capability:
            errors.append(f"source.{key}.capability 必须为 {capability}")
        try:
            artifact = Path(str(item.get("artifact", ""))).resolve()
        except OSError:
            artifact = Path("__invalid__")
        if artifact != path:
            errors.append(f"source.{key}.artifact 与当前上游不一致")
        if item.get("fingerprint") != fingerprint:
            errors.append(f"source.{key}.fingerprint 已过期；重新运行 prepare")


def validate_contracts(
    owner: str,
    contracts: Any,
    key: str,
    expected_nodes: set[str],
    known_nodes: set[str],
    known_formats: set[str],
    errors: list[str],
    *,
    required: bool,
) -> tuple[set[str], set[str]]:
    if not isinstance(contracts, list):
        errors.append(f"{owner} 必须是数组")
        return set(), set()
    used_nodes: set[str] = set()
    used_formats: set[str] = set()
    for index, item in enumerate(contracts):
        if not isinstance(item, dict):
            errors.append(f"{owner}[{index}] 必须是对象")
            continue
        if len(str(item.get("name", "")).strip()) < 2 or len(str(item.get("description", "")).strip()) < 4:
            errors.append(f"{owner}[{index}] 必须说明 name 和 description")
        if item.get("required") is not required:
            expectation = "true" if required else "false"
            errors.append(f"{owner}[{index}].required 必须为 {expectation}，不得把阶段交付降级为可选")
        nodes = set(validate_string_list(f"{owner}[{index}].relation_node_ids", item.get("relation_node_ids"), errors, minimum=1))
        unknown_nodes = nodes - known_nodes
        if unknown_nodes:
            errors.append(f"{owner}[{index}] 引用了未知关系节点：{', '.join(sorted(unknown_nodes))}")
        formats = set(validate_string_list(f"{owner}[{index}].{key}", item.get(key), errors))
        unknown_formats = formats - known_formats
        if unknown_formats:
            errors.append(f"{owner}[{index}] 引用了清单外格式：{', '.join(sorted(unknown_formats))}")
        used_nodes.update(nodes)
        used_formats.update(formats)
    if used_nodes != expected_nodes:
        errors.append(f"{owner} 必须精确覆盖流程阶段节点：期望 {sorted(expected_nodes)}，实际 {sorted(used_nodes)}")
    return used_nodes, used_formats


def contract_formats_by_node(contracts: Any, format_key: str) -> dict[str, set[str]]:
    result: dict[str, set[str]] = defaultdict(set)
    for item in contracts if isinstance(contracts, list) else []:
        if not isinstance(item, dict):
            continue
        formats = set(unique_strings(item.get(format_key)))
        for node_id in unique_strings(item.get("relation_node_ids")):
            result[node_id].update(formats)
    return result


def validate_plan(
    claims: dict[str, Any],
    relations: dict[str, Any],
    flow: dict[str, Any],
    relation_path: Path,
    flow_path: Path,
    relation_fingerprint: str,
    flow_fingerprint: str,
) -> list[str]:
    errors: list[str] = []
    validate_source_claim(claims, relation_path, flow_path, relation_fingerprint, flow_fingerprint, errors)
    if claims.get("schema_version") != SCHEMA_VERSION:
        errors.append(f"schema_version 必须为 {SCHEMA_VERSION}")
    if claims.get("generator_contract_version") != GENERATOR_CONTRACT_VERSION:
        errors.append(
            f"generator_contract_version 必须为 {GENERATOR_CONTRACT_VERSION}；重新运行 prepare，禁止复用旧候选"
        )
    scenario = claims.get("scenario") if isinstance(claims.get("scenario"), dict) else {}
    if str(scenario.get("name", "")).strip() != str(flow.get("scenario", {}).get("name", "")).strip():
        errors.append("scenario.name 必须与已验收流程一致")
    bundle = claims.get("bundle") if isinstance(claims.get("bundle"), dict) else {}
    validate_skill_name("bundle", bundle.get("name"), errors)
    if len(str(bundle.get("description", "")).strip()) < 40:
        errors.append("bundle.description 必须说明场景、能力范围和第三方用途")
    if bundle.get("target_agents") != "third_party_agents":
        errors.append("bundle.target_agents 必须为 third_party_agents")
    portability = claims.get("portability") if isinstance(claims.get("portability"), dict) else {}
    expected_portability = {
        "platform_independent": True,
        "python_requirement": ">=3.10",
        "resource_paths": "relative_to_each_skill",
        "credentials": "preserve_public_defaults_externalize_credentials",
    }
    for key, expected in expected_portability.items():
        if portability.get(key) != expected:
            errors.append(f"portability.{key} 必须为 {expected!r}")

    expected_inventory = build_inventory(relations, flow, relation_path)
    _, operational, operational_errors = operational_context(relations, relation_path)
    errors.extend(operational_errors)
    if claims.get("file_inventory") != expected_inventory:
        errors.append("file_inventory 是 prepare 生成的只读事实，不得改写")
    requirements = format_requirements(expected_inventory)
    knowledge_roles = knowledge_system_roles(relations, flow)
    known_formats = {str(item.get("extension", "")) for item in expected_inventory if item.get("extension")}
    expected_foundation_by_id = {
        str(FOUNDATION_SPECS[kind]["id"]): (kind, formats)
        for kind, formats in requirements.items()
    }
    if knowledge_roles:
        expected_foundation_by_id[str(FOUNDATION_SPECS["knowledge"]["id"])] = ("knowledge", [])
    foundations = claims.get("foundation_skills")
    if not isinstance(foundations, list):
        errors.append("foundation_skills 必须是数组")
        foundations = []
    foundation_by_id: dict[str, dict[str, Any]] = {}
    all_skill_names: set[str] = set()
    for index, item in enumerate(foundations):
        owner = f"foundation_skills[{index}]"
        if not isinstance(item, dict):
            errors.append(f"{owner} 必须是对象")
            continue
        identifier = validate_identifier(owner, item.get("id"), errors)
        if identifier in foundation_by_id:
            errors.append(f"基础能力 id 重复：{identifier}")
        foundation_by_id[identifier] = item
        skill_name = validate_skill_name(owner, item.get("skill_name"), errors)
        if skill_name in all_skill_names:
            errors.append(f"Skill 名称重复：{skill_name}")
        all_skill_names.add(skill_name)
        if identifier not in expected_foundation_by_id:
            errors.append(f"基础能力 {identifier} 不是当前文件格式需要的能力")
            continue
        kind, expected_formats = expected_foundation_by_id[identifier]
        spec = FOUNDATION_SPECS[kind]
        if item.get("kind") != kind or item.get("engine") != spec["engine"] or item.get("source_template") != spec["template"]:
            errors.append(f"基础能力 {identifier} 的 kind/engine/source_template 不得改写")
        if item.get("formats") != expected_formats:
            errors.append(f"基础能力 {identifier}.formats 必须精确覆盖 {expected_formats}")
        if len(str(item.get("display_name", "")).strip()) < 2:
            errors.append(f"基础能力 {identifier} 缺少 display_name")
        description = str(item.get("description", "")).strip()
        if not 40 <= len(description) <= 600:
            errors.append(f"基础能力 {identifier}.description 必须在 40-600 字符内并包含触发上下文")
        validate_string_list(f"基础能力 {identifier}.when_to_use", item.get("when_to_use"), errors, minimum=2)
        validate_string_list(f"基础能力 {identifier}.scenario_instructions", item.get("scenario_instructions"), errors, minimum=1)
        validate_string_list(f"基础能力 {identifier}.non_goals", item.get("non_goals"), errors, minimum=1)
        if not isinstance(item.get("file_roles"), list) or item.get("file_roles") != next(
            (template["file_roles"] for template in claims_template(
                relations, flow, relation_path, flow_path, relation_fingerprint, flow_fingerprint
            )["foundation_skills"] if template["id"] == identifier), []
        ):
            errors.append(f"基础能力 {identifier}.file_roles 是 prepare 生成的事实，不得改写")
        expected_system_roles = knowledge_roles if kind == "knowledge" else []
        if item.get("system_roles", []) != expected_system_roles:
            errors.append(f"基础能力 {identifier}.system_roles 是 prepare 生成的事实，不得改写")
    if set(foundation_by_id) != set(expected_foundation_by_id):
        errors.append("foundation_skills 必须与当前文件格式所需基础能力一一对应")

    node_by_id, edge_by_id = source_indexes(relations)
    known_nodes = set(node_by_id)
    flow_stages = {
        str(item.get("id")): item
        for item in flow.get("stages", [])
        if isinstance(item, dict) and item.get("id")
    }
    flow_controls = {
        str(item.get("id")): item
        for item in flow.get("controls", [])
        if isinstance(item, dict) and item.get("id")
    }
    flow_questions = {
        str(item.get("id")): item
        for item in flow.get("open_questions", [])
        if isinstance(item, dict) and item.get("id")
    }
    flow_states = {
        str(item.get("id")): item
        for item in flow.get("states", [])
        if isinstance(item, dict) and item.get("id")
    }
    flow_transitions = {
        str(item.get("id")): item
        for item in flow.get("transitions", [])
        if isinstance(item, dict) and item.get("id")
    }
    stage_formats = stage_format_map(flow, relations, relation_path, expected_inventory)
    stage_skills = claims.get("stage_skills")
    if not isinstance(stage_skills, list):
        errors.append("stage_skills 必须是数组")
        stage_skills = []
    stage_skill_by_stage: dict[str, dict[str, Any]] = {}
    stage_capability_ids: set[str] = set()
    for index, item in enumerate(stage_skills):
        owner = f"stage_skills[{index}]"
        if not isinstance(item, dict):
            errors.append(f"{owner} 必须是对象")
            continue
        identifier = validate_identifier(owner, item.get("id"), errors)
        stage_capability_ids.add(identifier)
        stage_id = str(item.get("stage_id", ""))
        stage = flow_stages.get(stage_id)
        if stage is None:
            errors.append(f"{owner}.stage_id 引用了未知流程阶段：{stage_id}")
            continue
        if stage_id in stage_skill_by_stage:
            errors.append(f"流程阶段重复蒸馏：{stage_id}")
        stage_skill_by_stage[stage_id] = item
        if identifier != f"cap-{stage_id}":
            errors.append(f"阶段 {stage_id} 的 capability id 必须为 cap-{stage_id}")
        skill_name = validate_skill_name(owner, item.get("skill_name"), errors)
        if skill_name in all_skill_names:
            errors.append(f"Skill 名称重复：{skill_name}")
        all_skill_names.add(skill_name)
        if len(str(item.get("display_name", "")).strip()) < 2:
            errors.append(f"阶段 {stage_id} 缺少 display_name")
        if not 50 <= len(str(item.get("description", "")).strip()) <= 800:
            errors.append(f"阶段 {stage_id}.description 必须在 50-800 字符内，说明何时调用和承担什么责任")
        if item.get("objective") != stage.get("objective") or item.get("outcome") != stage.get("outcome"):
            errors.append(f"阶段 {stage_id} 的 objective/outcome 必须与已验收流程一致")
        expected_execution = stage_execution_contract(stage, operational)
        if item.get("execution_contract") != expected_execution:
            errors.append(f"阶段 {stage_id}.execution_contract 是数据规模、规则源和字段链路决定的事实，不得改写")
        validate_string_list(f"阶段 {stage_id}.invocation_triggers", item.get("invocation_triggers"), errors, minimum=2)
        inputs = item.get("input_contract")
        outputs = item.get("output_contract")
        _, input_formats = validate_contracts(
            f"阶段 {stage_id}.input_contract", inputs, "accepted_formats",
            set(unique_strings(stage.get("input_node_ids"))), known_nodes, known_formats, errors, required=True,
        )
        validate_contracts(
            f"阶段 {stage_id}.output_contract", outputs, "formats",
            set(unique_strings(stage.get("output_node_ids"))), known_nodes, known_formats, errors, required=True,
        )
        expected_input_formats = {
            node_id: set(formats)
            for node_id, formats in stage_formats.get(stage_id, {}).get("inputs", {}).items()
        }
        expected_output_formats = {
            node_id: set(formats)
            for node_id, formats in stage_formats.get(stage_id, {}).get("outputs", {}).items()
        }
        if contract_formats_by_node(inputs, "accepted_formats") != expected_input_formats:
            errors.append(f"阶段 {stage_id}.input_contract 的文件格式映射是上游事实，不得改写")
        if contract_formats_by_node(outputs, "formats") != expected_output_formats:
            errors.append(f"阶段 {stage_id}.output_contract 的文件格式映射是上游事实，不得改写")
        deterministic_input_formats = {value for values in expected_input_formats.values() for value in values}
        stage_node_ids = unique_strings(stage.get("input_node_ids")) + unique_strings(stage.get("output_node_ids"))
        expected_foundations = set(expected_foundation_ids_for_stage(
            deterministic_input_formats, requirements, stage_node_ids, knowledge_roles
        ))
        actual_foundations = set(validate_string_list(
            f"阶段 {stage_id}.foundation_ids", item.get("foundation_ids"), errors
        ))
        if actual_foundations != expected_foundations:
            errors.append(f"阶段 {stage_id}.foundation_ids 应为 {sorted(expected_foundations)}")
        expected_predecessors = set(predecessor_ids(flow, stage_id))
        actual_predecessors = set(validate_string_list(
            f"阶段 {stage_id}.predecessor_stage_ids", item.get("predecessor_stage_ids"), errors
        ))
        if actual_predecessors != expected_predecessors:
            errors.append(f"阶段 {stage_id}.predecessor_stage_ids 不得改写")
        expected_successors = set(successor_ids(flow, stage_id))
        actual_successors = set(validate_string_list(
            f"阶段 {stage_id}.successor_stage_ids", item.get("successor_stage_ids"), errors
        ))
        if actual_successors != expected_successors:
            errors.append(f"阶段 {stage_id}.successor_stage_ids 不得改写")
        expected_controls = set(applicable_control_ids(flow, stage_id))
        actual_controls = set(validate_string_list(f"阶段 {stage_id}.control_ids", item.get("control_ids"), errors))
        if actual_controls != expected_controls:
            errors.append(f"阶段 {stage_id}.control_ids 必须精确覆盖 {sorted(expected_controls)}")
        expected_questions = set(related_question_ids(flow, stage_id))
        actual_questions = set(validate_string_list(
            f"阶段 {stage_id}.open_question_ids", item.get("open_question_ids"), errors
        ))
        if actual_questions != expected_questions:
            errors.append(f"阶段 {stage_id}.open_question_ids 必须精确覆盖相关待确认项")
        procedure = item.get("procedure")
        if not isinstance(procedure, list) or not 2 <= len(procedure) <= MAX_PROCEDURE_STEPS:
            errors.append(f"阶段 {stage_id}.procedure 必须包含 2-{MAX_PROCEDURE_STEPS} 个有依据步骤")
        else:
            allowed_sources = (
                {stage_id}
                | set(unique_strings(stage.get("input_node_ids")))
                | set(unique_strings(stage.get("output_node_ids")))
                | expected_controls
                | expected_questions
                | {
                    transition_id for transition_id, transition in flow_transitions.items()
                    if stage_id in {str(transition.get("source", "")), str(transition.get("target", ""))}
                }
                | {
                    state_id for state_id, state in flow_states.items()
                    if str(state.get("reached_after", "")) == stage_id
                }
            )
            for procedure_index, step in enumerate(procedure):
                if not isinstance(step, dict):
                    errors.append(f"阶段 {stage_id}.procedure[{procedure_index}] 必须是对象")
                    continue
                if len(str(step.get("action", "")).strip()) < 8:
                    errors.append(f"阶段 {stage_id}.procedure[{procedure_index}] 必须说明 action")
                if step.get("basis") not in {"flow_stage", "input_contract", "control", "handoff"}:
                    errors.append(f"阶段 {stage_id}.procedure[{procedure_index}].basis 不受支持")
                sources = set(validate_string_list(
                    f"阶段 {stage_id}.procedure[{procedure_index}].source_ids", step.get("source_ids"), errors, minimum=1
                ))
                unknown_sources = sources - allowed_sources
                if unknown_sources:
                    errors.append(
                        f"阶段 {stage_id}.procedure[{procedure_index}] 使用了未获上游支撑的 source_ids："
                        f"{', '.join(sorted(unknown_sources))}"
                    )
        validate_string_list(f"阶段 {stage_id}.non_goals", item.get("non_goals"), errors, minimum=1)
    if set(stage_skill_by_stage) != set(flow_stages):
        missing = set(flow_stages) - set(stage_skill_by_stage)
        extra = set(stage_skill_by_stage) - set(flow_stages)
        errors.append(f"每个流程阶段必须恰好一个 Skill；缺少 {sorted(missing)}，多余 {sorted(extra)}")

    orchestrator = claims.get("orchestrator") if isinstance(claims.get("orchestrator"), dict) else {}
    if orchestrator.get("id") != "scenario-orchestrator":
        errors.append("orchestrator.id 必须为 scenario-orchestrator")
    orchestrator_name = validate_skill_name("orchestrator", orchestrator.get("skill_name"), errors)
    if orchestrator_name in all_skill_names:
        errors.append(f"Skill 名称重复：{orchestrator_name}")
    if len(str(orchestrator.get("display_name", "")).strip()) < 2:
        errors.append("orchestrator.display_name 不能为空")
    if not 50 <= len(str(orchestrator.get("description", "")).strip()) <= 800:
        errors.append("orchestrator.description 必须在 50-800 字符内并说明场景路由触发条件")
    validate_string_list("orchestrator.invocation_triggers", orchestrator.get("invocation_triggers"), errors, minimum=2)
    if set(unique_strings(orchestrator.get("foundation_ids"))) != set(expected_foundation_by_id):
        errors.append("orchestrator.foundation_ids 必须覆盖全部基础能力")
    if orchestrator.get("main_flow") != flow.get("main_flow"):
        errors.append("orchestrator.main_flow 必须与已验收流程一致")
    routing = orchestrator.get("routing")
    actual_routing = {
        str(item.get("stage_id")): str(item.get("capability_id"))
        for item in routing or [] if isinstance(item, dict)
    }
    expected_routing = {stage_id: f"cap-{stage_id}" for stage_id in flow_stages}
    if actual_routing != expected_routing or len(routing or []) != len(expected_routing):
        errors.append("orchestrator.routing 必须将每个阶段精确映射到对应 capability id")
    validate_string_list("orchestrator.failure_policy", orchestrator.get("failure_policy"), errors, minimum=2)
    validate_string_list("orchestrator.non_goals", orchestrator.get("non_goals"), errors, minimum=1)

    unsupported = claims.get("unsupported_formats")
    if not isinstance(unsupported, list):
        errors.append("unsupported_formats 必须是数组")
        unsupported = []
    expected_unknown = {str(item.get("extension", "")) for item in expected_inventory if not item.get("categories")}
    actual_unknown: set[str] = set()
    for index, item in enumerate(unsupported):
        if not isinstance(item, dict):
            errors.append(f"unsupported_formats[{index}] 必须是对象")
            continue
        extension = str(item.get("extension", ""))
        actual_unknown.add(extension)
        if len(str(item.get("reason", "")).strip()) < 4:
            errors.append(f"unsupported_formats {extension} 必须说明 reason")
    if actual_unknown != expected_unknown:
        errors.append(f"unsupported_formats 必须精确覆盖无法处理的格式：{sorted(expected_unknown)}")
    return errors


def candidate_context(args: argparse.Namespace) -> tuple[
    Path, Path, Path, dict[str, Any], dict[str, Any], str, str, Path, dict[str, Any]
]:
    output_root, relation_path, flow_path, relations, flow, relation_fingerprint, flow_fingerprint = source_context(args)
    claims_path = Path(args.claims).resolve()
    if not claims_path.is_relative_to(output_root):
        raise ContractError("候选文件必须位于 capability-distillation 输出目录内")
    claims = load_json(claims_path, MAX_CANDIDATE_BYTES)
    return (
        output_root, relation_path, flow_path, relations, flow, relation_fingerprint,
        flow_fingerprint, claims_path, claims,
    )


def validation_payload(
    claims: dict[str, Any], relations: dict[str, Any], flow: dict[str, Any], output_root: Path, relation_path: Path,
    flow_path: Path, relation_fingerprint: str, flow_fingerprint: str, claims_path: Path,
) -> dict[str, Any]:
    errors = validate_plan(
        claims, relations, flow, relation_path, flow_path, relation_fingerprint, flow_fingerprint
    )
    reliability_gates: dict[str, Any] = {}
    question_errors = open_question_gate_errors(flow.get("open_questions"))
    if question_errors:
        errors.extend(question_errors)
        reliability_gates["open_questions"] = {
            "status": "blocked",
            "errors": question_errors,
            "policy": "Unresolved or critical business questions cannot be transferred into a publishable capability package.",
        }
    _, operational, operational_errors = operational_context(relations, relation_path)
    recipe_verification: dict[str, Any] | None = None
    recipe_replay_runtime: dict[str, Any] | None = None
    if not operational_errors:
        # ``compiled-recipes.json`` is reviewed after the generic capability
        # plan in many scenarios.  Never let a preflight validate a stale,
        # empty replay contract created by an earlier prepare invocation.
        refresh_recipe_replay_contract(output_root, flow, operational)
        if not errors:
            try:
                recipe_replay_runtime = materialize_recipe_replay_runtime(
                    claims, flow, output_root,
                )
                verification = recipe_replay_runtime.get("recipe_verification")
                recipe_verification = verification if isinstance(verification, dict) else None
            except ContractError as exc:
                errors.append(f"Cannot materialize the private recipe replay runtime: {exc}")
        else:
            recipe_verification = compiled_recipe_verification(output_root, flow, operational)
    if not errors:
        return {
            "schema_version": SCHEMA_VERSION,
            "status": "valid",
            "error_count": 0,
            "claims": str(claims_path),
            "foundation_count": len(claims.get("foundation_skills", [])),
            "stage_skill_count": len(claims.get("stage_skills", [])),
            "total_skill_count": len(claims.get("foundation_skills", [])) + len(claims.get("stage_skills", [])) + 2,
            "recipe_verification": recipe_verification,
            "recipe_replay_runtime": recipe_replay_runtime,
            "next_action": (
                "If compiled recipes require deterministic results, run recipe_replay_runner.py against "
                "recipe-replay-runtime.json with a platform-signed private oracle fixture, then rerun preflight and finalize. "
                "Otherwise finalize produces an evidence-only package."
            ),
        }
    payload = {
        "schema_version": SCHEMA_VERSION,
        "status": "validation_failed",
        "error_count": len(errors),
        "errors": errors,
        "claims": str(claims_path),
        "repair_target": str(claims_path),
        "candidate_preserved": True,
        "repair_hints": [
            "不得改写 prepare 生成的文件清单、格式、流程节点和输入输出事实。",
            "每个流程阶段恰好生成一个阶段 Skill；规则不足时写运行时契约或待确认项。",
            "只生成当前格式真正需要的基础能力，并在描述中写清场景文件角色和调用时机。",
            "程序步骤必须引用流程阶段、输入输出、控制、状态或交接 ID，不得凭历史记录补微观逻辑。",
            "所有输出 Skill 必须脱离 Studio，资源使用相对路径，秘密只来自环境变量。",
            "未解决问题必须记录可审计 resolution；临时放行必须写 runtime_exception 和用户批准证据。",
        ],
        "next_action": "一次性修正 repair_target 后重跑 preflight；fingerprint 过期时先重新 prepare。",
    }
    if recipe_verification is not None:
        payload["recipe_verification"] = recipe_verification
    if reliability_gates:
        payload["reliability_gates"] = reliability_gates
    return payload


def preflight(args: argparse.Namespace) -> tuple[int, dict[str, Any]]:
    (
        output_root, relation_path, flow_path, relations, flow, relation_fingerprint,
        flow_fingerprint, claims_path, claims,
    ) = candidate_context(args)
    payload = validation_payload(
        claims, relations, flow, output_root, relation_path, flow_path, relation_fingerprint, flow_fingerprint, claims_path
    )
    validation_path = output_root / "validation-errors.json"
    if payload["status"] == "validation_failed":
        atomic_json(validation_path, payload)
        return 2, payload
    validation_path.unlink(missing_ok=True)
    return 0, payload


def yaml_scalar(value: str) -> str:
    return json.dumps(compact(value, 2_000), ensure_ascii=False)


def skill_frontmatter(name: str, description: str) -> str:
    return f"---\nname: {name}\ndescription: {yaml_scalar(description)}\n---\n"


def render_agents_yaml(name: str, display_name: str, description: str) -> str:
    short = compact(description, 64)
    if len(short) < 25:
        short = compact(f"{display_name}：{description}", 64)
    return (
        "interface:\n"
        f"  display_name: {yaml_scalar(display_name)}\n"
        f"  short_description: {yaml_scalar(short)}\n"
        f"  default_prompt: {yaml_scalar(f'Use ${name} to complete the matching business responsibility.')}\n"
    )


def format_list(items: Iterable[str], empty: str = "无") -> str:
    values = [str(item) for item in items if str(item)]
    return "、".join(values) if values else empty


def write_skill_metadata(root: Path, name: str, display_name: str, description: str) -> None:
    atomic_text(root / "agents" / "openai.yaml", render_agents_yaml(name, display_name, description))


def foundation_role_lines(item: dict[str, Any]) -> list[str]:
    lines = []
    for role in item.get("file_roles", []):
        business = format_list(role.get("business_roles", []), "场景输入材料")
        stages = format_list(role.get("stage_roles", []), "按任务判断")
        lines.append(f"- `{role.get('extension', '')}`：业务角色 {business}；相关阶段 {stages}。")
    return lines or ["- 当前上游未提供可定位文件名；按运行时输入契约选择文件。"]


def system_role_lines(item: dict[str, Any]) -> list[str]:
    return [
        f"- `{role.get('node_id', '')}` / **{role.get('name', '')}**：{role.get('description', '')}；"
        f"相关阶段 {format_list(role.get('stage_ids', []))}。"
        for role in item.get("system_roles", []) if isinstance(role, dict)
    ] or ["- 当前上游没有声明外部系统能力角色。"]


def operational_sources_for(item: dict[str, Any], operational: dict[str, Any]) -> list[dict[str, Any]]:
    formats = set(unique_strings(item.get("formats")))
    return [
        source for source in operational.get("sources", [])
        if isinstance(source, dict) and str(source.get("extension", "")) in formats
    ]


def render_tabular_skill(item: dict[str, Any], scenario_name: str, operational: dict[str, Any]) -> str:
    sources = operational_sources_for(item, operational)
    runtime_lines = [
        f"- `{source.get('source_id', '')}` / `{source.get('view_name', '')}` / `{source.get('path', '')}`："
        "仅当当前规则和 SQL 引用它时才要求运行时绑定。"
        for source in sources if source.get("runtime_required") is True
    ] or ["- 当前契约没有该格式的运行时输入。"]
    template_lines = [
        f"- `{source.get('source_id', '')}` / `{source.get('path', '')}`：仅保留表头、字段、类型和定位作为输出模板；"
        "第三方运行时不需要原文件，也不得把它注册为查询表。"
        for source in sources if source.get("lifecycle") == "design_time_template"
    ] or ["- 当前契约没有该格式的设计态模板。"]
    rule_sources = set(unique_strings(operational.get("rule_source_ids")))
    rule_lines = [
        f"- `{source.get('source_id', '')}` / `{source.get('view_name', '')}` / `{source.get('path', '')}`"
        for source in sources if source.get("source_id") in rule_sources
    ] or ["- 当前表格清单没有被标记为规则源；按阶段输入契约接收规则对象。"]
    link_lines = [
        f"- `{link.get('link_id', '')}`：`{link.get('source_id', '')}` ↔ `{link.get('target_id', '')}`；"
        f"候选键 `{(link.get('recommended_candidate') or {}).get('source_field', '')}` ↔ "
        f"`{(link.get('recommended_candidate') or {}).get('target_field', '')}`；"
        f"契约键组 {len(link.get('candidate_key_sets', [])) or 1} 组"
        for link in operational.get("links", [])
        if isinstance(link, dict)
        and (isinstance(link.get("recommended_candidate"), dict) or link.get("candidate_key_sets"))
    ] or ["- 当前契约没有已通过证据门禁的跨表连接；禁止自行猜测连接键。"]
    lines = [
        skill_frontmatter(item["skill_name"], item["description"]),
        f"# {item['display_name']}", "",
        f"为“{scenario_name}”处理 {format_list(item['formats'])} 表格数据。该能力只负责有界读取、检查和只读查询，不替代业务阶段判断。",
        "", "## 场景文件角色", "", *foundation_role_lines(item),
        "", "## 调用条件", "", *[f"- {value}" for value in item["when_to_use"]],
        "", "## 执行", "",
        "1. 先用 `contract` 读取机器可读来源、正确表头、生命周期、列、角色和连接候选。只对当前规则、阶段或 SQL 实际引用的 runtime_input source_id 运行带 `--source-id` 的 `preflight-contract`；design_time_template 永远不是缺失输入。",
        "2. 若用户请求依赖结构化规则或政策，先对对应来源运行 `search-contract`，返回命中的完整记录；记录标识、选择字段、叙述字段及同一行其他字段都必须保留。若命中多条，继续用用户条件缩小；仍有多条实质不同记录时列出记录标识并请求选择，禁止拼接成一条记录。",
        "3. Agent 只根据用户目标与完整规则行推导投影、谓词、分组和 SQL；不得从历史结果样例固化规则。",
        "4. 每条跨表链路先运行 `validate-join`，检查空值、双向未匹配和连接放大。单键出现多对多时，只能继续验证契约已经列出的复合键组；所有键组均未通过则停止，不猜键。",
        "5. 使用 `query-contract` 只注册 SQL 中实际出现的视图并执行有界只读 SQL；多来源 SQL 必须用 `--link-id` 在同一次命令中重新校验连接。运行文件名与蒸馏样本不同时用 `--bind <source-id>=<relative-path>` 显式绑定，校验字段兼容性而非历史文件摘要。大型 Excel 不允许退化为 Agent 读取或内存样本物化。",
        "6. 用户要求全部结果时使用 `export-contract` 输出 CSV/Parquet；该命令不把全量行放入 Agent 上下文，并返回行数、查询摘要和文件摘要。",
        "", "## 运行时输入", "", *runtime_lines,
        "", "## 设计态输出模板", "", *template_lines,
        "", "## 规则源", "", *rule_lines,
        "", "## 已验证连接候选", "", *link_lines,
        "", "~~~text",
        "python \"<this-skill>/scripts/query_tabular.py\" contract --contract \"<this-skill>/references/operational-data-contract.json\"",
        "python \"<this-skill>/scripts/query_tabular.py\" preflight-contract --contract \"<this-skill>/references/operational-data-contract.json\" --data-root \"<data-root>\" --source-id \"<source-id>\" --bind \"<source-id>=<relative-runtime-file>\"",
        "python \"<this-skill>/scripts/query_tabular.py\" search-contract --contract \"<this-skill>/references/operational-data-contract.json\" --data-root \"<data-root>\" --source-id \"<rule-source-id>\" --bind \"<rule-source-id>=<relative-runtime-file>\" --term \"<user term>\" --max-rows 20",
        "python \"<this-skill>/scripts/query_tabular.py\" validate-join --contract \"<this-skill>/references/operational-data-contract.json\" --data-root \"<data-root>\" --link-id \"<link-id>\" --key-set-index 0",
        "python \"<this-skill>/scripts/query_tabular.py\" query-contract --contract \"<this-skill>/references/operational-data-contract.json\" --data-root \"<data-root>\" --bind \"<source-id>=<relative-runtime-file>\" --link-id \"<link-id>@<key-set-index>\" --sql \"SELECT ... FROM source_1 ...\" --max-rows 500",
        "python \"<this-skill>/scripts/query_tabular.py\" export-contract --contract \"<this-skill>/references/operational-data-contract.json\" --data-root \"<data-root>\" --bind \"<source-id>=<relative-runtime-file>\" --link-id \"<link-id>@<key-set-index>\" --sql \"SELECT ...\" --output \"<result.parquet>\"",
        "~~~", "", "## 场景约束", "", *[f"- {value}" for value in item["scenario_instructions"]],
        "", "## 非职责", "", *[f"- {value}" for value in item["non_goals"]],
        "", "## 可移植运行", "",
        "从本 Skill 的 `requirements.txt` 安装依赖。所有输入路径由调用方传入；不假设任何平台目录。源数据始终只读；仅 `export-contract` 可向调用方明确指定的 CSV/Parquet 路径写完整查询结果。", "",
    ]
    return "\n".join(lines)


def render_document_skill(
    item: dict[str, Any], scenario_name: str, ocr_skill_name: str, operational: dict[str, Any]
) -> str:
    ocr_handoff = f"若 PDF 返回 `ocr_recommended`，转交 `${ocr_skill_name}`。" if ocr_skill_name else "若 PDF 文本层稀疏，报告需要外部 OCR 能力。"
    rule_sources = set(unique_strings(operational.get("rule_source_ids")))
    document_rule_lines = [
        f"- `{source.get('source_id', '')}` / `{source.get('path', '')}`：检索并交付完整适用章节及其定位。"
        for source in operational_sources_for(item, operational)
        if source.get("source_id") in rule_sources and source.get("kind") != "tabular"
    ] or ["- 当前非结构化来源未被标记为规则源；只按阶段输入契约检索业务证据。"]
    lines = [
        skill_frontmatter(item["skill_name"], item["description"]),
        f"# {item['display_name']}", "",
        f"为“{scenario_name}”读取 {format_list(item['formats'])} 文档内容，并提供带来源的有界文本。",
        "", "## 场景文件角色", "", *foundation_role_lines(item),
        "", "## 调用条件", "", *[f"- {value}" for value in item["when_to_use"]],
        "", "## 执行", "",
        "1. 先运行 `inspect` 确认格式和是否需要 OCR；不得把整份文档放进 Agent 上下文。",
        "2. 对 TXT、Markdown、Word、可搜索 PDF 等运行 `index`，按页码、段落、幻灯片或行号建立本地分块索引。",
        "3. Agent 依据用户目标和完整规则记录生成检索词，只运行 `search` 获取有限命中；需要上下文时按 `chunk_id` 运行 `context` 取得相邻块，并用标题/定位确认完整章节边界。",
        f"4. {ocr_handoff} OCR 必须使用 `--output` 写 JSON，再由本 Skill 的 `index-ocr` 建索引，正文不得直接打印给 Agent。",
        "5. 每个业务判断必须携带 source_digest、locator、chunk_id 和 text_digest；语义命中不能替代结构化业务主键连接。",
        "", "## 非结构化规则源", "", *document_rule_lines,
        "", "~~~text",
        "python \"<this-skill>/scripts/extract_documents.py\" inspect --input \"<file>\"",
        "python \"<this-skill>/scripts/extract_documents.py\" index --input \"<file>\" --output \"<index.db>\"",
        "python \"<this-skill>/scripts/extract_documents.py\" search --index \"<index.db>\" --term \"<term>\" --limit 20",
        "python \"<this-skill>/scripts/extract_documents.py\" get --index \"<index.db>\" --chunk-id 1",
        "python \"<this-skill>/scripts/extract_documents.py\" context --index \"<index.db>\" --chunk-id 1 --before 2 --after 2",
        "python \"<this-skill>/scripts/extract_documents.py\" index-ocr --input-json \"<ocr-output.json>\" --output \"<index.db>\" --source \"<original-file>\"",
        "~~~", "", "## 场景约束", "", *[f"- {value}" for value in item["scenario_instructions"]],
        "", "## 非职责", "", *[f"- {value}" for value in item["non_goals"]],
        "", "## 可移植运行", "",
        "从本 Skill 的 `requirements.txt` 安装依赖。输入路径由调用方提供，资源路径相对于本 Skill 解析。", "",
    ]
    return "\n".join(lines)


def render_ocr_skill(item: dict[str, Any], scenario_name: str) -> str:
    lines = [
        skill_frontmatter(item["skill_name"], item["description"]),
        f"# {item['display_name']}", "",
        f"为“{scenario_name}”解析 {format_list(item['formats'])} 扫描文档或图片。保留通用 OCR 的本地路径、URL、Base64、批量输入和 JSON 输出能力，同时按当前场景限定触发时机。",
        "", "## 场景文件角色", "", *foundation_role_lines(item),
        "", "## 调用条件", "", *[f"- {value}" for value in item["when_to_use"]],
        "", "## 执行", "",
        "1. 仅在图片输入、扫描 PDF 或文档读取能力返回 `ocr_recommended` 时调用。",
        "2. 本地文件使用 `--path`，远程文件使用 `--url`，Base64 使用 `--b64` 与 `--name`；批量路径以逗号分隔。",
        "3. 必须使用 `--output` 将结构化 JSON 写入文件，只把有界状态返回 Agent；随后交给文档基础 Skill 的 `index-ocr` 建立可检索证据索引。",
        "4. OCR 文本是带置信边界的输入证据，不直接构成业务结论；图片或扫描件不得整篇进入 Agent 上下文。",
        "", "~~~text",
        "python \"<this-skill>/scripts/parse.py\" --path \"<file>\" --format json --output \"<ocr-output.json>\"",
        "~~~", "", "## 配置", "",
        "- `config/defaults.json` 继承系统 `ocr-parser` 的公开配置字段和值；API Key 等凭据不会写入能力包。",
        "- 第三方运行环境必须通过同名环境变量提供凭据并可覆盖包内配置；不得在日志、Agent 上下文或业务结果中回显凭据。",
        "- 可用 `OCR_LANG_LIST`、`OCR_TABLE_ENABLE_PDF`、`OCR_TABLE_ENABLE_IMAGE`、`OCR_AUTO_ROTATE_PDF` 和 `OCR_AUTO_ROTATE_IMAGE` 调整识别。",
        "", "## 场景约束", "", *[f"- {value}" for value in item["scenario_instructions"]],
        "", "## 非职责", "", *[f"- {value}" for value in item["non_goals"]],
        "", "## 可移植运行", "",
        "从本 Skill 的 `requirements.txt` 安装依赖。除场景化 `SKILL.md`、UI 元数据和场景绑定外，运行资源来自完整系统 Skill。", "",
    ]
    return "\n".join(lines)


def render_knowledge_skill(item: dict[str, Any], scenario_name: str) -> str:
    lines = [
        skill_frontmatter(item["skill_name"], item["description"]),
        f"# {item['display_name']}", "",
        f"为“{scenario_name}”检索外部知识库。保留系统 `vector-kb` 的检索、原文定位、结构化返回、错误处理和完整配置能力，只定制当前场景的触发条件与知识用途。",
        "", "## 场景系统角色", "", *system_role_lines(item),
        "", "## 调用条件", "", *[f"- {value}" for value in item["when_to_use"]],
        "", "## 执行", "",
        "1. 只有相关阶段或完整规则记录明确需要外部知识补充时才检索，不能用知识库内容替代规则原文或结构化业务事实。",
        "2. 使用包内 `scripts/scenario_kb.py` 检索或取得原文定位；Agent 不得临时编写 HTTP/Python 客户端。",
        "3. 先用有业务限定的查询获取有限切片；需要核对时按 `document_id`、`chunk_id` 调用原文定位能力。",
        "4. 输出必须保留标题、document_id、chunk_id、相似度及来源。完整规则把该知识声明为必需时传 `--required`；零结果、鉴权失败或服务不可达会返回 `manual_intervention_required`，必须停止判定并明确要求人工补充，不能猜测。可选增强失败时可以继续，但必须声明未使用该增强。",
        "", "~~~text",
        "python \"<this-skill>/scripts/scenario_kb.py\" search --query \"<业务问题与限定条件>\" --limit 5 [--required]",
        "python \"<this-skill>/scripts/scenario_kb.py\" source --document-id \"<document-id>\" --chunk-id \"<chunk-id>\"",
        "~~~", "", "## 配置", "",
        "- `config/defaults.json` 继承系统 `vector-kb` 的服务地址、知识库 ID、超时和其他公开字段；API Key 不会写入能力包。",
        "- 第三方环境必须通过 `VECTOR_KB_API_KEY` 等同名环境变量提供凭据并可覆盖包内配置；任何输出均不得回显凭据。",
        "", "## 场景约束", "", *[f"- {value}" for value in item["scenario_instructions"]],
        "", "## 非职责", "", *[f"- {value}" for value in item["non_goals"]],
        "", "## 可移植运行", "",
        "从本 Skill 的 `requirements.txt` 安装依赖。除场景化说明、UI 元数据和场景绑定外，运行资源来自完整系统 Skill。", "",
    ]
    return "\n".join(lines)


def render_contract_item(item: dict[str, Any], format_key: str) -> str:
    formats = format_list(item.get(format_key, []), "运行时对象")
    nodes = format_list(item.get("relation_node_ids", []))
    required = "必需" if item.get("required", False) else "可选"
    return f"- **{item.get('name', '')}**（{required}；{formats}；关系节点 {nodes}）：{item.get('description', '')}"


def scenario_executor_skill_name(bundle_name: str) -> str:
    suffix = "-main-executor"
    base = re.sub(r"[^a-z0-9-]+", "-", str(bundle_name).casefold()).strip("-") or "business-scenario"
    return f"{base[:63 - len(suffix)].rstrip('-')}{suffix}"


def _source_columns(source: dict[str, Any]) -> list[str]:
    return list(dict.fromkeys(
        str(column.get("query_name") or column.get("name") or "")
        for table in source.get("tables", []) if isinstance(table, dict)
        for column in table.get("columns", []) if isinstance(column, dict)
        if str(column.get("query_name") or column.get("name") or "")
    ))


def _pick_column(columns: list[str], markers: tuple[str, ...]) -> str:
    for marker in markers:
        for column in columns:
            if marker.casefold() in column.casefold():
                return column
    return ""


def source_material_role(source: dict[str, Any]) -> str:
    roles = {
        str(role.get("node_type", ""))
        for role in source.get("roles", []) if isinstance(role, dict)
    }
    if "rule" in roles:
        return "rule_or_policy_material"
    if "output" in roles:
        return "result_or_outcome_record"
    explicit = str(source.get("material_role", ""))
    if explicit:
        return explicit
    values = [str(item) for item in source.get("material_roles", []) if str(item)]
    return values[0] if values else "unknown_material"


def source_row_count(source: dict[str, Any]) -> int:
    return max(
        (int(table.get("row_count") or 0) for table in source.get("tables", []) if isinstance(table, dict)),
        default=0,
    )


def build_capability_model(
    flow_contract: dict[str, Any], operational: dict[str, Any],
) -> dict[str, Any]:
    """Build the portable, domain-neutral model consumed by the executor.

    The model is deliberately separate from Skill prose.  It preserves the
    accepted flow, source grain, semantic field roles, validated lineage and
    historical trace blueprint so another Agent can execute the same pattern
    against compatible runtime data without re-discovering the workflow.
    """
    sources = [item for item in operational.get("sources", []) if isinstance(item, dict)]
    runtime_ids = set(unique_strings(operational.get("runtime_source_ids")))
    result_ids = set(unique_strings(operational.get("result_source_ids")))
    rule_ids = set(unique_strings(operational.get("rule_source_ids")))
    profiles = []
    for source in sources:
        source_id = str(source.get("source_id", ""))
        descriptors = source_column_descriptors(source)
        profiles.append({
            "source_id": source_id,
            "path": source.get("path", ""),
            "kind": source.get("kind", ""),
            "material_role": source_material_role(source),
            "declared_roles": source.get("roles", []),
            "runtime_required": source_id in runtime_ids,
            "design_time_result": source_id in result_ids,
            "row_count": source_row_count(source),
            "tables": [
                {
                    "table": table.get("sheet_or_table") or table.get("table_name"),
                    "row_count": table.get("row_count"),
                    "column_count": table.get("column_count"),
                    "inferred_material_role": table.get("inferred_material_role", ""),
                }
                for table in source.get("tables", []) if isinstance(table, dict)
            ],
            "field_semantics": descriptors[:240],
        })
    stage_records = [
        {
            "stage_id": stage.get("stage_id", stage.get("id", "")),
            "name": stage.get("name", ""),
            "stage_type": stage.get("stage_type", ""),
            "objective": stage.get("objective", ""),
            "outcome": stage.get("outcome", ""),
            "input_contract": stage.get("input_contract", []),
            "output_contract": stage.get("output_contract", []),
            "predecessor_stage_ids": stage.get("predecessor_stage_ids", []),
            "successor_stage_ids": stage.get("successor_stage_ids", []),
            "procedure": stage.get("procedure", []),
        }
        for stage in flow_contract.get("stages", []) if isinstance(stage, dict)
    ]
    trace = compact_trace_evidence(operational, include_rows=False)
    warnings = [
        str(value) for value in operational.get("warnings", [])
        if str(value)
    ]
    unresolved = []
    if not rule_ids:
        unresolved.append({"kind": "rule_source", "message": "No structured rule/policy source was proven by the accepted relation contract."})
    if not any(profile["runtime_required"] for profile in profiles):
        unresolved.append({"kind": "runtime_source", "message": "No runtime-bound source was proven by the accepted relation contract."})
    if not flow_contract.get("main_flow"):
        unresolved.append({"kind": "flow", "message": "The accepted flow has no main sequence."})
    return {
        "schema_version": 1,
        "model_type": "evidence_backed_business_capability",
        "inference_policy": {
            "source": "accepted_relations_flow_and_historical_trace",
            "domain_specific_rules": "not_embedded",
            "runtime_content": "must_be_rebound_and_revalidated",
            "historical_trace": "design_time_blueprint_only",
        },
        "flow": {
            "main_flow": flow_contract.get("main_flow", []),
            "execution_mode": flow_contract.get("execution_mode", "evidence_pipeline"),
            "stages": stage_records,
            "controls": flow_contract.get("controls", []),
            "open_questions": flow_contract.get("open_questions", []),
        },
        "source_profiles": profiles,
        "rule_source_ids": sorted(rule_ids),
        "runtime_source_ids": sorted(runtime_ids),
        "result_source_ids": sorted(result_ids),
        "lineage": {
            "joins": [
                {
                    "link_id": link.get("link_id"),
                    "source_id": link.get("source_id"),
                    "target_id": link.get("target_id"),
                    "recommended_candidate": link.get("recommended_candidate", {}),
                    "candidate_key_sets": link.get("candidate_key_sets", []),
                    "runtime_eligible": link.get("runtime_eligible", True),
                }
                for link in operational.get("links", []) if isinstance(link, dict)
            ],
            "semantic_routes": operational.get("semantic_routes", []),
        },
        "historical_trace": trace,
        "output_templates": flow_contract.get("design_time_output_templates", []),
        "warnings": warnings,
        "unresolved": unresolved,
    }


def build_execution_plan(flow_contract: dict[str, Any], operational: dict[str, Any]) -> dict[str, Any]:
    """Compile a domain-neutral execution plan from accepted evidence facts."""
    sources = [item for item in operational.get("sources", []) if isinstance(item, dict)]
    source_by_id = {str(item.get("source_id", "")): item for item in sources}
    runtime_ids = set(unique_strings(operational.get("runtime_source_ids")))
    result_ids = set(unique_strings(operational.get("result_source_ids")))
    rule_ids = [source_id for source_id in unique_strings(operational.get("rule_source_ids")) if source_id in source_by_id]

    def trace_roles() -> dict[str, set[str]]:
        result: dict[str, set[str]] = defaultdict(set)
        trace = operational.get("trace_evidence") if isinstance(operational.get("trace_evidence"), dict) else {}
        for bundle in trace.get("bundles", []) if isinstance(trace.get("bundles"), list) else []:
            if not isinstance(bundle, dict):
                continue
            for item in bundle.get("sources", []) if isinstance(bundle.get("sources"), list) else []:
                if not isinstance(item, dict):
                    continue
                source_id = str(item.get("source_id", ""))
                if source_id:
                    result[source_id].add(str(item.get("role", "")))
        return result

    traced_roles = trace_roles()
    output_columns = {
        str(column)
        for template in flow_contract.get("design_time_output_templates", [])
        if isinstance(template, dict)
        for column in template.get("output_columns", []) if str(column)
    }
    ranked_candidates = []
    for source in sources:
        source_id = str(source.get("source_id", ""))
        if source_id not in runtime_ids or source_id in rule_ids or source_id in result_ids:
            continue
        descriptors = source_column_descriptors(source)
        overlap = sum(1 for item in descriptors if item["column"] in output_columns)
        semantic_weight = sum(
            1 for item in descriptors if item["semantic_role"] in {"subject", "measure", "temporal", "decision"}
        )
        trace_weight = len(traced_roles.get(source_id, set()).intersection({"linked_source", "result_anchor"}))
        ranked_candidates.append({
            "source_id": source_id,
            "path": source.get("path", ""),
            # Output overlap identifies useful projection fields; row grain
            # and the historical trace identify the source to anchor a
            # request.  The latter must win when a detail source is joined to
            # several aggregate/context sources.
            "score": round(overlap * 0.2 + trace_weight * 4 + semantic_weight * 0.1 + min(source_row_count(source) / 100_000, 10), 4),
            "evidence": {
                "output_column_overlap": overlap,
                "trace_roles": sorted(traced_roles.get(source_id, set())),
                "semantic_field_count": semantic_weight,
            },
        })
    ranked_candidates.sort(key=lambda item: (-item["score"], item["source_id"]))
    primary_id = str(ranked_candidates[0]["source_id"]) if ranked_candidates else ""
    primary = source_by_id.get(primary_id, {})

    rule_profiles = []
    dispatch_source = source_by_id.get(rule_ids[0], {}) if rule_ids else {}
    for source_id in rule_ids:
        source = source_by_id.get(source_id, {})
        descriptors = source_column_descriptors(source)
        by_role: dict[str, list[str]] = defaultdict(list)
        for descriptor in descriptors:
            by_role[str(descriptor["semantic_role"])].append(str(descriptor["column"]))
        selector_columns = list(dict.fromkeys(by_role.get("selector", []) + by_role.get("decision", [])))
        identifier_columns = by_role.get("identifier", [])
        narrative_columns = by_role.get("narrative", [])
        rule_profiles.append({
            "source_id": source_id,
            "path": source.get("path", ""),
            "selector_columns": selector_columns[:20],
            "identifier_columns": identifier_columns[:20],
            "narrative_columns": narrative_columns[:40],
            "decision_columns": by_role.get("decision", [])[:20],
            "field_semantics": descriptors[:240],
        })
    first_rule = rule_profiles[0] if rule_profiles else {}
    dispatch_key = (first_rule.get("selector_columns") or [""])[0]
    knowledge_id = (first_rule.get("identifier_columns") or [""])[0]
    knowledge_description = (first_rule.get("narrative_columns") or [""])[0]

    joins: list[dict[str, Any]] = []
    for link in operational.get("links", []):
        if not isinstance(link, dict) or link.get("runtime_eligible") is False:
            continue
        left, right = str(link.get("source_id", "")), str(link.get("target_id", ""))
        if left not in runtime_ids or right not in runtime_ids or left == right:
            continue
        recommended = link.get("recommended_candidate") if isinstance(link.get("recommended_candidate"), dict) else {}
        key_pairs = recommended.get("key_pairs") if isinstance(recommended.get("key_pairs"), list) else []
        candidate_sets = link.get("candidate_key_sets") if isinstance(link.get("candidate_key_sets"), list) else []
        joins.append({
            "link_id": link.get("link_id"),
            "left_source_id": left,
            "right_source_id": right,
            "left_source": source_by_id.get(left, {}).get("path", left),
            "right_source": source_by_id.get(right, {}).get("path", right),
            "recommended_key_pairs": [
                {
                    "left": pair.get("source_field") if left == str(link.get("source_id", "")) else pair.get("target_field"),
                    "right": pair.get("target_field") if left == str(link.get("source_id", "")) else pair.get("source_field"),
                }
                for pair in key_pairs if isinstance(pair, dict)
            ],
            "candidate_key_sets": candidate_sets[:10],
            "validation": "revalidate_nulls_unmatched_rows_and_fanout_at_runtime",
            "preferred": left == primary_id or right == primary_id,
        })
    joins.sort(key=lambda item: (not bool(item.get("preferred")), str(item.get("link_id", ""))))

    flow_stage_ids = [
        str(item) for item in flow_contract.get("main_flow", []) if str(item)
    ] or [
        str(stage.get("stage_id", ""))
        for stage in flow_contract.get("stages", [])
        if isinstance(stage, dict) and str(stage.get("stage_id", ""))
    ]
    steps: list[dict[str, Any]] = [
        {
            "order": 1,
            "operation": "RESOLVE_SCOPE",
            "source_ids": sorted(runtime_ids),
            "contract": "normalize the request and bind only declared compatible runtime sources",
        },
    ]
    if rule_ids:
        steps.append({
            "order": len(steps) + 1,
            "operation": "LOCATE_COMPLETE_RULE_RECORD",
            "source_ids": rule_ids,
            "selector_columns": [profile.get("selector_columns", []) for profile in rule_profiles],
            "contract": "select one complete governing record and preserve its provenance before bulk reads",
        })
    if ranked_candidates:
        steps.append({
            "order": len(steps) + 1,
            "operation": "READ_RUNTIME_EVIDENCE",
            "source_ids": [item["source_id"] for item in ranked_candidates],
            "primary_source_id": primary_id,
            "candidate_sources": ranked_candidates[:12],
            "contract": "search bounded evidence from the ranked source set; do not discard a source solely because it is not primary",
        })
    if joins:
        steps.append({
            "order": len(steps) + 1,
            "operation": "VALIDATE_LINEAGE_AND_JOIN",
            "link_ids": [item.get("link_id") for item in joins],
            "contract": "use only accepted key sets and report null, unmatched, fanout and truncation evidence",
        })
    if flow_stage_ids:
        steps.append({
            "order": len(steps) + 1,
            "operation": "APPLY_ACCEPTED_FLOW",
            "stage_ids": flow_stage_ids,
            "contract": "execute the accepted stage sequence and preserve stage inputs, outputs and unresolved questions",
        })
    templates = flow_contract.get("design_time_output_templates") if isinstance(flow_contract.get("design_time_output_templates"), list) else []
    output_specs = []
    for template in templates:
        if not isinstance(template, dict):
            continue
        output_specs.append({
            "output_id": template.get("template_id"),
            "name": template.get("name"),
            "format": template.get("format", "xlsx"),
            "columns": template.get("output_columns", []),
            "column_semantics": template.get("column_semantics", []),
            "required_source_ids": sorted(runtime_ids),
            "pipeline": [*steps, {
                "order": len(steps) + 1,
                "operation": "MATERIALIZE_DECLARED_OUTPUT",
                "output": template.get("name", "result"),
                "contract": "write the declared output shape with rule, data and coverage provenance",
            }],
        })
    model = build_capability_model(flow_contract, operational)
    model["selection"] = {
        "primary_source_id": primary_id,
        "primary_candidates": ranked_candidates[:12],
        "rule_profiles": rule_profiles,
    }
    field_roles: dict[str, list[str]] = defaultdict(list)
    for descriptor in source_column_descriptors(primary):
        field_roles[str(descriptor["semantic_role"])].append(
            f"{primary.get('path', '')}.{descriptor['column']}"
        )
    dispatch_config = {
        "knowledge_table": dispatch_source.get("path", "") if dispatch_source else "",
        "dispatch_key_column": dispatch_key,
        "knowledge_id_column": knowledge_id,
        "knowledge_description_column": knowledge_description,
        "nl_columns": first_rule.get("narrative_columns", []),
        "dispatch_policy": "select_complete_record_then_use_declared_selector_or_decision_fields; preserve_full_record",
        "rule_sources": rule_profiles,
        "field_role_map": {
            "by_semantic_role": dict(field_roles),
            "primary_source_id": primary_id,
            "primary_source_path": primary.get("path", "") if primary else "",
        },
    }
    model["execution_sequence"] = steps
    model["output_specs"] = output_specs
    return {
        "schema_version": 2,
        "mode": "knowledge_engine" if rule_ids and dispatch_key else "evidence_pipeline",
        "primary_source_id": primary_id,
        "primary_source_path": primary.get("path", "") if primary else "",
        "primary_candidates": ranked_candidates[:12],
        "rule_source_ids": rule_ids,
        "capability_model": model,
        "dispatch_config": dispatch_config,
        "join_plan": joins,
        "output_specs": output_specs,
        "steps": steps,
        "semantic_boundary": "The engine supplies the accepted flow, complete governing records, bounded runtime evidence, validated lineage and output mapping; the Agent applies business semantics only to that evidence and reports uncertainty.",
    }


def compiled_recipe_catalog(output_root: Path, operational: dict[str, Any]) -> dict[str, Any]:
    """Validate optional reviewed rule recipes without embedding domain logic in code.

    A recipe is a scenario artifact, reviewed against declared rule and runtime
    schemas.  The portable executor only evaluates supported recipes; it falls
    back to a bounded evidence handoff when no recipe is present for a rule.
    """
    path = output_root / "compiled-recipes.json"
    if not path.is_file():
        return {
            "schema_version": 1,
            "recipes": [],
            "policy": "No reviewed deterministic recipe is available; use the evidence handoff and never infer a result automatically.",
        }
    payload = load_json(path, MAX_CANDIDATE_BYTES)
    if payload.get("schema_version") != 1 or not isinstance(payload.get("recipes"), list):
        raise ContractError("compiled-recipes.json must contain schema_version 1 and a recipes list")
    sources = {
        str(item.get("source_id", "")): item
        for item in operational.get("sources", []) if isinstance(item, dict)
    }
    runtime_ids = {str(item) for item in operational.get("runtime_source_ids", [])}
    rule_ids = {str(item) for item in operational.get("rule_source_ids", [])}

    def fields(source_id: str) -> set[str]:
        source = sources.get(source_id, {})
        return {
            str(column.get("query_name") or column.get("name") or "")
            for table in source.get("tables", []) if isinstance(table, dict)
            for column in table.get("columns", []) if isinstance(column, dict)
            and str(column.get("query_name") or column.get("name") or "")
        }

    seen_ids: set[str] = set()
    for recipe in payload["recipes"]:
        if not isinstance(recipe, dict):
            raise ContractError("Each compiled recipe must be an object")
        recipe_id = str(recipe.get("id", ""))
        if not recipe_id or recipe_id in seen_ids:
            raise ContractError("Each compiled recipe requires a unique id")
        seen_ids.add(recipe_id)
        kind = str(recipe.get("kind", ""))
        if kind not in {"grouped_cooccurrence", "grouped_cooccurrence_from_rule_text"}:
            raise ContractError(f"Unsupported compiled recipe kind: {recipe.get('kind')}")
        selector = recipe.get("rule_selector")
        if not isinstance(selector, dict) or str(selector.get("source_id", "")) not in rule_ids:
            raise ContractError(f"Compiled recipe {recipe_id} must select one declared rule source")
        selector_fields = fields(str(selector["source_id"]))
        equals = selector.get("equals", {})
        fingerprint = str(selector.get("rule_fingerprint", "")).strip()
        is_family = str(selector.get("mode", "")) == "any_complete_rule"
        if not isinstance(equals, dict) or not set(map(str, equals)).issubset(selector_fields):
            raise ContractError(f"Compiled recipe {recipe_id} has an invalid rule selector")
        if is_family:
            if equals or fingerprint:
                raise ContractError(
                    f"Rule-family recipe {recipe_id} may not embed a historical rule value or fingerprint"
                )
        elif not equals and not fingerprint:
            raise ContractError(f"Compiled recipe {recipe_id} has an invalid rule selector")
        source_id = str(recipe.get("source_id", ""))
        if source_id not in runtime_ids:
            raise ContractError(f"Compiled recipe {recipe_id} references a non-runtime source")
        source_fields = fields(source_id)
        group_by = [str(item) for item in recipe.get("group_by", []) if str(item)]
        all_of_raw = recipe.get("all_of", [])
        any_of_raw = recipe.get("any_of", [])
        if kind == "grouped_cooccurrence_from_rule_text":
            if not is_family:
                raise ContractError(f"Rule-family recipe {recipe_id} must use mode=any_complete_rule")
            item_field = str(recipe.get("item_field", ""))
            if item_field not in source_fields:
                raise ContractError(f"Rule-family recipe {recipe_id} item_field is not present in its source")
            rule_fields = [str(item) for item in recipe.get("rule_text_fields", []) if str(item)]
            if not rule_fields or not set(rule_fields).issubset(selector_fields):
                raise ContractError(f"Rule-family recipe {recipe_id} rule_text_fields must be narrative fields of its rule source")
            if int(recipe.get("minimum_terms", 2)) < 2 or int(recipe.get("minimum_terms", 2)) > 8:
                raise ContractError(f"Rule-family recipe {recipe_id} minimum_terms must be 2-8")
            annotations_from_rule = recipe.get("result_annotations_from_rule", {})
            if not isinstance(annotations_from_rule, dict) or not annotations_from_rule or not set(
                str(value) for value in annotations_from_rule.values()
            ).issubset(selector_fields):
                raise ContractError(f"Rule-family recipe {recipe_id} result_annotations_from_rule is invalid")
            covered_outputs = recipe.get("covered_output_node_ids")
            if not isinstance(covered_outputs, list) or not unique_strings(covered_outputs):
                raise ContractError(f"Rule-family recipe {recipe_id} must declare covered_output_node_ids")
            replay_assertions = recipe.get("historical_replay_assertions")
            if not isinstance(replay_assertions, list) or not replay_assertions:
                raise ContractError(f"Rule-family recipe {recipe_id} must include historical_replay_assertions")
            replay_ids: set[str] = set()
            for assertion in replay_assertions:
                if not isinstance(assertion, dict):
                    raise ContractError(f"Rule-family recipe {recipe_id} replay assertion must be an object")
                assertion_id = str(assertion.get("id", "")).strip()
                if not assertion_id or assertion_id in replay_ids:
                    raise ContractError(f"Rule-family recipe {recipe_id} replay assertions require unique ids")
                replay_ids.add(assertion_id)
                if normalized_question_value(assertion.get("status")) not in {"passed", "approved", "通过"}:
                    raise ContractError(f"Rule-family recipe {recipe_id} replay assertion {assertion_id} is not passed")
                if not str(assertion.get("trace_bundle_id", "")).strip() or not str(assertion.get("evidence_ref", "")).strip():
                    raise ContractError(f"Rule-family recipe {recipe_id} replay assertion {assertion_id} lacks trace evidence")
                if not unique_strings(assertion.get("output_node_ids")):
                    raise ContractError(f"Rule-family recipe {recipe_id} replay assertion {assertion_id} lacks output coverage")
            if all_of_raw or any_of_raw:
                raise ContractError(f"Rule-family recipe {recipe_id} must not embed historical predicate literals")
            continue
        if not isinstance(all_of_raw, list) or not isinstance(any_of_raw, list):
            raise ContractError(f"Compiled recipe {recipe_id} predicates must be lists")
        predicates = [
            item for item in [*all_of_raw, *any_of_raw]
            if isinstance(item, dict)
        ]
        if not group_by or not all_of_raw or not any_of_raw or len(predicates) != len(all_of_raw) + len(any_of_raw):
            raise ContractError(f"Compiled recipe {recipe_id} requires group_by, all_of and any_of predicates")
        if not set(group_by).issubset(source_fields):
            raise ContractError(f"Compiled recipe {recipe_id} group_by is not present in its source")
        for predicate in predicates:
            if (
                str(predicate.get("field", "")) not in source_fields
                or str(predicate.get("operator", "")) not in {"contains", "equals"}
                or not str(predicate.get("value", ""))
            ):
                raise ContractError(f"Compiled recipe {recipe_id} has an invalid predicate")
        measure = str(recipe.get("summary_measure", ""))
        if measure and measure not in source_fields:
            raise ContractError(f"Compiled recipe {recipe_id} summary_measure is not present in its source")
        context_ids = recipe.get("context_source_ids", [])
        if not isinstance(context_ids, list) or any(str(item) not in runtime_ids for item in context_ids):
            raise ContractError(f"Compiled recipe {recipe_id} references an invalid context runtime source")
    return payload


def declared_structured_knowledge_source_ids(flow: dict[str, Any], operational: dict[str, Any]) -> list[str]:
    """Return rule sources that an accepted stage declares as executable knowledge adjudication."""

    return sorted(declared_structured_knowledge_requirements(flow, operational))


def declared_structured_knowledge_requirements(
    flow: dict[str, Any], operational: dict[str, Any],
) -> dict[str, list[str]]:
    """Map each declared structured rule source to its required stage outputs."""

    requirements: dict[str, set[str]] = defaultdict(set)
    declared_rule_ids = set(unique_strings(operational.get("rule_source_ids")))
    rule_node_ids = {
        str(role.get("node_id", ""))
        for source in operational.get("sources", []) if isinstance(source, dict)
        and str(source.get("source_id", "")) in declared_rule_ids
        for role in source.get("roles", []) if isinstance(role, dict)
        and str(role.get("node_type", "")).casefold() in {"rule", "policy", "control"}
        and str(role.get("node_id", ""))
    }
    for stage in flow.get("stages", []) if isinstance(flow.get("stages"), list) else []:
        if not isinstance(stage, dict):
            continue
        input_node_ids = set(unique_strings(stage.get("input_node_ids")))
        is_decision_stage = str(stage.get("stage_type", "")) == "decision"
        if not is_decision_stage and not input_node_ids.intersection(rule_node_ids):
            continue
        execution = stage_execution_contract(stage, operational)
        output_node_ids = unique_strings(stage.get("output_node_ids"))
        for source_id in unique_strings(execution.get("structured_rule_source_ids")):
            requirements[source_id].update(output_node_ids)
    return {source_id: sorted(output_ids) for source_id, output_ids in sorted(requirements.items())}


def approved_trace_bundle_ids(flow: dict[str, Any]) -> tuple[set[str], str]:
    """Resolve the accepted trace bundle that a replay assertion is allowed to cite."""

    source = flow.get("source") if isinstance(flow.get("source"), dict) else {}
    micro_claim = source.get("micro_process") if isinstance(source.get("micro_process"), dict) else {}
    try:
        micro_path = Path(str(micro_claim.get("artifact", ""))).resolve()
        micro = load_json(micro_path, MAX_OPERATIONAL_BYTES)
        micro_source = micro.get("source") if isinstance(micro.get("source"), dict) else {}
        review_path = Path(str(micro_source.get("trace_review", ""))).resolve()
        review = load_json(review_path, MAX_OPERATIONAL_BYTES)
    except (OSError, ContractError) as exc:
        return set(), f"accepted trace review cannot be read: {exc}"
    receipt_errors = platform_evidence_receipt_errors(micro_path.parent)
    if receipt_errors:
        return set(), "accepted trace approval receipt is invalid: " + "; ".join(receipt_errors)
    if micro_claim.get("fingerprint") != sha256_file(micro_path):
        return set(), "accepted micro-process fingerprint changed after flow approval"
    if micro_source.get("trace_review_fingerprint") != sha256_file(review_path):
        return set(), "accepted trace review fingerprint changed after micro-process approval"
    if micro.get("status") != "approved" or review.get("kind") != "trace_review" or review.get("status") != "approved":
        return set(), "accepted trace review is not approved"
    trace = review.get("trace") if isinstance(review.get("trace"), dict) else {}
    bundle_id = str(trace.get("bundle_id", "")).strip()
    if not bundle_id:
        return set(), "accepted trace review does not declare a trace bundle id"
    return {bundle_id}, ""


def approved_recipe_replay_context(flow: dict[str, Any]) -> tuple[dict[str, Any], str]:
    """Resolve the signed historical-evidence chain used by a recipe replay.

    The replay runner intentionally never reads the platform HMAC key or
    serializes the private fixture.  Promotion of its safe, hash-only report
    is therefore a generator responsibility: this function re-checks the
    accepted trace/micro-process chain and returns only the fingerprints that
    a platform receipt must bind.
    """

    source = flow.get("source") if isinstance(flow.get("source"), dict) else {}
    micro_claim = source.get("micro_process") if isinstance(source.get("micro_process"), dict) else {}
    try:
        micro_path = Path(str(micro_claim.get("artifact", ""))).resolve()
        micro = load_json(micro_path, MAX_OPERATIONAL_BYTES)
        review_source = micro.get("source") if isinstance(micro.get("source"), dict) else {}
        review_path = Path(str(review_source.get("trace_review", ""))).resolve()
        review = load_json(review_path, MAX_OPERATIONAL_BYTES)
        trace = review.get("trace") if isinstance(review.get("trace"), dict) else {}
        trace_path = Path(str(trace.get("artifact", ""))).resolve()
    except (OSError, ContractError) as exc:
        return {}, f"accepted recipe replay evidence cannot be read: {exc}"

    if not trace_path.is_file():
        return {}, "accepted trace review does not reference a readable trace-samples artifact"
    receipt_errors = platform_evidence_receipt_errors(micro_path.parent)
    if receipt_errors:
        return {}, "accepted trace approval receipt is invalid: " + "; ".join(receipt_errors)
    review_fingerprint = sha256_file(review_path)
    micro_fingerprint = sha256_file(micro_path)
    trace_fingerprint = sha256_file(trace_path)
    if micro_claim.get("fingerprint") != micro_fingerprint:
        return {}, "accepted micro-process fingerprint changed after flow approval"
    if review_source.get("trace_review_fingerprint") != review_fingerprint:
        return {}, "accepted trace review fingerprint changed after micro-process approval"
    if str(trace.get("fingerprint", "")) != trace_fingerprint:
        return {}, "accepted trace review does not bind the current trace-samples fingerprint"
    approval = review.get("approval") if isinstance(review.get("approval"), dict) else {}
    bundle_id = str(trace.get("bundle_id", "")).strip()
    if (
        review.get("schema_version") != SCHEMA_VERSION
        or review.get("kind") != "trace_review"
        or review.get("status") != "approved"
        or approval.get("decision") != "approved"
        or not bundle_id
    ):
        return {}, "accepted trace review is not an approved replay authority"
    return {
        "relation_root": micro_path.parent,
        "trace_fingerprint": trace_fingerprint,
        "trace_review_fingerprint": review_fingerprint,
        "trace_bundle_ids": {bundle_id},
    }, ""


def recipe_replay_fixture_receipt_errors(
    context: dict[str, Any], fixture_fingerprint: str, bindings: dict[str, str],
) -> list[str]:
    """Require a platform signature for the private replay oracle fixture.

    The fixture contains the historical request and approved result digest, so
    it is deliberately not copied into the package.  A signed receipt gives
    the public report a verifiable authority without disclosing that body.
    Bind the receipt to every executable byte/contract that the runner used so
    a prior replay cannot be reused after a recipe or runtime changes.
    """

    key = os.environ.get(PLATFORM_APPROVAL_KEY_ENV, "")
    if not key:
        return [
            f"Platform approval verifier is unavailable: {PLATFORM_APPROVAL_KEY_ENV} is not configured. "
            "Recipe replay promotion is fail-closed."
        ]
    relation_root = context.get("relation_root")
    if not isinstance(relation_root, Path):
        return ["Recipe replay approval context has no relation-root ledger"]
    ledger_path = platform_approvals_path(relation_root)
    try:
        ledger = load_json(ledger_path, MAX_PLATFORM_APPROVAL_BYTES)
    except ContractError as exc:
        return [f"Recipe replay approval ledger cannot be read: {exc}"]
    if (
        ledger.get("schema_version") != SCHEMA_VERSION
        or ledger.get("kind") != "platform_approval_envelopes"
        or ledger.get("issuer") != PLATFORM_APPROVAL_ISSUER
    ):
        return ["Recipe replay approval ledger has an unsupported issuer or schema"]
    approvals = ledger.get("approvals")
    if not isinstance(approvals, list):
        return ["Recipe replay approval ledger must contain an approvals array"]

    required = {
        "artifact_fingerprint": fixture_fingerprint,
        "trace_fingerprint": str(context.get("trace_fingerprint", "")),
        "trace_review_fingerprint": str(context.get("trace_review_fingerprint", "")),
        "recipe_catalog_fingerprint": bindings["recipe_catalog_fingerprint"],
        "replay_contract_fingerprint": bindings["replay_contract_fingerprint"],
        "executor_fingerprint": bindings["executor_fingerprint"],
        "executor_closure_fingerprint": bindings["executor_closure_fingerprint"],
        "runtime_contract_fingerprint": bindings["runtime_contract_fingerprint"],
        "flow_contract_fingerprint": bindings["flow_contract_fingerprint"],
    }
    matching_receipts = 0
    invalid_receipts: list[str] = []
    for index, envelope in enumerate(approvals):
        if not isinstance(envelope, dict) or envelope.get("artifact_kind") != "recipe_replay_cases":
            continue
        if str(envelope.get("artifact_fingerprint", "")).strip() != fixture_fingerprint:
            continue
        matching_receipts += 1
        mismatched = [
            field for field, expected in required.items()
            if str(envelope.get(field, "")).strip() != expected
        ]
        missing = [
            field for field in ("approval_id", "subject", "issued_at", "signature")
            if not str(envelope.get(field, "")).strip()
        ]
        if (
            envelope.get("schema_version") != SCHEMA_VERSION
            or envelope.get("issuer") != PLATFORM_APPROVAL_ISSUER
            or envelope.get("decision") != "approved"
            or mismatched
            or missing
        ):
            detail = ", ".join([*mismatched, *[f"missing {field}" for field in missing]])
            invalid_receipts.append(f"receipt #{index} has invalid bindings ({detail or 'contract'})")
            continue
        signature = str(envelope.get("signature", "")).strip().casefold()
        expected_signature = platform_approval_signature(envelope, key).casefold()
        if not hmac.compare_digest(signature, expected_signature):
            invalid_receipts.append(f"receipt #{index} has an invalid platform signature")
            continue
        return []
    if invalid_receipts:
        return ["Recipe replay oracle receipt is malformed or stale: " + "; ".join(invalid_receipts)]
    if matching_receipts:
        return ["No valid platform-signed recipe_replay_cases receipt matches the current replay bindings."]
    return ["No platform-signed recipe_replay_cases receipt is present for the private replay fixture."]


def recipe_replay_report_receipt_errors(
    context: dict[str, Any],
    report_fingerprint: str,
    fixture_fingerprint: str,
    bindings: dict[str, str],
    verified_recipe_ids: Sequence[str],
) -> list[str]:
    """Require post-execution approval of the hash-only replay report itself.

    A fixture receipt authorizes the private oracle, but it does not prove a
    particular public report came from the runner.  The platform signs this
    second receipt after inspecting the report's hash and its complete binding
    set.  Both receipts are mandatory before a package may execute a recipe
    deterministically.
    """

    key = os.environ.get(PLATFORM_APPROVAL_KEY_ENV, "")
    if not key:
        return [
            f"Platform approval verifier is unavailable: {PLATFORM_APPROVAL_KEY_ENV} is not configured. "
            "Recipe replay report promotion is fail-closed."
        ]
    relation_root = context.get("relation_root")
    if not isinstance(relation_root, Path):
        return ["Recipe replay report approval context has no relation-root ledger"]
    try:
        ledger = load_json(platform_approvals_path(relation_root), MAX_PLATFORM_APPROVAL_BYTES)
    except ContractError as exc:
        return [f"Recipe replay report approval ledger cannot be read: {exc}"]
    if (
        ledger.get("schema_version") != SCHEMA_VERSION
        or ledger.get("kind") != "platform_approval_envelopes"
        or ledger.get("issuer") != PLATFORM_APPROVAL_ISSUER
    ):
        return ["Recipe replay report approval ledger has an unsupported issuer or schema"]
    approvals = ledger.get("approvals")
    if not isinstance(approvals, list):
        return ["Recipe replay report approval ledger must contain an approvals array"]
    required = {
        "artifact_fingerprint": report_fingerprint,
        "fixture_fingerprint": fixture_fingerprint,
        "trace_fingerprint": str(context.get("trace_fingerprint", "")),
        "trace_review_fingerprint": str(context.get("trace_review_fingerprint", "")),
        "recipe_catalog_fingerprint": bindings["recipe_catalog_fingerprint"],
        "replay_contract_fingerprint": bindings["replay_contract_fingerprint"],
        "executor_fingerprint": bindings["executor_fingerprint"],
        "executor_closure_fingerprint": bindings["executor_closure_fingerprint"],
        "runtime_contract_fingerprint": bindings["runtime_contract_fingerprint"],
        "flow_contract_fingerprint": bindings["flow_contract_fingerprint"],
    }
    wanted_ids = sorted(unique_strings(list(verified_recipe_ids)))
    matching_receipts = 0
    invalid_receipts: list[str] = []
    for index, envelope in enumerate(approvals):
        if not isinstance(envelope, dict) or envelope.get("artifact_kind") != "recipe_replay_report":
            continue
        if str(envelope.get("artifact_fingerprint", "")).strip() != report_fingerprint:
            continue
        matching_receipts += 1
        mismatched = [
            field for field, expected in required.items()
            if str(envelope.get(field, "")).strip() != expected
        ]
        receipt_ids = unique_strings(envelope.get("verified_recipe_ids"))
        if receipt_ids != wanted_ids:
            mismatched.append("verified_recipe_ids")
        missing = [
            field for field in ("approval_id", "subject", "issued_at", "signature")
            if not str(envelope.get(field, "")).strip()
        ]
        if (
            envelope.get("schema_version") != SCHEMA_VERSION
            or envelope.get("issuer") != PLATFORM_APPROVAL_ISSUER
            or envelope.get("decision") != "approved"
            or mismatched
            or missing
        ):
            detail = ", ".join([*mismatched, *[f"missing {field}" for field in missing]])
            invalid_receipts.append(f"receipt #{index} has invalid bindings ({detail or 'contract'})")
            continue
        signature = str(envelope.get("signature", "")).strip().casefold()
        expected_signature = platform_approval_signature(envelope, key).casefold()
        if not hmac.compare_digest(signature, expected_signature):
            invalid_receipts.append(f"receipt #{index} has an invalid platform signature")
            continue
        return []
    if invalid_receipts:
        return ["Recipe replay report receipt is malformed or stale: " + "; ".join(invalid_receipts)]
    if matching_receipts:
        return ["No valid platform-signed recipe_replay_report receipt matches the current replay bindings."]
    return ["No platform-signed recipe_replay_report receipt is present for the current replay report."]


def recipe_coverage_status(
    flow: dict[str, Any], operational: dict[str, Any], recipe_catalog: dict[str, Any],
) -> dict[str, Any]:
    """Prove that every declared structured adjudication has a reviewed family recipe.

    A recipe selected by one historical rule value is deliberately insufficient:
    it cannot prove generalization to the next batch.  Only a value-free
    ``grouped_cooccurrence_from_rule_text`` family recipe can cover a declared
    rule source for publication.
    """

    requirements = declared_structured_knowledge_requirements(flow, operational)
    declared = sorted(requirements)
    recipes = recipe_catalog.get("recipes") if isinstance(recipe_catalog.get("recipes"), list) else []
    covering_recipes = [
        recipe for recipe in recipes
        if isinstance(recipe, dict)
        and recipe.get("kind") == "grouped_cooccurrence_from_rule_text"
        and isinstance(recipe.get("rule_selector"), dict)
        and str(recipe["rule_selector"].get("mode", "")) == "any_complete_rule"
        and str(recipe["rule_selector"].get("source_id", "")) in set(declared)
    ]
    covered = sorted({
        str(recipe["rule_selector"].get("source_id", ""))
        for recipe in covering_recipes
    })
    uncovered = sorted(set(declared) - set(covered))
    if not declared:
        return {
            "status": "not_required",
            "verifiable": True,
            "publishable": True,
            "declared_knowledge_source_ids": [],
            "covered_source_ids": [],
            "uncovered_source_ids": [],
            "recipe_count": len(recipes),
            "policy": "No structured knowledge adjudication is declared by an accepted stage.",
        }
    trace_bundle_ids, trace_error = approved_trace_bundle_ids(flow)
    output_coverage: dict[str, set[str]] = defaultdict(set)
    replay_coverage: dict[str, set[str]] = defaultdict(set)
    replay_assertion_ids: dict[str, list[str]] = defaultdict(list)
    for recipe in covering_recipes:
        selector = recipe.get("rule_selector") if isinstance(recipe.get("rule_selector"), dict) else {}
        source_id = str(selector.get("source_id", ""))
        output_coverage[source_id].update(unique_strings(recipe.get("covered_output_node_ids")))
        for assertion in recipe.get("historical_replay_assertions", []) if isinstance(recipe.get("historical_replay_assertions"), list) else []:
            if not isinstance(assertion, dict):
                continue
            assertion_id = str(assertion.get("id", "")).strip()
            evidence_ref = str(assertion.get("evidence_ref", "")).strip()
            bundle_id = str(assertion.get("trace_bundle_id", "")).strip()
            passed = normalized_question_value(assertion.get("status")) in {"passed", "approved", "通过"}
            if assertion_id and evidence_ref and passed and bundle_id in trace_bundle_ids:
                replay_assertion_ids[source_id].append(assertion_id)
                replay_coverage[source_id].update(unique_strings(assertion.get("output_node_ids")))
    missing_outputs = {
        source_id: sorted(set(required_outputs) - output_coverage[source_id])
        for source_id, required_outputs in requirements.items()
        if set(required_outputs) - output_coverage[source_id]
    }
    missing_replays = {
        source_id: sorted(set(required_outputs) - replay_coverage[source_id])
        for source_id, required_outputs in requirements.items()
        if set(required_outputs) - replay_coverage[source_id]
    }
    reasons: list[str] = []
    if uncovered:
        reasons.append("Empty, exact-value, or partial recipes cannot prove coverage of declared knowledge adjudication.")
    if missing_outputs:
        reasons.append("Executable recipes do not cover every required output node of the governed stage.")
    if trace_error:
        reasons.append(trace_error)
    if missing_replays:
        reasons.append("Historical replay assertions are missing, failed, or do not cover every required output node.")
    if reasons:
        return {
            "status": "unverified",
            "verifiable": False,
            "publishable": False,
            "declared_knowledge_source_ids": declared,
            "covered_source_ids": covered,
            "uncovered_source_ids": uncovered,
            "recipe_count": len(recipes),
            "required_recipe_kind": "grouped_cooccurrence_from_rule_text",
            "required_selector_mode": "any_complete_rule",
            "required_output_node_ids_by_source": requirements,
            "covered_output_node_ids_by_source": {
                source_id: sorted(output_coverage[source_id]) for source_id in declared
            },
            "missing_output_node_ids_by_source": missing_outputs,
            "accepted_trace_bundle_ids": sorted(trace_bundle_ids),
            "historical_replay_assertion_ids_by_source": {
                source_id: sorted(replay_assertion_ids[source_id]) for source_id in declared
            },
            "missing_replay_output_node_ids_by_source": missing_replays,
            "reason": " ".join(reasons),
        }
    return {
        "status": "verified",
        "verifiable": True,
        "publishable": True,
        "declared_knowledge_source_ids": declared,
        "covered_source_ids": covered,
        "uncovered_source_ids": [],
        "recipe_count": len(recipes),
        "required_recipe_kind": "grouped_cooccurrence_from_rule_text",
        "required_selector_mode": "any_complete_rule",
        "required_output_node_ids_by_source": requirements,
        "covered_output_node_ids_by_source": {
            source_id: sorted(output_coverage[source_id]) for source_id in declared
        },
        "accepted_trace_bundle_ids": sorted(trace_bundle_ids),
        "historical_replay_assertion_ids_by_source": {
            source_id: sorted(replay_assertion_ids[source_id]) for source_id in declared
        },
    }


def recipe_replay_contract_template(
    output_root: Path, flow: dict[str, Any], operational: dict[str, Any],
) -> dict[str, Any]:
    """Describe, but do not fabricate, the evidence a later replay runner must write."""

    try:
        catalog = compiled_recipe_catalog(output_root, operational)
    except ContractError as exc:
        return {
            "schema_version": 1,
            "kind": "compiled_recipe_replay_report",
            "status": "blocked_invalid_recipe_catalog",
            "error": str(exc),
        }
    recipes = [item for item in catalog.get("recipes", []) if isinstance(item, dict)]
    recipe_ids = [str(item.get("id", "")) for item in recipes if str(item.get("id", ""))]
    source = flow.get("source") if isinstance(flow.get("source"), dict) else {}
    micro = source.get("micro_process") if isinstance(source.get("micro_process"), dict) else {}
    approved_trace_bundle_ids_, trace_error = approved_trace_bundle_ids(flow)
    declared_runtime_source_ids = set(unique_strings(operational.get("runtime_source_ids")))
    required_cases: list[dict[str, Any]] = []
    for recipe in recipes:
        recipe_id = str(recipe.get("id", "")).strip()
        if not recipe_id:
            continue
        selector = recipe.get("rule_selector") if isinstance(recipe.get("rule_selector"), dict) else {}
        recipe_runtime_source_ids = unique_strings([
            str(recipe.get("source_id", "")),
            str(selector.get("source_id", "")),
            *unique_strings(recipe.get("context_source_ids")),
        ])
        recipe_runtime_source_ids = [
            source_id for source_id in recipe_runtime_source_ids
            if source_id in declared_runtime_source_ids
        ]
        assertions = recipe.get("historical_replay_assertions")
        if not isinstance(assertions, list) or not assertions:
            # A non-family recipe may not have historical assertions.  Keep an
            # explicit unresolved case instead of silently emitting an empty
            # replay contract that could be mistaken for a completed review.
            required_cases.append({
                "case_id": f"{recipe_id}:missing-approved-trace-case",
                "recipe_id": recipe_id,
                "status": "blocked_missing_approved_trace_case",
                "trace_bundle_id": "",
                "runtime_source_ids": recipe_runtime_source_ids,
                "required_output_node_ids": unique_strings(recipe.get("covered_output_node_ids")),
                "reason": "A compiled recipe needs an approved trace case before real replay can begin.",
            })
            continue
        for assertion in assertions:
            if not isinstance(assertion, dict):
                continue
            assertion_id = str(assertion.get("id", "")).strip()
            bundle_id = str(assertion.get("trace_bundle_id", "")).strip()
            required_cases.append({
                "case_id": f"{recipe_id}:{assertion_id or 'approved-trace'}",
                "recipe_id": recipe_id,
                "assertion_id": assertion_id,
                "trace_bundle_id": bundle_id,
                "approved_assertion_ref": str(assertion.get("evidence_ref", "")).strip(),
                "runtime_source_ids": recipe_runtime_source_ids,
                "required_output_node_ids": unique_strings(assertion.get("output_node_ids")),
                "expected_oracle": "Derive the normalized result digest and result-anchor count from the approved trace artifact; never treat this assertion string as the oracle.",
            })
    return {
        "schema_version": 1,
        "kind": "compiled_recipe_replay_report",
        "status": "pending_real_replay",
        "recipe_catalog_fingerprint": (
            sha256_file(output_root / "compiled-recipes.json")
            if (output_root / "compiled-recipes.json").is_file() else ""
        ),
        "approved_trace": {
            "artifact": micro.get("artifact", ""),
            "fingerprint": micro.get("fingerprint", ""),
            "status": micro.get("status", ""),
            "bundle_ids": sorted(approved_trace_bundle_ids_),
            "validation_error": trace_error,
        },
        "recipe_ids": recipe_ids,
        "required_cases": required_cases,
        "required_evidence": [
            "one persisted execution artifact per recipe/approved trace case",
            "expected and actual normalized result digest with an explicit comparison status",
            "expected and actual result-anchor/row count; zero-row replay is a failure unless the approved oracle is explicitly empty",
            "runtime source fingerprints, executor dependency-closure fingerprint, and a command/parameter digest without secrets",
            "a platform-signed recipe_replay_cases receipt for the private oracle fixture and a platform-signed recipe_replay_report receipt for the hash-only result report",
        ],
        "private_oracle_contract": {
            "kind": "compiled_recipe_replay_cases",
            "distribution": "private_only_never_copy_to_capability_package",
            "required_receipts": ["recipe_replay_cases", "recipe_replay_report"],
            "report_runner": "scripts/recipe_replay_runner.py",
        },
        "failure_policy": "A recipe assertion or approval string is not a replay. Until a real replay report is supplied and independently validated, the capability is unverified and non-publishable.",
    }


def refresh_recipe_replay_contract(
    output_root: Path, flow: dict[str, Any], operational: dict[str, Any],
) -> dict[str, Any]:
    """Regenerate replay requirements whenever the reviewed recipe catalog changes.

    Recipes are authored after ``prepare`` in many normal workflows.  Refresh
    at every validation/generation boundary so the replay contract cannot stay
    empty or keep an obsolete recipe fingerprint after a recipe is added.
    """

    contract = recipe_replay_contract_template(output_root, flow, operational)
    atomic_json(output_root / "recipe-replay-contract.json", contract)
    return contract


def recipe_executor_closure_fingerprint(executor_path: Path) -> str:
    """Hash the package-local executor dependency closure used by a replay."""

    executor_path = executor_path.resolve()
    scripts_root = executor_path.parent
    executor_root = scripts_root.parent
    required = [executor_path, scripts_root / "recipe_runtime.py"]
    optional = [scripts_root / "query_tabular.py", scripts_root / "extract_documents.py"]
    if any(not path.is_file() for path in required):
        raise ContractError("Recipe replay executor is missing execute_scenario.py or recipe_runtime.py")
    files = [
        path for path in [*required, *optional, executor_root / "requirements.txt"] if path.is_file()
    ]
    digest = hashlib.sha256()
    for path in sorted(files, key=lambda item: item.relative_to(executor_root).as_posix()):
        digest.update(path.relative_to(executor_root).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def recipe_replay_runtime_digests(
    runtime_artifacts: dict[str, Path] | None,
) -> tuple[dict[str, str], list[str]]:
    """Hash the exact executable artifacts that a replay report must bind."""

    required = {
        "catalog": "recipe_catalog_fingerprint",
        "executor": "executor_fingerprint",
        "runtime_contract": "runtime_contract_fingerprint",
        "flow_contract": "flow_contract_fingerprint",
    }
    if not isinstance(runtime_artifacts, dict):
        return {}, ["no exact replay runtime artifacts were supplied"]
    digests: dict[str, str] = {}
    errors: list[str] = []
    for name, output_name in required.items():
        raw_path = runtime_artifacts.get(name)
        try:
            path = Path(raw_path).resolve() if raw_path is not None else None
        except (OSError, TypeError):
            path = None
        if path is None or not path.is_file():
            errors.append(f"missing replay runtime artifact: {name}")
            continue
        digests[output_name] = sha256_file(path)
    if not errors:
        try:
            digests["executor_closure_fingerprint"] = recipe_executor_closure_fingerprint(
                Path(runtime_artifacts["executor"]),
            )
        except (ContractError, OSError, TypeError) as exc:
            errors.append(f"cannot fingerprint replay executor closure: {exc}")
    return digests, errors


def recipe_required_runtime_source_ids(
    recipe: dict[str, Any], operational: dict[str, Any],
) -> set[str]:
    """Return every declared source the executor can read for a replay.

    This mirrors the replay worker rather than merely listing the recipe's
    final data source.  Rule selection opens every tabular rule source, and a
    hybrid request indexes every non-tabular runtime source before the recipe
    path begins.  Their fingerprints are therefore part of the proof.
    """

    selector = recipe.get("rule_selector") if isinstance(recipe.get("rule_selector"), dict) else {}
    values = {
        str(recipe.get("source_id", "")).strip(),
        str(selector.get("source_id", "")).strip(),
        *unique_strings(recipe.get("context_source_ids")),
    }
    rule_source_ids = set(unique_strings(operational.get("rule_source_ids")))
    for source in operational.get("sources", []) if isinstance(operational.get("sources"), list) else []:
        if not isinstance(source, dict):
            continue
        source_id = str(source.get("source_id", "")).strip()
        kind = str(source.get("kind", "")).casefold()
        if source_id in rule_source_ids and kind == "tabular":
            values.add(source_id)
        if source.get("runtime_required") is True and kind != "tabular":
            values.add(source_id)
    values.discard("")
    return values


def compiled_recipe_replay_status(
    output_root: Path,
    catalog: dict[str, Any],
    flow: dict[str, Any] | None = None,
    runtime_artifacts: dict[str, Path] | None = None,
) -> dict[str, Any]:
    """Validate a real, signed replay report before allowing deterministic output.

    A report is only proof when it is produced by the replay runner against
    the same catalog, executor and two runtime contracts that will be shipped.
    The report contains hashes rather than historical request/result bodies;
    a platform-signed receipt authorizes the private oracle fixture that those
    hashes were compared against.
    """

    recipes = [item for item in catalog.get("recipes", []) if isinstance(item, dict)]
    if not recipes:
        return {
            "status": "not_required",
            "passed": True,
            "verified_recipe_ids": [],
            "reason": "No compiled deterministic recipe is declared.",
        }
    path = output_root / "recipe-replay-report.json"
    if not path.is_file():
        return {
            "status": "missing_real_replay_report",
            "passed": False,
            "verified_recipe_ids": [],
            "required_report": "recipe-replay-report.json",
            "template": "recipe-replay-contract.json",
            "reason": "Compiled recipes have no real replay report; approved assertion strings are not replay evidence.",
        }
    try:
        report_fingerprint, report = load_json_with_digest(path, MAX_CANDIDATE_BYTES)
    except ContractError as exc:
        return {
            "status": "invalid_real_replay_report",
            "passed": False,
            "verified_recipe_ids": [],
            "reason": str(exc),
        }

    # Keep a lightweight, fail-closed status for preflight callers that have
    # not yet materialized the canonical replay runtime.  A later call from
    # the preview/final bundle supplies those paths and performs the strict
    # validation below.
    source_catalog = output_root / "compiled-recipes.json"
    basic_shape = (
        report.get("schema_version") == 1
        and report.get("kind") == "compiled_recipe_replay_report"
        and report.get("status") == "passed"
        and source_catalog.is_file()
        and report.get("recipe_catalog_fingerprint") == sha256_file(source_catalog)
        and isinstance(report.get("replays"), list)
        and bool(report.get("replays"))
    )
    if not isinstance(flow, dict) or runtime_artifacts is None:
        return {
            "status": "reported_pending_runner_validation" if basic_shape else "invalid_real_replay_report",
            "passed": False,
            "verified_recipe_ids": [],
            "report": "recipe-replay-report.json",
            "reason": (
                "A report is present but still needs validation against the exact generated replay runtime and the signed private oracle receipt."
                if basic_shape
                else "Replay report is missing the current recipe fingerprint, passed status, or replay records."
            ),
        }

    errors: list[str] = []
    replay_contract_path = output_root / "recipe-replay-contract.json"
    try:
        replay_contract_fingerprint, replay_contract = load_json_with_digest(
            replay_contract_path, MAX_CANDIDATE_BYTES,
        )
    except ContractError as exc:
        return {
            "status": "invalid_real_replay_report",
            "passed": False,
            "verified_recipe_ids": [],
            "reason": f"Current replay contract cannot be read: {exc}",
        }
    runtime_digests, runtime_errors = recipe_replay_runtime_digests(runtime_artifacts)
    if runtime_errors:
        return {
            "status": "invalid_real_replay_runtime",
            "passed": False,
            "verified_recipe_ids": [],
            "reason": "; ".join(runtime_errors),
        }
    try:
        runtime_contract_digest, replay_runtime_contract = load_json_with_digest(
            Path(runtime_artifacts["runtime_contract"]), MAX_OPERATIONAL_BYTES,
        )
    except (ContractError, OSError, TypeError) as exc:
        return {
            "status": "invalid_real_replay_runtime",
            "passed": False,
            "verified_recipe_ids": [],
            "reason": f"Replay runtime contract cannot be read: {exc}",
        }
    if runtime_contract_digest != runtime_digests["runtime_contract_fingerprint"]:
        return {
            "status": "invalid_real_replay_runtime",
            "passed": False,
            "verified_recipe_ids": [],
            "reason": "Replay runtime contract changed while it was being read.",
        }
    if replay_contract.get("recipe_catalog_fingerprint") != runtime_digests["recipe_catalog_fingerprint"]:
        errors.append("current replay contract is not bound to the exact generated recipe catalog")
    expected_top_level = {
        "recipe_catalog_fingerprint": runtime_digests["recipe_catalog_fingerprint"],
        "replay_contract_fingerprint": replay_contract_fingerprint,
    }
    for field, expected in expected_top_level.items():
        if report.get(field) != expected:
            errors.append(f"replay report {field} does not match the current artifact")
    if report.get("schema_version") != 1 or report.get("kind") != "compiled_recipe_replay_report":
        errors.append("replay report has an unsupported schema or kind")
    if report.get("status") != "passed":
        errors.append("replay report status is not passed")

    executor = report.get("executor") if isinstance(report.get("executor"), dict) else {}
    if (
        executor.get("sha256") != runtime_digests["executor_fingerprint"]
        or executor.get("closure_sha256") != runtime_digests["executor_closure_fingerprint"]
        or executor.get("interface") != "execute_scenario.execute"
    ):
        errors.append("replay report executor does not match the generated executor")
    for field, report_name in (
        ("runtime_contract_fingerprint", "runtime_contract"),
        ("flow_contract_fingerprint", "flow_contract"),
    ):
        bound = report.get(report_name) if isinstance(report.get(report_name), dict) else {}
        if bound.get("sha256") != runtime_digests[field]:
            errors.append(f"replay report {report_name} does not match the generated contract")

    context, context_error = approved_recipe_replay_context(flow)
    if context_error:
        errors.append(context_error)
    trace_review = report.get("trace_review") if isinstance(report.get("trace_review"), dict) else {}
    if context:
        reported_bundle_ids = {
            str(item) for item in trace_review.get("trace_bundle_ids", []) if str(item)
        } if isinstance(trace_review.get("trace_bundle_ids"), list) else set()
        if (
            trace_review.get("fingerprint") != context["trace_review_fingerprint"]
            or trace_review.get("status") != "approved"
            or reported_bundle_ids != context["trace_bundle_ids"]
        ):
            errors.append("replay report trace-review binding is stale or incomplete")

    fixtures = report.get("fixtures") if isinstance(report.get("fixtures"), dict) else {}
    fixture_fingerprint = str(fixtures.get("sha256", "")).strip().casefold()
    if not is_sha256_digest(fixture_fingerprint):
        errors.append("replay report fixture fingerprint is invalid")
    case_count = fixtures.get("case_count")
    if isinstance(case_count, bool) or not isinstance(case_count, int) or case_count < 1:
        errors.append("replay report fixture case_count is invalid")

    required_cases = replay_contract.get("required_cases")
    if not isinstance(required_cases, list) or not required_cases:
        errors.append("current replay contract has no required cases")
        required_by_id: dict[str, dict[str, Any]] = {}
    else:
        required_by_id = {}
        for required in required_cases:
            if not isinstance(required, dict):
                errors.append("current replay contract contains an invalid required case")
                continue
            case_id = str(required.get("case_id", "")).strip()
            recipe_id = str(required.get("recipe_id", "")).strip()
            trace_bundle_id = str(required.get("trace_bundle_id", "")).strip()
            if not case_id or not recipe_id or not trace_bundle_id or case_id in required_by_id:
                errors.append("current replay contract contains an incomplete or duplicate required case")
                continue
            required_by_id[case_id] = required

    recipes_by_id = {str(recipe.get("id", "")).strip(): recipe for recipe in recipes}
    replays = report.get("replays")
    replay_by_case: dict[str, dict[str, Any]] = {}
    if not isinstance(replays, list) or len(replays) != len(required_by_id):
        errors.append("replay report does not contain exactly one result for every required case")
    elif case_count != len(required_by_id):
        errors.append("replay report fixture case_count does not match the required case set")
    else:
        for replay in replays:
            if not isinstance(replay, dict):
                errors.append("replay report contains a non-object replay result")
                continue
            case_id = str(replay.get("case_id", "")).strip()
            if not case_id or case_id in replay_by_case:
                errors.append("replay report contains an empty or duplicate case_id")
                continue
            replay_by_case[case_id] = replay
        if set(replay_by_case) != set(required_by_id):
            errors.append("replay report case set does not match the current replay contract")

    passed_recipe_ids: set[str] = set()
    for case_id, required in required_by_id.items():
        replay = replay_by_case.get(case_id)
        if replay is None:
            continue
        identity = {
            "recipe_id": str(required.get("recipe_id", "")).strip(),
            "assertion_id": str(required.get("assertion_id", "")).strip(),
            "trace_bundle_id": str(required.get("trace_bundle_id", "")).strip(),
        }
        if any(str(replay.get(field, "")).strip() != expected for field, expected in identity.items()):
            errors.append(f"replay report identity does not match required case {case_id}")
            continue
        recipe_id = identity["recipe_id"]
        recipe = recipes_by_id.get(recipe_id)
        if recipe is None:
            errors.append(f"replay report refers to a recipe absent from the current catalog: {recipe_id}")
            continue
        expected = replay.get("expected") if isinstance(replay.get("expected"), dict) else {}
        actual = replay.get("actual") if isinstance(replay.get("actual"), dict) else {}
        comparison = replay.get("comparison") if isinstance(replay.get("comparison"), dict) else {}
        execution = replay.get("execution") if isinstance(replay.get("execution"), dict) else {}
        expected_count = expected.get("result_anchor_count")
        actual_count = actual.get("result_anchor_count")
        matched_group_count = actual.get("matched_group_count")
        if (
            not is_sha256_digest(expected.get("normalized_result_digest"))
            or not is_sha256_digest(actual.get("normalized_result_digest"))
            or expected.get("normalized_result_digest") != actual.get("normalized_result_digest")
            or isinstance(expected_count, bool) or not isinstance(expected_count, int) or expected_count < 0
            or isinstance(actual_count, bool) or not isinstance(actual_count, int) or actual_count < 0
            or isinstance(matched_group_count, bool) or not isinstance(matched_group_count, int) or matched_group_count < 0
            or not isinstance(expected.get("approved_empty_result"), bool)
            or expected.get("approved_empty_result") != (expected_count == 0)
            or actual.get("complete") is not True
            or actual_count != expected_count
            or comparison.get("status") != "passed"
            or comparison.get("digest_match") is not True
            or comparison.get("count_match") is not True
            or comparison.get("nonempty_policy_match") is not True
            or comparison.get("complete_match") is not True
            or execution.get("status") != "completed_deterministically"
            or execution.get("recipe_execution_status") != "verified_recipe"
            or str(replay.get("failure_code", "")).strip()
        ):
            errors.append(f"replay report result comparison did not pass for required case {case_id}")
            continue
        source_fingerprints = replay.get("source_fingerprints")
        source_ids: set[str] = set()
        if not isinstance(source_fingerprints, list):
            errors.append(f"replay report source fingerprints are missing for required case {case_id}")
            continue
        source_fingerprints_valid = True
        for source in source_fingerprints:
            if not isinstance(source, dict):
                source_fingerprints_valid = False
                break
            source_id = str(source.get("source_id", "")).strip()
            size_bytes = source.get("size_bytes")
            if (
                not source_id or source_id in source_ids
                or not is_sha256_digest(source.get("sha256"))
                or isinstance(size_bytes, bool) or not isinstance(size_bytes, int) or size_bytes < 0
            ):
                source_fingerprints_valid = False
                break
            source_ids.add(source_id)
        if not source_fingerprints_valid or source_ids != recipe_required_runtime_source_ids(recipe, replay_runtime_contract):
            errors.append(f"replay report source fingerprints do not cover the exact recipe sources for case {case_id}")
            continue
        passed_recipe_ids.add(recipe_id)

    expected_recipe_ids = {str(required.get("recipe_id", "")).strip() for required in required_by_id.values()}
    expected_recipe_ids.discard("")
    # A recipe with more than one approved case is verified only when all of
    # its cases passed; the preceding loop added an id for each pass, so compare
    # the report allow-list against the required case grouping explicitly.
    fully_passed_recipe_ids = {
        recipe_id for recipe_id in expected_recipe_ids
        if all(
            str(required.get("recipe_id", "")).strip() != recipe_id
            or (
                replay_by_case.get(case_id, {}).get("comparison", {}).get("status") == "passed"
                and str(replay_by_case.get(case_id, {}).get("recipe_id", "")).strip() == recipe_id
            )
            for case_id, required in required_by_id.items()
        )
    }
    reported_recipe_ids = unique_strings(report.get("verified_recipe_ids"))
    if (
        reported_recipe_ids != sorted(fully_passed_recipe_ids)
        or fully_passed_recipe_ids != expected_recipe_ids
        or passed_recipe_ids != expected_recipe_ids
    ):
        errors.append("replay report verified_recipe_ids are not exactly the recipes whose required cases all passed")
    failures = report.get("failures")
    if not isinstance(failures, list) or failures:
        errors.append("a passed replay report must have an empty failures list")

    if context and is_sha256_digest(fixture_fingerprint):
        receipt_bindings = {
            **runtime_digests,
            "replay_contract_fingerprint": replay_contract_fingerprint,
        }
        receipt_errors = recipe_replay_fixture_receipt_errors(
            context,
            fixture_fingerprint,
            receipt_bindings,
        )
        errors.extend(receipt_errors)
        if not errors:
            errors.extend(recipe_replay_report_receipt_errors(
                context,
                report_fingerprint,
                fixture_fingerprint,
                receipt_bindings,
                sorted(fully_passed_recipe_ids),
            ))

    if errors:
        return {
            "status": "invalid_real_replay_report",
            "passed": False,
            "verified_recipe_ids": [],
            "report": "recipe-replay-report.json",
            "reason": "; ".join(errors),
        }
    final_runtime_digests, final_runtime_errors = recipe_replay_runtime_digests(runtime_artifacts)
    if final_runtime_errors or final_runtime_digests != runtime_digests:
        return {
            "status": "invalid_real_replay_runtime",
            "passed": False,
            "verified_recipe_ids": [],
            "report": "recipe-replay-report.json",
            "reason": "Replay runtime artifacts changed while the report was being validated.",
        }
    if sha256_file(path) != report_fingerprint or sha256_file(replay_contract_path) != replay_contract_fingerprint:
        return {
            "status": "invalid_real_replay_report",
            "passed": False,
            "verified_recipe_ids": [],
            "report": "recipe-replay-report.json",
            "reason": "Replay report or replay contract changed while the report was being validated.",
        }
    return {
        "status": "passed_real_replay",
        "passed": True,
        "verified_recipe_ids": sorted(fully_passed_recipe_ids),
        "report": "recipe-replay-report.json",
        "reason": "Every approved replay case passed against the exact generated executor and signed private oracle fixture.",
    }


def compiled_recipe_verification(
    output_root: Path,
    flow: dict[str, Any],
    operational: dict[str, Any],
    runtime_artifacts: dict[str, Path] | None = None,
) -> dict[str, Any]:
    """Combine recipe coverage with the exact-runtime replay validation gate."""

    declared = declared_structured_knowledge_source_ids(flow, operational)
    try:
        catalog = compiled_recipe_catalog(output_root, operational)
    except ContractError as exc:
        return {
            "status": "unverified", "verifiable": False, "publishable": False,
            "declared_knowledge_source_ids": declared, "covered_source_ids": [],
            "uncovered_source_ids": declared, "recipe_count": 0,
            "reason": f"compiled-recipes.json is invalid: {exc}",
        }
    coverage = recipe_coverage_status(flow, operational, catalog)
    replay = compiled_recipe_replay_status(output_root, catalog, flow, runtime_artifacts)
    verified = coverage.get("publishable") is True and replay.get("passed") is True
    return {
        **coverage,
        "status": "verified" if verified else "unverified",
        "verifiable": verified,
        "publishable": verified,
        "verified_recipe_ids": (
            unique_strings(replay.get("verified_recipe_ids")) if verified else []
        ),
        "replay": replay,
        "reason": " ".join(str(value) for value in (coverage.get("reason"), replay.get("reason")) if str(value)),
    }


def portable_recipe_verification_contract(
    catalog_path: Path, catalog: dict[str, Any], verification: dict[str, Any],
) -> dict[str, Any]:
    """Bind runtime deterministic execution to independently verified recipes.

    A compiled recipe is executable code, not proof that it models a business
    rule correctly.  The primary executor therefore needs a small, portable
    policy document that it can inspect without trusting a prose prompt or a
    hand-authored replay assertion.  Until a real replay runner promotes the
    catalog, the explicit allow-list is empty and the executor returns bounded
    evidence for one Agent judgment instead of a false deterministic result.
    """

    recipes = [item for item in catalog.get("recipes", []) if isinstance(item, dict)]
    catalog_recipe_ids = {
        str(item.get("id", "")).strip() for item in recipes if str(item.get("id", "")).strip()
    }
    verified = verification.get("verifiable") is True and verification.get("publishable") is True
    verified_recipe_ids = [
        recipe_id for recipe_id in unique_strings(verification.get("verified_recipe_ids"))
        if recipe_id in catalog_recipe_ids
    ] if verified else []
    return {
        "schema_version": 1,
        "kind": "portable_recipe_runtime_verification",
        # The runtime intentionally fingerprints the exact package-local
        # bytes.  The reviewed source catalog may have different whitespace,
        # so bind this certificate only after the canonical copy has been
        # written beside execute_scenario.py.
        "recipe_catalog_fingerprint": sha256_file(catalog_path) if catalog_path.is_file() else "",
        "status": "verified" if verified else "unverified",
        "verifiable": verified,
        "publishable": verified,
        "verified_recipe_ids": verified_recipe_ids,
        "verification_summary": {
            "coverage_status": verification.get("status", "unverified"),
            "replay_status": (
                verification.get("replay", {}).get("status", "missing_real_replay_report")
                if isinstance(verification.get("replay"), dict) else "missing_real_replay_report"
            ),
            "verified_recipe_ids": verified_recipe_ids,
            "reason": verification.get("reason", ""),
        },
        "runtime_policy": {
            "allow_completed_deterministically_only_for_verified_recipe_ids": True,
            "unverified_recipe_action": "return_bounded_evidence_for_agent_judgment",
            "replay_contract": "recipe-replay-contract.json",
            "replay_report": "recipe-replay-report.json",
        },
    }


def portable_flow_contract(claims: dict[str, Any], flow: dict[str, Any], operational: dict[str, Any]) -> dict[str, Any]:
    """Keep only execution facts needed by the portable primary entrypoint."""

    flow_stages = {
        str(item.get("id", "")): item
        for item in flow.get("stages", [])
        if isinstance(item, dict) and str(item.get("id", ""))
    }
    stages = []
    knowledge_driven = False
    for item in claims.get("stage_skills", []):
        if not isinstance(item, dict):
            continue
        stage_id = str(item.get("stage_id", ""))
        source_stage = flow_stages.get(stage_id, {})
        execution = item.get("execution_contract") if isinstance(item.get("execution_contract"), dict) else {}
        if execution.get("structured_rule_source_ids") or execution.get("semantic_route_ids"):
            knowledge_driven = True
        stages.append({
            "stage_id": stage_id,
            "name": source_stage.get("name", item.get("display_name", stage_id)),
            "stage_type": source_stage.get("stage_type", ""),
            "objective": item.get("objective", source_stage.get("objective", "")),
            "outcome": item.get("outcome", source_stage.get("outcome", "")),
            "input_contract": item.get("input_contract", []),
            "output_contract": item.get("output_contract", []),
            "control_ids": item.get("control_ids", []),
            "predecessor_stage_ids": item.get("predecessor_stage_ids", []),
            "successor_stage_ids": item.get("successor_stage_ids", []),
            "open_question_ids": item.get("open_question_ids", []),
            "procedure": item.get("procedure", []),
            "execution_contract": execution,
        })
    contract = {
        "schema_version": 1,
        "scenario": claims.get("scenario", flow.get("scenario", {})),
        "execution_mode": "knowledge_engine" if knowledge_driven else "evidence_pipeline",
        "main_flow": [str(item) for item in claims.get("orchestrator", {}).get("main_flow", [])],
        "stages": stages,
        "controls": [item for item in flow.get("controls", []) if isinstance(item, dict)],
        "open_questions": [item for item in flow.get("open_questions", []) if isinstance(item, dict)],
        "execution_policy": flow.get("execution_policy", {}),
        "runtime_source_ids": operational.get("runtime_source_ids", []),
        "rule_source_ids": operational.get("rule_source_ids", []),
        "result_source_ids": operational.get("result_source_ids", []),
        "design_time_output_templates": operational.get("design_time_output_templates", []),
        "output_contract": operational.get("output_contract", {}),
        "contract_fingerprint": operational.get("source", {}).get("portable_copy_of_fingerprint", ""),
    }
    contract["execution_plan"] = build_execution_plan(contract, operational)
    contract["capability_model"] = contract["execution_plan"].get("capability_model", {})
    if contract["execution_plan"].get("mode") == "knowledge_engine":
        contract["execution_mode"] = "knowledge_engine"
    return contract


def render_executor_skill(
    claims: dict[str, Any], flow_contract: dict[str, Any], operational: dict[str, Any],
    executor_name: str,
) -> str:
    scenario = claims.get("scenario", {})
    runtime_ids = format_list(flow_contract.get("runtime_source_ids", []), "无运行时输入")
    rule_ids = format_list(flow_contract.get("rule_source_ids", []), "未声明规则/政策来源")
    stages = flow_contract.get("stages", [])
    stage_lines = [
        f"- `{stage.get('stage_id', '')}`：{stage.get('name', '')}；{stage.get('objective', '')}"
        for stage in stages if isinstance(stage, dict)
    ] or ["- 当前流程没有可执行阶段描述；必须根据返回的证据缺口停止并报告。"]
    source_lines = [
        f"- `{source.get('source_id', '')}` / `{source.get('view_name', '')}` / `{source.get('path', '')}`："
        f"生命周期 `{source.get('lifecycle', 'runtime_input')}`，运行时绑定 `{source.get('runtime_binding', 'required_when_referenced')}`"
        for source in operational.get("sources", [])
        if isinstance(source, dict) and source.get("runtime_required") is True
    ] or ["- 当前没有可绑定的运行时输入。"]
    return "\n".join([
        skill_frontmatter(
            executor_name,
            f"端到端执行“{scenario.get('name', '')}”业务场景：一次调用完成规则定位、运行时数据检索、关联校验、证据汇总和可追溯交付；适用于第三方 Agent 处理完整业务请求。",
        ),
        "",
        "## Agent handoff contract",
        "",
        "- The `agent_handoff` artifact is the Agent-facing source of truth. Its stdout is intentionally compact; read `agent_handoff` once and use the sibling full `artifact` only when audit detail is explicitly needed.",
        "- Consume `execution_steps` in order. Do not recreate the stage state machine, inspect generator code, or retry the same request after a successful artifact is written.",
        "- A completed deterministic result exposes `result_handle`. For a follow-up that filters or summarizes the same result, invoke only `continue --result <artifact> --filter <result-field>=<value>`; never call `execute`, `search-rules`, `query`, a shell data tool, or a raw source reader again.",
        "- Read `references/delivery-contract.json` for the request, evidence, terminal-status, and result fields contract. `references/capability-model.json` and `references/execution-plan.json` are audit metadata, not a call-by-call checklist for the Agent to recreate.",
        "- `references/compiled-recipes.json` contains reviewed declarative rule recipes. When `execute` reports `completed_deterministically`, its `deterministic_result` is the final business fact; report it directly and never reconstruct its SQL or run the rule again.",
        "- `references/dispatch-config.json` and `references/output-specs.json` are executable metadata, not examples. Preserve their rule id, dispatch value, source provenance, and output columns in the final result.",
        "- When status is `ready_for_agent_judgment`, use the governing record when present, the accepted flow, and `candidate_evidence` to make one business-evaluation pass, then fill every field in `result_contract`.",
        "- If `candidate_evidence.coverage.complete_for_all_matching_runtime_rows` is false, disclose that the evidence is a bounded preview and do not claim an exhaustive audit.",
        "- Use `query` only when `next_step.query_allowed_only_if` is satisfied. A query must name the missing field or relationship and must not restart rule discovery.",
        f"# {scenario.get('name', '')} 主执行器", "",
        f"这是“{scenario.get('name', '')}”能力包的唯一首选端到端入口。执行模式：`{flow_contract.get('execution_mode', 'evidence_pipeline')}`。",
        "主执行器先做机器可验证的规则、数据和证据准备，再把有限结果交给 Agent 应用完整规则；它不把原始大表加载进上下文，也不把语义不确定性伪装成确定结论。",
        "", "## 强制调用策略", "",
        "1. 用户请求覆盖整个业务场景时，只先调用本 Skill 的 `execute`；不要先逐个调用阶段 Skill，也不要手工启动状态机。",
        "2. `execute` 返回 `completed_deterministically` 时，直接使用 `deterministic_result` 交付；同一结果的追问只能复用 `result_handle` 执行 `continue` 投影。返回 `ready_for_agent_judgment` 时，读取 handoff 并仅基于其中完整规则/文档章节和有界证据完成一次业务判断，再填满 `result_contract`；不得重新找规则或临时 SQL 猜测。",
        "3. 返回 `blocked_rule_not_found`、`blocked_rule_selection_required` 或 `blocked_missing_or_incompatible_sources` 时，先说明证据缺口；`blocked_ocr_required` 时只调用已声明 OCR 能力一次、把 JSON 重新绑定到同一 source_id 后再开始一个恢复事务；不得循环重试同一请求或猜测规则。",
        "4. 只有主执行器明确返回可追踪的 SQL/关联缺口时，才使用 `query` 做一次有界补充；阶段 Skill 是降级/人工分步调试入口，不是正常端到端路径。",
        "", "## 固定入口", "",
        "~~~text",
        f"python \"<this-skill>/scripts/execute_scenario.py\" describe",
        f"python \"<this-skill>/scripts/execute_scenario.py\" execute --request \"<用户完整请求>\" --data-root \"<data-root>\" --output \"<evidence-package.json>\" --bind \"<source-id>=<relative-runtime-file>\"",
        f"python \"<this-skill>/scripts/execute_scenario.py\" continue --result \"<evidence-package.json>\" --filter \"<已交付结果字段>=<用户限定值>\"",
        f"python \"<this-skill>/scripts/execute_scenario.py\" query --data-root \"<data-root>\" --sql \"<bounded SELECT>\" --link-id \"<validated-link-id>@<key-set-index>\"",
        "~~~", "",
        "## 场景执行事实", "",
        f"- 规则源：{rule_ids}",
        f"- 运行时输入：{runtime_ids}",
        f"- 主流程：{format_list(flow_contract.get('main_flow', []), '未声明')}",
        *stage_lines,
        "", "## 运行时绑定", "", *source_lines,
        "", "## 输出边界", "",
        "- 输出必须首先给出业务结论或明确的阻塞原因，然后按 `delivery-contract.json` 列出规则完整行/文档章节、数据源/查询、关联校验（适用时）和证据定位。",
        "- `ready_for_agent_judgment` 只表示证据包完整可供 Agent 应用 accepted flow/controls，不表示脚本替代了业务语义判断。",
        "- 所有行级结果有界；全量结果必须由显式导出请求写入指定文件，不能打印到 Agent 上下文。",
        "- 运行时缺失、规则多选、关联放大、OCR 必需或规则与数据无法对应时，停止并返回可修复的证据缺口。",
        "", "## 非职责", "",
        "- 不修改原始业务文件，不创建临时 Python/SQL/HTTP 客户端，不依赖原平台 Tool、固定挂载目录或持久会话。",
        "- 不使用历史样本代替当前运行时数据，不将设计时输出模板当作运行时输入。",
        "",
    ])


def render_stage_skill(
    item: dict[str, Any], scenario_name: str, foundation_names: dict[str, str], flow: dict[str, Any]
) -> str:
    controls = {
        str(control.get("id")): control
        for control in flow.get("controls", []) if isinstance(control, dict)
    }
    questions = {
        str(question.get("id")): question
        for question in flow.get("open_questions", []) if isinstance(question, dict)
    }
    lines = [
        skill_frontmatter(item["skill_name"], item["description"]),
        f"# {item['display_name']}", "",
        f"在“{scenario_name}”中承担一个稳定业务责任。目标：{item['objective']} 结果：{item['outcome']}",
        "", "## 调用条件", "", *[f"- {value}" for value in item["invocation_triggers"]],
        "", "## 输入契约", "", *[render_contract_item(value, "accepted_formats") for value in item["input_contract"]],
        "", "## 依赖基础 Skill", "",
    ]
    if item["foundation_ids"]:
        lines.extend(f"- `${foundation_names[foundation_id]}`：只负责读取或解析对应输入，不替代本阶段判断。" for foundation_id in item["foundation_ids"])
    else:
        lines.append("- 无文件解析依赖；直接消费上游阶段提供的结构化业务对象。")
    execution = item.get("execution_contract", {})
    lines.extend(["", "## 数据执行约束", ""])
    lines.append("- 不直接打开或把源文件载入 Agent 上下文；只通过基础 Skill 的有界命令访问。")
    if execution.get("structured_rule_source_ids"):
        lines.append(
            f"- 表格规则源 {format_list(execution['structured_rule_source_ids'])} 必须返回被选中规则的完整一行，"
            "不能只保留规则名称或一段描述。"
        )
    if execution.get("document_rule_source_ids"):
        lines.append(
            f"- 文档规则源 {format_list(execution['document_rule_source_ids'])} 必须返回完整适用章节及来源定位，"
            "不能从孤立命中片段直接概括规则。"
        )
    if execution.get("large_source_ids"):
        lines.append(
            f"- 大数据源 {format_list(execution['large_source_ids'])} 只能在规则已定位后通过只读 SQL 投影、过滤、连接和聚合。"
        )
    if execution.get("link_ids"):
        lines.append(
            f"- 使用字段链路 {format_list(execution['link_ids'])} 前必须运行空值率、未匹配率和连接放大校验。"
        )
    if execution.get("document_source_ids"):
        lines.append(
            f"- 非结构化来源 {format_list(execution['document_source_ids'])} 必须先解析/OCR 后建立分块索引，"
            "只传递带来源摘要和页码/段落/行号的有限证据命中。"
        )
    if execution.get("semantic_route_ids"):
        lines.append(
            f"- 语义检索路径 {format_list(execution['semantic_route_ids'])} 只能用于查找证据；"
            "未出现明确业务主键时，不得把文档命中与结构化记录强行连接。"
        )
    if execution.get("trace_bundle_ids"):
        lines.append(
            f"- 设计期追踪蓝图 {format_list(execution['trace_bundle_ids'])} 已证明同一结果锚点可沿指定来源、投影字段和键组回溯；"
            "运行时必须在当前批次重新校验规则适用性、键值与基数，不得复制历史样例取值。"
        )
    lines.append("- 机器可读来源、表头、列、字段链路和非结构化检索路径见基础 Skill 的 `references/operational-data-contract.json`。")
    lines.extend(["", "## 执行", ""])
    lines.extend([
        "先使用包内阶段运行器建立受约束工作单；不得为本阶段临时编写 Python。文件读取、OCR、知识检索和大表 SQL 继续调用上面列出的基础 Skill 脚本。", "",
        "~~~text",
        "python \"<this-skill>/scripts/run_stage.py\" contract",
        "python \"<this-skill>/scripts/run_stage.py\" start --request \"<用户请求>\" --input \"<bounded-input.json>\" --output \"<work-order.json>\"",
        "~~~", "",
    ])
    for index, step in enumerate(item["procedure"], 1):
        lines.append(
            f"{index}. {step['action']}（依据：{step['basis']}；来源 {format_list(step['source_ids'])}）"
        )
    lines.extend(["", "## 业务控制", ""])
    if item["control_ids"]:
        for control_id in item["control_ids"]:
            control = controls.get(control_id, {})
            lines.append(f"- **{control.get('name', control_id)}**：{control.get('policy', '')}")
    else:
        lines.append("- 当前已验收流程没有为本阶段声明独立控制；不得自行补规则。")
    lines.extend(["", "## 输出契约", ""])
    lines.extend(render_contract_item(value, "formats") for value in item["output_contract"])
    lines.extend([
        "", "## 交接", "",
        f"- 前置阶段：{format_list(item['predecessor_stage_ids'])}",
        f"- 后续阶段：{format_list(item['successor_stage_ids'])}",
        "- 只交付符合输出契约的业务对象和可定位依据，不传递无关原始记录。",
        "", "## 待确认边界", "",
    ])
    if item["open_question_ids"]:
        for question_id in item["open_question_ids"]:
            question = questions.get(question_id, {})
            lines.append(f"- {question.get('question', question_id)}（影响：{question.get('impact', '待评估')}）")
    else:
        lines.append("- 无与本阶段直接关联的已声明待确认项。")
    lines.extend(["", "## 非职责", "", *[f"- {value}" for value in item["non_goals"]], "", "## 可移植运行", ""])
    lines.append("完成推理后把有界结果写为 JSON，并运行 `python \"<this-skill>/scripts/run_stage.py\" finish --work-order \"<work-order.json>\" --result \"<result.json>\" --output \"<handoff.json>\"`。阶段运行器会验证必需输出、产物存在性和 SHA-256，并拒绝原始业务文件路径。")
    lines.append("文件型输出必须使用 `value` 或 `artifact` 中的 `kind=exported_query_result`/`bounded_artifact_reference`、绝对 `path`、文件后缀和真实 `sha256`；可直接复用 `query_tabular.py export-contract` 返回的 `artifact` 对象。不得把原始行、全文或未校验路径塞进交接 JSON。")
    lines.append("本 Skill 不依赖原平台 Tool、固定目录或会话状态。调用方负责提供输入；运行配置由依赖基础 Skill 完整携带或由第三方同名环境变量覆盖。")
    lines.append("")
    return "\n".join(lines)


def render_orchestrator_skill(
    item: dict[str, Any], claims: dict[str, Any], capability_by_id: dict[str, dict[str, Any]], flow: dict[str, Any]
) -> str:
    stage_by_id = {str(stage.get("id")): stage for stage in flow.get("stages", []) if isinstance(stage, dict)}
    lines = [
        skill_frontmatter(item["skill_name"], item["description"]),
        f"# {item['display_name']}", "",
        f"编排“{claims['scenario']['name']}”的场景级能力。只选择需要的阶段 Skill，并遵守已验收主流程、控制和待确认边界。",
        "", "## 调用条件", "", *[f"- {value}" for value in item["invocation_triggers"]],
        "", "## 能力路由", "",
        "| 流程阶段 | 阶段 Skill | 业务结果 |",
        "|---|---|---|",
    ]
    for route in item["routing"]:
        stage_id = str(route["stage_id"])
        capability = capability_by_id[str(route["capability_id"])]
        stage = stage_by_id.get(stage_id, {})
        lines.append(f"| {stage.get('name', stage_id)} | `${capability['skill_name']}` | {stage.get('outcome', '')} |")
    lines.extend(["", "## 主流程", ""])
    for index, stage_id in enumerate(item["main_flow"], 1):
        capability = capability_by_id[f"cap-{stage_id}"]
        lines.append(f"{index}. 调用 `${capability['skill_name']}` 完成“{stage_by_id.get(stage_id, {}).get('name', stage_id)}”。")
    lines.extend(["", "## 文件基础能力", ""])
    if item["foundation_ids"]:
        for foundation_id in item["foundation_ids"]:
            capability = capability_by_id[foundation_id]
            lines.append(f"- `${capability['skill_name']}`：{capability['description']}")
    else:
        lines.append("- 当前场景没有从上游识别到需要随包提供的文件读取基础能力。")
    lines.extend(["", "## 失败与不确定性", "", *[f"- {value}" for value in item["failure_policy"]]])
    lines.extend(["", "## 非职责", "", *[f"- {value}" for value in item["non_goals"]]])
    lines.extend([
        "", "## 可移植运行", "",
        "使用包内状态机启动、查询和记录主流程交接，不得临时编写编排脚本：", "",
        "~~~text",
        "python \"<this-skill>/scripts/orchestrate.py\" start --request \"<用户请求>\" --output \"<flow-state.json>\"",
        "python \"<this-skill>/scripts/orchestrate.py\" status --state \"<flow-state.json>\"",
        "python \"<this-skill>/scripts/orchestrate.py\" record --state \"<flow-state.json>\" --handoff \"<stage-handoff.json>\"",
        "python \"<this-skill>/scripts/orchestrate.py\" route --stage-id \"<optional-stage-id>\"",
        "~~~", "",
        "所有依赖均按 Skill 名称解析；不假设原平台目录、Tool 网关或持久会话。若某项输入、规则或分支仍属待确认，不得由历史样本或常识补齐。", "",
    ])
    return "\n".join(lines)


def safe_replace_directory(target: Path, staging: Path, output_root: Path) -> None:
    target = target.resolve()
    staging = staging.resolve()
    output_root = output_root.resolve()
    if not target.is_relative_to(output_root) or not staging.is_relative_to(output_root):
        raise ContractError("生成目录逃逸 capability-distillation 输出根目录")
    if target.exists():
        shutil.rmtree(target)
    staging.replace(target)


def copy_template(template_name: str, target: Path) -> None:
    source = Path(__file__).resolve().parents[1] / "assets" / template_name
    if not source.is_dir():
        raise ContractError(f"缺少基础能力模板：{source}")
    shutil.copytree(source, target, ignore=_source_copy_ignore)


def install_primary_executor_runtimes(
    executor_root: Path, claims: dict[str, Any], operational: dict[str, Any],
) -> set[str]:
    """Make the one published executor self-contained for every declared input shape.

    A third-party host may install only ``scenario-main``.  Therefore source
    kind routing cannot depend on a separately installed reader Skill.  The
    executor always keeps its bounded tabular adapter; document parser code and
    its dependencies are copied only for scenarios that declare a non-tabular
    runtime source (or a document/OCR foundation).
    """

    assets_root = Path(__file__).resolve().parents[1] / "assets"
    tabular_reader_root = assets_root / "portable-tabular-reader"
    document_reader_root = assets_root / "portable-document-reader"
    reader_template = tabular_reader_root / "scripts" / "query_tabular.py"
    if not reader_template.is_file():
        raise ContractError(f"Missing portable tabular runtime: {reader_template}")
    shutil.copy2(reader_template, executor_root / "scripts" / "query_tabular.py")

    runtime_source_kinds = {
        str(item.get("kind", "")).casefold()
        for item in operational.get("sources", [])
        if isinstance(item, dict) and item.get("runtime_required") is True
    }
    foundation_kinds = {
        str(item.get("kind", "")).casefold()
        for item in claims.get("foundation_skills", []) if isinstance(item, dict)
    }
    installed: set[str] = {"tabular_adapter"}
    if "tabular" in runtime_source_kinds or "tabular" in foundation_kinds:
        merge_requirement_files(
            executor_root / "requirements.txt", tabular_reader_root / "requirements.txt"
        )
    if runtime_source_kinds & {"document", "unstructured", "text", "ocr"} or foundation_kinds & {"document", "ocr"}:
        document_runtime = document_reader_root / "scripts" / "extract_documents.py"
        if not document_runtime.is_file():
            raise ContractError(f"Missing portable document runtime: {document_runtime}")
        # The executor's document loader first looks beside itself.  Copy it
        # here rather than relying on an independently installed document Skill
        # or an implementation-specific sibling directory in the host.
        shutil.copy2(document_runtime, executor_root / "scripts" / "extract_documents.py")
        merge_requirement_files(
            executor_root / "requirements.txt", document_reader_root / "requirements.txt"
        )
        installed.add("document_adapter")
    return installed


def copy_compiled_recipe_catalog(
    output_root: Path, target: Path, catalog: dict[str, Any],
) -> None:
    """Preserve reviewed recipe bytes so replay and package fingerprints agree."""

    source = output_root / "compiled-recipes.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    if source.is_file():
        shutil.copy2(source, target)
    else:
        atomic_json(target, catalog)


def materialize_recipe_replay_runtime(
    claims: dict[str, Any], flow: dict[str, Any], output_root: Path,
) -> dict[str, Any]:
    """Build a deterministic, non-distributable runtime for private replay.

    `finalize` must be able to validate a report against the same executor it
    will publish, but a signed package cannot be a prerequisite for gathering
    that report.  This preview is intentionally local to the distillation
    output, carries no private replay fixture, and is atomically replaced on
    every successful preflight.
    """

    output_root = output_root.resolve()
    preview_root = output_root / "recipe-replay-runtime"
    staging_root = output_root / ".recipe-replay-runtime-staging"
    if staging_root.exists():
        if not staging_root.resolve().is_relative_to(output_root):
            raise ContractError("Recipe replay staging directory escapes the capability output root")
        shutil.rmtree(staging_root)
    operational = portable_operational_contract(claims)
    flow_contract = portable_flow_contract(claims, flow, operational)
    delivery_contract = portable_delivery_contract(claims, flow_contract, operational)
    catalog = compiled_recipe_catalog(output_root, operational)
    refresh_recipe_replay_contract(output_root, flow, operational)
    copy_template("portable-scenario-executor", staging_root)
    install_primary_executor_runtimes(staging_root, claims, operational)
    references = staging_root / "references"
    atomic_json(references / "operational-data-contract.json", operational)
    atomic_json(references / "flow-contract.json", flow_contract)
    atomic_json(references / "delivery-contract.json", delivery_contract)
    copy_compiled_recipe_catalog(output_root, references / "compiled-recipes.json", catalog)
    runtime_artifacts = {
        "catalog": references / "compiled-recipes.json",
        "executor": staging_root / "scripts" / "execute_scenario.py",
        "runtime_contract": references / "operational-data-contract.json",
        "flow_contract": references / "flow-contract.json",
    }
    verification = compiled_recipe_verification(
        output_root, flow, operational, runtime_artifacts,
    )
    atomic_json(
        references / "recipe-verification.json",
        portable_recipe_verification_contract(
            references / "compiled-recipes.json", catalog, verification,
        ),
    )
    safe_replace_directory(preview_root, staging_root, output_root)
    preview_references = preview_root / "references"
    final_artifacts = {
        "catalog": preview_references / "compiled-recipes.json",
        "executor": preview_root / "scripts" / "execute_scenario.py",
        "runtime_contract": preview_references / "operational-data-contract.json",
        "flow_contract": preview_references / "flow-contract.json",
    }
    digests, digest_errors = recipe_replay_runtime_digests(final_artifacts)
    if digest_errors:
        raise ContractError("; ".join(digest_errors))
    metadata = {
        "schema_version": 1,
        "kind": "recipe_replay_runtime",
        "status": "ready",
        "root": str(preview_root),
        "executor": str(final_artifacts["executor"]),
        "catalog": str(final_artifacts["catalog"]),
        "runtime_contract": str(final_artifacts["runtime_contract"]),
        "flow_contract": str(final_artifacts["flow_contract"]),
        "replay_contract": str(output_root / "recipe-replay-contract.json"),
        "digests": digests,
        "recipe_verification": verification,
        "private_fixture_policy": (
            "Pass a private compiled_recipe_replay_cases fixture directly to recipe_replay_runner.py; "
            "do not copy it into this output directory or a release package."
        ),
    }
    atomic_json(output_root / "recipe-replay-runtime.json", metadata)
    return metadata


def release_skill_root_document(claims: dict[str, Any], executor_name: str) -> str:
    """Create a standard top-level Skill entrypoint for archive importers."""

    bundle = claims["bundle"]
    scenario = claims["scenario"]
    return "\n".join([
        "---",
        f"name: {bundle['name']}",
        f"description: {json.dumps(str(bundle['description']), ensure_ascii=False)}",
        "---",
        "",
        f"# {scenario['name']}",
        "",
        "Read `system_prompt.md` before serving this scenario. For a complete business request, use the primary executor first:",
        f"`skills/{executor_name}/scripts/execute_scenario.py execute`.",
        "",
        "For hosts that support MCP, install the sibling `mcp-stdio.zip` package instead; it exposes the same primary executor as discoverable tools.",
        "",
    ])


def render_mcp_installation(server_name: str) -> str:
    return "\n".join([
        f"# {server_name} portable MCP package",
        "",
        "This package is detached from the source platform. It exposes the distilled primary executor as standard stdio MCP tools.",
        "",
        "## Install",
        "",
        "```bash",
        "pip install -r requirements.txt",
        "python run_mcp.py",
        "```",
        "",
        "Configure an MCP host with the absolute path to `run_mcp.py`. See `mcp_config.example.json`; package-oriented hosts can also import `mcp.json`.",
        "",
        "The server emits ASCII-only JSON-RPC on stdout. This intentionally remains valid when a legacy Windows host incorrectly decodes child output with GBK; business artifacts remain UTF-8 files.",
        "",
        "## Tools",
        "",
        "- `execute` is the only tool exposed by default. It selects the governing record, checks runtime sources, collects bounded linked evidence, and writes an Agent handoff when an output directory is supplied.",
        "- This deliberate one-tool surface prevents a host Agent from rebuilding the transaction with rule searches or ad-hoc SQL. Use a package-local test harness, not the production Agent, for diagnostics.",
        "",
    ])


def mcp_tool_definitions(namespace: str) -> list[dict[str, Any]]:
    """Render the package-host MCP contract used by the reference platform."""
    data_location = {
        "data_dir": {"type": "string", "description": "Directory containing the uploaded runtime data files."},
    }

    def tool(action: str, description: str, properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
        schema: dict[str, Any] = {"type": "object", "properties": properties}
        if required:
            schema["required"] = required
        return {
            "name": f"{namespace}__{action}",
            "action": action,
            "description": description,
            "inputSchema": schema,
        }

    return [
        tool("execute", "Run the one complete business request transaction. Return the terminal status, bounded evidence, artifact handles and Agent handoff; do not manually reconstruct it with rule searches or SQL. An explicit JSON/CSV/XLSX final-result path is honored only after a verified deterministic completion.", {
            **data_location,
            "request": {"type": "string", "description": "The complete business request."},
            "output_id": {"type": "string", "default": "execute_business_request", "description": "Compatibility output id; leave as the declared default."},
            "params": {"type": ["string", "object", "null"], "description": "Compatibility alias of the complete request when a host cannot send request."},
            "max_rows": {"type": "integer"},
            "out_dir": {"type": "string"},
            "delivery_output": {"type": "string", "description": "Explicit final JSON, CSV, or XLSX result path. Requires a persistent evidence output directory."},
            "delivery_format": {"type": "string", "enum": ["auto", "json", "csv", "xlsx"], "default": "auto"},
            "delivery_template_id": {"type": "string", "description": "Declared template id; it is materialized only when all columns come directly from a verified recipe."},
        }, ["data_dir", "request"]),
    ]


def platform_compatibility_contracts(executor_root: Path, claims: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], list[str]]:
    """Create the data files consumed by the reference package-host runtime."""
    operational = load_json(executor_root / "references" / "operational-data-contract.json", MAX_CANDIDATE_BYTES)
    flow = load_json(executor_root / "references" / "flow-contract.json", MAX_CANDIDATE_BYTES)
    source_ids = {str(item) for item in operational.get("rule_source_ids", [])}
    tables: list[dict[str, Any]] = []
    source_name_by_id: dict[str, str] = {}
    for index, source in enumerate(operational.get("sources", []), start=1):
        if not isinstance(source, dict):
            continue
        source_id = str(source.get("source_id", ""))
        raw_path = str(source.get("path", ""))
        table_name = Path(raw_path).stem if raw_path and "://" not in raw_path else str(source.get("view_name", ""))
        table_name = table_name or str(source.get("view_name", "")) or f"source_{index}"
        source_name_by_id[source_id] = table_name
        first_table = next((item for item in source.get("tables", []) if isinstance(item, dict)), {})
        columns = [
            {"name": str(column.get("query_name") or column.get("name")), "kind": column.get("kind", "text")}
            for column in first_table.get("columns", []) if isinstance(column, dict)
            and str(column.get("query_name") or column.get("name") or "")
        ]
        tables.append({
            "table_name": table_name,
            "source_id": source_id,
            "file_path": raw_path,
            "lifecycle": source.get("lifecycle", "runtime_input"),
            "role": "knowledge" if source_id in source_ids else "input",
            "columns": columns,
            "header_row": first_table.get("header", {}).get("header_row", 0) if isinstance(first_table.get("header"), dict) else 0,
        })
    relations = [
        {
            "source_table": source_name_by_id.get(str(link.get("source_id", "")), str(link.get("source_id", ""))),
            "target_table": source_name_by_id.get(str(link.get("target_id", "")), str(link.get("target_id", ""))),
            "key_pairs": link.get("key_pairs") or link.get("recommended_candidate", {}),
        }
        for link in operational.get("links", []) if isinstance(link, dict)
    ]
    knowledge_table = next((source_name_by_id.get(source_id, "") for source_id in source_ids if source_name_by_id.get(source_id)), "")
    execution_plan = flow.get("execution_plan") if isinstance(flow.get("execution_plan"), dict) else {}
    output_specs = {
        "outputs": [{
            "output_id": "execute_business_request",
            "name": claims["scenario"].get("business_outcome") or claims["scenario"].get("name", "business result"),
            "format": "scenario_evidence",
            "description": "Run the one primary scenario transaction and return the selected governing rule, bounded evidence, terminal status, artifact handles and Agent handoff. The host must preserve this structured response rather than replacing it with a generic row-count summary.",
            "result_contract": execution_plan.get("result_contract", {}),
            "host_action": {
                "action": "execute",
                "default_output_id": "execute_business_request",
                "response_contract": "portable_business_request_transaction_result",
                "required_response_fields": [
                    "status", "selected_rule", "candidate_evidence", "deterministic_result",
                    "agent_handoff", "next_step", "artifact",
                ],
            },
        }],
    }
    domain = {
        "schema_version": RELEASE_CONTRACT_VERSION,
        "scenario": claims["scenario"],
        "tables": tables,
        "relations": relations,
        "knowledge_table": knowledge_table,
    }
    dispatch = {
        "knowledge_table": knowledge_table,
        "dispatch_key_column": "",
        "primary_action": "execute",
        "normal_action_allowlist": ["execute"],
        "fallback_actions": ["query_data"],
        "fallback_policy": "query_data_only_when_execute.next_step_names_a_concrete_missing_field_or_relationship",
        "default_output_id": "execute_business_request",
        "response_contract": "portable_business_request_transaction_result",
        "structured_passthrough_required": True,
    }
    required_tables = [
        item["table_name"]
        for item in tables
        if item.get("lifecycle") == "runtime_input"
    ]
    return domain, output_specs, dispatch, required_tables


def render_platform_system_prompt(
    claims: dict[str, Any], executor_name: str, required_tables: list[str],
    delivery_contract: dict[str, Any],
) -> str:
    """Render instructions for the reference platform's standard Skill importer."""
    scenario = claims.get("scenario", {})
    scenario_name = str(scenario.get("name", "业务场景"))
    purpose = str(scenario.get("purpose", "完成已声明的业务目标"))
    tables = "、".join(required_tables) or "由 describe_schema 返回的运行时表"
    delivery_fields = format_list(
        delivery_contract.get("delivery_contract", {}).get("required_fields", []),
        "business conclusion, evidence, coverage, and uncertainty",
    )
    return "\n".join([
        f"# {scenario_name} 子 Agent System Prompt",
        "",
        "## 唯一允许的执行路径",
        "",
        "本能力包的业务执行只能使用平台提供的能力动作，内部主入口是 "
        "`main_skill/scripts/skill_executor.py`。不得绕过该入口读取包内 JSON/配置文件、"
        "临时创建 Python 或 SQL 脚本，或自行猜测表结构、关联关系和业务规则。",
        "",
        "可用平台动作可能还会列出 `describe_capability`、`describe_schema`、`list_outputs`、"
        "`list_knowledge`、`search_knowledge`、`query_data`；其中 `execute` 是唯一的正常业务路径。其余动作只用于 `execute.next_step` 报告的明确证据缺口，不能作为预处理步骤。",
        f"唯一交付契约是 `main_skill/references/delivery-contract.json`；完成响应必须包含：{delivery_fields}。",
        "",
        "## 业务职责",
        "",
        f"你负责“{scenario_name}”场景：{purpose}。",
        f"运行时业务数据由宿主上传并绑定；本场景需要的表为：{tables}。",
        "",
        "## 执行顺序",
        "",
        "1. 对完整业务请求，首先且只调用一次 `execute`，固定传入 `output_id=execute_business_request`、用户完整请求作为 `params`、宿主提供的 `data_dir`，以及可写的 `out_dir`。不要先分拆为搜索规则、读表、阶段 Skill 或手工 SQL。",
        "2. 宿主必须保留 `execute` 的结构化事务结果（至少 `status`、`selected_rule`、`candidate_evidence`、`deterministic_result`、`agent_handoff`、`next_step` 与 `artifact`），不得把它缩成“输出 N 行”的通用提示。以这些字段为唯一业务事实完成一次判定和交付。零命中、规则不唯一或来源不兼容时，按返回的 blocker 向用户说明，禁止猜测或盲目重试。",
        "3. 只有 `next_step` 明确指出缺失字段或未解决关联时，才使用一次 `query_data` 做该补充；不得用它重新实现整条审计规则。",
        "4. 用户对刚完成的审计结果提出筛选、统计或追问时，优先基于本轮交付的结果和证据继续回答。确需重新执行时，`params` 必须包含原始规则上下文与新条件，不能只传一句过滤条件。",
        "5. 输出结论时说明所用规则、数据范围、关键字段、证据路径以及未匹配/截断/待确认边界。",
        "",
        "## 禁止事项",
        "",
        "- 禁止把历史结果样本当作运行时规则或当前业务事实。",
        "- 禁止直接打开大文件、全量灌入上下文，或执行未声明的外部访问。",
        "- 禁止在证据不足、关联未验证或规则不唯一时编造结论。",
        f"- 禁止绕过 `{executor_name}` 对应的主能力入口。",
        "",
    ])


def render_platform_main_skill_guide(
    required_tables: list[str], executor_name: str, delivery_contract: dict[str, Any],
) -> str:
    """Render the host-facing root Skill without losing the importer contract.

    Some third-party action adapters identify this root by ``scenario-main``;
    it is intentionally distinct from the generated executor directory name.
    The compatibility files remain alongside it and are the implementation
    boundary for the single exposed action.
    """

    tables = "、".join(required_tables) or "由 describe_schema 返回的运行时表"
    required_fields = format_list(
        delivery_contract.get("delivery_contract", {}).get("required_fields", []),
        "业务结论、理由、范围、证据与不确定性",
    )
    return "\n".join([
        "---",
        "name: scenario-main",
        "description: \"Third-party portable business scenario entrypoint. Execute one complete request through the compatibility action and return its auditable delivery contract.\"",
        "---",
        "",
        "# 平台主 Skill 使用说明",
        "",
        "本目录由第三方平台运行时加载。唯一正常业务动作是 `execute`，其兼容入口为 `scripts/skill_executor.py`；"
        "它读取同目录兼容配置并转交 `main_executor`。不要直接读取配置 JSON、绕过该入口或手工调用内部阶段。",
        "",
        f"运行时需提供的数据表：{tables}。",
        "",
        f"标准单动作契约：对每个新的完整请求只调用一次 `execute(output_id=execute_business_request)` → 读取 `agent_handoff` → 交付 {required_fields}。"
        "仅当 `next_step` 指出具体证据缺口时才使用一次 `query_data`；同一完成结果的追问复用结果工件，不重跑源数据。",
        "宿主必须把 `execute` 的结构化事务结果传给 Agent，不能只显示泛化的行数/文件名摘要；兼容响应字段见 `dispatch_config.json`。",
        "",
        f"内部主执行器：`{executor_name}`；交付字段与终态规则见同目录 `references/delivery-contract.json`。",
        "",
    ])


def build_release_bundle(
    claims: dict[str, Any], output_root: Path, skills_root: Path, executor_name: str,
    skill_manifest: list[dict[str, Any]],
) -> dict[str, Any]:
    """Emit installable Skill and stdio-MCP packages beside the source tree.

    Distillation previously stopped at a nested Skills directory.  That is
    sufficient for this Studio but not for a third-party host, which needs a
    concrete import archive or a process-level MCP endpoint.
    """

    staging = output_root / ".release-staging"
    if staging.exists():
        if not staging.resolve().is_relative_to(output_root.resolve()):
            raise ContractError("Release staging directory escapes the distillation output root")
        shutil.rmtree(staging)
    staging.mkdir(parents=True)
    try:
        skill_root = staging / "skill"
        skill_root.mkdir()
        # The host scans every root-level Skill and exposes each one to the
        # Agent.  Stage readers and orchestrators are useful source-package
        # diagnostics, but publishing them makes the host choose among a set of
        # incomplete implementation fragments.  Publish only the primary
        # executor as `main_skill`; its package actions retain the controlled
        # diagnostic fallbacks when a real evidence gap needs one.
        executor_source = skills_root / executor_name
        if not executor_source.is_dir():
            raise ContractError(f"Generated main executor is missing: {executor_name}")
        shutil.copytree(executor_source, skill_root / "main_skill", ignore=_source_copy_ignore)
        compatibility_root = Path(__file__).resolve().parents[1] / "assets" / "portable-platform-compatibility"
        if not compatibility_root.is_dir():
            raise ContractError("Missing portable platform compatibility assets")
        shutil.copy2(
            compatibility_root / "scripts" / "compatibility_runtime.py",
            skill_root / "main_skill" / "scripts" / "compatibility_runtime.py",
        )
        shutil.copy2(
            compatibility_root / "scripts" / "skill_executor.py",
            skill_root / "main_skill" / "scripts" / "skill_executor.py",
        )
        delivery_contract = load_json(
            skill_root / "main_skill" / "references" / "delivery-contract.json",
            MAX_CANDIDATE_BYTES,
        )
        domain_knowledge, output_specs, dispatch_config, required_tables = platform_compatibility_contracts(
            skill_root / "main_skill", claims,
        )
        atomic_json(skill_root / "main_skill" / "domain_knowledge.json", domain_knowledge)
        atomic_json(skill_root / "main_skill" / "output_specs.json", output_specs)
        atomic_json(skill_root / "main_skill" / "dispatch_config.json", dispatch_config)
        atomic_text(
            skill_root / "system_prompt.md",
            render_platform_system_prompt(claims, executor_name, required_tables, delivery_contract),
        )
        atomic_text(
            skill_root / "main_skill" / "SKILL.md",
            render_platform_main_skill_guide(
                required_tables,
                executor_name,
                delivery_contract,
            ),
        )

        mcp_root = staging / "mcp"
        copy_template("portable-mcp-adapter", mcp_root)
        shutil.copytree(skills_root / executor_name, mcp_root / "main_executor", ignore=_source_copy_ignore)
        shutil.copytree(skill_root / "main_skill", mcp_root / "main_skill", ignore=_source_copy_ignore)
        shutil.copytree(
            compatibility_root / "tools", mcp_root / "tools", ignore=_source_copy_ignore,
        )
        runtime_dependencies = sorted({
            dependency
            for item in skill_manifest
            if item.get("name") == executor_name
            for dependency in item.get("python_dependencies", [])
        })
        atomic_text(
            mcp_root / "requirements.txt",
            "\n".join(runtime_dependencies) + ("\n" if runtime_dependencies else ""),
        )
        server_name = executor_name.removesuffix("-main-executor") or executor_name
        namespace = "s_" + hashlib.sha256(server_name.encode("utf-8")).hexdigest()[:8]
        tool_definitions = mcp_tool_definitions(namespace)
        atomic_json(mcp_root / "mcp.json", {
            "schema_version": RELEASE_CONTRACT_VERSION,
            "protocol": "mcp",
            "spec_version": "2024-11-05",
            "protocol_version": "2024-11-05",
            "namespace": namespace,
            "server_name": server_name,
            "skill_name": server_name,
            "display_name": claims["scenario"].get("name", server_name),
            "summary": claims["bundle"].get("description", ""),
            "required_tables": required_tables,
            "knowledge_table": dispatch_config["knowledge_table"],
            "execution_mode": "knowledge_engine",
            "command": "python",
            "args": ["run_mcp.py"],
            "entrypoint": "run_mcp.py",
            "main_executor": executor_name,
            "delivery_contract": "main_executor/references/delivery-contract.json",
            "transport": "stdio",
            "stdout_contract": "ascii_json_rpc",
            "requires_host_llm_reasoning": True,
            "primary_install_mode": "mcp_stdio",
            "normal_action_allowlist": ["execute"],
            "diagnostic_actions": [
                "describe_capability", "describe_schema", "list_outputs", "list_knowledge",
                "search_knowledge", "query_data",
            ],
            "tool_surface_policy": "expose_execute_only_by_default",
            "tools": tool_definitions,
        })
        atomic_json(mcp_root / "mcp_config.example.json", {
            "mcpServers": {
                server_name: {
                    "command": "python",
                    "args": ["/absolute/path/to/package/run_mcp.py"],
                }
            }
        })
        atomic_text(mcp_root / "INSTALL.md", render_mcp_installation(server_name))
        atomic_json(mcp_root / "manifest.json", {
            "schema_version": RELEASE_CONTRACT_VERSION,
            "format": "portable-business-capability-mcp",
            "scenario": claims["scenario"],
            "bundle": claims["bundle"],
            "main_executor": executor_name,
            "namespace": namespace,
            "tool_actions": [
                "execute",
            ],
            "tool_names": [item["name"] for item in tool_definitions],
            "runtime_dependencies": runtime_dependencies,
            "reference_contracts": {
                "delivery_contract": "main_executor/references/delivery-contract.json",
                "delivery_contract_sha256": sha256_file(
                    mcp_root / "main_executor" / "references" / "delivery-contract.json"
                ),
                "recipe_runtime_verification": "main_executor/references/recipe-verification.json",
                "recipe_runtime_verification_sha256": sha256_file(
                    mcp_root / "main_executor" / "references" / "recipe-verification.json"
                ),
            },
        })

        artifacts_root = staging / "artifacts"
        artifacts_root.mkdir()
        skill_archive = Path(shutil.make_archive(
            str(artifacts_root / "skill"), "zip", root_dir=staging, base_dir="skill"
        ))
        mcp_archive = Path(shutil.make_archive(
            str(artifacts_root / "mcp-stdio"), "zip", root_dir=staging, base_dir="mcp"
        ))
        release_payload = {
            "schema_version": RELEASE_CONTRACT_VERSION,
            "format": "portable-business-capability-release",
            "scenario": claims["scenario"],
            "bundle": claims["bundle"],
            "main_executor": executor_name,
            "install_modes": {
                "skill_directory": {
                    "root": "skill",
                    "archive": "artifacts/skill.zip",
                    "system_prompt": "system_prompt.md",
                    "primary_entrypoint": "main_skill/scripts/skill_executor.py",
                },
                "mcp_stdio": {
                    "root": "mcp",
                    "archive": "artifacts/mcp-stdio.zip",
                    "config": "mcp/mcp_config.example.json",
                    "descriptor": "mcp/mcp.json",
                    "command": "python",
                    "args": ["run_mcp.py"],
                    "stdout_contract": "ascii_json_rpc",
                },
            },
            "artifact_digests": {
                "skill_zip": sha256_file(skill_archive),
                "mcp_stdio_zip": sha256_file(mcp_archive),
            },
            "reference_contracts": {
                "delivery_contract": {
                    "skill": "skill/main_skill/references/delivery-contract.json",
                    "mcp_main_executor": "mcp/main_executor/references/delivery-contract.json",
                    "sha256": sha256_file(
                        skill_root / "main_skill" / "references" / "delivery-contract.json"
                    ),
                },
                "recipe_runtime_verification": {
                    "skill": "skill/main_skill/references/recipe-verification.json",
                    "mcp_main_executor": "mcp/main_executor/references/recipe-verification.json",
                    "sha256": sha256_file(
                        skill_root / "main_skill" / "references" / "recipe-verification.json"
                    ),
                },
            },
        }
        atomic_json(staging / "release.json", release_payload)
        target = output_root / "release"
        safe_replace_directory(target, staging, output_root)
        return {
            "schema_version": RELEASE_CONTRACT_VERSION,
            "root": "release",
            "skill_archive": "release/artifacts/skill.zip",
            "mcp_stdio_archive": "release/artifacts/mcp-stdio.zip",
            "mcp_config": "release/mcp/mcp_config.example.json",
            "stdout_contract": "ascii_json_rpc",
            "artifact_digests": release_payload["artifact_digests"],
        }
    except Exception:
        if staging.exists():
            shutil.rmtree(staging)
        raise


def archive_source_digest_errors(
    archive_path: Path, source_root: Path, archive_root: str,
) -> list[str]:
    """Require an install archive to be an exact byte-for-byte source snapshot."""

    if not source_root.is_dir():
        return [f"Release source directory is missing: {source_root}"]
    expected = {
        f"{archive_root}/{path.relative_to(source_root).as_posix()}": path
        for path in source_root.rglob("*")
        if path.is_file()
        and "__pycache__" not in path.parts
        and path.suffix.casefold() not in {".pyc", ".pyo"}
    }
    try:
        with zipfile.ZipFile(archive_path) as opened:
            members = {
                item.filename: item
                for item in opened.infolist()
                if not item.is_dir()
            }
            missing = sorted(set(expected) - set(members))
            unexpected = sorted(set(members) - set(expected))
            errors: list[str] = []
            if missing:
                errors.append(
                    f"Release archive {archive_path.name} is missing source members: "
                    + ", ".join(missing[:8])
                )
            if unexpected:
                errors.append(
                    f"Release archive {archive_path.name} has members absent from its source tree: "
                    + ", ".join(unexpected[:8])
                )
            for member, source_path in expected.items():
                info = members.get(member)
                if info is None:
                    continue
                digest = hashlib.sha256()
                with opened.open(info) as payload:
                    while chunk := payload.read(1024 * 1024):
                        digest.update(chunk)
                if digest.hexdigest() != sha256_file(source_path):
                    errors.append(
                        f"Release archive {archive_path.name} content differs from source: {member}"
                    )
            return errors
    except (OSError, zipfile.BadZipFile) as exc:
        return [f"Invalid release archive {archive_path.name}: {exc}"]


def validate_release_bundle(output_root: Path, executor_name: str) -> list[str]:
    root = output_root / "release"
    required = [
        root / "release.json",
        root / "skill" / "system_prompt.md",
        root / "skill" / "main_skill" / "SKILL.md",
        root / "skill" / "main_skill" / "scripts" / "skill_executor.py",
        root / "skill" / "main_skill" / "scripts" / "compatibility_runtime.py",
        root / "skill" / "main_skill" / "scripts" / "execute_scenario.py",
        root / "skill" / "main_skill" / "scripts" / "recipe_runtime.py",
        root / "skill" / "main_skill" / "domain_knowledge.json",
        root / "skill" / "main_skill" / "output_specs.json",
        root / "skill" / "main_skill" / "dispatch_config.json",
        root / "skill" / "main_skill" / "references" / "compiled-recipes.json",
        root / "skill" / "main_skill" / "references" / "recipe-verification.json",
        root / "skill" / "main_skill" / "references" / "delivery-contract.json",
        root / "mcp" / "run_mcp.py",
        root / "mcp" / "mcp_server.py",
        root / "mcp" / "mcp.json",
        root / "mcp" / "manifest.json",
        root / "mcp" / "mcp_config.example.json",
        root / "mcp" / "main_executor" / "scripts" / "execute_scenario.py",
        root / "mcp" / "main_executor" / "scripts" / "recipe_runtime.py",
        root / "mcp" / "main_executor" / "references" / "compiled-recipes.json",
        root / "mcp" / "main_executor" / "references" / "recipe-verification.json",
        root / "mcp" / "main_executor" / "references" / "delivery-contract.json",
        root / "mcp" / "main_skill" / "SKILL.md",
        root / "mcp" / "main_skill" / "scripts" / "skill_executor.py",
        root / "mcp" / "main_skill" / "scripts" / "compatibility_runtime.py",
        root / "mcp" / "main_skill" / "domain_knowledge.json",
        root / "mcp" / "main_skill" / "output_specs.json",
        root / "mcp" / "main_skill" / "dispatch_config.json",
        root / "mcp" / "main_skill" / "references" / "delivery-contract.json",
        root / "mcp" / "tools" / "knowledge" / "search_knowledge.py",
        root / "mcp" / "tools" / "knowledge" / "list_knowledge.py",
        root / "artifacts" / "skill.zip",
        root / "artifacts" / "mcp-stdio.zip",
    ]
    errors = [f"Missing release artifact: {path.relative_to(output_root).as_posix()}" for path in required if not path.is_file()]
    if errors:
        return errors
    try:
        release = load_json(root / "release.json", MAX_CANDIDATE_BYTES)
        mcp = load_json(root / "mcp" / "mcp.json", MAX_CANDIDATE_BYTES)
        mcp_manifest = load_json(root / "mcp" / "manifest.json", MAX_CANDIDATE_BYTES)
    except ContractError as exc:
        return [str(exc)]
    if release.get("format") != "portable-business-capability-release":
        errors.append("Release manifest format is invalid")
    skill_main_root = root / "skill" / "main_skill"
    main_skill_text = (skill_main_root / "SKILL.md").read_text(encoding="utf-8")
    frontmatter = re.match(r"\A---\r?\n(?P<body>.*?)\r?\n---\r?\n", main_skill_text, re.DOTALL)
    if frontmatter is None or not re.search(
        r"(?m)^name:\s*scenario-main\s*$", frontmatter.group("body") if frontmatter else ""
    ):
        errors.append("Release root main_skill/SKILL.md must declare frontmatter name: scenario-main")
    if "scripts/skill_executor.py" not in main_skill_text or "`execute`" not in main_skill_text:
        errors.append("Release root main_skill/SKILL.md does not declare the single compatibility execute action")
    install_modes = release.get("install_modes") if isinstance(release.get("install_modes"), dict) else {}
    skill_install = install_modes.get("skill_directory") if isinstance(install_modes.get("skill_directory"), dict) else {}
    if skill_install.get("primary_entrypoint") != "main_skill/scripts/skill_executor.py":
        errors.append("Release manifest primary entrypoint is not the root compatibility action")
    try:
        skill_delivery = load_json(
            skill_main_root / "references" / "delivery-contract.json", MAX_CANDIDATE_BYTES
        )
        skill_recipe_catalog = load_json(
            skill_main_root / "references" / "compiled-recipes.json", MAX_CANDIDATE_BYTES
        )
        skill_recipe_verification = load_json(
            skill_main_root / "references" / "recipe-verification.json", MAX_CANDIDATE_BYTES
        )
        skill_operational = load_json(
            skill_main_root / "references" / "operational-data-contract.json", MAX_OPERATIONAL_BYTES
        )
    except ContractError as exc:
        errors.append(str(exc))
        skill_delivery = {}
        skill_recipe_catalog = {}
        skill_recipe_verification = {}
        skill_operational = {}
    if (
        skill_delivery.get("contract_kind") != "portable_business_request_transaction"
        or skill_delivery.get("entrypoint", {}).get("command") != "execute"
    ):
        errors.append("Release root delivery-contract is invalid or does not bind execute")
    skill_catalog_path = skill_main_root / "references" / "compiled-recipes.json"
    expected_catalog_digest = sha256_file(skill_catalog_path)
    verified_recipe_ids = skill_recipe_verification.get("verified_recipe_ids")
    if (
        skill_recipe_verification.get("kind") != "portable_recipe_runtime_verification"
        or skill_recipe_verification.get("recipe_catalog_fingerprint") != expected_catalog_digest
        or not isinstance(verified_recipe_ids, list)
    ):
        errors.append("Release root recipe runtime verification is invalid or unbound from compiled recipes")
    skill_sources = skill_operational.get("sources", [])
    if not isinstance(skill_sources, list):
        skill_sources = []
    declared_document_runtime = any(
        isinstance(source, dict)
        and source.get("runtime_required") is True
        and str(source.get("kind", "")).casefold() in {"document", "unstructured", "text", "ocr"}
        for source in skill_sources
    )
    if declared_document_runtime:
        if not (skill_main_root / "scripts" / "extract_documents.py").is_file():
            errors.append("Release root declares document runtime input but omits extract_documents.py")
        dependency_set = set(python_dependencies(skill_main_root))
        if not {"pypdf>=5.0.0,<7.0.0", "python-docx>=1.1.0,<2.0.0", "python-pptx>=1.0.0,<2.0.0"}.issubset(dependency_set):
            errors.append("Release root declares document runtime input but omits document parser requirements")
    try:
        dispatch = load_json(skill_main_root / "dispatch_config.json", MAX_CANDIDATE_BYTES)
        output_specs = load_json(skill_main_root / "output_specs.json", MAX_CANDIDATE_BYTES)
    except ContractError as exc:
        errors.append(str(exc))
        dispatch = {}
        output_specs = {}
    host_outputs = output_specs.get("outputs") if isinstance(output_specs.get("outputs"), list) else []
    primary_output = next(
        (item for item in host_outputs if isinstance(item, dict) and item.get("output_id") == "execute_business_request"),
        {},
    )
    host_action = primary_output.get("host_action") if isinstance(primary_output.get("host_action"), dict) else {}
    if (
        dispatch.get("primary_action") != "execute"
        or dispatch.get("normal_action_allowlist") != ["execute"]
        or dispatch.get("structured_passthrough_required") is not True
        or dispatch.get("default_output_id") != "execute_business_request"
        or host_action.get("response_contract") != "portable_business_request_transaction_result"
    ):
        errors.append("Release compatibility dispatch does not enforce the single structured execute transaction")
    release_references = release.get("reference_contracts") if isinstance(release.get("reference_contracts"), dict) else {}
    release_delivery = release_references.get("delivery_contract") if isinstance(release_references.get("delivery_contract"), dict) else {}
    expected_delivery_paths = {
        "skill": "skill/main_skill/references/delivery-contract.json",
        "mcp_main_executor": "mcp/main_executor/references/delivery-contract.json",
    }
    if any(release_delivery.get(key) != value for key, value in expected_delivery_paths.items()):
        errors.append("Release manifest does not wire the canonical delivery-contract paths")
    skill_delivery_digest = sha256_file(skill_main_root / "references" / "delivery-contract.json")
    mcp_delivery_path = root / "mcp" / "main_executor" / "references" / "delivery-contract.json"
    if (
        release_delivery.get("sha256") != skill_delivery_digest
        or sha256_file(mcp_delivery_path) != skill_delivery_digest
    ):
        errors.append("Release delivery-contract copies or digest differ")
    release_recipe = release_references.get("recipe_runtime_verification") if isinstance(
        release_references.get("recipe_runtime_verification"), dict
    ) else {}
    expected_recipe_paths = {
        "skill": "skill/main_skill/references/recipe-verification.json",
        "mcp_main_executor": "mcp/main_executor/references/recipe-verification.json",
    }
    skill_recipe_digest = sha256_file(skill_main_root / "references" / "recipe-verification.json")
    mcp_recipe_path = root / "mcp" / "main_executor" / "references" / "recipe-verification.json"
    if (
        any(release_recipe.get(key) != value for key, value in expected_recipe_paths.items())
        or release_recipe.get("sha256") != skill_recipe_digest
        or sha256_file(mcp_recipe_path) != skill_recipe_digest
    ):
        errors.append("Release recipe runtime verification copies or digest differ")
    if mcp.get("protocol") != "mcp" or mcp.get("transport") != "stdio":
        errors.append("MCP package does not declare standard stdio transport")
    if mcp.get("main_executor") != executor_name:
        errors.append("MCP package primary executor differs from the capability manifest")
    if mcp.get("stdout_contract") != "ascii_json_rpc":
        errors.append("MCP package does not declare an ASCII-safe stdout contract")
    if mcp.get("delivery_contract") != "main_executor/references/delivery-contract.json":
        errors.append("MCP descriptor does not wire the main executor delivery-contract")
    mcp_references = mcp_manifest.get("reference_contracts") if isinstance(mcp_manifest.get("reference_contracts"), dict) else {}
    if (
        mcp_references.get("delivery_contract") != "main_executor/references/delivery-contract.json"
        or mcp_references.get("delivery_contract_sha256") != skill_delivery_digest
    ):
        errors.append("MCP manifest delivery-contract reference or digest is invalid")
    if (
        mcp_references.get("recipe_runtime_verification") != "main_executor/references/recipe-verification.json"
        or mcp_references.get("recipe_runtime_verification_sha256") != skill_recipe_digest
    ):
        errors.append("MCP manifest recipe runtime verification reference or digest is invalid")
    namespace = str(mcp.get("namespace", ""))
    tools = mcp.get("tools", [])
    if not namespace or not isinstance(tools, list) or not tools:
        errors.append("MCP package lacks a discoverable namespace/tool descriptor")
    elif any(
        not isinstance(item, dict)
        or not str(item.get("action", ""))
        or item.get("name") != f"{namespace}__{item.get('action')}"
        or not isinstance(item.get("inputSchema"), dict)
        for item in tools
    ):
        errors.append("MCP package tool descriptor is invalid")
    server_text = (root / "mcp" / "mcp_server.py").read_text(encoding="utf-8")
    if "ensure_ascii=True" not in server_text or "sys.stdout.buffer.write" not in server_text:
        errors.append("MCP server does not enforce an ASCII-safe wire format")
    expected_members = {
        "skill/system_prompt.md": root / "artifacts" / "skill.zip",
        "skill/main_skill/SKILL.md": root / "artifacts" / "skill.zip",
        "skill/main_skill/scripts/skill_executor.py": root / "artifacts" / "skill.zip",
        "skill/main_skill/scripts/compatibility_runtime.py": root / "artifacts" / "skill.zip",
        "skill/main_skill/scripts/execute_scenario.py": root / "artifacts" / "skill.zip",
        "skill/main_skill/scripts/recipe_runtime.py": root / "artifacts" / "skill.zip",
        "skill/main_skill/requirements.txt": root / "artifacts" / "skill.zip",
        "skill/main_skill/domain_knowledge.json": root / "artifacts" / "skill.zip",
        "skill/main_skill/output_specs.json": root / "artifacts" / "skill.zip",
        "skill/main_skill/dispatch_config.json": root / "artifacts" / "skill.zip",
        "skill/main_skill/references/compiled-recipes.json": root / "artifacts" / "skill.zip",
        "skill/main_skill/references/recipe-verification.json": root / "artifacts" / "skill.zip",
        "skill/main_skill/references/delivery-contract.json": root / "artifacts" / "skill.zip",
        "mcp/run_mcp.py": root / "artifacts" / "mcp-stdio.zip",
        "mcp/mcp_server.py": root / "artifacts" / "mcp-stdio.zip",
        "mcp/mcp.json": root / "artifacts" / "mcp-stdio.zip",
        "mcp/manifest.json": root / "artifacts" / "mcp-stdio.zip",
        "mcp/main_skill/SKILL.md": root / "artifacts" / "mcp-stdio.zip",
        "mcp/main_skill/scripts/skill_executor.py": root / "artifacts" / "mcp-stdio.zip",
        "mcp/main_skill/scripts/compatibility_runtime.py": root / "artifacts" / "mcp-stdio.zip",
        "mcp/main_skill/references/delivery-contract.json": root / "artifacts" / "mcp-stdio.zip",
        "mcp/main_executor/scripts/recipe_runtime.py": root / "artifacts" / "mcp-stdio.zip",
        "mcp/main_executor/requirements.txt": root / "artifacts" / "mcp-stdio.zip",
        "mcp/requirements.txt": root / "artifacts" / "mcp-stdio.zip",
        "mcp/main_executor/references/recipe-verification.json": root / "artifacts" / "mcp-stdio.zip",
        "mcp/main_executor/references/delivery-contract.json": root / "artifacts" / "mcp-stdio.zip",
    }
    archive_names: dict[Path, set[str]] = {}
    for archive in set(expected_members.values()):
        try:
            with zipfile.ZipFile(archive) as opened:
                archive_names[archive] = set(opened.namelist())
        except (OSError, zipfile.BadZipFile) as exc:
            errors.append(f"Invalid release archive {archive.name}: {exc}")
    for member, archive in expected_members.items():
        if member not in archive_names.get(archive, set()):
            errors.append(f"Release archive {archive.name} is missing {member}")
    artifact_digests = release.get("artifact_digests") if isinstance(release.get("artifact_digests"), dict) else {}
    if artifact_digests.get("skill_zip") != sha256_file(root / "artifacts" / "skill.zip"):
        errors.append("Release manifest skill archive digest differs from the archive")
    if artifact_digests.get("mcp_stdio_zip") != sha256_file(root / "artifacts" / "mcp-stdio.zip"):
        errors.append("Release manifest MCP archive digest differs from the archive")
    errors.extend(
        archive_source_digest_errors(root / "artifacts" / "skill.zip", root / "skill", "skill")
    )
    errors.extend(
        archive_source_digest_errors(root / "artifacts" / "mcp-stdio.zip", root / "mcp", "mcp")
    )
    return errors


def _source_copy_ignore(_: str, names: list[str]) -> set[str]:
    return {name for name in names if name == "__pycache__" or name.endswith((".pyc", ".pyo"))}


def platform_skill_secrets() -> dict[str, dict[str, str]]:
    candidates = []
    explicit = str(os.environ.get("BUSINESS_FLOW_SKILL_SECRET_STORE", "")).strip()
    if explicit:
        candidates.append(Path(explicit).expanduser())
    candidates.append(Path(__file__).resolve().parents[3] / "system" / "studio" / "skill_secrets.json")
    for path in candidates:
        if not path.is_file():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        raw = payload.get("skills", payload) if isinstance(payload, dict) else {}
        if not isinstance(raw, dict):
            continue
        return {
            str(skill): {
                str(key): str(value)
                for key, value in values.items()
                if isinstance(values, dict) and str(value).strip()
            }
            for skill, values in raw.items()
            if isinstance(values, dict)
        }
    return {}


def materialize_system_skill_credentials(
    source_skill: str, target: Path, *, include_secret_values: bool = False,
) -> dict[str, Any]:
    spec = next(
        (value for value in FOUNDATION_SPECS.values() if value.get("source_skill") == source_skill), {}
    )
    bindings = spec.get("credential_bindings", {}) if isinstance(spec, dict) else {}
    stored = platform_skill_secrets().get(source_skill, {})
    fields: list[dict[str, Any]] = []
    for environment_key, binding in bindings.items():
        relative_path, json_key = binding
        config_path = target / relative_path
        config = load_json(config_path, 128 * 1024)
        source_value = str(config.get(json_key, "") or "").strip()
        environment_value = str(os.environ.get(environment_key, "") or "").strip()
        stored_value = str(stored.get(environment_key, "") or "").strip()
        if include_secret_values:
            value = environment_value or stored_value or source_value
            origin = "environment" if environment_value else "platform_secret_store" if stored_value else "source_config" if source_value else "missing"
            if value and value != source_value:
                config[json_key] = value
                atomic_json(config_path, config)
            exported = bool(value)
        else:
            # A platform credential may be available to the generator, but it
            # must never cross the packaging boundary into a third-party Skill.
            # Keep public defaults intact and let the target environment provide
            # the same named variable at runtime.
            value = environment_value or stored_value or source_value
            origin = (
                "environment_not_exported" if environment_value
                else "platform_secret_store_not_exported" if stored_value
                else "source_config_not_exported" if source_value
                else "missing"
            )
            if source_value:
                config[json_key] = ""
                atomic_json(config_path, config)
            exported = False
        fields.append({
            "environment_key": environment_key,
            "config_path": relative_path,
            "config_key": json_key,
            "configured": bool(value),
            "origin": origin,
            "exported": exported,
        })
    return {
        "policy": (
            "preserve_and_materialize_without_redaction"
            if include_secret_values else "preserve_public_defaults_externalize_credentials"
        ),
        "source_skill": source_skill,
        "fields": fields,
        "all_required_credentials_configured": all(item["configured"] for item in fields),
        "credentials_exported": any(item["exported"] for item in fields),
    }


def inherited_resource_inventory(source: Path, target: Path) -> list[str]:
    resources = []
    for path in sorted(item for item in source.rglob("*") if item.is_file()):
        relative = path.relative_to(source)
        if "__pycache__" in relative.parts or path.suffix.casefold() in {".pyc", ".pyo"}:
            continue
        if relative.as_posix() in {"SKILL.md", "agents/openai.yaml"}:
            continue
        if not (target / relative).is_file():
            raise ContractError(f"定制 Skill 缺少来源资源：{relative.as_posix()}")
        resources.append(relative.as_posix())
    return resources


def copy_customized_system_skill(
    target: Path, item: dict[str, Any], scenario_name: str,
) -> dict[str, Any]:
    source_skill = str(FOUNDATION_SPECS[item["kind"]].get("source_skill", ""))
    source = Path(__file__).resolve().parents[2] / source_skill
    if not source.is_dir():
        raise ContractError(f"缺少需定制的系统 Skill：{source_skill}")
    if not (source / "scripts").is_dir() or not any((source / "scripts").glob("*.py")):
        raise ContractError(f"系统 Skill {source_skill} 缺少可执行 scripts")
    shutil.copytree(source, target, ignore=_source_copy_ignore)
    if item["kind"] == "knowledge":
        wrapper = Path(__file__).resolve().parents[1] / "assets" / "portable-knowledge-wrapper" / "scripts" / "scenario_kb.py"
        shutil.copy2(wrapper, target / "scripts" / "scenario_kb.py")
    credential_status = materialize_system_skill_credentials(
        source_skill, target, include_secret_values=False
    )
    resources = inherited_resource_inventory(source, target)
    atomic_json(target / "references" / "scenario_binding.json", {
        "schema_version": 1,
        "scenario": scenario_name,
        "formats": item.get("formats", []),
        "file_roles": item.get("file_roles", []),
        "system_roles": item.get("system_roles", []),
        "runtime_overrides": {},
        "source_skill": source_skill,
        "inherited_resources": resources,
    })
    return {
        "source_skill": source_skill,
        "source_skill_digest": tree_digest(source),
        "inherited_resources": resources,
        "credential_status": credential_status,
    }


def infer_design_time_output_templates(
    relation_path: Path, relations: dict[str, Any], operational: dict[str, Any],
) -> list[dict[str, Any]]:
    """Retain output shape/provenance without turning a historical result into runtime input."""
    cards_path = relation_path.parent / "evidence-cards.json"
    catalog_path = relation_path.parent / "_field-evidence" / "catalog.json"
    if not cards_path.is_file() or not catalog_path.is_file():
        return []
    try:
        cards_payload = load_json(cards_path, MAX_EVIDENCE_BYTES)
        catalog = load_json(catalog_path, MAX_EVIDENCE_BYTES)
    except ContractError:
        return []
    cards = {
        str(item.get("id", "")): item
        for item in cards_payload.get("cards", [])
        if isinstance(item, dict) and str(item.get("id", ""))
    }
    output_evidence_ids = {
        str(evidence_id)
        for node in relations.get("nodes", [])
        if isinstance(node, dict) and str(node.get("type", "")) == "output"
        for evidence_id in node.get("evidence_ids", [])
        if str(evidence_id)
    }
    candidate_files: dict[str, set[str]] = defaultdict(set)
    for evidence_id in output_evidence_ids:
        card = cards.get(evidence_id, {})
        for source in card.get("sources", []) if isinstance(card.get("sources"), list) else []:
            if isinstance(source, dict) and source.get("file"):
                candidate_files[str(source["file"])].add(evidence_id)
    runtime_paths = {
        str(source.get("path", ""))
        for source in operational.get("sources", [])
        if isinstance(source, dict) and source.get("runtime_required") is True
    }
    templates: list[dict[str, Any]] = []
    for item in catalog.get("files", []) if isinstance(catalog.get("files"), list) else []:
        if not isinstance(item, dict):
            continue
        relative_path = str(item.get("path", ""))
        if not relative_path or relative_path not in candidate_files or relative_path in runtime_paths:
            continue
        tables = []
        output_columns: list[str] = []
        column_semantics: list[dict[str, Any]] = []
        for table in item.get("tables", []) if isinstance(item.get("tables"), list) else []:
            if not isinstance(table, dict):
                continue
            columns = [
                str(column.get("query_name") or column.get("name"))
                for column in table.get("columns", [])
                if isinstance(column, dict) and str(column.get("query_name") or column.get("name"))
            ]
            output_columns.extend(column for column in columns if column not in output_columns)
            for column in table.get("columns", []) if isinstance(table.get("columns"), list) else []:
                if not isinstance(column, dict):
                    continue
                name = str(column.get("query_name") or column.get("name") or "")
                if not name or any(item.get("column") == name for item in column_semantics):
                    continue
                column_semantics.append({
                    "column": name,
                    "kind": str(column.get("kind", "other")),
                    "semantic_role": infer_column_semantic_role(column),
                })
            tables.append({
                "table": table.get("table_name") or table.get("name"),
                "row_count": table.get("row_count"),
                "column_count": table.get("column_count", len(columns)),
                "columns": columns,
            })
        templates.append({
            "template_id": "output-" + hashlib.sha256(relative_path.encode("utf-8")).hexdigest()[:12],
            "name": Path(relative_path).stem,
            "path": relative_path,
            "format": str(item.get("extension", Path(relative_path).suffix)).lstrip("."),
            "source_kind": item.get("kind", "tabular"),
            "runtime_required": False,
            "original_file_required_at_runtime": False,
            "evidence_ids": sorted(candidate_files[relative_path]),
            "tables": tables,
            "output_columns": output_columns,
            "column_semantics": column_semantics,
            "usage": "Use this only as the final result schema and field naming contract; query current runtime sources for values.",
        })
    return templates


def portable_operational_contract(claims: dict[str, Any]) -> dict[str, Any]:
    source = claims.get("source") if isinstance(claims.get("source"), dict) else {}
    claim = source.get("operational_data_contract") if isinstance(source.get("operational_data_contract"), dict) else {}
    relation_claim = source.get("relations") if isinstance(source.get("relations"), dict) else {}
    path = Path(str(claim.get("artifact", ""))).resolve()
    payload = load_json(path, MAX_OPERATIONAL_BYTES)
    if payload.get("status") != "ready" or sha256_file(path) != claim.get("fingerprint"):
        raise ContractError("数据执行契约未就绪或 fingerprint 已变化")
    relation_path = Path(str(relation_claim.get("artifact", ""))).resolve()
    relations = load_json(relation_path, MAX_SOURCE_BYTES)
    portable = normalize_operational_runtime_contract(relations, payload)
    output_templates = infer_design_time_output_templates(relation_path, relations, portable)
    portable["design_time_output_templates"] = output_templates
    portable["output_contract"] = {
        "template_count": len(output_templates),
        "templates": [
            {
                "template_id": item.get("template_id"),
                "name": item.get("name"),
                "format": item.get("format"),
                "columns": item.get("output_columns", []),
                "column_semantics": item.get("column_semantics", []),
                "runtime_required": False,
            }
            for item in output_templates
        ],
        "required_result_fields": [
            "business_conclusion", "decision_reason", "scope_or_measure",
            "rule_provenance", "data_provenance", "coverage", "uncertainties",
        ],
        "policy": "Historical result files define structure only; never use them as current business evidence.",
    }
    portable["trace_evidence"] = compact_trace_evidence(portable, include_rows=False)
    upstream = portable.get("source") if isinstance(portable.get("source"), dict) else {}
    portable["source"] = {
        "field_evidence_fingerprint": upstream.get("field_evidence_fingerprint", ""),
        "portable_copy_of_fingerprint": claim.get("fingerprint", ""),
    }
    portable["runtime_binding"] = {
        "data_root": "provided_by_third_party_at_runtime",
        "source_paths": "contract_relative_or_source_id_override_relative_to_data_root",
        "override_cli": "--bind <source-id>=<relative-path>",
        "content_identity": "not_required_for_runtime_inputs",
        "schema_compatibility": "required_for_each_referenced_runtime_input",
        "design_time_templates": "schema_metadata_only; original files are not runtime dependencies",
        "indexes": "created_by_foundation_skills_in_runtime_writable_storage",
    }
    return portable


def portable_delivery_contract(
    claims: dict[str, Any], flow_contract: dict[str, Any], operational: dict[str, Any],
) -> dict[str, Any]:
    """Describe the single-request transaction that a third-party Agent must complete.

    The capability model already carries stages and data-access facts, but it
    did not give an external host one compact answer to three practical
    questions: what can be supplied at runtime, what evidence comes back from
    the first call, and what a finished business response must contain.  Keep
    this contract data-only and domain-neutral so it is valid for a tabular,
    document, OCR, or mixed scenario without embedding an example scenario.
    """

    sources = [item for item in operational.get("sources", []) if isinstance(item, dict)]
    runtime_sources = [
        item for item in sources
        if item.get("runtime_required") is True and str(item.get("source_id", ""))
    ]
    sources_by_kind: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for source in runtime_sources:
        kind = str(source.get("kind", "document") or "document")
        sources_by_kind[kind].append(source)

    input_modes = []
    for kind, items in sorted(sources_by_kind.items()):
        source_ids = [str(item.get("source_id", "")) for item in items]
        extensions = sorted({
            str(item.get("extension") or extension_for(str(item.get("path", ""))))
            for item in items
            if str(item.get("extension") or extension_for(str(item.get("path", ""))))
        })
        if kind == "tabular":
            access = "bounded_read_only_sql_and_validated_links"
            evidence = "rows_with_query_digest_and_join_validation"
        else:
            access = "parse_or_ocr_then_provenance_chunk_search"
            evidence = "chunks_with_source_digest_locator_chunk_id_and_text_digest"
        input_modes.append({
            "kind": kind,
            "source_ids": source_ids,
            "extensions": extensions,
            "runtime_binding": "--bind <source-id>=<relative-runtime-file>",
            "access": access,
            "evidence": evidence,
        })

    declared_output = flow_contract.get("output_contract")
    if not isinstance(declared_output, dict):
        declared_output = {}
    required_fields = list(dict.fromkeys([
        *[
            str(value) for value in declared_output.get("required_result_fields", [])
            if str(value)
        ],
        "business_conclusion",
        "decision_reason",
        "scope_or_measure",
        "rule_provenance",
        "data_provenance",
        "coverage",
        "uncertainties",
    ]))
    return {
        "schema_version": 1,
        "contract_kind": "portable_business_request_transaction",
        "scenario": claims.get("scenario", flow_contract.get("scenario", {})),
        "entrypoint": {
            "command": "execute",
            "policy": "one_primary_execution_per_new_complete_business_request",
            "successor_policy": "read_agent_handoff_once_then_deliver_or_report_the_named_blocker",
            "follow_up_policy": "reuse_result_handle_with_continue_when_the_result_is_complete",
            "host_response_policy": "preserve_structured_transaction_fields_instead_of_converting_to_a_generic_row_count_message",
            "required_host_response_fields": [
                "status", "selected_rule", "candidate_evidence", "deterministic_result",
                "agent_handoff", "next_step", "artifact",
            ],
        },
        "runtime_input_contract": {
            "modes": input_modes,
            "design_time_templates": "schema_metadata_only_not_runtime_inputs",
            "schema_compatibility": "validate_only_sources_referenced_by_the_current_transaction",
        },
        "evidence_contract": {
            "structured": "bounded_rows_with_source_and_query_provenance",
            "unstructured": "parsed_or_ocr_text_chunks_with_source_digest_locator_chunk_id_and_text_digest",
            "merge_policy": "merge_structured_and_unstructured_evidence_only_when_an_accepted_business_key_or_explicit_link_exists",
            "raw_content_policy": "do_not_place_full_large_tables_or_full_documents_in_agent_context",
        },
        "delivery_contract": {
            "required_fields": required_fields,
            "response_order": [
                "business_conclusion",
                "decision_reason",
                "scope_or_measure",
                "rule_provenance",
                "data_provenance",
                "coverage",
                "uncertainties",
            ],
            "declared_output_templates": declared_output.get("templates", []),
            "full_result_policy": "export_only_when_explicitly_requested_and_return_the_artifact_reference_not_all_rows",
        },
        "terminal_statuses": {
            "completed_deterministically": "deliver_deterministic_result_without_requery",
            "ready_for_agent_judgment": "apply_the_accepted_flow_to_the_handoff_evidence_once_then_fill_delivery_contract",
            "blocked_rule_not_found": "request_the_missing_or_disambiguating_rule_information",
            "blocked_rule_selection_required": "ask_the_user_to_select_or_narrow_the_complete_governing_record",
            "blocked_missing_or_incompatible_sources": "report_the_named_source_or_schema_gap",
            "blocked_ocr_required": "run_the_declared_ocr_capability_once_and_rebind_its_json_output",
            "blocked_uncompiled_rule_family": "report_that_no_reviewed_deterministic_recipe_covers_the_selected_rule_family",
        },
    }


def render_agent_prompts(
    claims: dict[str, Any], flow: dict[str, Any], operational: dict[str, Any], skills: list[dict[str, Any]],
    delivery_contract: dict[str, Any],
) -> str:
    scenario = claims.get("scenario", {})
    orchestrator = claims["orchestrator"]
    foundations = [item for item in skills if item.get("kind") == "foundation"]
    main_executors = [item for item in skills if item.get("kind") == "main_executor"]
    foundation_kinds = {str(item.get("foundation_kind", "")) for item in foundations}
    stages = [item for item in claims.get("stage_skills", []) if isinstance(item, dict)]
    source_lines = []
    for source in operational.get("sources", []):
        if not isinstance(source, dict):
            continue
        roles = format_list(
            [str(role.get("node_name", "")) for role in source.get("roles", []) if isinstance(role, dict)],
            "待运行时确认",
        )
        lifecycle = str(source.get("lifecycle", "runtime_input"))
        requirement = (
            "当前操作引用时必须绑定运行文件"
            if source.get("runtime_required") is True
            else "仅保留设计元数据，运行时不需要原文件"
        )
        source_lines.append(
            f"- `{source.get('source_id', '')}`（`{source.get('path', '')}`，{source.get('kind', '')}，"
            f"角色：{roles}，生命周期 `{lifecycle}`，{requirement}）："
            f"访问方式 `{source.get('content_retrieval', {}).get('mode', source.get('access_policy', ''))}`。"
        )
    stage_lines = [
        f"- `{item.get('skill_name', '')}`：当需要“{item.get('display_name', '')}”时调用；"
        f"负责产出“{item.get('outcome', '')}”。"
        for item in stages
    ]
    foundation_lines = [
        f"- `{item.get('name', '')}`：{item.get('foundation_kind', '')} 基础能力；"
        f"支持 {format_list(item.get('formats', []), '场景声明的外部系统能力')}。"
        for item in foundations
    ]
    open_questions = [
        f"- {item.get('question', '')}（影响：{item.get('impact', '')}）"
        for item in flow.get("open_questions", []) if isinstance(item, dict)
    ] or ["- 当前没有已声明的开放问题；运行时发现契约缺口仍须停止并说明。"]
    input_modes = delivery_contract.get("runtime_input_contract", {}).get("modes", [])
    input_mode_lines = [
        f"- `{item.get('kind', '')}`：来源 {format_list(item.get('source_ids', []))}；"
        f"格式 {format_list(item.get('extensions', []), '由来源契约声明')}；"
        f"访问 `{item.get('access', '')}`；证据 `{item.get('evidence', '')}`。"
        for item in input_modes if isinstance(item, dict)
    ] or ["- 当前没有已验收的运行时输入；只可报告该契约缺口。"]
    delivery = delivery_contract.get("delivery_contract", {})
    delivery_fields = format_list(delivery.get("required_fields", []), "业务结论、证据和不确定性")
    lines = [
        "## Third-party execution contract",
        "",
        "For each new complete business request, call the generated main executor exactly once first, with `--output`. Read its `agent_handoff` exactly once and follow `next_step`; do not manually fan out to stage skills, readers, join validators, or SQL tools.",
        "A compact stdout response is successful when it contains an artifact handle. For `ready_for_agent_judgment`, use the handoff evidence for one business-evaluation pass and fill the delivery contract. For `completed_deterministically`, report the deterministic result without re-querying. If the user explicitly requests a file, request `delivery_output` only for a JSON/CSV/XLSX result after `recipe_execution.verified=true`; `--output` itself is always the audit evidence package.",
        "Use another tool only when `next_step` names a concrete missing field, relationship, or OCR recovery. Never blind-retry the same executor request. If coverage is bounded, disclose the limit and never call the preview exhaustive.",
        f"# {scenario.get('name', '')} Agent 系统提示词", "",
        f"你是“{scenario.get('name', '')}”业务 Agent。你的目标是：{scenario.get('purpose', flow.get('scenario', {}).get('business_outcome', '完成场景业务目标'))}。",
        "你只负责理解用户意图、依据主执行器返回的完整规则/有界证据进行一次业务判断，并按交付契约组织结果。文件解析、OCR、索引、大表扫描、连接验证和 SQL 执行必须交给已安装 Skill；禁止把原始大文件或整篇文档直接读入上下文。", "",
        "禁止临时创建 Python、SQL 执行器、HTTP 客户端或文件解析脚本。完整请求优先走主执行器的单事务入口；总控与阶段 Skill 仅用于主执行器明确降级、用户明确只要求单阶段，或修复已命名的证据缺口。", "",
        "## 已安装能力", "",
        *[
            f"- **首选端到端入口**：`{item.get('name', '')}`。完整业务请求必须先调用其 `execute`；一次返回规则、结构化/非结构化证据、关联校验（适用时）和交付 handoff。"
            for item in main_executors
        ],
        f"- 场景总控：`{orchestrator.get('skill_name', '')}`。仅在主执行器阻塞、用户明确指定阶段或需要降级调试时调用。",
        *stage_lines,
        *foundation_lines,
        "", "## 数据来源契约", "", *source_lines,
        "", "所有基础 Skill 都携带 `references/operational-data-contract.json`；主执行器另携带 `references/delivery-contract.json`。运行时由调用方提供 `<data-root>`；文件名变化时只能用 `--bind <source-id>=<relative-path>` 显式绑定。`design_time_template` 只提供输出字段/类型/格式约束，缺少其历史原文件不是运行阻塞。", "",
        "## 本次事务可接受的输入", "", *input_mode_lines,
        "", "## 单事务执行顺序", "",
        "1. 对新的完整业务请求：调用主执行器 `execute --request ... --data-root ... --output ...` 一次。不要先执行 `describe`、规则搜索、文档索引、`validate-join`、`query`、总控或阶段 Skill。",
        "2. 读取生成的 `agent_handoff` 一次，并按 `status` 行动：`completed_deterministically` 直接交付 `deterministic_result`；`ready_for_agent_judgment` 仅基于 handoff 的完整规则与证据执行一次业务判断；`blocked_rule_not_found` / `blocked_rule_selection_required` / `blocked_missing_or_incompatible_sources` 只报告具名缺口或向用户索取消歧条件。",
        "3. `blocked_ocr_required` 是唯一的文档恢复路径：调用已声明 OCR Skill 一次生成 JSON，将 JSON 绑定到同一 source_id，再作为新的恢复事务调用主执行器一次。未命中、歧义、缺字段或连接异常不是重试理由。",
        "4. 只有 `next_step.query_allowed_only_if` 指出具体缺失字段或关系时，才做一次有界补充查询；查询必须使用已声明的来源、键组和只读入口。用户明确要求结果文件时，只有已验证的确定性结果可用 `delivery_output` 生成 JSON/CSV/XLSX；模板列必须由 recipe 直接提供。任何待 Agent 判断、OCR 阻塞或未验证结果只交付证据/阻塞说明，不能伪造结果文件。",
        "5. 非结构化证据必须保留 source_digest、locator、chunk_id 和 text_digest；没有已验收业务键或显式关系时，不得与结构化记录强行合并。外部知识仅在完整规则或流程明确要求时调用，服务不可用/零命中时返回 `manual_intervention_required`。", "",
        "## 输出要求", "",
        f"- 必填交付字段：{delivery_fields}。先给业务结论或明确阻塞原因，再给理由、范围、规则/数据证据、覆盖范围和不确定性。",
        "- 每条判定应能追溯到规则完整行或文档章节、结构化来源/查询与文档/OCR 定位（适用时）。",
        "- 明确列出未匹配、截断、OCR 不确定、连接放大、待确认项和未覆盖范围。",
        "- `--output` 是审计证据包；JSON/CSV/XLSX 结果文件仅从 `completed_deterministically` 且 `recipe_execution.verified=true` 的输出物化。XLSX 仅写入一个无公式的数据工作表，列必须由 recipe 直接提供；历史 DOCX/PDF 模板在没有专用渲染器和已验证字段映射时仅是格式契约，不能假称已生成。",
        "- 除非用户明确要求，不展示大段原文、全量数据或内部执行日志。", "",
        "## 已知待确认边界", "", *open_questions,
        "", "不得声称本提示词或 Skills 能消除现实数据中的全部不确定性；质量门禁失败时，正确行为是阻断并给出可修复的证据缺口。", "",
    ]
    return "\n".join(lines)


def tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(item for item in root.rglob("*") if item.is_file() and "__pycache__" not in item.parts):
        relative = path.relative_to(root).as_posix()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def python_dependencies(skill_root: Path) -> list[str]:
    requirements = skill_root / "requirements.txt"
    if not requirements.is_file():
        return []
    return sorted({
        line.strip()
        for line in requirements.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    })


def merge_requirement_files(target: Path, *sources: Path) -> None:
    """Merge portable runtime requirements without making a sibling Skill a dependency.

    The primary executor is intentionally installable by itself.  A generated
    package may route a single request through both its table and document
    adapters, so copying one template's requirements over the other would make
    a hybrid package pass generation but fail only after installation.  Keep
    the file deterministic and retain comments only from neither source: the
    manifest uses the normalized dependency set as its portable contract.
    """

    lines: set[str] = set()
    if target.is_file():
        lines.update(
            line.strip()
            for line in target.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        )
    for source in sources:
        if not source.is_file():
            raise ContractError(f"Missing portable runtime requirements: {source}")
        lines.update(
            line.strip()
            for line in source.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        )
    atomic_text(target, "\n".join(sorted(lines)) + ("\n" if lines else ""))


def validate_generated_skills(skills_root: Path) -> list[str]:
    errors: list[str] = []
    names: set[str] = set()
    for skill_dir in sorted(item for item in skills_root.iterdir() if item.is_dir()):
        skill_file = skill_dir / "SKILL.md"
        if not skill_file.is_file():
            errors.append(f"{skill_dir.name} 缺少 SKILL.md")
            continue
        text = skill_file.read_text(encoding="utf-8")
        match = re.match(r"\A---\r?\n(.*?)\r?\n---\r?\n", text, re.DOTALL)
        if not match:
            errors.append(f"{skill_dir.name}/SKILL.md 缺少合法 frontmatter")
            continue
        name_match = re.search(r"(?m)^name:\s*([^\r\n]+)$", match.group(1))
        description_match = re.search(r"(?m)^description:\s*(.+)$", match.group(1))
        name = str(name_match.group(1)).strip() if name_match else ""
        if name != skill_dir.name:
            errors.append(f"{skill_dir.name}/SKILL.md name 必须匹配目录")
        if name in names:
            errors.append(f"生成 Skill 名称重复：{name}")
        names.add(name)
        if not description_match or len(str(description_match.group(1)).strip()) < 20:
            errors.append(f"{skill_dir.name}/SKILL.md description 过短")
        scripts_root = skill_dir / "scripts"
        scripts = sorted(scripts_root.glob("*.py")) if scripts_root.is_dir() else []
        executor_entrypoint = scripts_root / "execute_scenario.py"
        stage_entrypoint = scripts_root / "run_stage.py"
        orchestrator_entrypoint = scripts_root / "orchestrate.py"
        if executor_entrypoint.is_file():
            try:
                contract = load_json(
                    skill_dir / "references" / "operational-data-contract.json", MAX_OPERATIONAL_BYTES
                )
                flow_contract = load_json(
                    skill_dir / "references" / "flow-contract.json", MAX_CANDIDATE_BYTES
                )
                delivery_contract = load_json(
                    skill_dir / "references" / "delivery-contract.json", MAX_CANDIDATE_BYTES
                )
                recipe_catalog_path = skill_dir / "references" / "compiled-recipes.json"
                recipe_runtime_verification = load_json(
                    skill_dir / "references" / "recipe-verification.json", MAX_CANDIDATE_BYTES
                )
                if contract.get("status") != "ready":
                    errors.append(f"{skill_dir.name}/references/operational-data-contract.json is not ready")
                if not str(flow_contract.get("execution_mode", "")).strip():
                    errors.append(f"{skill_dir.name}/references/flow-contract.json is missing execution_mode")
                if not isinstance(flow_contract.get("main_flow"), list):
                    errors.append(f"{skill_dir.name}/references/flow-contract.json is missing main_flow")
                if (
                    delivery_contract.get("contract_kind") != "portable_business_request_transaction"
                    or delivery_contract.get("entrypoint", {}).get("command") != "execute"
                    or not isinstance(delivery_contract.get("delivery_contract", {}).get("required_fields"), list)
                ):
                    errors.append(f"{skill_dir.name}/references/delivery-contract.json is invalid")
                if (
                    recipe_runtime_verification.get("kind") != "portable_recipe_runtime_verification"
                    or recipe_runtime_verification.get("recipe_catalog_fingerprint") != sha256_file(recipe_catalog_path)
                    or not isinstance(recipe_runtime_verification.get("verified_recipe_ids"), list)
                ):
                    errors.append(f"{skill_dir.name}/references/recipe-verification.json is invalid or unbound")
            except ContractError as exc:
                errors.append(str(exc))
        elif stage_entrypoint.is_file():
            try:
                contract = load_json(skill_dir / "references" / "contract.json", MAX_CANDIDATE_BYTES)
                if not str(contract.get("stage_id", "")).strip():
                    errors.append(f"{skill_dir.name}/references/contract.json is missing stage_id")
                # A preparation/closure stage may intentionally hand off an
                # internal state without owning a material output node.  The
                # empty list is an explicit contract; only a missing or
                # malformed field is a generation error.
                if not isinstance(contract.get("output_contract"), list):
                    errors.append(f"{skill_dir.name}/references/contract.json is missing output_contract")
            except ContractError as exc:
                errors.append(str(exc))
        elif orchestrator_entrypoint.is_file():
            try:
                routing = load_json(
                    skill_dir / "references" / "capability-routing.json", MAX_CANDIDATE_BYTES
                )
                if not isinstance(routing.get("main_flow"), list) or not isinstance(routing.get("routing"), list):
                    errors.append(f"{skill_dir.name}/references/capability-routing.json is missing main_flow/routing")
            except ContractError as exc:
                errors.append(str(exc))
        else:
            try:
                operational = load_json(
                    skill_dir / "references" / "operational-data-contract.json", MAX_CANDIDATE_BYTES
                )
                if operational.get("status") != "ready":
                    errors.append(f"{skill_dir.name}/references/operational-data-contract.json is not ready")
            except ContractError as exc:
                errors.append(str(exc))
        if not scripts:
            errors.append(f"{skill_dir.name} 缺少 scripts/*.py 可执行入口；不得让第三方 Agent 临时编写脚本")
        if "scripts/" not in text:
            errors.append(f"{skill_dir.name}/SKILL.md 未声明包内 scripts 调用入口")
        for script in scripts:
            try:
                ast.parse(script.read_text(encoding="utf-8"), filename=str(script))
            except (OSError, UnicodeError, SyntaxError) as exc:
                errors.append(f"{script.relative_to(skills_root)} 不是合法 Python：{exc}")
        for path in skill_dir.rglob("*"):
            if not path.is_file() or path.suffix.casefold() not in {".md", ".yaml", ".yml", ".json", ".txt", ".py"}:
                continue
            content = path.read_text(encoding="utf-8", errors="replace")
            for forbidden, meaning in FORBIDDEN_PORTABLE_TEXT.items():
                if forbidden.casefold() in content.casefold():
                    errors.append(f"{path.relative_to(skills_root)} 含平台耦合内容 {forbidden}（{meaning}）")
    return errors


def write_capability_map(claims: dict[str, Any], path: Path) -> None:
    capability_by_id = {
        str(item["id"]): item
        for item in [*claims["foundation_skills"], *claims["stage_skills"]]
    }
    lines = ["flowchart LR"]
    orchestrator = claims["orchestrator"]
    executor_name = scenario_executor_skill_name(str(claims["bundle"]["name"]))
    lines.append(f'    main_executor["{compact(executor_name, 80)}\\n首选端到端入口"]')
    lines.append(f'    orchestrator["{compact(orchestrator["display_name"], 80).replace(chr(34), chr(39))}"]')
    lines.append("    main_executor -.-> orchestrator")
    for item in claims["foundation_skills"]:
        lines.append(f'    {item["id"].replace("-", "_")}{{{{"{compact(item["display_name"], 70)}"}}}}')
    for item in claims["stage_skills"]:
        node_id = item["id"].replace("-", "_")
        lines.append(f'    {node_id}["{compact(item["display_name"], 70)}"]')
        lines.append(f'    orchestrator -.-> {node_id}')
        for foundation_id in item["foundation_ids"]:
            lines.append(f'    {foundation_id.replace("-", "_")} -.-> {node_id}')
    stage_capability_by_stage = {item["stage_id"]: item["id"].replace("-", "_") for item in claims["stage_skills"]}
    for source, target in zip(orchestrator["main_flow"], orchestrator["main_flow"][1:]):
        lines.append(f'    {stage_capability_by_stage[source]} --> {stage_capability_by_stage[target]}')
    atomic_text(path, "\n".join(lines) + "\n")


def write_report(manifest: dict[str, Any], path: Path) -> None:
    lines = [
        f"# {manifest['scenario']['name']}：Skill 能力蒸馏报告", "",
        "## 结果", "",
        f"- 能力包源码名称：`{manifest['bundle']['name']}`",
        f"- 目标：{manifest['bundle']['target_agents']}",
        f"- 总 Skill 数：{manifest['skill_count']}",
        f"- 主执行 Skill：`{manifest.get('main_executor_skill', '')}`（完整请求首选入口）",
        f"- 通用能力模型：`{manifest.get('artifacts', {}).get('capability_model', '')}`（来源角色、字段语义、历史追踪蓝图、流程和输出契约）",
        f"- 流程阶段 Skill：{manifest['stage_skill_count']}（与流程节点 1:1）",
        f"- 基础文件 Skill：{manifest['foundation_skill_count']}",
        "- 第三方 Agent 提示词：`agent_prompts.md`（可直接复制为系统提示词）",
        "- 当前产物是待最终打包验收的可移植源码，不是安装包。", "",
        "## Skill 清单", "",
        "| Skill | 类型 | 对应阶段/基础能力 | 依赖 |",
        "|---|---|---|---|",
    ]
    for item in manifest["skills"]:
        lines.append(
            f"| `{item['name']}` | {item['kind']} | {item.get('stage_id') or item.get('foundation_kind') or '场景编排'} | "
            f"{format_list(item.get('depends_on', []))} |"
        )
    lines.extend(["", "## 文件格式覆盖", ""])
    for item in manifest["file_inventory"]:
        lines.append(
            f"- `{item['path']}`（{item.get('extension') or '未知格式'}）："
            f"{format_list(item.get('categories', []), '未支持')}；业务角色 {format_list(item.get('relation_roles', []), '待运行时确认')}。"
        )
    runtime = manifest.get("runtime_requirements", {})
    lines.extend(["", "## 运行要求", ""])
    lines.append(f"- Python：`{runtime.get('python', '未声明')}`")
    lines.append(f"- Python 依赖：{format_list(runtime.get('python_dependencies', []), '无')}。")
    for service in runtime.get("external_services", []):
        lines.append(
            f"- `{service.get('skill', '')}` 需要外部 {service.get('kind', 'service')}；"
            f"配置策略 `{service.get('configuration_policy', '')}`；凭据字段 "
            f"{format_list(service.get('credential_fields', []), '无')}；"
            f"当前已配置：{bool(service.get('credentials_configured'))}。"
        )
    lines.extend(["", "## 可移植性", ""])
    lines.extend([
        "- 生成 Skill 仅使用相对资源路径，不依赖原平台 Tool、挂载目录或持久会话。",
        "- 定制系统 Skill 完整继承公开配置字段和值；秘密字段保持空值并由第三方运行环境通过同名环境变量注入，manifest、报告和提示词不回显其值。",
        "- 每个 Skill 都携带稳定 scripts 入口；阶段工作单与总控状态机阻止第三方 Agent 临时拼写执行脚本。",
        "- 阶段 Skill 的输入、输出、控制、交接和待确认项均来自已验收上游。",
        "- 最终发布前仍需由独立 package-business-skill 校验、版本化和打包。", "",
    ])
    atomic_text(path, "\n".join(lines))


def generate_bundle(
    claims: dict[str, Any], flow: dict[str, Any], output_root: Path, relation_fingerprint: str,
    flow_fingerprint: str,
) -> dict[str, Any]:
    staging = output_root / ".skills-staging"
    if staging.exists():
        if not staging.resolve().is_relative_to(output_root.resolve()):
            raise ContractError("暂存目录逃逸输出根目录")
        shutil.rmtree(staging)
    staging.mkdir(parents=True)
    scenario_name = str(claims["scenario"]["name"])
    foundation_by_id = {str(item["id"]): item for item in claims["foundation_skills"]}
    stage_by_id = {str(item["id"]): item for item in claims["stage_skills"]}
    capability_by_id = {**foundation_by_id, **stage_by_id}
    foundation_names = {identifier: str(item["skill_name"]) for identifier, item in foundation_by_id.items()}
    ocr_skill_name = foundation_names.get("foundation-ocr", "")
    skill_manifest: list[dict[str, Any]] = []
    operational = portable_operational_contract(claims)
    flow_contract = portable_flow_contract(claims, flow, operational)
    delivery_contract = portable_delivery_contract(claims, flow_contract, operational)
    recipe_catalog = compiled_recipe_catalog(output_root, operational)
    refresh_recipe_replay_contract(output_root, flow, operational)
    executor_name = scenario_executor_skill_name(str(claims["bundle"]["name"]))
    if executor_name in {str(item["skill_name"]) for item in [*claims["foundation_skills"], *claims["stage_skills"]]}:
        raise ContractError(f"生成的主执行 Skill 名称冲突：{executor_name}")

    executor_root = staging / executor_name
    copy_template("portable-scenario-executor", executor_root)
    install_primary_executor_runtimes(executor_root, claims, operational)
    atomic_json(executor_root / "references" / "operational-data-contract.json", operational)
    atomic_json(executor_root / "references" / "flow-contract.json", flow_contract)
    atomic_json(executor_root / "references" / "delivery-contract.json", delivery_contract)
    execution_plan = flow_contract.get("execution_plan", {})
    atomic_json(
        executor_root / "references" / "capability-model.json",
        execution_plan.get("capability_model", {}),
    )
    atomic_json(executor_root / "references" / "execution-plan.json", execution_plan)
    copy_compiled_recipe_catalog(
        output_root, executor_root / "references" / "compiled-recipes.json", recipe_catalog,
    )
    replay_runtime_artifacts = {
        "catalog": executor_root / "references" / "compiled-recipes.json",
        "executor": executor_root / "scripts" / "execute_scenario.py",
        "runtime_contract": executor_root / "references" / "operational-data-contract.json",
        "flow_contract": executor_root / "references" / "flow-contract.json",
    }
    recipe_verification = compiled_recipe_verification(
        output_root, flow, operational, replay_runtime_artifacts,
    )
    recipe_runtime_verification = portable_recipe_verification_contract(
        executor_root / "references" / "compiled-recipes.json",
        recipe_catalog,
        recipe_verification,
    )
    atomic_json(
        executor_root / "references" / "recipe-verification.json", recipe_runtime_verification
    )
    atomic_json(
        executor_root / "references" / "dispatch-config.json",
        execution_plan.get("dispatch_config", {}),
    )
    atomic_json(
        executor_root / "references" / "output-specs.json",
        {"outputs": execution_plan.get("output_specs", [])},
    )
    atomic_text(
        executor_root / "SKILL.md",
        render_executor_skill(claims, flow_contract, operational, executor_name),
    )
    write_skill_metadata(
        executor_root,
        executor_name,
        f"{claims['scenario']['name']}主执行器",
        f"端到端执行{claims['scenario']['name']}：规则定位、数据检索、关联校验、证据汇总和可追溯交付。",
    )
    skill_manifest.append({
        "name": executor_name,
        "kind": "main_executor",
        "role": "primary_end_to_end_executor",
        "priority": "first",
        "path": f"skills/{executor_name}",
        "depends_on": list(foundation_names.values()),
        "python_dependencies": python_dependencies(executor_root),
        "executables": sorted(
            path.relative_to(executor_root).as_posix()
            for path in (executor_root / "scripts").glob("*.py")
        ),
        "execution_modes": ["describe", "search-rules", "produce", "execute", "query"],
        "reference_contracts": [
            "references/capability-model.json",
            "references/delivery-contract.json",
            "references/execution-plan.json",
            "references/compiled-recipes.json",
            "references/recipe-verification.json",
            "references/operational-data-contract.json",
            "references/flow-contract.json",
        ],
    })

    for item in claims["foundation_skills"]:
        skill_root = staging / item["skill_name"]
        source_metadata: dict[str, Any] = {}
        if item["kind"] == "tabular":
            copy_template("portable-tabular-reader", skill_root)
            content = render_tabular_skill(item, scenario_name, operational)
        elif item["kind"] == "document":
            copy_template("portable-document-reader", skill_root)
            content = render_document_skill(item, scenario_name, ocr_skill_name, operational)
        elif item["kind"] == "ocr":
            source_metadata = copy_customized_system_skill(skill_root, item, scenario_name)
            content = render_ocr_skill(item, scenario_name)
        elif item["kind"] == "knowledge":
            source_metadata = copy_customized_system_skill(skill_root, item, scenario_name)
            content = render_knowledge_skill(item, scenario_name)
        else:
            raise ContractError(f"未知基础能力类型：{item['kind']}")
        atomic_json(skill_root / "references" / "operational-data-contract.json", operational)
        atomic_text(skill_root / "SKILL.md", content)
        write_skill_metadata(skill_root, item["skill_name"], item["display_name"], item["description"])
        skill_manifest.append({
            "name": item["skill_name"],
            "kind": "foundation",
            "foundation_kind": item["kind"],
            "path": f"skills/{item['skill_name']}",
            "depends_on": [],
            "formats": item["formats"],
            "python_dependencies": python_dependencies(skill_root),
            "executables": sorted(path.relative_to(skill_root).as_posix() for path in (skill_root / "scripts").glob("*.py")),
            **source_metadata,
        })

    for item in claims["stage_skills"]:
        skill_root = staging / item["skill_name"]
        copy_template("portable-stage-runtime", skill_root)
        atomic_text(skill_root / "SKILL.md", render_stage_skill(item, scenario_name, foundation_names, flow))
        write_skill_metadata(skill_root, item["skill_name"], item["display_name"], item["description"])
        atomic_json(skill_root / "references" / "contract.json", {
            "schema_version": 1,
            "scenario": scenario_name,
            "stage_id": item["stage_id"],
            "objective": item["objective"],
            "outcome": item["outcome"],
            "input_contract": item["input_contract"],
            "output_contract": item["output_contract"],
            "control_ids": item["control_ids"],
            "predecessor_stage_ids": item["predecessor_stage_ids"],
            "successor_stage_ids": item["successor_stage_ids"],
            "open_question_ids": item["open_question_ids"],
            "execution_contract": item["execution_contract"],
            "foundation_skills": [foundation_names[value] for value in item["foundation_ids"]],
            "procedure": item["procedure"],
        })
        skill_manifest.append({
            "name": item["skill_name"],
            "kind": "stage",
            "stage_id": item["stage_id"],
            "path": f"skills/{item['skill_name']}",
            "depends_on": [foundation_names[value] for value in item["foundation_ids"]],
            "python_dependencies": python_dependencies(skill_root),
            "executables": ["scripts/run_stage.py"],
        })

    orchestrator = claims["orchestrator"]
    orchestrator_root = staging / orchestrator["skill_name"]
    copy_template("portable-orchestrator-runtime", orchestrator_root)
    atomic_text(
        orchestrator_root / "SKILL.md",
        render_orchestrator_skill(orchestrator, claims, capability_by_id, flow),
    )
    write_skill_metadata(
        orchestrator_root, orchestrator["skill_name"], orchestrator["display_name"], orchestrator["description"]
    )
    atomic_json(orchestrator_root / "references" / "capability-routing.json", {
        "schema_version": 1,
        "scenario": scenario_name,
        "main_flow": orchestrator["main_flow"],
        "routing": [
            {
                **route,
                "skill_name": capability_by_id[route["capability_id"]]["skill_name"],
            }
            for route in orchestrator["routing"]
        ],
        "foundations": [
            {"capability_id": identifier, "skill_name": foundation_names[identifier]}
            for identifier in orchestrator["foundation_ids"]
        ],
    })
    skill_manifest.append({
        "name": orchestrator["skill_name"],
        "kind": "orchestrator",
        "path": f"skills/{orchestrator['skill_name']}",
        "depends_on": [item["skill_name"] for item in claims["stage_skills"]] + list(foundation_names.values()),
        "python_dependencies": python_dependencies(orchestrator_root),
        "executables": ["scripts/orchestrate.py"],
    })

    generation_errors = validate_generated_skills(staging)
    if generation_errors:
        raise ContractError("生成 Skill 未通过可移植性校验：" + "；".join(generation_errors))
    target = output_root / "skills"
    safe_replace_directory(target, staging, output_root)
    for item in skill_manifest:
        item["digest"] = tree_digest(target / item["name"])
    agent_prompt_path = output_root / "agent_prompts.md"
    atomic_text(
        agent_prompt_path,
        render_agent_prompts(claims, flow, operational, skill_manifest, delivery_contract),
    )
    agent_prompt_digest = sha256_file(agent_prompt_path)
    release = build_release_bundle(claims, output_root, target, executor_name, skill_manifest)
    release_errors = validate_release_bundle(output_root, executor_name)
    if release_errors:
        raise ContractError("Generated release package failed validation: " + "; ".join(release_errors))
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "generator_contract_version": GENERATOR_CONTRACT_VERSION,
        "status": "complete",
        "generated_at": utc_now(),
        "strategy": "accepted_flow_to_portable_primary_executor_with_stage_fallback",
        "verification": recipe_verification,
        "publication": {
            # The generator can prove replay/recipe completeness, but it is
            # not the human authority that may release an installable package.
            # Platform storage changes this record only after an independent
            # package review; until then these archives are a release
            # candidate, never a publishable capability.
            "status": "pending_human_platform_package_approval",
            "verifiable": True,
            "publishable": False,
            "approval_required": {
                "authority": PLATFORM_APPROVAL_ISSUER,
                "artifact_kind": "capability_package",
                "decision": "pending",
                "review_scope": [
                    "release/release.json",
                    "release/artifacts/skill.zip",
                    "release/artifacts/mcp-stdio.zip",
                ],
            },
        },
        "source": {
            "relation_fingerprint": relation_fingerprint,
            "flow_fingerprint": flow_fingerprint,
            "operational_contract_fingerprint": claims["source"]["operational_data_contract"]["fingerprint"],
        },
        "scenario": claims["scenario"],
        "bundle": claims["bundle"],
        "portability": claims["portability"],
        "runtime_requirements": {
            "python": claims["portability"]["python_requirement"],
            "python_dependencies": sorted({
                dependency
                for item in skill_manifest
                for dependency in item["python_dependencies"]
            }),
            "features": {
                "duckdb_excel_reader": "required_for_large_xlsx_xls_xlsb; verify with preflight-contract",
                "result_export": "csv_or_parquet_via_export-contract",
                "document_index": "sqlite_runtime_writable_storage",
            },
            "external_services": [
                {
                    "skill": item["name"],
                    "kind": "ocr_http_api" if item.get("foundation_kind") == "ocr" else "vector_kb_http_api",
                    "source_skill": item.get("source_skill", ""),
                    "credential_fields": [
                        field.get("environment_key", "")
                        for field in item.get("credential_status", {}).get("fields", [])
                    ],
                    "credentials_configured": bool(
                        item.get("credential_status", {}).get("all_required_credentials_configured")
                    ),
                    "credentials_exported": bool(
                        item.get("credential_status", {}).get("credentials_exported")
                    ),
                    "configuration_policy": "preserve_public_defaults_externalize_credentials",
                }
                for item in skill_manifest
                if item.get("foundation_kind") in {"ocr", "knowledge"}
            ],
        },
        "file_inventory": claims["file_inventory"],
        "unsupported_formats": claims["unsupported_formats"],
        "skill_count": len(skill_manifest),
        "foundation_skill_count": len(claims["foundation_skills"]),
        "stage_skill_count": len(claims["stage_skills"]),
        "main_executor_skill": executor_name,
        "main_executor_skill_count": 1,
        "orchestrator_skill": orchestrator["skill_name"],
        "main_flow": orchestrator["main_flow"],
        "skills": skill_manifest,
        "release": release,
        "artifacts": {
            "skills": "skills",
            "manifest": "capability-manifest.json",
            "report": "distillation-report.md",
            "map": "capability-map.mmd",
            "plan": "capability-plan.json",
            "agent_prompts": "agent_prompts.md",
            "capability_model": f"skills/{executor_name}/references/capability-model.json",
            "delivery_contract": f"skills/{executor_name}/references/delivery-contract.json",
            "recipe_runtime_verification": f"skills/{executor_name}/references/recipe-verification.json",
            "release": "release/release.json",
            "skill_archive": "release/artifacts/skill.zip",
            "mcp_stdio_archive": "release/artifacts/mcp-stdio.zip",
            "mcp_config": "release/mcp/mcp_config.example.json",
        },
        "artifact_digests": {
            "agent_prompts": agent_prompt_digest,
            "skill_archive": release["artifact_digests"]["skill_zip"],
            "mcp_stdio_archive": release["artifact_digests"]["mcp_stdio_zip"],
            "portable_operational_contract": hashlib.sha256(
                (json.dumps(operational, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
            ).hexdigest(),
            "delivery_contract": hashlib.sha256(
                (json.dumps(delivery_contract, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
            ).hexdigest(),
            "recipe_runtime_verification": sha256_file(
                target / executor_name / "references" / "recipe-verification.json"
            ),
        },
    }
    return manifest


def blocked_reliability_manifest(
    claims: dict[str, Any], claims_path: Path, relation_fingerprint: str, flow_fingerprint: str,
    validation: dict[str, Any],
) -> dict[str, Any]:
    """Persist an explicit non-publishable manifest without discarding the candidate."""

    gates = validation.get("reliability_gates") if isinstance(validation.get("reliability_gates"), dict) else {}
    if set(gates) == {"compiled_recipes"}:
        status = "blocked_unverified_recipes"
    elif set(gates) == {"open_questions"}:
        status = "blocked_open_questions"
    else:
        status = "blocked_reliability_gates"
    source = claims.get("source") if isinstance(claims.get("source"), dict) else {}
    operational = source.get("operational_data_contract") if isinstance(source.get("operational_data_contract"), dict) else {}
    return {
        "schema_version": SCHEMA_VERSION,
        "generator_contract_version": GENERATOR_CONTRACT_VERSION,
        "status": status,
        "generated_at": utc_now(),
        "scenario": claims.get("scenario", {}),
        "bundle": claims.get("bundle", {}),
        "source": {
            "relation_fingerprint": relation_fingerprint,
            "flow_fingerprint": flow_fingerprint,
            "operational_contract_fingerprint": operational.get("fingerprint", ""),
        },
        "verification": {
            "status": "unverified",
            "verifiable": False,
            "publishable": False,
            "recipe_coverage": validation.get("recipe_verification"),
        },
        "publication": {
            "status": "blocked",
            "verifiable": False,
            "publishable": False,
        },
        "candidate": {
            "claims": str(claims_path),
            "preserved": True,
        },
        "reliability_gates": gates,
        "next_action": "Resolve every reliability gate, then rerun preflight and finalize with the same candidate path.",
    }


def compact_summary(manifest: dict[str, Any], offset: int, limit: int) -> dict[str, Any]:
    skills = manifest.get("skills", [])
    offset = max(0, offset)
    limit = max(1, min(limit, 30))
    return {
        "schema_version": manifest.get("schema_version"),
        "generator_contract_version": manifest.get("generator_contract_version"),
        "status": manifest.get("status"),
        "scenario": manifest.get("scenario"),
        "bundle": manifest.get("bundle"),
        "skill_count": manifest.get("skill_count"),
        "foundation_skill_count": manifest.get("foundation_skill_count"),
        "stage_skill_count": manifest.get("stage_skill_count"),
        "orchestrator_skill": manifest.get("orchestrator_skill"),
        "runtime_requirements": manifest.get("runtime_requirements", {}),
        "verification": manifest.get("verification", {}),
        "publication": manifest.get("publication", {}),
        "offset": offset,
        "limit": limit,
        "has_more": offset + limit < len(skills),
        "skills": skills[offset : offset + limit],
        "unsupported_formats": manifest.get("unsupported_formats", []),
        "artifacts": manifest.get("artifacts", {}),
    }


def human_package_publication_is_approved(publication: dict[str, Any]) -> bool:
    """Return whether the platform approved a distributable capability package.

    ``verification`` is deliberately not part of this check: it controls
    whether a compiled recipe may return a deterministic business conclusion.
    An evidence-only capability remains safe to review and publish because its
    executor keeps unverified recipes on the human-judgment path.
    """

    return (
        publication.get("verifiable") is True
        and publication.get("status") == "approved"
        and publication.get("publishable") is True
    )


def finalize(args: argparse.Namespace) -> tuple[int, dict[str, Any]]:
    (
        output_root, relation_path, flow_path, relations, flow, relation_fingerprint,
        flow_fingerprint, claims_path, claims,
    ) = candidate_context(args)
    validation = validation_payload(
        claims, relations, flow, output_root, relation_path, flow_path, relation_fingerprint, flow_fingerprint, claims_path
    )
    if validation["status"] != "valid":
        atomic_json(output_root / "validation-errors.json", validation)
        if validation.get("reliability_gates"):
            atomic_json(
                output_root / "capability-manifest.json",
                blocked_reliability_manifest(
                    claims, claims_path, relation_fingerprint, flow_fingerprint, validation
                ),
            )
        return 2, validation
    try:
        manifest = generate_bundle(claims, flow, output_root, relation_fingerprint, flow_fingerprint)
    except ContractError as exc:
        payload = {
            "schema_version": SCHEMA_VERSION,
            "status": "generation_failed",
            "error": str(exc),
            "claims": str(claims_path),
            "candidate_preserved": True,
        }
        atomic_json(output_root / "validation-errors.json", payload)
        return 2, payload
    atomic_json(output_root / "capability-plan.json", claims)
    atomic_json(output_root / "capability-manifest.json", manifest)
    write_capability_map(claims, output_root / "capability-map.mmd")
    write_report(manifest, output_root / "distillation-report.md")
    (output_root / "validation-errors.json").unlink(missing_ok=True)
    return 0, compact_summary(manifest, 0, args.summary_limit)


def brief(args: argparse.Namespace) -> tuple[int, dict[str, Any]]:
    payload = load_json(Path(args.brief).resolve(), MAX_SOURCE_BYTES)
    if payload.get("generator_contract_version") != GENERATOR_CONTRACT_VERSION:
        raise ContractError("distillation-brief.json 的生成器契约版本已过期；必须重新运行 prepare")
    _, relation_path, flow_path, _, _, relation_fingerprint, flow_fingerprint = source_context(args)
    source = payload.get("source") if isinstance(payload.get("source"), dict) else {}
    relation_claim = source.get("relations") if isinstance(source.get("relations"), dict) else {}
    flow_claim = source.get("flow") if isinstance(source.get("flow"), dict) else {}
    if (
        Path(str(relation_claim.get("artifact", ""))).resolve() != relation_path
        or relation_claim.get("fingerprint") != relation_fingerprint
        or Path(str(flow_claim.get("artifact", ""))).resolve() != flow_path
        or flow_claim.get("fingerprint") != flow_fingerprint
    ):
        raise ContractError("distillation-brief.json 的上游 fingerprint 已过期；必须重新运行 prepare")
    return 0, payload


def summary(args: argparse.Namespace) -> tuple[int, dict[str, Any]]:
    manifest_path = Path(args.result).resolve()
    manifest = load_json(manifest_path, MAX_SOURCE_BYTES)
    if manifest.get("status") != "complete":
        raise ContractError("capability-manifest.json 尚未 complete")
    if (manifest_path.parent / "validation-errors.json").exists():
        raise ContractError("能力蒸馏输出目录存在 validation-errors.json；不得交付旧的 complete 能力包")
    if manifest.get("generator_contract_version") != GENERATOR_CONTRACT_VERSION:
        raise ContractError("能力蒸馏产物的生成器契约版本已过期；必须重新 prepare/finalize，禁止交付旧源码")
    publication = manifest.get("publication") if isinstance(manifest.get("publication"), dict) else {}
    if not human_package_publication_is_approved(publication):
        raise ContractError(
            "能力蒸馏产物尚未获得平台人工能力包审批；不得交付源码或发布能力包"
        )
    _, relation_path, _, relations, _, relation_fingerprint, flow_fingerprint = source_context(args)
    source = manifest.get("source") if isinstance(manifest.get("source"), dict) else {}
    if source.get("relation_fingerprint") != relation_fingerprint or source.get("flow_fingerprint") != flow_fingerprint:
        raise ContractError("能力蒸馏产物的上游 fingerprint 已过期；不得交付旧源码")
    operational_path, _, operational_errors = operational_context(relations, relation_path)
    if operational_errors or source.get("operational_contract_fingerprint") != sha256_file(operational_path):
        raise ContractError("能力蒸馏产物的数据执行契约 fingerprint 已过期；不得交付旧源码")
    package_receipt_errors = platform_package_receipt_errors(
        relation_path.parent,
        manifest_path,
        manifest,
    )
    if package_receipt_errors:
        raise ContractError(
            "能力包平台审批回执无效；不得交付源码或发布能力包："
            + "；".join(package_receipt_errors)
        )
    skills_root = Path(args.result).resolve().parent / "skills"
    generation_errors = validate_generated_skills(skills_root) if skills_root.is_dir() else ["缺少 skills 目录"]
    prompt_path = Path(args.result).resolve().parent / "agent_prompts.md"
    artifact_digests = manifest.get("artifact_digests") if isinstance(manifest.get("artifact_digests"), dict) else {}
    if not prompt_path.is_file():
        generation_errors.append("缺少可复制的 agent_prompts.md")
    elif artifact_digests.get("agent_prompts") != sha256_file(prompt_path):
        generation_errors.append("agent_prompts.md 内容摘要已变化")
    artifacts = manifest.get("artifacts") if isinstance(manifest.get("artifacts"), dict) else {}
    delivery_relative = str(artifacts.get("delivery_contract", "")).strip()
    delivery_path = Path(args.result).resolve().parent / delivery_relative if delivery_relative else None
    if delivery_path is None or not delivery_path.is_file():
        generation_errors.append("缺少主执行器 delivery-contract.json")
    elif artifact_digests.get("delivery_contract") != sha256_file(delivery_path):
        generation_errors.append("delivery-contract.json 内容摘要已变化")
    recipe_verification_relative = str(artifacts.get("recipe_runtime_verification", "")).strip()
    recipe_verification_path = (
        Path(args.result).resolve().parent / recipe_verification_relative
        if recipe_verification_relative else None
    )
    if recipe_verification_path is None or not recipe_verification_path.is_file():
        generation_errors.append("缺少主执行器 recipe-verification.json")
    elif artifact_digests.get("recipe_runtime_verification") != sha256_file(recipe_verification_path):
        generation_errors.append("recipe-verification.json 内容摘要已变化")
    for item in manifest.get("skills", []):
        if not isinstance(item, dict):
            generation_errors.append("manifest.skills 包含非对象项")
            continue
        skill_root = skills_root / str(item.get("name", ""))
        if not skill_root.is_dir():
            generation_errors.append(f"manifest 引用的 Skill 不存在：{item.get('name', '')}")
        elif item.get("digest") != tree_digest(skill_root):
            generation_errors.append(f"Skill 内容摘要已变化：{item.get('name', '')}")
        declared_dependencies = sorted(unique_strings(item.get("python_dependencies")))
        actual_dependencies = python_dependencies(skill_root) if skill_root.is_dir() else []
        if declared_dependencies != actual_dependencies:
            generation_errors.append(f"Skill Python 依赖声明与 requirements.txt 不一致：{item.get('name', '')}")
        declared_executables = sorted(unique_strings(item.get("executables")))
        actual_executables = sorted(
            path.relative_to(skill_root).as_posix()
            for path in (skill_root / "scripts").glob("*.py")
        ) if skill_root.is_dir() and (skill_root / "scripts").is_dir() else []
        if declared_executables != actual_executables:
            generation_errors.append(f"Skill scripts 清单与 manifest 不一致：{item.get('name', '')}")
        inherited = unique_strings(item.get("inherited_resources"))
        for relative in inherited:
            if not (skill_root / relative).is_file():
                generation_errors.append(f"Skill 缺少完整继承资源 {relative}：{item.get('name', '')}")
        source_skill = str(item.get("source_skill", ""))
        if source_skill:
            source_root = Path(__file__).resolve().parents[2] / source_skill
            if not source_root.is_dir() or item.get("source_skill_digest") != tree_digest(source_root):
                generation_errors.append(f"来源系统 Skill 已变化，必须重新蒸馏：{source_skill}")
        if item.get("kind") == "stage" and skill_root.is_dir():
            try:
                contract = load_json(skill_root / "references" / "contract.json", MAX_CANDIDATE_BYTES)
            except ContractError as exc:
                generation_errors.append(str(exc))
            else:
                for output in contract.get("output_contract", []):
                    if not isinstance(output, dict) or output.get("required") is not True:
                        generation_errors.append(f"阶段输出契约必须为必需：{item.get('name', '')}")
                        break
    runtime = manifest.get("runtime_requirements") if isinstance(manifest.get("runtime_requirements"), dict) else {}
    declared_all = sorted(unique_strings(runtime.get("python_dependencies")))
    actual_all = sorted({
        dependency
        for item in manifest.get("skills", [])
        if isinstance(item, dict)
        for dependency in unique_strings(item.get("python_dependencies"))
    })
    if declared_all != actual_all:
        generation_errors.append("manifest.runtime_requirements.python_dependencies 与各 Skill 不一致")
    service_skills = {
        str(item.get("name", "")): str(item.get("foundation_kind", ""))
        for item in manifest.get("skills", [])
        if isinstance(item, dict) and item.get("foundation_kind") in {"ocr", "knowledge"}
    }
    external_services = runtime.get("external_services") if isinstance(runtime.get("external_services"), list) else []
    declared_services = {
        str(item.get("skill", "")): str(item.get("kind", ""))
        for item in external_services
        if isinstance(item, dict)
        and item.get("kind") in {"ocr_http_api", "vector_kb_http_api"}
        and item.get("configuration_policy") == "preserve_public_defaults_externalize_credentials"
    }
    expected_services = {
        skill: "ocr_http_api" if kind == "ocr" else "vector_kb_http_api"
        for skill, kind in service_skills.items()
    }
    if declared_services != expected_services:
        generation_errors.append("系统基础 Skill 外部服务声明缺失或不一致")
    executor_name = str(manifest.get("main_executor_skill", ""))
    release = manifest.get("release") if isinstance(manifest.get("release"), dict) else {}
    if release.get("schema_version") != RELEASE_CONTRACT_VERSION:
        generation_errors.append("Capability release contract is missing or outdated")
    elif not executor_name:
        generation_errors.append("Capability release has no primary executor")
    else:
        release_root = Path(args.result).resolve().parent
        generation_errors.extend(validate_release_bundle(release_root, executor_name))
        expected_release_digests = (
            release.get("artifact_digests") if isinstance(release.get("artifact_digests"), dict) else {}
        )
        for key, relative in (
            ("skill_zip", "release/artifacts/skill.zip"),
            ("mcp_stdio_zip", "release/artifacts/mcp-stdio.zip"),
        ):
            artifact = release_root / relative
            if artifact.is_file() and expected_release_digests.get(key) != sha256_file(artifact):
                generation_errors.append(f"Release archive digest changed: {relative}")
    if generation_errors:
        raise ContractError("生成源码已损坏或不再可移植：" + "；".join(generation_errors))
    return 0, compact_summary(manifest, args.offset, args.limit)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("prepare", "draft", "preflight", "finalize"):
        command = commands.add_parser(name)
        command.add_argument("--relations", default="/workspace/outputs/data-relations/scenario-relationship.json")
        command.add_argument("--flow", default="/workspace/outputs/business-flow/business-flow.json")
        command.add_argument("--output", default="/workspace/outputs/capability-distillation")
        command.add_argument("--summary-limit", type=int, default=30)
        if name in {"draft", "preflight", "finalize"}:
            command.add_argument("--claims", required=True)
    brief_parser = commands.add_parser("brief")
    brief_parser.add_argument("--brief", default="/workspace/outputs/capability-distillation/distillation-brief.json")
    brief_parser.add_argument("--relations", default="/workspace/outputs/data-relations/scenario-relationship.json")
    brief_parser.add_argument("--flow", default="/workspace/outputs/business-flow/business-flow.json")
    brief_parser.add_argument("--output", default="/workspace/outputs/capability-distillation")
    summary_parser = commands.add_parser("summary")
    summary_parser.add_argument("--result", default="/workspace/outputs/capability-distillation/capability-manifest.json")
    summary_parser.add_argument("--relations", default="/workspace/outputs/data-relations/scenario-relationship.json")
    summary_parser.add_argument("--flow", default="/workspace/outputs/business-flow/business-flow.json")
    summary_parser.add_argument("--output", default="/workspace/outputs/capability-distillation")
    summary_parser.add_argument("--offset", type=int, default=0)
    summary_parser.add_argument("--limit", type=int, default=30)
    return parser


def run(argv: Sequence[str] | None = None) -> tuple[int, dict[str, Any]]:
    args = build_parser().parse_args(argv)
    handlers = {
        "prepare": prepare,
        "draft": draft,
        "brief": brief,
        "preflight": preflight,
        "finalize": finalize,
        "summary": summary,
    }
    return handlers[args.command](args)


def main(argv: Sequence[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    try:
        code, payload = run(argv)
    except (ContractError, OSError, ValueError) as exc:
        code = 2
        payload = {"schema_version": SCHEMA_VERSION, "status": "error", "error": str(exc)}
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
