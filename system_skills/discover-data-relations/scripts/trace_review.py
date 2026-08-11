#!/usr/bin/env python3
"""Versioned review and micro-process contracts for result-anchored traces.

This module deliberately keeps two concerns apart:

* A trace may contain a few redacted rows or bounded document segments so that
  a reviewer can decide whether its exact links tell the right story.
* The downstream micro-process contract contains only endpoints, locators,
  digests, fields, identifier replay rules, cardinality and invariants.  It
  never carries row values or document text forward.

The separation is what lets a reviewed example teach an execution principle
without turning the example itself into a hard-coded business rule.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


SCHEMA_VERSION = 1
REVIEW_STATUS = {"pending_review", "revision_required", "approved", "superseded"}
DECISIONS = {"pending", "revision_required", "approved"}


class ReviewError(ValueError):
    """Raised when a trace-review handoff cannot be trusted."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(64 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def stable_id(prefix: str, *parts: Any) -> str:
    body = json.dumps(parts, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return prefix + hashlib.sha1(body.encode("utf-8")).hexdigest()[:12]


def atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def load_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ReviewError(f"无法读取合法 JSON：{path}：{exc}") from exc
    if not isinstance(payload, dict):
        raise ReviewError(f"顶层 JSON 必须为对象：{path}")
    return payload


def _warnings(trace: dict[str, Any]) -> list[str]:
    quality = trace.get("quality_gates") if isinstance(trace.get("quality_gates"), dict) else {}
    collected = [str(item) for item in quality.get("warnings", []) if str(item)]
    for bundle in trace.get("bundles", []):
        if isinstance(bundle, dict):
            collected.extend(str(item) for item in bundle.get("warnings", []) if str(item))
    return list(dict.fromkeys(collected))


def _reviewable_bundle(trace: dict[str, Any], bundle_id: str) -> dict[str, Any]:
    for bundle in trace.get("bundles", []):
        if isinstance(bundle, dict) and str(bundle.get("bundle_id", "")) == bundle_id:
            return bundle
    raise ReviewError(f"追踪产物中不存在链路样本：{bundle_id}")


def trace_reference(trace_path: Path, trace: dict[str, Any], bundle_id: str) -> dict[str, Any]:
    return {
        "artifact": str(trace_path.resolve()),
        "fingerprint": file_sha256(trace_path),
        "field_evidence_fingerprint": str(trace.get("field_evidence_fingerprint", "")),
        "bundle_id": bundle_id,
        "strategy": str(trace.get("strategy", "")),
    }


def _bounded_strings(value: Any, *, limit: int = 16, width: int = 128) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item)[:width] for item in value[:limit] if str(item)]


def _bounded_document_segment(value: Any) -> dict[str, Any]:
    """Keep the reviewer evidence useful without copying an unbounded document."""

    if not isinstance(value, dict):
        return {}
    try:
        specificity = int(value.get("specificity", 0) or 0)
    except (TypeError, ValueError):
        specificity = 0
    segment = {
        "locator": str(value.get("locator", ""))[:160],
        "source_digest": str(value.get("source_digest", ""))[:128],
        "segment_digest": str(value.get("segment_digest", ""))[:128],
        "snippet": str(value.get("snippet", ""))[:320],
        "value_fingerprint": str(value.get("value_fingerprint", ""))[:128],
        "value_preview": str(value.get("value_preview", ""))[:160],
        "specificity": specificity,
    }
    return {key: item for key, item in segment.items() if item not in ("", 0)}


def _review_source(source: dict[str, Any]) -> dict[str, Any]:
    result = {
        "path": source.get("path"),
        "table": source.get("table"),
        "role": source.get("role"),
        "selected_columns": source.get("selected_columns", []),
        "sample_row_numbers": [
            row.get("row_number") for row in source.get("rows", []) if isinstance(row, dict)
        ],
    }
    endpoint = source.get("endpoint") if isinstance(source.get("endpoint"), dict) else {}
    if endpoint:
        result["endpoint"] = {
            key: endpoint.get(key)
            for key in ("file", "table", "kind", "locator")
            if endpoint.get(key) not in (None, "")
        }
    segment = _bounded_document_segment(source.get("segment"))
    if segment:
        result["segment"] = segment
    return result


