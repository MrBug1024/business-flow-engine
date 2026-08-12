"""Compatibility helpers for package-oriented Business Flow MCP hosts.

The host's published-package contract expects a ``main_skill`` directory and
knowledge helper modules.  These helpers keep that contract while delegating
all business retrieval to the generated portable scenario executor.
"""

from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path
from typing import Any


MAIN_SKILL = Path(__file__).resolve().parents[1]
EXECUTOR_PATH = MAIN_SKILL / "scripts" / "execute_scenario.py"
_EXECUTOR: Any | None = None
_SPLIT_RE = re.compile(r"[、，,；;。:：_\\-\\s（）()\\\\/\"'“”‘’]+")


def executor() -> Any:
    global _EXECUTOR
    if _EXECUTOR is not None:
        return _EXECUTOR
    spec = importlib.util.spec_from_file_location("portable_platform_executor", EXECUTOR_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Missing generated executor: {EXECUTOR_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    _EXECUTOR = module
    return module


def _rows(payload: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        row
        for result in payload.get("results", []) if isinstance(result, dict)
        for row in result.get("rows", []) if isinstance(row, dict)
    ]


def request_text(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        return " ".join(str(item).strip() for item in value.values() if str(item).strip())
    if isinstance(value, (list, tuple)):
        return " ".join(str(item).strip() for item in value if str(item).strip())
    return str(value or "").strip()


def _artifact_path(value: Any) -> str:
    """Normalize executor artifact metadata for legacy package hosts.

    The modern executor returns a provenance-bearing artifact object. Older
    hosts expect ``artifact`` to be a string and otherwise raise while trying
    to call ``Path(artifact)``. Keep the rich object under a separate field so
    an updated host can pass the full transaction through unchanged.
    """

    if isinstance(value, dict):
        return str(value.get("path", "")).strip()
    return str(value or "").strip()


def host_action_envelope(payload: dict[str, Any]) -> dict[str, Any]:
    """Add a stable response bridge without losing the executor transaction.

    ``scenario_execution`` is intentionally not a legacy tabular export: a
    positive count means bounded evidence groups/chunks are available, not a
    final business conclusion. The host must pass ``agent_handoff`` (or its
    artifact) to the Agent before it delivers an answer. This bridge prevents
    old hosts from crashing on an artifact dictionary while making that
    requirement machine-readable for newer adapters.
    """

    evidence_artifact = payload.get("artifact")
    handoff_artifact = payload.get("agent_handoff")
    deterministic = payload.get("deterministic_result")
    candidate = payload.get("candidate_evidence")
    documents = payload.get("document_evidence")
    row_count = 0
    row_semantics = "no_materialized_business_rows"
    if isinstance(deterministic, dict) and isinstance(deterministic.get("rows"), list):
        row_count = len(deterministic["rows"])
        row_semantics = "deterministic_business_result_rows"
    elif isinstance(candidate, dict) and isinstance(candidate.get("records"), list):
        row_count = len(candidate["records"])
        row_semantics = "bounded_evidence_record_groups_not_final_business_rows"
    elif isinstance(documents, dict):
        sources = documents.get("sources") if isinstance(documents.get("sources"), list) else []
        row_count = sum(
            int(source.get("hit_count_returned", 0))
            for source in sources if isinstance(source, dict)
        )
        if row_count:
            row_semantics = "bounded_document_evidence_hits_not_final_business_rows"

    result = dict(payload)
    result.update({
        "mode": "scenario_execution",
        # Compatibility with hosts that expect a numeric result count and a
        # string artifact path. Never erase the modern metadata below.
        "rows": row_count,
        "row_semantics": row_semantics,
        "artifact": _artifact_path(evidence_artifact),
        "evidence_artifact": evidence_artifact,
        "agent_handoff_artifact": handoff_artifact,
        "host_response_contract": {
            "kind": "portable_business_request_transaction_result",
            "status": result.get("status", ""),
            "normal_next_action": "read_agent_handoff_then_deliver_once",
            "agent_handoff_path": _artifact_path(handoff_artifact),
            "evidence_artifact_path": _artifact_path(evidence_artifact),
            "requires_structured_passthrough": True,
            "legacy_row_semantics": row_semantics,
        },
    })
    return result


def search(keyword: str, limit: int, data_dir: str) -> list[dict[str, Any]]:
    module = executor()
    terms = module.unique_terms(keyword)
    if not terms:
        return []
    payload = module.run([
        "search-rules", "--data-root", str(data_dir), "--max-rows", str(max(1, min(int(limit), 200))),
        *[part for term in terms[:12] for part in ("--term", term)],
    ])
    return _rows(payload)


def list_all(limit: int, data_dir: str) -> list[dict[str, Any]]:
    module = executor()
    contract = module.load_json(module.DEFAULT_CONTRACT)
    source_map = module.source_map(contract)
    rule_ids = [str(item) for item in contract.get("rule_source_ids", []) if str(item) in source_map]
    if not rule_ids:
        return []
    view_name = str(source_map[rule_ids[0]].get("view_name", ""))
    if not view_name:
        return []
    reader = module.tabular_runtime()
    relation = reader.quote_identifier(view_name)
    payload = module.run([
        "query", "--data-root", str(data_dir), "--max-rows", str(max(1, min(int(limit), 200))),
        "--sql", f"SELECT * FROM {relation}",
    ])
    columns = [str(item) for item in payload.get("columns", [])]
    return [
        {column: row[index] if index < len(row) else None for index, column in enumerate(columns)}
        for row in payload.get("rows", []) if isinstance(row, list)
    ]


def columns(data_dir: str) -> list[str]:
    rows = list_all(1, data_dir)
    return list(rows[0]) if rows else []


def knowledge_table_name() -> str:
    """Use the generated host-facing name instead of a fixed implementation name."""
    try:
        domain = json.loads((MAIN_SKILL / "domain_knowledge.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return "rules"
    name = domain.get("knowledge_table") if isinstance(domain, dict) else ""
    return str(name).strip() or "rules"


def dispatch_config() -> dict[str, Any]:
    try:
        payload = json.loads((MAIN_SKILL / "dispatch_config.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def all_knowledge_rows(limit: int, data_dir: str) -> list[dict[str, Any]]:
    """Read the declared rule source without turning a request into extra search terms.

    This matches the host's knowledge-engine contract: first select rule rows
    using the submitted parameters, then let the Agent query business data.
    """
    module = executor()
    contract = module.load_json(module.DEFAULT_CONTRACT)
    source_map = module.source_map(contract)
    source_ids = [str(item) for item in contract.get("rule_source_ids", []) if str(item) in source_map]
    if not source_ids:
        return []
    source = source_map[source_ids[0]]
    view_name = str(source.get("view_name", "")).strip()
    if not view_name:
        return []
    reader = module.tabular_runtime()
    payload = reader.query_contract(
        contract,
        Path(data_dir).expanduser().resolve(),
        f"SELECT * FROM {reader.quote_identifier(view_name)}",
        max(1, min(int(limit), 20_000)),
    )
    columns = [str(item) for item in payload.get("columns", [])]
    return [
        {column: row[index] if index < len(row) else None for index, column in enumerate(columns)}
        for row in payload.get("rows", []) if isinstance(row, list)
    ]


def _tokens(value: str, prefixes: tuple[str, ...]) -> list[str]:
    tokens: list[str] = []
    for part in _SPLIT_RE.split(str(value or "")):
        token = part.strip()
        for prefix in prefixes:
            if token.startswith(prefix) and len(token) > len(prefix) + 1:
                token = token[len(prefix):]
                break
        if 2 <= len(token) <= 14 and token not in tokens:
            tokens.append(token)
    return tokens[:12]


def _filter_knowledge(rows: list[dict[str, Any]], params: Any) -> list[dict[str, Any]]:
    """Apply the published host's generic keyword/exact-field filter semantics."""
    if params is None:
        return rows
    if isinstance(params, str):
        params = {"keyword": params}
    if not isinstance(params, dict):
        return rows
    selected = list(rows)
    keyword = str(params.get("keyword", "")).strip()
    if keyword:
        corpus = [" ".join(str(value or "") for value in row.values()) for row in selected]
        direct = [row for row, text in zip(selected, corpus) if keyword in text]
        if direct:
            selected = direct
        else:
            prefixes = tuple(str(item) for item in dispatch_config().get("filter_strip_prefixes", []) if str(item))
            tokens = _tokens(keyword, prefixes)
            if tokens:
                scored = [
                    (sum(token in text for token in tokens), index, row)
                    for index, (row, text) in enumerate(zip(selected, corpus))
                ]
                threshold = max(1, (len(tokens) + 1) // 2)
                matches = [item for item in scored if item[0] >= threshold]
                if not matches:
                    matches = [item for item in scored if item[0] >= 1]
                if matches:
                    selected = [item[2] for item in sorted(matches, key=lambda item: (-item[0], item[1]))[:5]]
                else:
                    selected = []
    for column, value in params.items():
        if column == "keyword" or not selected or column not in selected[0]:
            continue
        wanted = {str(item).strip() for item in value} if isinstance(value, (list, tuple, set)) else {str(value).strip()}
        selected = [row for row in selected if str(row.get(column, "")).strip() in wanted]
    return selected


def produce(
    output_id: str,
    data_dir: str,
    out_dir: str = "",
    params: Any = None,
    max_rows: int = 50,
) -> dict[str, Any]:
    """Run the generated scenario executor through the package-host entrypoint.

    Older compatibility releases returned only matching knowledge rows here.
    That forced a host Agent to reconstruct the remaining business workflow
    with ad-hoc SQL, and it re-ran rule selection for every follow-up.  The
    portable executor already owns rule selection, source preflight, bounded
    evidence collection and declared-link validation, so this adapter must
    delegate to it rather than expose a weaker parallel workflow.
    """
    request = request_text(params) or request_text(output_id)
    if not request:
        raise ValueError("A business request is required")
    try:
        bounded_rows = max(1, min(int(max_rows), 200))
    except (TypeError, ValueError) as exc:
        raise ValueError("max_rows must be an integer") from exc

    argv = [
        "execute",
        "--request", request,
        "--data-root", str(data_dir),
        "--max-rows", str(bounded_rows),
    ]
    if str(out_dir or "").strip():
        output = Path(out_dir).expanduser() / "scenario-evidence.json"
        argv.extend(["--output", str(output)])
    payload = executor().run(argv)
    if not isinstance(payload, dict):
        raise RuntimeError("Generated scenario executor returned an invalid payload")
    payload.setdefault("mode", "scenario_execution")
    payload.setdefault(
        "guidance",
        "Use the returned selected_rule, candidate_evidence, execution_steps and "
        "next_step as one execution transaction. Do not restart rule search unless "
        "next_step explicitly reports a concrete missing input.",
    )
    return host_action_envelope(payload)
