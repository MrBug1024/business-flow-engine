#!/usr/bin/env python3
"""Gate, validate, and render one macro business flow from an accepted relation graph."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections import defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence


SCHEMA_VERSION = 1
UPSTREAM_CAPABILITY = "discover-data-relations"
MAX_SOURCE_BYTES = 1 * 1024 * 1024
MAX_CARDS_BYTES = 8 * 1024 * 1024
MAX_CANDIDATE_BYTES = 96 * 1024
MAX_BRIEF_CARDS = 40
MAX_STAGES = 10
MAX_TRANSITIONS = 16
MAX_BRANCH_TRANSITIONS = 4
MAX_STATES = 12
MAX_CONTROLS = 8
MAX_VALIDATION_CHECKS = 12

STAGE_TYPES = {
    "initiation",
    "preparation",
    "processing",
    "decision",
    "fulfillment",
    "closure",
    "oversight",
}
TRANSITION_TYPES = {"normal", "handoff", "conditional", "exception", "return"}
STATE_TYPES = {"entry", "intermediate", "terminal", "exception"}
INFERENCE_BASES = {"explicit", "structural"}
ORDER_EDGE_TYPES = {"triggers", "precedes", "branches_to", "returns_to"}
DIRECTIONAL_EDGE_TYPES = {
    "triggers",
    "consumes",
    "produces",
    "transforms",
    "precedes",
    "branches_to",
    "updates",
    "returns_to",
    "feeds",
    "derives",
}
RULE_EDGE_TYPES = {"governs", "governed_by"}
VALIDATION_METHODS = {
    "sequence_consistency",
    "state_coverage",
    "input_output_traceability",
    "branch_frequency",
    "outcome_reconciliation",
    "control_conformance",
}
ID_PATTERN = re.compile(r"^[a-z][a-z0-9_-]{1,63}$")
MICRO_NAME_PATTERNS = (
    re.compile(r"(?i)\b(row|record|column|field|cell|sheet|table)\b"),
    re.compile(r"字段|列值|单元格|工作表|数据行|逐行|逐条记录|某条记录"),
    re.compile(r"(?i)\.(csv|xlsx?|jsonl|parquet|sqlite3?|docx?|pptx?|pdf)$"),
)
GENERIC_STAGE_NAMES = {
    "处理数据",
    "数据处理",
    "执行业务流程",
    "执行流程",
    "系统判断",
    "业务处理",
    "process data",
    "execute process",
}


class ContractError(ValueError):
    """Raised when a source or candidate violates a stable handoff contract."""


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


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(64 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def unique_strings(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return list(dict.fromkeys(str(item) for item in value if str(item)))


def compact_text(value: Any, limit: int = 360) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def validate_id(owner: str, value: Any, errors: list[str]) -> str:
    identifier = str(value or "")
    if not ID_PATTERN.fullmatch(identifier):
        errors.append(f"{owner} id 必须是简短稳定的 ASCII 标识：{identifier or '<empty>'}")
    return identifier


def is_macro_name(name: str) -> bool:
    text = name.strip()
    if not 2 <= len(text) <= 80:
        return False
    if text.casefold() in GENERIC_STAGE_NAMES:
        return False
    if any(pattern.search(text) for pattern in MICRO_NAME_PATTERNS):
        return False
    compact = re.sub(r"\s+", "", text)
    if re.fullmatch(r"[0-9][0-9,./:\-]*", compact):
        return False
    digits = sum(char.isdigit() for char in compact)
    return not (len(compact) >= 9 and digits / len(compact) >= 0.5)


def source_indexes(source: dict[str, Any]) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]], set[str]]:
    node_by_id = {
        str(item.get("id")): item
        for item in source.get("nodes", [])
        if isinstance(item, dict) and item.get("id")
    }
    edge_by_id = {
        str(item.get("id")): item
        for item in source.get("edges", [])
        if isinstance(item, dict) and item.get("id")
    }
    evidence_ids: set[str] = set()
    for item in [*node_by_id.values(), *edge_by_id.values()]:
        evidence_ids.update(unique_strings(item.get("evidence_ids")))
    return node_by_id, edge_by_id, evidence_ids


def validate_upstream(path: Path) -> tuple[dict[str, Any], list[str]]:
    errors: list[str] = []
    try:
        payload = load_json(path, MAX_SOURCE_BYTES)
    except ContractError as exc:
        return {}, [str(exc)]

    if payload.get("status") != "complete":
        errors.append("上游 scenario-relationship.json 的 status 必须为 complete")
    if not isinstance(payload.get("schema_version"), int):
        errors.append("上游产物缺少整数 schema_version")
    scenario = payload.get("scenario")
    if not isinstance(scenario, dict) or not str(scenario.get("name", "")).strip():
        errors.append("上游产物缺少有效 scenario.name")
    nodes = payload.get("nodes")
    edges = payload.get("edges")
    if not isinstance(nodes, list) or not nodes:
        errors.append("上游产物缺少非空 nodes")
        nodes = []
    if not isinstance(edges, list) or not edges:
        errors.append("上游产物缺少非空 edges")
        edges = []
    node_ids = [str(item.get("id", "")) for item in nodes if isinstance(item, dict)]
    edge_ids = [str(item.get("id", "")) for item in edges if isinstance(item, dict)]
    if len(node_ids) != len(nodes) or any(not item for item in node_ids) or len(set(node_ids)) != len(node_ids):
        errors.append("上游 nodes 必须具有非空且唯一的 id")
    if len(edge_ids) != len(edges) or any(not item for item in edge_ids) or len(set(edge_ids)) != len(edge_ids):
        errors.append("上游 edges 必须具有非空且唯一的 id")
    known_nodes = set(node_ids)
    for edge in edges:
        if not isinstance(edge, dict):
            continue
        if str(edge.get("source", "")) not in known_nodes or str(edge.get("target", "")) not in known_nodes:
            errors.append(f"上游关系 {edge.get('id', '')} 引用了不存在的节点")
    data_path = payload.get("primary_data_path", payload.get("main_chain"))
    if not isinstance(data_path, list) or len(data_path) < 3:
        errors.append("上游产物缺少至少三个节点的 primary_data_path/main_chain")
    elif any(str(item) not in known_nodes for item in data_path):
        errors.append("上游主数据路径引用了不存在的节点")

    upstream_root = path.parent
    for required in ("relations.mmd", "relation-report.md"):
        if not (upstream_root / required).is_file():
            errors.append(f"上游 complete 交付不完整，缺少 {required}")
    if (upstream_root / "validation-errors.json").exists():
        errors.append("上游目录仍存在 validation-errors.json，必须先由 discover-data-relations 修复")
    return payload, errors


def compact_card(card: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(card.get("id", "")),
        "kind": str(card.get("kind", "")),
        "strength": str(card.get("strength", "")),
        "statement": compact_text(card.get("statement", card.get("summary", "")), 320),
        "snippet": compact_text(card.get("snippet", ""), 320),
        "sources": [
            {
                "file": compact_text(source.get("file", ""), 180),
                "locator": compact_text(source.get("locator", ""), 180),
            }
            for source in card.get("sources", [])[:4]
            if isinstance(source, dict)
        ],
    }


def build_brief(source: dict[str, Any], source_path: Path, fingerprint: str) -> dict[str, Any]:
    node_by_id, edge_by_id, cited_evidence_ids = source_indexes(source)
    cards_path = source_path.parent / "evidence-cards.json"
    selected_cards: list[dict[str, Any]] = []
    card_warning = ""
    if cards_path.is_file():
        try:
            cards = load_json(cards_path, MAX_CARDS_BYTES).get("cards", [])
            selected_cards = [
                compact_card(card)
                for card in cards
                if isinstance(card, dict) and str(card.get("id", "")) in cited_evidence_ids
            ][:MAX_BRIEF_CARDS]
        except ContractError as exc:
            card_warning = str(exc)
    else:
        card_warning = "上游未提供 evidence-cards.json；流程仍可基于已验收节点、关系和证据 ID 推导。"

    explicit_order_edges = [
        edge_id for edge_id, edge in edge_by_id.items() if str(edge.get("type", "")) in ORDER_EDGE_TYPES
    ]
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "ready_for_synthesis",
        "source": {
            "capability": UPSTREAM_CAPABILITY,
            "artifact": str(source_path),
            "fingerprint": fingerprint,
            "status": source.get("status"),
        },
        "scenario": source.get("scenario", {}),
        "primary_data_path": source.get("primary_data_path", source.get("main_chain", [])),
        "nodes": [
            {
                "id": node_id,
                "name": compact_text(node.get("name", ""), 120),
                "type": str(node.get("type", "")),
                "description": compact_text(node.get("description", ""), 300),
                "evidence_ids": unique_strings(node.get("evidence_ids")),
            }
            for node_id, node in node_by_id.items()
        ],
        "edges": [
            {
                "id": edge_id,
                "source": str(edge.get("source", "")),
                "target": str(edge.get("target", "")),
                "type": str(edge.get("type", "")),
                "label": compact_text(edge.get("label", ""), 180),
                "confidence": edge.get("confidence"),
                "evidence_ids": unique_strings(edge.get("evidence_ids")),
            }
            for edge_id, edge in edge_by_id.items()
        ],
        "explicit_order_edge_ids": explicit_order_edges,
        "evidence_cards": selected_cards,
        "evidence_card_warning": card_warning,
        "inference_policy": {
            "grain": "macro_business_scenario",
            "allowed": [
                "explicit：上游有明确 triggers/precedes/branches_to/returns_to 关系",
                "structural：由已验收 feeds/derives 等方向性依赖推导宏观阶段，但必须说明理由和置信度",
            ],
            "forbidden": [
                "把 main_chain 当成现成流程",
                "把文件、表、字段、单条历史记录或具体取值变成步骤",
                "用历史记录中偶然出现的顺序定义标准流程",
                "把未支撑的假设放入正式主流程；假设只进入 open_questions",
            ],
            "history_role": "历史数据只验证流程覆盖、可追溯性、状态、分支和结果，不定义规范流程。",
        },
        "next_action": (
            "完整阅读流程推导协议，综合一个 4-8 阶段左右的宏观候选，写入 "
            "flow-claims.candidate.json，预检通过后再 finalize。"
        ),
    }


def claims_template(source: dict[str, Any], source_path: Path, fingerprint: str) -> dict[str, Any]:
    node_by_id, edge_by_id, _ = source_indexes(source)
    scenario = source.get("scenario", {})
    return {
        "schema_version": SCHEMA_VERSION,
        "source": {
            "capability": UPSTREAM_CAPABILITY,
            "artifact": str(source_path),
            "fingerprint": fingerprint,
        },
        "scenario": {
            "name": str(scenario.get("name", "")),
            "purpose": str(scenario.get("purpose", "")),
            "business_outcome": "",
            "grain": "macro_business_scenario",
        },
        "history_policy": {
            "role": "validation_only",
            "statement": "历史数据仅用于验证流程覆盖、顺序一致性、状态、分支和结果，不用于定义标准流程。",
        },
        "stages": [],
        "transitions": [],
        "main_flow": [],
        "states": [],
        "controls": [],
        "validation_checks": [],
        "open_questions": [],
        "coverage": {
            "used_upstream_node_ids": [],
            "used_upstream_edge_ids": [],
            "context_only": [],
            "inventory_upstream_node_ids": list(node_by_id),
            "inventory_upstream_edge_ids": list(edge_by_id),
        },
    }


def prepare(args: argparse.Namespace) -> tuple[int, dict[str, Any]]:
    source_path = Path(args.relations).resolve()
    output_root = Path(args.output).resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    source, errors = validate_upstream(source_path)
    if errors:
        payload = {
            "schema_version": SCHEMA_VERSION,
            "status": "blocked_missing_or_invalid_relations",
            "source": str(source_path),
            "errors": errors,
            "next_action": (
                "先运行或修复 discover-data-relations，直到 scenario-relationship.json 为 complete，"
                "relations.mmd 与 relation-report.md 存在且无 validation-errors.json；不得直接读取原始历史数据代替上游。"
            ),
        }
        atomic_json(output_root / "prepare-status.json", payload)
        return 2, payload

    fingerprint = file_sha256(source_path)
    brief = build_brief(source, source_path, fingerprint)
    template = claims_template(source, source_path, fingerprint)
    atomic_json(output_root / "flow-brief.json", brief)
    atomic_json(output_root / "flow-claims.template.json", template)
    payload = {
        "schema_version": SCHEMA_VERSION,
        "status": "ready_for_synthesis",
        "source": str(source_path),
        "source_fingerprint": fingerprint,
        "node_count": len(source.get("nodes", [])),
        "edge_count": len(source.get("edges", [])),
        "brief": str(output_root / "flow-brief.json"),
        "template": str(output_root / "flow-claims.template.json"),
        "next_action": "综合宏观流程候选并运行 preflight。",
    }
    atomic_json(output_root / "prepare-status.json", payload)
    return 0, {**payload, "flow_brief": brief}


def validate_string_list(owner: str, value: Any, known: set[str], errors: list[str]) -> list[str]:
    if not isinstance(value, list):
        errors.append(f"{owner} 必须是数组")
        return []
    items = [str(item) for item in value]
    if len(items) != len(set(items)):
        errors.append(f"{owner} 不能包含重复项")
    unknown = sorted(set(items) - known)
    if unknown:
        errors.append(f"{owner} 引用了未知 ID：{', '.join(unknown)}")
    return items


def validate_support(
    owner: str,
    support: Any,
    node_by_id: dict[str, dict[str, Any]],
    edge_by_id: dict[str, dict[str, Any]],
    evidence_ids: set[str],
    errors: list[str],
) -> tuple[set[str], set[str]]:
    if not isinstance(support, dict):
        errors.append(f"{owner}.support 必须是对象")
        return set(), set()
    nodes = validate_string_list(
        f"{owner}.support.upstream_node_ids",
        support.get("upstream_node_ids"),
        set(node_by_id),
        errors,
    )
    edges = validate_string_list(
        f"{owner}.support.upstream_edge_ids",
        support.get("upstream_edge_ids"),
        set(edge_by_id),
        errors,
    )
    cited = validate_string_list(
        f"{owner}.support.evidence_ids",
        support.get("evidence_ids"),
        evidence_ids,
        errors,
    )
    if not nodes and not edges:
        errors.append(f"{owner} 必须引用至少一个上游节点或关系")
    if not cited:
        errors.append(f"{owner} 必须引用至少一个上游 evidence_id")
    allowed_evidence: set[str] = set()
    for node_id in nodes:
        allowed_evidence.update(unique_strings(node_by_id.get(node_id, {}).get("evidence_ids")))
    for edge_id in edges:
        allowed_evidence.update(unique_strings(edge_by_id.get(edge_id, {}).get("evidence_ids")))
    unrelated_evidence = sorted(set(cited) - allowed_evidence)
    if unrelated_evidence:
        errors.append(
            f"{owner}.support.evidence_ids 必须来自当前引用的上游节点或关系："
            f"{', '.join(unrelated_evidence)}"
        )
    return set(nodes), set(edges)


def validate_inference(
    owner: str,
    value: Any,
    support_edges: set[str],
    edge_by_id: dict[str, dict[str, Any]],
    errors: list[str],
    *,
    transition: bool = False,
) -> None:
    if not isinstance(value, dict):
        errors.append(f"{owner}.inference 必须是对象")
        return
    basis = str(value.get("basis", ""))
    if basis not in INFERENCE_BASES:
        errors.append(f"{owner}.inference.basis 只能是 explicit 或 structural；假设必须移入 open_questions")
    confidence = value.get("confidence")
    if not isinstance(confidence, (int, float)) or not 0.5 <= confidence <= 1:
        errors.append(f"{owner}.inference.confidence 必须在 0.5 到 1 之间")
    if len(str(value.get("rationale", "")).strip()) < 8:
        errors.append(f"{owner}.inference.rationale 必须说明宏观推导理由")
    edge_types = {str(edge_by_id[item].get("type", "")) for item in support_edges if item in edge_by_id}
    if transition and basis == "explicit" and not edge_types.intersection(ORDER_EDGE_TYPES):
        errors.append(f"{owner} 标为 explicit 时必须引用上游明确时序/分支关系")
    if transition and basis == "structural" and not edge_types.intersection(DIRECTIONAL_EDGE_TYPES):
        errors.append(f"{owner} 的结构性顺序必须引用 feeds/derives 等方向性上游关系")


def validate_candidate(
    claims: dict[str, Any],
    source: dict[str, Any],
    source_path: Path,
    fingerprint: str,
) -> list[str]:
    errors: list[str] = []
    node_by_id, edge_by_id, evidence_ids = source_indexes(source)
    source_claim = claims.get("source")
    if not isinstance(source_claim, dict):
        errors.append("source 必须是对象")
    else:
        if source_claim.get("capability") != UPSTREAM_CAPABILITY:
            errors.append(f"source.capability 必须是 {UPSTREAM_CAPABILITY}")
        if str(source_claim.get("fingerprint", "")) != fingerprint:
            errors.append("候选引用的上游 fingerprint 已过期；重新运行 prepare，不得沿用旧候选")
        try:
            claimed_path = Path(str(source_claim.get("artifact", ""))).resolve()
        except OSError:
            claimed_path = Path("__invalid__")
        if claimed_path != source_path:
            errors.append("source.artifact 必须精确指向本次验收的 scenario-relationship.json")

    scenario = claims.get("scenario")
    if not isinstance(scenario, dict):
        errors.append("scenario 必须是对象")
    else:
        if scenario.get("grain") != "macro_business_scenario":
            errors.append("scenario.grain 必须是 macro_business_scenario")
        if not str(scenario.get("name", "")).strip():
            errors.append("scenario.name 不能为空")
        elif str(scenario.get("name", "")).strip() != str(source.get("scenario", {}).get("name", "")).strip():
            errors.append("scenario.name 必须与已验收上游场景一致，不能在流程层扩大或切换范围")
        if len(str(scenario.get("purpose", "")).strip()) < 4:
            errors.append("scenario.purpose 必须说明当前场景范围")
        if len(str(scenario.get("business_outcome", "")).strip()) < 4:
            errors.append("scenario.business_outcome 必须说明整个场景的业务结果")

    history_policy = claims.get("history_policy")
    if not isinstance(history_policy, dict) or history_policy.get("role") != "validation_only":
        errors.append("history_policy.role 必须是 validation_only")
    elif len(str(history_policy.get("statement", "")).strip()) < 8:
        errors.append("history_policy.statement 必须明确历史数据不定义规范流程")

    stages = claims.get("stages")
    transitions = claims.get("transitions")
    main_flow = claims.get("main_flow")
    states = claims.get("states")
    controls = claims.get("controls")
    validation_checks = claims.get("validation_checks")
    open_questions = claims.get("open_questions")
    if not all(isinstance(item, list) for item in (stages, transitions, main_flow, states, controls, validation_checks, open_questions)):
        return errors + [
            "stages、transitions、main_flow、states、controls、validation_checks、open_questions 必须都是数组"
        ]
    if not 3 <= len(stages) <= MAX_STAGES:
        errors.append(f"宏观流程必须包含 3-{MAX_STAGES} 个阶段，通常保持在 4-8 个")
    if len(transitions) > MAX_TRANSITIONS:
        errors.append(f"transitions 最多 {MAX_TRANSITIONS} 条")
    if len(states) > MAX_STATES:
        errors.append(f"states 最多 {MAX_STATES} 个")
    if len(controls) > MAX_CONTROLS:
        errors.append(f"controls 最多 {MAX_CONTROLS} 个")
    if len(validation_checks) > MAX_VALIDATION_CHECKS:
        errors.append(f"validation_checks 最多 {MAX_VALIDATION_CHECKS} 个")

    used_nodes: set[str] = set()
    used_edges: set[str] = set()
    stage_by_id: dict[str, dict[str, Any]] = {}
    for index, stage in enumerate(stages):
        owner = f"stage[{index}]"
        if not isinstance(stage, dict):
            errors.append(f"{owner} 必须是对象")
            continue
        identifier = validate_id(owner, stage.get("id"), errors)
        if identifier in stage_by_id:
            errors.append(f"阶段 id 重复：{identifier}")
        stage_by_id[identifier] = stage
        name = str(stage.get("name", "")).strip()
        if not is_macro_name(name):
            errors.append(f"阶段 {identifier} 名称必须是宏观业务阶段，不能是文件、表、字段或单条记录：{name}")
        if stage.get("stage_type") not in STAGE_TYPES:
            errors.append(f"阶段 {identifier} 的 stage_type 不受支持")
        if len(str(stage.get("objective", "")).strip()) < 4:
            errors.append(f"阶段 {identifier} 必须说明 objective")
        if len(str(stage.get("outcome", "")).strip()) < 4:
            errors.append(f"阶段 {identifier} 必须说明 outcome")
        inputs = validate_string_list(
            f"阶段 {identifier}.input_node_ids", stage.get("input_node_ids"), set(node_by_id), errors
        )
        outputs = validate_string_list(
            f"阶段 {identifier}.output_node_ids", stage.get("output_node_ids"), set(node_by_id), errors
        )
        if not inputs and not outputs:
            errors.append(f"阶段 {identifier} 至少映射一个宏观输入或输出节点")
        used_nodes.update(inputs)
        used_nodes.update(outputs)
        support_nodes, support_edges = validate_support(
            f"阶段 {identifier}", stage.get("support"), node_by_id, edge_by_id, evidence_ids, errors
        )
        unsupported_mappings = (set(inputs) | set(outputs)) - support_nodes
        if unsupported_mappings:
            errors.append(
                f"阶段 {identifier} 的 input/output 映射必须同时出现在 support.upstream_node_ids："
                f"{', '.join(sorted(unsupported_mappings))}"
            )
        if str(stage.get("owner_role", "")).strip():
            support_types = {
                str(node_by_id[item].get("type", "")) for item in support_nodes if item in node_by_id
            }
            if "actor" not in support_types:
                errors.append(f"阶段 {identifier}.owner_role 只有在 support 引用上游 actor 节点时才能填写")
        used_nodes.update(support_nodes)
        used_edges.update(support_edges)
        validate_inference(f"阶段 {identifier}", stage.get("inference"), support_edges, edge_by_id, errors)

    stage_ids = set(stage_by_id)
    transition_by_id: dict[str, dict[str, Any]] = {}
    pair_types: dict[tuple[str, str], set[str]] = defaultdict(set)
    adjacency: dict[str, set[str]] = defaultdict(set)
    branch_count = 0
    for index, transition in enumerate(transitions):
        owner = f"transition[{index}]"
        if not isinstance(transition, dict):
            errors.append(f"{owner} 必须是对象")
            continue
        identifier = validate_id(owner, transition.get("id"), errors)
        if identifier in transition_by_id:
            errors.append(f"流转 id 重复：{identifier}")
        transition_by_id[identifier] = transition
        source_id = str(transition.get("source", ""))
        target_id = str(transition.get("target", ""))
        if source_id not in stage_ids or target_id not in stage_ids:
            errors.append(f"流转 {identifier} 引用了未知阶段")
        if source_id == target_id:
            errors.append(f"流转 {identifier} 不能自循环")
        transition_type = str(transition.get("type", ""))
        if transition_type not in TRANSITION_TYPES:
            errors.append(f"流转 {identifier} 的 type 不受支持")
        if transition_type in {"conditional", "exception", "return"}:
            branch_count += 1
        if transition_type in {"conditional", "exception"} and len(str(transition.get("condition", "")).strip()) < 2:
            errors.append(f"流转 {identifier} 必须说明宏观 condition")
        support_nodes, support_edges = validate_support(
            f"流转 {identifier}", transition.get("support"), node_by_id, edge_by_id, evidence_ids, errors
        )
        used_nodes.update(support_nodes)
        used_edges.update(support_edges)
        validate_inference(
            f"流转 {identifier}", transition.get("inference"), support_edges, edge_by_id, errors, transition=True
        )
        inference_basis = str(transition.get("inference", {}).get("basis", ""))
        support_edge_types = {
            str(edge_by_id[item].get("type", "")) for item in support_edges if item in edge_by_id
        }
        if inference_basis == "explicit":
            required_explicit_types = {
                "normal": {"triggers", "precedes"},
                "handoff": {"triggers", "precedes"},
                "conditional": {"branches_to"},
                "exception": {"branches_to"},
                "return": {"returns_to"},
            }.get(transition_type, set())
            if required_explicit_types and not support_edge_types.intersection(required_explicit_types):
                errors.append(
                    f"显式流转 {identifier} 的 {transition_type} 类型与上游时序关系不匹配；"
                    f"需要 {', '.join(sorted(required_explicit_types))}"
                )
        if transition_type in {"conditional", "exception"}:
            support_node_types = {str(node_by_id[item].get("type", "")) for item in support_nodes if item in node_by_id}
            if not support_node_types.intersection({"rule", "decision"}) and "branches_to" not in support_edge_types:
                errors.append(f"流转 {identifier} 的分支必须由上游规则、判定或明确 branches_to 支撑")
        if transition_type == "return":
            edge_types = {str(edge_by_id[item].get("type", "")) for item in support_edges if item in edge_by_id}
            if "returns_to" not in edge_types:
                errors.append(f"返回流转 {identifier} 必须有上游 returns_to 明确证据")
        pair_types[(source_id, target_id)].add(transition_type)
        adjacency[source_id].add(target_id)
        adjacency[target_id].add(source_id)
    if branch_count > MAX_BRANCH_TRANSITIONS:
        errors.append(f"条件、异常和返回流转合计最多 {MAX_BRANCH_TRANSITIONS} 条")

    if not isinstance(main_flow, list) or len(main_flow) < 3:
        errors.append("main_flow 至少包含三个宏观阶段")
    else:
        main_ids = [str(item) for item in main_flow]
        if len(main_ids) != len(set(main_ids)):
            errors.append("main_flow 不能重复阶段")
        unknown = set(main_ids) - stage_ids
        if unknown:
            errors.append(f"main_flow 引用了未知阶段：{', '.join(sorted(unknown))}")
        for source_id, target_id in zip(main_ids, main_ids[1:]):
            if not pair_types[(source_id, target_id)].intersection({"normal", "handoff"}):
                errors.append(f"主流程 {source_id} -> {target_id} 缺少 normal 或 handoff 流转")
        if main_ids and main_ids[-1] in stage_by_id:
            final_type = stage_by_id[main_ids[-1]].get("stage_type")
            if final_type not in {"fulfillment", "closure"}:
                errors.append("main_flow 最后阶段应是 fulfillment 或 closure，表达场景级业务结果")

    if stage_ids:
        start = next(iter(stage_ids))
        visited = {start}
        queue = deque([start])
        while queue:
            current = queue.popleft()
            for neighbor in adjacency[current]:
                if neighbor not in visited:
                    visited.add(neighbor)
                    queue.append(neighbor)
        disconnected = stage_ids - visited
        if disconnected:
            errors.append(f"所有阶段必须属于同一宏观流程，当前孤立：{', '.join(sorted(disconnected))}")

    state_by_id: dict[str, dict[str, Any]] = {}
    terminal_count = 0
    for index, state in enumerate(states):
        owner = f"state[{index}]"
        if not isinstance(state, dict):
            errors.append(f"{owner} 必须是对象")
            continue
        identifier = validate_id(owner, state.get("id"), errors)
        if identifier in state_by_id:
            errors.append(f"状态 id 重复：{identifier}")
        state_by_id[identifier] = state
        if not is_macro_name(str(state.get("name", ""))):
            errors.append(f"状态 {identifier} 名称必须是宏观业务状态")
        state_type = str(state.get("state_type", ""))
        if state_type not in STATE_TYPES:
            errors.append(f"状态 {identifier} 的 state_type 不受支持")
        if state_type == "terminal":
            terminal_count += 1
        reached_after = str(state.get("reached_after", ""))
        if state_type == "entry":
            if reached_after and reached_after not in stage_ids:
                errors.append(f"入口状态 {identifier} reached_after 引用了未知阶段")
        elif reached_after not in stage_ids:
            errors.append(f"状态 {identifier}.reached_after 必须引用一个阶段")
        support_nodes, support_edges = validate_support(
            f"状态 {identifier}", state.get("support"), node_by_id, edge_by_id, evidence_ids, errors
        )
        used_nodes.update(support_nodes)
        used_edges.update(support_edges)
        validate_inference(f"状态 {identifier}", state.get("inference"), support_edges, edge_by_id, errors)
    if terminal_count == 0:
        errors.append("至少需要一个 terminal 宏观状态")

    control_ids: set[str] = set()
    for index, control in enumerate(controls):
        owner = f"control[{index}]"
        if not isinstance(control, dict):
            errors.append(f"{owner} 必须是对象")
            continue
        identifier = validate_id(owner, control.get("id"), errors)
        if identifier in control_ids:
            errors.append(f"控制 id 重复：{identifier}")
        control_ids.add(identifier)
        applies_to = validate_string_list(
            f"控制 {identifier}.applies_to", control.get("applies_to"), stage_ids, errors
        )
        if not applies_to:
            errors.append(f"控制 {identifier} 必须作用于至少一个阶段")
        support_nodes, support_edges = validate_support(
            f"控制 {identifier}", control.get("support"), node_by_id, edge_by_id, evidence_ids, errors
        )
        used_nodes.update(support_nodes)
        used_edges.update(support_edges)
        node_types = {str(node_by_id[item].get("type", "")) for item in support_nodes if item in node_by_id}
        edge_types = {str(edge_by_id[item].get("type", "")) for item in support_edges if item in edge_by_id}
        if "rule" not in node_types and not edge_types.intersection(RULE_EDGE_TYPES):
            errors.append(f"控制 {identifier} 必须由上游 rule 节点或 governs/governed_by 关系支撑")

    check_ids: set[str] = set()
    valid_targets = {
        "stage": stage_ids,
        "transition": set(transition_by_id),
        "state": set(state_by_id),
        "control": control_ids,
    }
    for index, check in enumerate(validation_checks):
        owner = f"validation_check[{index}]"
        if not isinstance(check, dict):
            errors.append(f"{owner} 必须是对象")
            continue
        identifier = validate_id(owner, check.get("id"), errors)
        if identifier in check_ids:
            errors.append(f"验证检查 id 重复：{identifier}")
        check_ids.add(identifier)
        target_kind = str(check.get("target_kind", ""))
        target_id = str(check.get("target_id", ""))
        if target_kind not in valid_targets or target_id not in valid_targets.get(target_kind, set()):
            errors.append(f"验证检查 {identifier} 引用了未知目标 {target_kind}:{target_id}")
        if check.get("method") not in VALIDATION_METHODS:
            errors.append(f"验证检查 {identifier} 的 method 不受支持")
        if check.get("role") != "validate_not_define":
            errors.append(f"验证检查 {identifier}.role 必须是 validate_not_define")
        if len(str(check.get("question", "")).strip()) < 4 or len(str(check.get("pass_signal", "")).strip()) < 4:
            errors.append(f"验证检查 {identifier} 必须说明 question 与 pass_signal")
        source_nodes = validate_string_list(
            f"验证检查 {identifier}.source_node_ids", check.get("source_node_ids"), set(node_by_id), errors
        )
        used_nodes.update(source_nodes)

    question_ids: set[str] = set()
    for index, question in enumerate(open_questions):
        owner = f"open_question[{index}]"
        if not isinstance(question, dict):
            errors.append(f"{owner} 必须是对象")
            continue
        identifier = validate_id(owner, question.get("id"), errors)
        if identifier in question_ids:
            errors.append(f"待确认项 id 重复：{identifier}")
        question_ids.add(identifier)
        if len(str(question.get("question", "")).strip()) < 4:
            errors.append(f"待确认项 {identifier} 必须说明 question")
        validate_string_list(
            f"待确认项 {identifier}.related_stage_ids", question.get("related_stage_ids"), stage_ids, errors
        )

    coverage = claims.get("coverage")
    if not isinstance(coverage, dict):
        errors.append("coverage 必须是对象")
    else:
        declared_nodes = set(validate_string_list(
            "coverage.used_upstream_node_ids",
            coverage.get("used_upstream_node_ids"),
            set(node_by_id),
            errors,
        ))
        declared_edges = set(validate_string_list(
            "coverage.used_upstream_edge_ids",
            coverage.get("used_upstream_edge_ids"),
            set(edge_by_id),
            errors,
        ))
        if declared_nodes != used_nodes:
            errors.append("coverage.used_upstream_node_ids 必须精确等于各阶段、状态、控制和验证实际引用的上游节点")
        if declared_edges != used_edges:
            errors.append("coverage.used_upstream_edge_ids 必须精确等于各阶段、流转、状态和控制实际引用的上游关系")
        context_only = coverage.get("context_only")
        context_node_ids: set[str] = set()
        context_edge_ids: set[str] = set()
        if not isinstance(context_only, list):
            errors.append("coverage.context_only 必须是数组")
        else:
            for index, item in enumerate(context_only):
                if not isinstance(item, dict):
                    errors.append(f"coverage.context_only[{index}] 必须是对象")
                    continue
                kind = str(item.get("kind", ""))
                item_id = str(item.get("id", ""))
                if kind == "node":
                    if item_id not in node_by_id:
                        errors.append(f"context_only 引用了未知节点 {item_id}")
                    context_node_ids.add(item_id)
                elif kind == "edge":
                    if item_id not in edge_by_id:
                        errors.append(f"context_only 引用了未知关系 {item_id}")
                    context_edge_ids.add(item_id)
                else:
                    errors.append(f"context_only[{index}].kind 只能是 node 或 edge")
                if len(str(item.get("reason", "")).strip()) < 4:
                    errors.append(f"context_only {kind}:{item_id} 必须说明不进入流程骨架的原因")
        if used_nodes.intersection(context_node_ids) or used_edges.intersection(context_edge_ids):
            errors.append("同一上游项不能同时标为 used 和 context_only")
        missing_nodes = set(node_by_id) - used_nodes - context_node_ids
        missing_edges = set(edge_by_id) - used_edges - context_edge_ids
        if missing_nodes:
            errors.append(f"coverage 未交代上游节点：{', '.join(sorted(missing_nodes))}")
        if missing_edges:
            errors.append(f"coverage 未交代上游关系：{', '.join(sorted(missing_edges))}")
    return errors


def candidate_context(args: argparse.Namespace) -> tuple[Path, Path, dict[str, Any], str, dict[str, Any]]:
    output_root = Path(args.output).resolve()
    claims_path = Path(args.claims).resolve()
    if not claims_path.is_relative_to(output_root):
        raise ContractError("候选文件必须位于 business-flow 输出目录内")
    claims = load_json(claims_path, MAX_CANDIDATE_BYTES)
    source_path = Path(args.relations).resolve()
    source, upstream_errors = validate_upstream(source_path)
    if upstream_errors:
        raise ContractError("；".join(upstream_errors))
    fingerprint = file_sha256(source_path)
    return output_root, source_path, source, fingerprint, claims


def validation_payload(
    claims: dict[str, Any], source: dict[str, Any], source_path: Path, fingerprint: str, claims_path: Path
) -> dict[str, Any]:
    errors = validate_candidate(claims, source, source_path, fingerprint)
    if not errors:
        return {
            "schema_version": SCHEMA_VERSION,
            "status": "valid",
            "error_count": 0,
            "claims": str(claims_path),
            "stage_count": len(claims.get("stages", [])),
            "transition_count": len(claims.get("transitions", [])),
            "state_count": len(claims.get("states", [])),
            "next_action": "使用完全相同的候选路径运行 finalize。",
        }
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "validation_failed",
        "error_count": len(errors),
        "errors": errors,
        "claims": str(claims_path),
        "repair_target": str(claims_path),
        "candidate_preserved": True,
        "repair_hints": [
            "把文件、表、字段、记录或具体值合并回宏观业务阶段。",
            "main_chain 是主数据路径，不是流程；仅以方向依赖做 structural 推断并写明理由。",
            "未获支撑的顺序或分支移入 open_questions，不得以历史偶然顺序补齐。",
            "修正 coverage，使每个上游节点和关系恰好属于 used 或 context_only。",
        ],
        "next_action": "一次性修正 repair_target 后重跑 preflight；若上游 fingerprint 过期则先重新 prepare。",
    }


def preflight(args: argparse.Namespace) -> tuple[int, dict[str, Any]]:
    output_root, source_path, source, fingerprint, claims = candidate_context(args)
    claims_path = Path(args.claims).resolve()
    payload = validation_payload(claims, source, source_path, fingerprint, claims_path)
    validation_path = output_root / "validation-errors.json"
    if payload["status"] == "validation_failed":
        atomic_json(validation_path, payload)
        return 2, payload
    validation_path.unlink(missing_ok=True)
    return 0, payload


def mermaid_escape(value: Any) -> str:
    return compact_text(value, 100).replace("\\", "\\\\").replace('"', "'").replace("\n", " ")


def write_mermaid(result: dict[str, Any], path: Path) -> None:
    lines = ["flowchart LR"]
    stage_by_id = {str(item["id"]): item for item in result["stages"]}
    for stage in result["stages"]:
        identifier = str(stage["id"])
        label = mermaid_escape(f"{stage['name']}\n{stage['stage_type']}")
        if stage["stage_type"] == "decision":
            lines.append(f'    {identifier}{{"{label}"}}')
        elif stage["stage_type"] in {"initiation", "closure", "fulfillment"}:
            lines.append(f'    {identifier}(["{label}"])')
        else:
            lines.append(f'    {identifier}["{label}"]')
    main_pairs = set(zip(result["main_flow"], result["main_flow"][1:]))
    for transition in result["transitions"]:
        source_id, target_id = str(transition["source"]), str(transition["target"])
        label = mermaid_escape(transition.get("label") or transition.get("condition") or transition["type"])
        if (source_id, target_id) in main_pairs and transition["type"] in {"normal", "handoff"}:
            lines.append(f'    {source_id} -->|"{label}"| {target_id}')
        elif transition["type"] == "return":
            lines.append(f'    {source_id} -.->|"{label}"| {target_id}')
        else:
            lines.append(f'    {source_id} -->|"{label}"| {target_id}')
    for control in result["controls"]:
        control_id = f"ctl_{control['id']}"
        lines.append(f'    {control_id}{{{{"{mermaid_escape(control.get("name", "业务控制"))}"}}}}')
        for stage_id in control.get("applies_to", []):
            if stage_id in stage_by_id:
                lines.append(f'    {control_id} -.->|"约束"| {stage_id}')
    for state in result["states"]:
        state_id = f"state_{state['id']}"
        lines.append(f'    {state_id}(["状态：{mermaid_escape(state["name"])}"])')
        reached_after = str(state.get("reached_after", ""))
        if reached_after in stage_by_id:
            lines.append(f'    {reached_after} -.-> {state_id}')
    atomic_text(path, "\n".join(lines) + "\n")


def support_text(item: dict[str, Any]) -> str:
    support = item.get("support", {})
    nodes = ", ".join(support.get("upstream_node_ids", [])) or "无"
    edges = ", ".join(support.get("upstream_edge_ids", [])) or "无"
    evidence = ", ".join(support.get("evidence_ids", [])) or "无"
    return f"上游节点 {nodes}；上游关系 {edges}；证据 {evidence}"


def write_report(result: dict[str, Any], path: Path) -> None:
    stage_by_id = {str(item["id"]): item for item in result["stages"]}
    lines = [
        f"# {result['scenario']['name']}：宏观业务流程推导",
        "",
        "## 范围与结论",
        "",
        f"- 业务结果：{result['scenario']['business_outcome']}",
        f"- 粒度：{result['scenario']['grain']}；流程阶段是跨业务实例稳定成立的宏观责任，不是历史记录操作轨迹。",
        f"- 上游来源：`{result['source']['artifact']}`",
        f"- 上游指纹：`{result['source']['fingerprint']}`",
        f"- 历史数据角色：{result['history_policy']['statement']}",
        "",
        "## 主流程",
        "",
    ]
    for index, stage_id in enumerate(result["main_flow"], 1):
        stage = stage_by_id[str(stage_id)]
        inference = stage["inference"]
        lines.extend([
            f"### {index}. {stage['name']}",
            "",
            f"- 目标：{stage['objective']}",
            f"- 阶段结果：{stage['outcome']}",
            f"- 推导：{inference['basis']}，置信度 {float(inference['confidence']):.2f}；{inference['rationale']}",
            f"- 支撑：{support_text(stage)}",
            "",
        ])
    lines.extend(["## 流转与分支", ""])
    for transition in result["transitions"]:
        inference = transition["inference"]
        condition = f"；条件：{transition.get('condition')}" if transition.get("condition") else ""
        lines.append(
            f"- **{stage_by_id[transition['source']]['name']} → {stage_by_id[transition['target']]['name']}** "
            f"({transition['type']}{condition})：{transition.get('label', '')}。"
            f"{inference['basis']} / {float(inference['confidence']):.2f}；{inference['rationale']}；{support_text(transition)}。"
        )
    lines.extend(["", "## 状态与控制", ""])
    for state in result["states"]:
        after = stage_by_id.get(str(state.get("reached_after", "")), {}).get("name", "流程入口")
        lines.append(f"- 状态 **{state['name']}**（{state['state_type']}），位于“{after}”之后；{support_text(state)}。")
    for control in result["controls"]:
        targets = "、".join(stage_by_id[item]["name"] for item in control.get("applies_to", []) if item in stage_by_id)
        lines.append(f"- 控制 **{control['name']}** 约束：{targets}；{control.get('policy', '')}；{support_text(control)}。")
    lines.extend(["", "## 历史数据验证计划", ""])
    if result["validation_checks"]:
        for check in result["validation_checks"]:
            lines.append(
                f"- **{check['id']} / {check['method']}**：{check['question']}；通过信号：{check['pass_signal']}。"
                "该检查只验证流程，不定义流程。"
            )
    else:
        lines.append("- 当前未声明历史数据验证检查；不影响流程骨架，但应在取得可用历史数据后补充验证。")
    lines.extend(["", "## 待业务确认", ""])
    if result["open_questions"]:
        for question in result["open_questions"]:
            lines.append(f"- {question['question']}（影响：{question.get('impact', '待评估')}）")
    else:
        lines.append("- 无。正式流程中未纳入缺少上游支撑的假设。")
    coverage = result["coverage"]
    lines.extend([
        "",
        "## 覆盖边界",
        "",
        f"- 已使用上游节点：{len(coverage['used_upstream_node_ids'])}",
        f"- 已使用上游关系：{len(coverage['used_upstream_edge_ids'])}",
        f"- 仅作为上下文、未进入流程骨架：{len(coverage['context_only'])}",
        "- 完成状态只证明该流程与已验收关系图谱一致，不证明原始材料中不存在未表达的人工例外。",
        "",
    ])
    atomic_text(path, "\n".join(lines))


def compact_summary(result: dict[str, Any], offset: int, limit: int) -> dict[str, Any]:
    stages = result.get("stages", [])
    offset = max(0, offset)
    limit = max(1, min(limit, 20))
    return {
        "schema_version": result.get("schema_version"),
        "status": result.get("status"),
        "scenario": result.get("scenario"),
        "source": result.get("source"),
        "stage_count": len(stages),
        "transition_count": len(result.get("transitions", [])),
        "state_count": len(result.get("states", [])),
        "control_count": len(result.get("controls", [])),
        "main_flow": result.get("main_flow", []),
        "offset": offset,
        "limit": limit,
        "has_more": offset + limit < len(stages),
        "stages": [
            {
                "id": item.get("id"),
                "name": item.get("name"),
                "stage_type": item.get("stage_type"),
                "basis": item.get("inference", {}).get("basis"),
                "confidence": item.get("inference", {}).get("confidence"),
            }
            for item in stages[offset : offset + limit]
        ],
        "open_question_count": len(result.get("open_questions", [])),
        "artifacts": result.get("artifacts", {}),
    }


def finalize(args: argparse.Namespace) -> tuple[int, dict[str, Any]]:
    output_root, source_path, source, fingerprint, claims = candidate_context(args)
    claims_path = Path(args.claims).resolve()
    validation = validation_payload(claims, source, source_path, fingerprint, claims_path)
    if validation["status"] != "valid":
        atomic_json(output_root / "validation-errors.json", validation)
        return 2, validation
    canonical_claims = output_root / "flow-claims.json"
    atomic_json(canonical_claims, claims)
    result = {
        "schema_version": SCHEMA_VERSION,
        "status": "complete",
        "generated_at": utc_now(),
        "strategy": "accepted_relation_graph_to_macro_business_flow",
        "source": claims["source"],
        "scenario": claims["scenario"],
        "history_policy": claims["history_policy"],
        "stages": claims["stages"],
        "transitions": claims["transitions"],
        "main_flow": claims["main_flow"],
        "states": claims["states"],
        "controls": claims["controls"],
        "validation_checks": claims["validation_checks"],
        "open_questions": claims["open_questions"],
        "coverage": {
            **claims["coverage"],
            "guarantee": (
                "流程仅消费已验收 discover-data-relations 产物；阶段是宏观业务责任；"
                "历史数据只参与验证；所有正式顺序均为明确或有理由的结构性推断。"
            ),
        },
        "artifacts": {
            "json": str(output_root / "business-flow.json"),
            "markdown": str(output_root / "business-flow-report.md"),
            "mermaid": str(output_root / "business-flow.mmd"),
            "claims": str(canonical_claims),
            "brief": str(output_root / "flow-brief.json"),
        },
    }
    atomic_json(output_root / "business-flow.json", result)
    write_mermaid(result, output_root / "business-flow.mmd")
    write_report(result, output_root / "business-flow-report.md")
    (output_root / "validation-errors.json").unlink(missing_ok=True)
    return 0, compact_summary(result, 0, args.summary_limit)


def brief(args: argparse.Namespace) -> tuple[int, dict[str, Any]]:
    path = Path(args.brief).resolve()
    payload = load_json(path, MAX_SOURCE_BYTES)
    source_path = Path(args.relations).resolve()
    _, upstream_errors = validate_upstream(source_path)
    if upstream_errors:
        raise ContractError("；".join(upstream_errors))
    source_claim = payload.get("source", {})
    claimed_artifact = (
        Path(str(source_claim.get("artifact", ""))).resolve()
        if isinstance(source_claim, dict)
        else None
    )
    if (
        not isinstance(source_claim, dict)
        or claimed_artifact != source_path
        or source_claim.get("fingerprint") != file_sha256(source_path)
    ):
        raise ContractError("flow-brief.json 的上游 fingerprint 已过期；必须重新运行 prepare")
    return 0, payload


def summary(args: argparse.Namespace) -> tuple[int, dict[str, Any]]:
    result = load_json(Path(args.result).resolve(), MAX_SOURCE_BYTES)
    if result.get("status") != "complete":
        raise ContractError("business-flow.json 尚未 complete")
    source_path = Path(args.relations).resolve()
    _, upstream_errors = validate_upstream(source_path)
    if upstream_errors:
        raise ContractError("；".join(upstream_errors))
    source_claim = result.get("source", {})
    claimed_artifact = (
        Path(str(source_claim.get("artifact", ""))).resolve()
        if isinstance(source_claim, dict)
        else None
    )
    if (
        not isinstance(source_claim, dict)
        or claimed_artifact != source_path
        or source_claim.get("fingerprint") != file_sha256(source_path)
    ):
        raise ContractError("business-flow.json 的上游 fingerprint 已过期；不得交付旧流程")
    return 0, compact_summary(result, args.offset, args.limit)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    prepare_parser = commands.add_parser("prepare", help="Gate upstream output and build a bounded brief")
    prepare_parser.add_argument(
        "--relations", default="/workspace/outputs/data-relations/scenario-relationship.json"
    )
    prepare_parser.add_argument("--output", default="/workspace/outputs/business-flow")
    prepare_parser.add_argument("--summary-limit", type=int, default=20)
    brief_parser = commands.add_parser("brief", help="Read the bounded synthesis brief")
    brief_parser.add_argument("--brief", default="/workspace/outputs/business-flow/flow-brief.json")
    brief_parser.add_argument(
        "--relations", default="/workspace/outputs/data-relations/scenario-relationship.json"
    )
    for name in ("preflight", "finalize"):
        command = commands.add_parser(name)
        command.add_argument("--claims", required=True)
        command.add_argument(
            "--relations", default="/workspace/outputs/data-relations/scenario-relationship.json"
        )
        command.add_argument("--output", default="/workspace/outputs/business-flow")
        command.add_argument("--summary-limit", type=int, default=20)
    summary_parser = commands.add_parser("summary", help="Read a bounded final summary")
    summary_parser.add_argument("--result", default="/workspace/outputs/business-flow/business-flow.json")
    summary_parser.add_argument(
        "--relations", default="/workspace/outputs/data-relations/scenario-relationship.json"
    )
    summary_parser.add_argument("--offset", type=int, default=0)
    summary_parser.add_argument("--limit", type=int, default=20)
    return parser


def run(argv: Sequence[str] | None = None) -> tuple[int, dict[str, Any]]:
    args = build_parser().parse_args(argv)
    handlers = {
        "prepare": prepare,
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
        payload = {
            "schema_version": SCHEMA_VERSION,
            "status": "error",
            "error": str(exc),
        }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