def _review_link(link: dict[str, Any]) -> dict[str, Any]:
    return {
        "edge_id": link.get("edge_id"),
        "link_kind": link.get("link_kind", "table_key"),
        "source_file": link.get("source_file"),
        "source_table": link.get("source_table"),
        "target_file": link.get("target_file"),
        "target_table": link.get("target_table"),
        "key_pairs": link.get("key_pairs", []),
        "relation_ids": _bounded_strings(link.get("relation_ids")),
        "search_mode": link.get("search_mode"),
        "key_fingerprints": _bounded_strings(link.get("key_fingerprints")),
        "matched_row_count": link.get("matched_row_count"),
        "fanout_warning": bool(link.get("fanout_warning")),
        "materialization_truncated": bool(link.get("materialization_truncated")),
    }


def review_template(trace_path: Path, trace: dict[str, Any]) -> dict[str, Any]:
    bundles = [item for item in trace.get("bundles", []) if isinstance(item, dict)]
    if trace.get("status") != "complete" or len(bundles) != 1:
        raise ReviewError("只有恰好一条 complete 的结果锚定链路才可以进入审阅")
    bundle = bundles[0]
    bundle_id = str(bundle.get("bundle_id", ""))
    if not bundle_id:
        raise ReviewError("链路样本缺少 bundle_id")
    warnings = _warnings(trace)
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": "trace_review",
        "status": "pending_review",
        "created_at": utc_now(),
        "trace": trace_reference(trace_path, trace, bundle_id),
        "review_surface": {
            "anchor": bundle.get("anchor", {}),
            "sources": [_review_source(source) for source in bundle.get("sources", []) if isinstance(source, dict)],
            "links": [_review_link(link) for link in bundle.get("links", []) if isinstance(link, dict)],
            "warnings": warnings,
        },
        "review_checklist": [
            "结果锚点是否是业务需要解释的结果，而非中间表或随机记录？",
            "每一条跨源链路的文件、表或文档片段定位器，以及字段或标识符是否符合业务事实？",
            "精确证据能否从当前文件重新定位和重放，而非仅依赖语义相似或样本正文？",
            "存在一对多/多对多或一个标识符命中多个片段时，是否已说明其业务含义并要求运行时复核？",
            "该样本是否体现可参数化的处理原则，而非只能复现样本中的具体值？",
        ],
        "corrections": [],
        "approval": {
            "decision": "pending",
            "reviewer": "",
            "note": "",
            "accepted_warnings": [],
            "reviewed_at": "",
        },
    }


def _as_pairs(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, list) or not value:
        return []
    pairs: list[dict[str, str]] = []
    for item in value:
        if not isinstance(item, dict):
            return []
        source = str(item.get("source_field", "")).strip()
        target = str(item.get("target_field", "")).strip()
        if not source or not target:
            return []
        pairs.append({"source_field": source, "target_field": target})
    return pairs


def validate_corrections(corrections: Any) -> list[str]:
    errors: list[str] = []
    if not isinstance(corrections, list):
        return ["corrections 必须为数组"]
    seen: set[tuple[str, str, str, str, tuple[tuple[str, str], ...]]] = set()
    for index, correction in enumerate(corrections, 1):
        label = f"corrections[{index}]"
        if not isinstance(correction, dict):
            errors.append(f"{label} 必须为对象")
            continue
        source_file = str(correction.get("source_file", "")).strip()
        target_file = str(correction.get("target_file", "")).strip()
        source_table = str(correction.get("source_table", "")).strip()
        target_table = str(correction.get("target_table", "")).strip()
        if not all((source_file, target_file, source_table, target_table)):
            errors.append(f"{label} 必须提供 source/target 文件与表")
        pairs = _as_pairs(correction.get("key_pairs"))
        if not pairs:
            errors.append(f"{label} 必须提供至少一个 source_field/target_field 键对")
        if not str(correction.get("reason", "")).strip():
            errors.append(f"{label} 必须解释业务原因")
        signature = (source_file, source_table, target_file, target_table, tuple((item["source_field"], item["target_field"]) for item in pairs))
        if signature in seen:
            errors.append(f"{label} 与前一条复合关联重复")
        seen.add(signature)
    return errors


