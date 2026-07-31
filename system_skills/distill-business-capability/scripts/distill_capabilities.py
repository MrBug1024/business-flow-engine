#!/usr/bin/env python3
"""Distill accepted relation and flow artifacts into portable multi-Skill source."""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import re
import shutil
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence


SCHEMA_VERSION = 1
GENERATOR_CONTRACT_VERSION = 2
RELATION_CAPABILITY = "discover-data-relations"
FLOW_CAPABILITY = "derive-business-flow"
MAX_SOURCE_BYTES = 2 * 1024 * 1024
MAX_EVIDENCE_BYTES = 8 * 1024 * 1024
MAX_OPERATIONAL_BYTES = 8 * 1024 * 1024
MAX_CANDIDATE_BYTES = 512 * 1024
MAX_FILES = 500
MAX_STAGE_SKILLS = 12
MAX_PROCEDURE_STEPS = 10

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


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(64 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def compact(value: Any, limit: int = 360) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def unique_strings(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return list(dict.fromkeys(str(item) for item in value if str(item)))


def is_external_knowledge_node(node: dict[str, Any]) -> bool:
    text = " ".join(str(node.get(key, "")) for key in ("name", "description", "type")).casefold()
    return any(marker.casefold() in text for marker in KNOWLEDGE_MARKERS) or any(
        marker in text
        for marker in ("crawler", "scraper", "web search", "remote api", "external api", "爬虫", "网络检索", "外部接口", "远程接口")
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
    expected_execution_policy = {
        "rule_resolution": "complete_rule_record_before_bulk_query",
        "bulk_data_access": "bounded_read_only_sql",
        "join_policy": "evidence_backed_keys_with_runtime_fanout_validation",
        "agent_direct_file_read": False,
        "unstructured_access": "parse_or_ocr_then_provenance_chunk_search",
    }
    if payload.get("execution_policy") != expected_execution_policy:
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
    return {
        "source_ids": sorted(set(source_ids)),
        "large_source_ids": sorted(set(large_source_ids)),
        "rule_source_ids": rule_source_ids,
        "structured_rule_source_ids": structured_rule_source_ids,
        "document_rule_source_ids": document_rule_source_ids,
        "link_ids": sorted(set(link_ids)),
        "document_source_ids": sorted(set(document_source_ids)),
        "semantic_route_ids": sorted(set(semantic_route_ids)),
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
            "credentials": "preserve_system_skill_configuration",
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
    atomic_json(output_root / "distillation-brief.json", brief)
    atomic_json(output_root / "capability-plan.template.json", template)
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
        "next_action": "综合并写入 capability-plan.candidate.json，然后运行 preflight。",
    }
    atomic_json(output_root / "prepare-status.json", payload)
    return 0, {**payload, "distillation_brief": brief}


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
        "credentials": "preserve_system_skill_configuration",
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
    claims: dict[str, Any], relations: dict[str, Any], flow: dict[str, Any], relation_path: Path,
    flow_path: Path, relation_fingerprint: str, flow_fingerprint: str, claims_path: Path,
) -> dict[str, Any]:
    errors = validate_plan(
        claims, relations, flow, relation_path, flow_path, relation_fingerprint, flow_fingerprint
    )
    if not errors:
        return {
            "schema_version": SCHEMA_VERSION,
            "status": "valid",
            "error_count": 0,
            "claims": str(claims_path),
            "foundation_count": len(claims.get("foundation_skills", [])),
            "stage_skill_count": len(claims.get("stage_skills", [])),
            "total_skill_count": len(claims.get("foundation_skills", [])) + len(claims.get("stage_skills", [])) + 1,
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
            "不得改写 prepare 生成的文件清单、格式、流程节点和输入输出事实。",
            "每个流程阶段恰好生成一个阶段 Skill；规则不足时写运行时契约或待确认项。",
            "只生成当前格式真正需要的基础能力，并在描述中写清场景文件角色和调用时机。",
            "程序步骤必须引用流程阶段、输入输出、控制、状态或交接 ID，不得凭历史记录补微观逻辑。",
            "所有输出 Skill 必须脱离 Studio，资源使用相对路径，秘密只来自环境变量。",
        ],
        "next_action": "一次性修正 repair_target 后重跑 preflight；fingerprint 过期时先重新 prepare。",
    }


def preflight(args: argparse.Namespace) -> tuple[int, dict[str, Any]]:
    (
        output_root, relation_path, flow_path, relations, flow, relation_fingerprint,
        flow_fingerprint, claims_path, claims,
    ) = candidate_context(args)
    payload = validation_payload(
        claims, relations, flow, relation_path, flow_path, relation_fingerprint, flow_fingerprint, claims_path
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
        "2. 若用户请求依赖规则，先对规则源运行 `search-contract`，返回命中的完整规则行；问题清单、违规类型、参考示例、用途及同一行其他字段都必须保留。若命中多条，继续用用户条件缩小；仍有多条实质不同规则时列出规则标识并请求选择，禁止拼接成一条规则。",
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
        "- `config/defaults.json` 完整继承系统 `ocr-parser` 的既有字段和值；生成器不得删除、清空或改写已有服务地址与 API Key。",
        "- 第三方运行环境仍可用同名环境变量覆盖包内配置；不得在日志、Agent 上下文或业务结果中回显凭据。",
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
        "- `config/defaults.json` 完整继承系统 `vector-kb` 的服务地址、知识库 ID、API Key、超时和其他字段；生成器不得删减或清空。",
        "- 第三方环境可用 `VECTOR_KB_*` 环境变量覆盖包内配置；任何输出均不得回显凭据。",
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
    lines.append("完成推理后把有界结果写为 JSON，并运行 `python \"<this-skill>/scripts/run_stage.py\" finish --work-order \"<work-order.json>\" --result \"<result.json>\" --output \"<handoff.json>\"`。阶段运行器会验证必需输出并拒绝原始业务文件路径。")
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
    shutil.copytree(source, target)


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


def materialize_system_skill_credentials(source_skill: str, target: Path) -> dict[str, Any]:
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
        value = environment_value or stored_value or source_value
        origin = "environment" if environment_value else "platform_secret_store" if stored_value else "source_config" if source_value else "missing"
        if value and value != source_value:
            config[json_key] = value
            atomic_json(config_path, config)
        fields.append({
            "environment_key": environment_key,
            "config_path": relative_path,
            "config_key": json_key,
            "configured": bool(value),
            "origin": origin,
        })
    return {
        "policy": "preserve_and_materialize_without_redaction",
        "source_skill": source_skill,
        "fields": fields,
        "all_required_credentials_configured": all(item["configured"] for item in fields),
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
    credential_status = materialize_system_skill_credentials(source_skill, target)
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


def render_agent_prompts(
    claims: dict[str, Any], flow: dict[str, Any], operational: dict[str, Any], skills: list[dict[str, Any]]
) -> str:
    scenario = claims.get("scenario", {})
    orchestrator = claims["orchestrator"]
    foundations = [item for item in skills if item.get("kind") == "foundation"]
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
    if "document" in foundation_kinds:
        document_step = (
            "TXT、Markdown、Word、可搜索 PDF 先由文档基础 Skill 建分块索引再检索；"
            + (
                "图片和扫描 PDF 先由 OCR Skill 使用 `--output` 落结构化 JSON，再由文档 Skill `index-ocr` 建索引。"
                if "ocr" in foundation_kinds
                else "当前包未包含 OCR；遇到图片或扫描 PDF 时停止并要求重新发现、蒸馏或安装已声明的 OCR 能力。"
            )
        )
        provenance_step = "非结构化命中必须携带原始 source_digest、页码/段落/幻灯片/行号（若解析器可提供）、chunk_id 和 text_digest。OCR 若缺页块、坐标或置信度元数据，必须明确标记这一不确定性。语义相似只能用于寻找证据；没有明确业务主键时，不能把文档命中强行归到某条结构化记录。"
    else:
        document_step = "当前数据契约没有 TXT、Markdown、Word、PDF 或图片来源，也没有安装文档/OCR 基础 Skill。运行时若出现这些契约外格式，停止并要求从数据关系发现重新生成能力包；不得让 Agent 临时直接打开全文或假装已有解析能力。"
        provenance_step = "不得把契约外文档的语义命中强行归到结构化记录；只有重新发现后生成了带 source_digest、locator、chunk_id 与 text_digest 的检索路径，才能把非结构化证据纳入判定。"
    lines = [
        f"# {scenario.get('name', '')} Agent 系统提示词", "",
        f"你是“{scenario.get('name', '')}”业务 Agent。你的目标是：{scenario.get('purpose', flow.get('scenario', {}).get('business_outcome', '完成场景业务目标'))}。",
        "你只负责理解用户意图、选择业务阶段、根据完整业务证据推理并组织结果。文件解析、OCR、索引、大表扫描和 SQL 执行必须交给已安装 Skill，禁止把原始大文件或整篇文档直接读入上下文。", "",
        "禁止临时创建 Python、SQL 执行器、HTTP 客户端或文件解析脚本。每个已安装 Skill 都提供 `scripts/` CLI；先运行场景总控状态机和阶段运行器，再按阶段工作单调用基础 Skill 脚本。", "",
        "## 已安装能力", "",
        f"- 场景总控：`{orchestrator.get('skill_name', '')}`。端到端请求、跨阶段请求或不确定路由时必须先调用它。",
        *stage_lines,
        *foundation_lines,
        "", "## 数据来源契约", "", *source_lines,
        "", "所有基础 Skill 都携带 `references/operational-data-contract.json`。运行时由调用方提供 `<data-root>`；文件名变化时只能用基础 Skill 的 `--bind <source-id>=<relative-path>` 显式绑定，不得猜测路径。`design_time_template` 只提供输出字段/类型/格式约束，缺少其历史原文件不是运行阻塞。", "",
        "## 强制执行顺序", "",
        "1. 识别用户是在请求整个场景还是某一阶段，并选择场景总控或对应阶段 Skill。",
        "2. 若任务受规则约束，必须先取得完整适用规则记录。表格规则源用 `search-contract` 返回完整一行，规则名称、问题清单、违规类型、参考示例、用途及同一行其他字段均保留；文档规则源先建索引，检索并取得完整适用章节及定位。零命中时报告缺失；多条命中时先按用户条件缩小，仍有实质不同候选则列出规则标识并请求选择，禁止把多条规则拼接成一条。不得只摘一句或一个命中片段。",
        "3. 根据用户需求和完整规则行，推导实际需要的结构化字段、过滤条件、分组、比较逻辑、结果字段以及非结构化检索词。历史结果只保留模板结构和可选脱敏示例，用于输出字段约束、验证与对账；其原文件不是运行输入，也不能成为规则。",
        "4. 对每条结构化关联先运行 `validate-join`。单键出现无法解释的多对多时，只能按 operational-data-contract 中已有的 `candidate_key_sets` 继续验证复合键；查询时用 `<link-id>@<key-set-index>` 绑定已通过的键组。连接为零、未匹配异常或所有候选仍放大时停止并报告，禁止猜测替代键。",
        "5. 大型 Excel/CSV/Parquet 等只能通过契约化只读 SQL 访问。只预检和注册当前规则/SQL 引用的 runtime_input；运行数据按字段兼容性校验，不要求与蒸馏样本的大小或内容摘要相同。预览和核验使用 `query-contract`；用户要求全部结果时使用 `export-contract` 写 CSV/Parquet，并只把行数、查询摘要和文件摘要返回上下文。Agent 不得直接打开、全量 sample 或把全部记录放入上下文。",
        f"6. {document_step}",
        f"7. {provenance_step}",
        "8. 若已安装 knowledge 基础 Skill，只有相关流程节点或完整规则明确需要外部知识时才调用 `scenario_kb.py`；由 Agent 根据完整规则决定检索内容。规则声明为必需时使用 `--required`，知识库、爬虫或其他已声明外部能力全部不可用/零命中则必须返回 `manual_intervention_required` 并要求人工处理；不得凭空补齐。知识切片必须带来源，不能替代规则和业务事实。",
        "9. 用阶段 Skill 的 `run_stage.py start/finish` 生成工作单和交接文件，并由总控 Skill 的 `orchestrate.py` 记录顺序；只传递最小必要的结构化对象和证据定位。",
        "10. 输出前对照规则完整行、SQL 谓词、连接校验、结果字段和证据定位。若输入或证据不足，明确说明缺口，不补造事实。", "",
        "## 输出要求", "",
        "- 先回答业务结论或交付物，再说明所用规则、数据范围和关键证据。",
        "- 每条判定应能追溯到规则行、结构化来源/查询与文档/OCR 定位（若使用）。",
        "- 明确列出未匹配、截断、OCR 不确定、连接放大和待确认边界。",
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
    lines.append(f'    orchestrator["{compact(orchestrator["display_name"], 80).replace(chr(34), chr(39))}"]')
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
        "- 定制系统 Skill 完整继承配置字段和值；已配置凭据按原字段物化，但 manifest、报告和提示词不回显其值。",
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
    atomic_text(agent_prompt_path, render_agent_prompts(claims, flow, operational, skill_manifest))
    agent_prompt_digest = sha256_file(agent_prompt_path)
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "generator_contract_version": GENERATOR_CONTRACT_VERSION,
        "status": "complete",
        "generated_at": utc_now(),
        "strategy": "accepted_flow_to_portable_multi_skill_source",
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
                    "configuration_policy": "preserved_from_system_skill_without_redaction",
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
        "orchestrator_skill": orchestrator["skill_name"],
        "main_flow": orchestrator["main_flow"],
        "skills": skill_manifest,
        "artifacts": {
            "skills": "skills",
            "manifest": "capability-manifest.json",
            "report": "distillation-report.md",
            "map": "capability-map.mmd",
            "plan": "capability-plan.json",
            "agent_prompts": "agent_prompts.md",
        },
        "artifact_digests": {
            "agent_prompts": agent_prompt_digest,
            "portable_operational_contract": hashlib.sha256(
                (json.dumps(operational, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
            ).hexdigest(),
        },
    }
    return manifest


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
        "offset": offset,
        "limit": limit,
        "has_more": offset + limit < len(skills),
        "skills": skills[offset : offset + limit],
        "unsupported_formats": manifest.get("unsupported_formats", []),
        "artifacts": manifest.get("artifacts", {}),
    }


def finalize(args: argparse.Namespace) -> tuple[int, dict[str, Any]]:
    (
        output_root, relation_path, flow_path, relations, flow, relation_fingerprint,
        flow_fingerprint, claims_path, claims,
    ) = candidate_context(args)
    validation = validation_payload(
        claims, relations, flow, relation_path, flow_path, relation_fingerprint, flow_fingerprint, claims_path
    )
    if validation["status"] != "valid":
        atomic_json(output_root / "validation-errors.json", validation)
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
    manifest = load_json(Path(args.result).resolve(), MAX_SOURCE_BYTES)
    if manifest.get("status") != "complete":
        raise ContractError("capability-manifest.json 尚未 complete")
    if manifest.get("generator_contract_version") != GENERATOR_CONTRACT_VERSION:
        raise ContractError("能力蒸馏产物的生成器契约版本已过期；必须重新 prepare/finalize，禁止交付旧源码")
    _, relation_path, _, relations, _, relation_fingerprint, flow_fingerprint = source_context(args)
    source = manifest.get("source") if isinstance(manifest.get("source"), dict) else {}
    if source.get("relation_fingerprint") != relation_fingerprint or source.get("flow_fingerprint") != flow_fingerprint:
        raise ContractError("能力蒸馏产物的上游 fingerprint 已过期；不得交付旧源码")
    operational_path, _, operational_errors = operational_context(relations, relation_path)
    if operational_errors or source.get("operational_contract_fingerprint") != sha256_file(operational_path):
        raise ContractError("能力蒸馏产物的数据执行契约 fingerprint 已过期；不得交付旧源码")
    skills_root = Path(args.result).resolve().parent / "skills"
    generation_errors = validate_generated_skills(skills_root) if skills_root.is_dir() else ["缺少 skills 目录"]
    prompt_path = Path(args.result).resolve().parent / "agent_prompts.md"
    artifact_digests = manifest.get("artifact_digests") if isinstance(manifest.get("artifact_digests"), dict) else {}
    if not prompt_path.is_file():
        generation_errors.append("缺少可复制的 agent_prompts.md")
    elif artifact_digests.get("agent_prompts") != sha256_file(prompt_path):
        generation_errors.append("agent_prompts.md 内容摘要已变化")
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
        and item.get("configuration_policy") == "preserved_from_system_skill_without_redaction"
    }
    expected_services = {
        skill: "ocr_http_api" if kind == "ocr" else "vector_kb_http_api"
        for skill, kind in service_skills.items()
    }
    if declared_services != expected_services:
        generation_errors.append("系统基础 Skill 外部服务声明缺失或不一致")
    if generation_errors:
        raise ContractError("生成源码已损坏或不再可移植：" + "；".join(generation_errors))
    return 0, compact_summary(manifest, args.offset, args.limit)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("prepare", "preflight", "finalize"):
        command = commands.add_parser(name)
        command.add_argument("--relations", default="/workspace/outputs/data-relations/scenario-relationship.json")
        command.add_argument("--flow", default="/workspace/outputs/business-flow/business-flow.json")
        command.add_argument("--output", default="/workspace/outputs/capability-distillation")
        command.add_argument("--summary-limit", type=int, default=30)
        if name in {"preflight", "finalize"}:
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