def validate_review(review: dict[str, Any], trace_path: Path | None = None) -> list[str]:
    errors: list[str] = []
    if review.get("schema_version") != SCHEMA_VERSION or review.get("kind") != "trace_review":
        errors.append("不是受支持的 trace_review 契约")
    status = str(review.get("status", ""))
    if status not in REVIEW_STATUS:
        errors.append("trace_review.status 无效")
    trace = review.get("trace") if isinstance(review.get("trace"), dict) else {}
    if not str(trace.get("artifact", "")).strip() or not str(trace.get("fingerprint", "")).strip():
        errors.append("trace_review.trace 必须锁定追踪产物与 fingerprint")
    if not str(trace.get("bundle_id", "")).strip():
        errors.append("trace_review.trace 必须锁定一个 bundle_id")
    approval = review.get("approval") if isinstance(review.get("approval"), dict) else {}
    decision = str(approval.get("decision", ""))
    if decision not in DECISIONS:
        errors.append("trace_review.approval.decision 无效")
    errors.extend(validate_corrections(review.get("corrections", [])))
    if trace_path is not None:
        if not trace_path.is_file():
            errors.append("当前 trace-samples.json 不存在")
        elif trace.get("fingerprint") != file_sha256(trace_path):
            errors.append("trace-samples.json 已变化；既有审阅已失效，必须重新审阅")
        else:
            trace_payload = load_json(trace_path)
            try:
                _reviewable_bundle(trace_payload, str(trace.get("bundle_id", "")))
            except ReviewError as exc:
                errors.append(str(exc))
    if status == "approved" or decision == "approved":
        if status != "approved" or decision != "approved":
            errors.append("已批准审阅必须同时将 status 和 decision 置为 approved")
        if not str(approval.get("reviewer", "")).strip():
            errors.append("已批准审阅必须记录 reviewer")
        if not str(approval.get("reviewed_at", "")).strip():
            errors.append("已批准审阅必须记录 reviewed_at")
        surface = review.get("review_surface") if isinstance(review.get("review_surface"), dict) else {}
        warnings = {str(item) for item in surface.get("warnings", []) if str(item)}
        accepted = {str(item) for item in approval.get("accepted_warnings", []) if str(item)}
        unaccepted = warnings - accepted
        if unaccepted:
            errors.append("存在未明确接受的链路警示：" + "；".join(sorted(unaccepted)))
    return errors


def load_approved_review(review_path: Path, trace_path: Path | None = None) -> dict[str, Any]:
    review = load_json(review_path)
    trace_reference_path = trace_path
    if trace_reference_path is None:
        trace = review.get("trace") if isinstance(review.get("trace"), dict) else {}
        try:
            trace_reference_path = Path(str(trace.get("artifact", ""))).resolve()
        except OSError:
            trace_reference_path = Path("__invalid__")
    errors = validate_review(review, trace_reference_path)
    if errors:
        raise ReviewError("；".join(errors))
    if review.get("status") != "approved":
        raise ReviewError("链路样本尚未批准；必须先审阅、纠偏或确认警示")
    return review


def normalized_overrides(review: dict[str, Any]) -> list[dict[str, Any]]:
    """Return user-confirmed key sets without sample values or business rules."""

    values = []
    for correction in review.get("corrections", []):
        if not isinstance(correction, dict):
            continue
        pairs = _as_pairs(correction.get("key_pairs"))
        if not pairs:
            continue
        values.append({
            "source_file": str(correction.get("source_file", "")),
            "source_table": str(correction.get("source_table", "")),
            "target_file": str(correction.get("target_file", "")),
            "target_table": str(correction.get("target_table", "")),
            "key_pairs": pairs,
            "reason": str(correction.get("reason", "")),
            "review_correction_id": str(correction.get("id", "")),
        })
    return values


def _micro_endpoint(
    bundle: dict[str, Any], file_name: Any, table_name: Any, relation_ids: Any,
) -> dict[str, Any]:
    """Describe a replay endpoint without carrying row or document values."""

    file_value = str(file_name or "")
    table_value = str(table_name or "")
    if table_value:
        return {"file": file_value, "kind": "table", "table": table_value}
    relation_id_set = set(_bounded_strings(relation_ids))
    evidence = next((
        item for item in bundle.get("semantic_evidence", [])
        if isinstance(item, dict)
        and str(item.get("path", "")) == file_value
        and item.get("evidence_kind") == "exact_value_document_segment"
        and (not relation_id_set or str(item.get("relation_id", "")) in relation_id_set)
    ), {})
    evidence_locator = str(evidence.get("locator", ""))
    source = next((
        item for item in bundle.get("sources", [])
        if isinstance(item, dict) and str(item.get("path", "")) == file_value
        and isinstance(item.get("segment"), dict)
        and (
            not evidence_locator
            or str(item.get("segment", {}).get("locator", "")) == evidence_locator
        )
    ), {})
    segment = source.get("segment") if isinstance(source.get("segment"), dict) else {}
    endpoint = {
        "file": file_value,
        "kind": "document_segment",
        "locator": evidence_locator or str(segment.get("locator", "")),
        "source_digest": str(evidence.get("source_digest") or segment.get("source_digest", "")),
        "segment_digest": str(evidence.get("segment_digest") or segment.get("segment_digest", "")),
    }
    return {key: value for key, value in endpoint.items() if value != ""}


def _anchor_operation(anchor: dict[str, Any]) -> dict[str, Any]:
    if anchor.get("kind") == "document_segment":
        return {
            "id": "op_locate_document_result_segment",
            "kind": "locate_document_result_segment",
            "input": {
                "path": anchor.get("path"),
                "locator": anchor.get("locator"),
                "source_digest": anchor.get("source_digest"),
                "segment_digest": anchor.get("segment_digest"),
            },
            "output": "result_anchor",
            "sample_value_free": True,
        }
    return {
        "id": "op_select_result_anchor",
        "kind": "select_result_anchor",
        "input": {"path": anchor.get("path"), "table": anchor.get("table")},
        "output": "result_anchor",
        "sample_value_free": True,
    }


def micro_process_template(review_path: Path, review: dict[str, Any], trace_path: Path) -> dict[str, Any]:
    trace = load_json(trace_path)
    trace_ref = review.get("trace") if isinstance(review.get("trace"), dict) else {}
    bundle = _reviewable_bundle(trace, str(trace_ref.get("bundle_id", "")))
    anchor = bundle.get("anchor") if isinstance(bundle.get("anchor"), dict) else {}
    operations: list[dict[str, Any]] = [_anchor_operation(anchor)]
    invariants: list[dict[str, Any]] = []
    has_document_contract = anchor.get("kind") == "document_segment"
    for index, link in enumerate(bundle.get("links", []), 1):
        if not isinstance(link, dict):
            continue
        operation_id = f"op_follow_link_{index}"
        pairs = _as_pairs(link.get("key_pairs"))
        is_document_link = link.get("link_kind") == "exact_value_document_segment"
        has_document_contract = has_document_contract or is_document_link
        if is_document_link:
            operation = {
                "id": operation_id,
                "kind": "follow_exact_document_identifier",
                "input": {
                    "source_endpoint": _micro_endpoint(
                        bundle, link.get("source_file"), link.get("source_table"),
                        link.get("relation_ids"),
                    ),
                    "target_endpoint": _micro_endpoint(
                        bundle, link.get("target_file"), link.get("target_table"),
                        link.get("relation_ids"),
                    ),
                    "key_pairs": pairs,
                    "replay": {
                        "mode": link.get("search_mode") or "field_evidence_exact_locator_replay",
                        "reextract_document_segment": True,
                        "recompute_identifier_fingerprint": True,
                        "require_exact_fingerprint_match": True,
                    },
                },
                "output": f"linked_source_{index}",
                "sample_value_free": True,
                "runtime_cardinality_check": {
                    "observed_matched_row_count": link.get("matched_row_count"),
                    "fanout_requires_validation": bool(link.get("fanout_warning")),
                },
            }
            invariant_statement = (
                "运行时必须重新提取文档定位器中的当前标识符并复算指纹，再与另一端精确匹配；"
                "不得携带样本文本、样本标识符值或以语义相似替代。"
            )
        else:
            operation = {
                "id": operation_id,
                "kind": "follow_exact_key_set",
                "input": {
                    "source_file": link.get("source_file"),
                    "source_table": link.get("source_table"),
                    "target_file": link.get("target_file"),
                    "target_table": link.get("target_table"),
                    "key_pairs": pairs,
                },
                "output": f"linked_source_{index}",
                "sample_value_free": True,
                "runtime_cardinality_check": {
                    "observed_matched_row_count": link.get("matched_row_count"),
                    "fanout_requires_validation": bool(link.get("fanout_warning")),
                },
            }
            invariant_statement = "运行时必须按已审阅键组进行精确匹配，不得使用样本中的具体值、行号或未确认的单字段替代。"
        operations.append(operation)
        invariants.append({
            "id": f"invariant_key_set_{index}",
            "statement": invariant_statement,
            "operation_id": operation_id,
        })
    parameters = ["runtime_result_selector", "runtime_rule_selector", "reviewed_key_sets"]
    must_hold = [
        "所有跨表关联必须使用已审阅的精确键组，并在运行时复核基数。",
        "规则、筛选条件和输出投影必须由运行时输入或已审阅规则源决定。",
    ]
    forbidden = ["sample_row_number", "sample_cell_value", "sample_anchor_identity"]
    scope = "该微观复现契约仅描述结构化处理原理；领域语义不充分时必须保留为待确认问题，而不得由样本值补写。"
    if has_document_contract:
        parameters.extend([
            "runtime_document_parser",
            "runtime_ocr_policy",
            "reviewed_document_identifier_links",
        ])
        must_hold[0] = (
            "所有跨源关联必须使用已审阅的精确键组或文档标识符重放契约，"
            "并在运行时复核定位器、当前内容摘要、精确匹配与基数。"
        )
        forbidden.extend(["sample_document_text", "sample_identifier_value"])
        scope = (
            "该微观复现契约描述结构化表与非结构化文档片段的可重放处理原理；"
            "文档必须经运行时解析或按策略 OCR 后重新定位，领域语义不充分时必须保留为待确认问题。"
        )
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": "trace_micro_process",
        "status": "pending_review",
        "created_at": utc_now(),
        "source": {
            "trace_review": str(review_path.resolve()),
            "trace_review_fingerprint": file_sha256(review_path),
            "trace": trace_reference(trace_path, trace, str(trace_ref.get("bundle_id", ""))),
        },
        "sample_reconstruction": {
            "anchor": {key: value for key, value in anchor.items() if key != "row_number"},
            "operations": operations,
            "invariants": invariants,
        },
        "generalization_contract": {
            "parameters": parameters,
            "must_hold": must_hold,
            "must_not_depend_on": forbidden,
            "scope": scope,
        },
        "open_questions": [
            "请确认每条一对多链路的业务基数含义，以及是否需要聚合、去重或逐明细输出。",
            "请确认规则源如何选择适用规则；没有规则证据时不得从历史结果样本反推具体规则。",
        ],
        "approval": {"decision": "pending", "reviewer": "", "note": "", "reviewed_at": ""},
    }


def validate_micro_process(payload: dict[str, Any], review_path: Path | None = None) -> list[str]:
    errors: list[str] = []
    if payload.get("schema_version") != SCHEMA_VERSION or payload.get("kind") != "trace_micro_process":
        errors.append("不是受支持的 trace_micro_process 契约")
    source = payload.get("source") if isinstance(payload.get("source"), dict) else {}
    if not str(source.get("trace_review", "")).strip() or not str(source.get("trace_review_fingerprint", "")).strip():
        errors.append("micro-process 必须锁定已审阅 trace-review")
    reconstruction = payload.get("sample_reconstruction") if isinstance(payload.get("sample_reconstruction"), dict) else {}
    operations = reconstruction.get("operations") if isinstance(reconstruction.get("operations"), list) else []
    if not operations or any(not item.get("sample_value_free") for item in operations if isinstance(item, dict)):
        errors.append("micro-process 必须包含且只包含 sample_value_free 的复现操作")
    generalization = payload.get("generalization_contract") if isinstance(payload.get("generalization_contract"), dict) else {}
    forbidden = {str(item) for item in generalization.get("must_not_depend_on", []) if str(item)}
    if not {"sample_row_number", "sample_cell_value"}.issubset(forbidden):
        errors.append("micro-process 必须明确禁止依赖样本行号和样本单元格值")
    approval = payload.get("approval") if isinstance(payload.get("approval"), dict) else {}
    if payload.get("status") == "approved":
        if approval.get("decision") != "approved" or not str(approval.get("reviewer", "")).strip():
            errors.append("已批准 micro-process 必须记录批准人和 decision")
    if review_path is not None:
        if not review_path.is_file():
            errors.append("关联的 trace-review 不存在")
        elif source.get("trace_review_fingerprint") != file_sha256(review_path):
            errors.append("trace-review 已变化；micro-process 必须重新生成并审阅")
        else:
            try:
                load_approved_review(review_path)
            except ReviewError as exc:
                errors.append(str(exc))
    return errors


def load_approved_micro_process(path: Path) -> dict[str, Any]:
    payload = load_json(path)
    source = payload.get("source") if isinstance(payload.get("source"), dict) else {}
    try:
        review_path = Path(str(source.get("trace_review", ""))).resolve()
    except OSError:
        review_path = Path("__invalid__")
    errors = validate_micro_process(payload, review_path)
    if errors:
        raise ReviewError("；".join(errors))
    if payload.get("status") != "approved":
        raise ReviewError("微观复现契约尚未批准；不得推导宏观流程")
    return payload


def compact_micro_process(payload: dict[str, Any]) -> dict[str, Any]:
    reconstruction = payload.get("sample_reconstruction") if isinstance(payload.get("sample_reconstruction"), dict) else {}
    return {
        "status": payload.get("status"),
        "source": payload.get("source", {}),
        "operations": reconstruction.get("operations", []),
        "generalization_contract": payload.get("generalization_contract", {}),
        "open_questions": payload.get("open_questions", []),
    }


def make_corrections(value: Any) -> list[dict[str, Any]]:
    values: Iterable[Any]
    if isinstance(value, dict):
        values = value.get("corrections", []) if isinstance(value.get("corrections"), list) else []
    elif isinstance(value, list):
        values = value
    else:
        values = []
    result: list[dict[str, Any]] = []
    for item in values:
        if not isinstance(item, dict):
            continue
        normalized = dict(item)
        normalized["key_pairs"] = _as_pairs(item.get("key_pairs"))
        normalized["id"] = str(item.get("id") or stable_id(
            "trace-correction-", normalized.get("source_file"), normalized.get("source_table"),
            normalized.get("target_file"), normalized.get("target_table"), normalized["key_pairs"],
        ))
        result.append(normalized)
    errors = validate_corrections(result)
    if errors:
        raise ReviewError("；".join(errors))
    return result
