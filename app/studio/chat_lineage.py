"""Server-side routing and safe parsing for chat-driven data lineage.

The chat surface is the entry point for tracing.  This module deliberately
does *not* let a natural-language message become a shell command or an
unvalidated relationship override.  It only recognises a small set of
lineage intents and, for a correction, resolves table/field names against the
already traced schema.  Anything ambiguous is returned as a focused question
for the user instead of being guessed.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, Literal


ChatLineageIntentKind = Literal["trace", "correction", "approve_roles", "none"]

_TRACE_TERMS = (
    "数据链路追踪",
    "数据链路追溯",
    "链路追踪",
    "链路追溯",
    "data lineage",
    "lineage trace",
)
_TRACE_ACTION_TERMS = ("开始追踪", "重新追踪", "开始追溯", "重新追溯", "追踪数据", "追溯数据")
_CORRECTION_TERMS = (
    "不对",
    "不正确",
    "改用",
    "应该用",
    "应使用",
    "而不是",
    "重新关联",
    "关联追踪",
    "\u590d\u5408\u5173\u8054",
    "\u590d\u5408\u952e",
    "join",
)
_ROLE_APPROVAL_TERMS = (
    "确认文件角色",
    "确认角色标注",
    "确认表格角色",
    "确认角色",
    "批准文件角色",
)
_ROLE_READY_TERMS = (
    "我已经设置",
    "已经设置",
    "我已设置",
    "设置好了",
    "角色设置完成",
    "我已经标注",
    "已经标注",
    "我已标注",
    "标注好了",
    "角色标注完成",
)


def _normalized(value: Any) -> str:
    return re.sub(
        r"[\s`'\"，。；：、()（）【】\[\]{}<>《》_\-]+",
        "",
        unicodedata.normalize("NFKC", str(value or "")).casefold(),
    )


def _path_aliases(value: Any) -> set[str]:
    raw = str(value or "").replace("\\", "/").strip("/")
    if raw.casefold().startswith("data/"):
        raw = raw[5:]
    if not raw:
        return set()
    path = PurePosixPath(raw)
    values = {raw, path.name, path.stem}
    return {_normalized(item) for item in values if len(_normalized(item)) >= 2}


def _endpoint_key(file: str, table: str) -> tuple[str, str]:
    normalized_file = str(file or "").replace("\\", "/").strip("/")
    if normalized_file.casefold().startswith("data/"):
        normalized_file = normalized_file[5:]
    return _normalized(normalized_file), _normalized(table)


def is_trace_request(message: str) -> bool:
    """Whether a chat message asks to run the result-anchored trace action."""

    text = _normalized(message)
    if not text:
        return False
    return any(_normalized(term) in text for term in _TRACE_TERMS) or any(
        _normalized(term) in text for term in _TRACE_ACTION_TERMS
    )


def is_correction_request(message: str) -> bool:
    """Whether a message asks to revise the latest lineage relationship."""

    text = _normalized(message)
    if not text:
        return False
    return any(_normalized(term) in text for term in _CORRECTION_TERMS)


def is_role_approval_request(message: str) -> bool:
    """Whether the user explicitly confirms the current role snapshot."""

    text = _normalized(message)
    return bool(text) and any(_normalized(term) in text for term in _ROLE_APPROVAL_TERMS)


def is_role_ready_followup(message: str) -> bool:
    """Whether the user says the previously requested roles are now saved."""

    text = _normalized(message)
    return bool(text) and any(_normalized(term) in text for term in _ROLE_READY_TERMS)


def classify_lineage_chat(
    message: str,
    *,
    trace_available: bool,
    role_preflight_pending: bool = False,
) -> ChatLineageIntentKind:
    """Route correction before trace so “重新追踪，改用…” retains feedback."""

    if is_role_approval_request(message):
        return "approve_roles"
    if trace_available and is_correction_request(message):
        return "correction"
    if is_trace_request(message):
        return "trace"
    if role_preflight_pending and is_role_ready_followup(message):
        return "trace"
    return "none"


@dataclass(frozen=True, slots=True)
class ParsedTraceCorrection:
    correction: dict[str, Any] | None
    clarification: str = ""
    interpretation: str = ""
    corrections: tuple[dict[str, Any], ...] = ()
    semantic_notes: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class _Endpoint:
    file: str
    table: str
    fields: tuple[str, ...]
    aliases: tuple[str, ...]
    role: str = ""

    @property
    def label(self) -> str:
        return f"{self.file} / {self.table}"


def _business_label(endpoint: _Endpoint) -> str:
    if endpoint.role == "result_anchor":
        return "\u7ed3\u679c\u8868"
    if endpoint.role == "localized_semantic_evidence":
        return "\u89c4\u5219\u8868"
    table = endpoint.table.strip()
    if len(table) > 32 or re.match(r"^\d{2}-", table):
        filename = PurePosixPath(endpoint.file).stem.strip()
        if filename:
            return filename
    return table


def parse_trace_correction(
    message: str,
    *,
    trace: dict[str, Any],
    field_evidence: dict[str, Any] | None = None,
) -> ParsedTraceCorrection:
    """Backward-compatible entry point for conversational correction planning."""

    return plan_trace_correction(
        message,
        trace=trace,
        field_evidence=field_evidence,
    )


def plan_trace_correction(
    message: str,
    *,
    trace: dict[str, Any],
    field_evidence: dict[str, Any] | None = None,
) -> ParsedTraceCorrection:
    """Resolve one natural-language field correction against trace evidence.

    The user should be able to describe the business change in ordinary
    language.  The planner first tries to complete a safe, evidence-backed
    correction.  It asks for one business decision only when the supplied
    wording still identifies more than one valid relationship.
    """

    endpoints = _trace_endpoints(trace, field_evidence)
    if len(endpoints) < 2:
        return ParsedTraceCorrection(
            None,
            "\u5f53\u524d\u8fd8\u6ca1\u6709\u8db3\u591f\u7684\u94fe\u8def\u6837\u672c\u53ef\u4ee5\u4fee\u6b63\u3002\u8bf7\u5148\u5b8c\u6210\u4e00\u6b21\u6570\u636e\u94fe\u8def\u8ffd\u8e2a\u3002",
        )

    text = _normalized(message)
    mentioned = _mentioned_endpoints(text, endpoints)
    explicit, semantic_notes = _explicit_mapping_corrections(message, endpoints, trace)
    composite_sources = _composite_source_endpoints(message, endpoints)
    if explicit or composite_sources:
        planned = list(explicit)
        seen = {_relation_signature(item.correction) for item in planned if item.correction is not None}
        for source in composite_sources:
            inferred = _infer_counterpart_correction(
                message,
                trace=trace,
                source=source,
                endpoints=endpoints,
            )
            if inferred.correction is None:
                return inferred
            signature = _relation_signature(inferred.correction)
            if signature not in seen:
                planned.append(inferred)
                seen.add(signature)
        combined = _combine_correction_plans(planned, semantic_notes=semantic_notes)
        if combined is not None:
            return combined
    if semantic_notes:
        return ParsedTraceCorrection(
            None,
            "\n".join(semantic_notes),
            semantic_notes=semantic_notes,
        )
    if len(mentioned) == 0 and len(endpoints) == 2:
        mentioned = list(endpoints)
    if len(mentioned) == 1:
        return _infer_counterpart_correction(
            message,
            trace=trace,
            source=mentioned[0],
            endpoints=endpoints,
        )
    if len(mentioned) != 2:
        return ParsedTraceCorrection(None, _endpoint_question(mentioned, endpoints))
    return _plan_endpoint_pair(message, mentioned[0], mentioned[1])


def _trace_endpoints(
    trace: dict[str, Any],
    field_evidence: dict[str, Any] | None,
) -> tuple[_Endpoint, ...]:
    bundles = trace.get("bundles") if isinstance(trace.get("bundles"), list) else []
    bundle = next((item for item in bundles if isinstance(item, dict)), None)
    if bundle is None:
        return ()
    evidence_fields = _evidence_field_index(field_evidence)
    endpoints: list[_Endpoint] = []
    seen: set[tuple[str, str]] = set()
    for source in bundle.get("sources", []) if isinstance(bundle.get("sources"), list) else []:
        if not isinstance(source, dict):
            continue
        file = str(source.get("path", "")).replace("\\", "/").strip("/")
        table = str(source.get("table", "")).strip()
        endpoint = _endpoint_key(file, table)
        if not file or not table or endpoint in seen:
            continue
        seen.add(endpoint)
        fields = evidence_fields.get(endpoint)
        if not fields:
            raw = source.get("selected_columns") if isinstance(source.get("selected_columns"), list) else []
            fields = tuple(str(item).strip() for item in raw if str(item).strip())
        if not fields:
            continue
        aliases = set(_path_aliases(file))
        aliases.add(_normalized(table))
        aliases.add(_normalized(f"{file}{table}"))
        role = str(source.get("role", "")).strip()
        if role == "result_anchor":
            aliases.add(_normalized("\u7ed3\u679c\u8868"))
            aliases.add(_normalized("\u5386\u53f2\u7ed3\u679c"))
        endpoints.append(
            _Endpoint(
                file=file,
                table=table,
                fields=tuple(dict.fromkeys(fields)),
                aliases=tuple(alias for alias in aliases if len(alias) >= 2),
                role=role,
            )
        )

    # A user may identify a missing source table that the first heuristic
    # trace did not reach.  Include every *schema-backed* data endpoint in
    # the clarification/parser surface so such a correction can be proposed;
    # the signed role manifest and storage validation still decide whether it
    # is a current, non-ignored source before anything is persisted.
    for file_info in (
        field_evidence.get("files", [])
        if isinstance(field_evidence, dict) and isinstance(field_evidence.get("files"), list)
        else []
    ):
        if not isinstance(file_info, dict):
            continue
        file = str(file_info.get("path", "")).replace("\\", "/").strip("/")
        for table_info in file_info.get("tables", []) if isinstance(file_info.get("tables"), list) else []:
            if not isinstance(table_info, dict):
                continue
            table = str(table_info.get("table_name", "")).strip()
            endpoint = _endpoint_key(file, table)
            if not file or not table or endpoint in seen:
                continue
            fields = tuple(
                str(item.get("name", "")).strip()
                for item in table_info.get("columns", [])
                if isinstance(item, dict) and str(item.get("name", "")).strip()
            )
            if not fields:
                continue
            seen.add(endpoint)
            aliases = set(_path_aliases(file))
            aliases.add(_normalized(table))
            aliases.add(_normalized(f"{file}{table}"))
            endpoints.append(
                _Endpoint(
                    file=file,
                    table=table,
                    fields=tuple(dict.fromkeys(fields)),
                    aliases=tuple(alias for alias in aliases if len(alias) >= 2),
                )
            )
    return tuple(endpoints)


def _evidence_field_index(field_evidence: dict[str, Any] | None) -> dict[tuple[str, str], tuple[str, ...]]:
    if not isinstance(field_evidence, dict):
        return {}
    result: dict[tuple[str, str], tuple[str, ...]] = {}
    for file_info in field_evidence.get("files", []) if isinstance(field_evidence.get("files"), list) else []:
        if not isinstance(file_info, dict):
            continue
        path = str(file_info.get("path", "")).replace("\\", "/").strip("/")
        for table in file_info.get("tables", []) if isinstance(file_info.get("tables"), list) else []:
            if not isinstance(table, dict):
                continue
            table_name = str(table.get("table_name", "")).strip()
            fields = tuple(
                str(item.get("name", "")).strip()
                for item in table.get("columns", [])
                if isinstance(item, dict) and str(item.get("name", "")).strip()
            )
            if path and table_name and fields:
                result[(_normalized(path), _normalized(table_name))] = fields
                # The tracing artifact can be relative to `data/` while the
                # field evidence is relative to the data root (or vice versa).
                if path.casefold().startswith("data/"):
                    result[(_normalized(path[5:]), _normalized(table_name))] = fields
                else:
                    result[(_normalized(f"data/{path}"), _normalized(table_name))] = fields
    return result


def _mentioned_endpoints(text: str, endpoints: tuple[_Endpoint, ...]) -> list[_Endpoint]:
    positions: list[tuple[int, _Endpoint]] = []
    for endpoint in endpoints:
        matches = [text.find(alias) for alias in endpoint.aliases if alias and text.find(alias) >= 0]
        if matches:
            positions.append((min(matches), endpoint))
    positions.sort(key=lambda item: (item[0], item[1].label.casefold()))
    return [endpoint for _position, endpoint in positions]


def _mentioned_fields(text: str, endpoints: tuple[_Endpoint, _Endpoint]) -> list[tuple[_Endpoint, str]]:
    hits: list[tuple[int, _Endpoint, str]] = []
    for endpoint in endpoints:
        for field in endpoint.fields:
            normalized = _normalized(field)
            if len(normalized) < 2:
                continue
            position = text.find(normalized)
            if position >= 0:
                hits.append((position, endpoint, field))
    hits.sort(key=lambda item: (item[0], item[1].label.casefold(), item[2].casefold()))
    # A shared field occurs once in natural language but matches both tables;
    # keep it once so the shared-field branch above can reason about it.
    unique: list[tuple[_Endpoint, str]] = []
    seen: set[str] = set()
    for _position, endpoint, field in hits:
        token = _normalized(field)
        if token in seen:
            continue
        seen.add(token)
        unique.append((endpoint, field))
    return unique


def _explicit_mapping_corrections(
    message: str,
    endpoints: tuple[_Endpoint, ...],
    trace: dict[str, Any],
) -> tuple[tuple[ParsedTraceCorrection, ...], tuple[str, ...]]:
    """Read strict ``table.field -> table.field`` statements from feedback."""

    groups: dict[tuple[tuple[str, str], tuple[str, str]], list[tuple[_Endpoint, str, _Endpoint, str]]] = {}
    raw = unicodedata.normalize("NFKC", str(message or ""))
    for fragment in re.split(r"[;\uff1b\n&]", raw):
        match = re.search(
            r"(?P<left>.+?)\s*(?:-+>|=+>|[\u2013\u2014]+>|\u2192)\s*(?P<right>.+)",
            fragment,
        )
        if match is None:
            continue
        left = _endpoint_field_reference(match.group("left"), endpoints)
        right = _endpoint_field_reference(match.group("right"), endpoints)
        if left is None or right is None or left[0] == right[0]:
            continue
        source, source_field = left
        target, target_field = right
        key = (
            _endpoint_key(source.file, source.table),
            _endpoint_key(target.file, target.table),
        )
        pairs = groups.setdefault(key, [])
        if not any(
            item[1] == source_field and item[3] == target_field
            for item in pairs
        ):
            pairs.append((source, source_field, target, target_field))
    plans: list[ParsedTraceCorrection] = []
    semantic_notes: list[str] = []
    for pairs in groups.values():
        if not pairs or len(pairs) > 16:
            continue
        source = pairs[0][0]
        target = pairs[0][2]
        if _is_semantic_rule_constraint(source, target, trace):
            semantic_notes.append(_semantic_rule_note(source, target, trace))
            continue
        plans.append(
            _parsed_correction(
                message,
                source,
                target,
                [
                    {"source_field": item[1], "target_field": item[3]}
                    for item in pairs
                ],
            )
        )
    return tuple(plans), tuple(dict.fromkeys(semantic_notes))


def _is_semantic_rule_constraint(
    source: _Endpoint,
    target: _Endpoint,
    trace: dict[str, Any],
) -> bool:
    result = source if source.role == "result_anchor" else target
    rule = target if result == source else source
    if result.role != "result_anchor" or rule.role != "localized_semantic_evidence":
        return False
    bundles = trace.get("bundles") if isinstance(trace.get("bundles"), list) else []
    bundle = next((item for item in bundles if isinstance(item, dict)), {})
    evidence = bundle.get("semantic_evidence")
    items = evidence if isinstance(evidence, list) else [evidence]
    rule_key = _endpoint_key(rule.file, rule.table)
    for item in items:
        if not isinstance(item, dict):
            continue
        item_key = _endpoint_key(item.get("path", ""), item.get("table", ""))
        if item_key != rule_key or str(item.get("approved_role", "")).strip() != "rule":
            continue
        row = item.get("row") if isinstance(item.get("row"), dict) else {}
        values = row.get("values") if isinstance(row.get("values"), dict) else {}
        if str(item.get("snippet", "")).strip() or values:
            return True
    return False


def _semantic_rule_note(
    source: _Endpoint,
    target: _Endpoint,
    trace: dict[str, Any],
) -> str:
    result = source if source.role == "result_anchor" else target
    rule = target if result == source else source
    if _result_field_is_blank(trace, result, "\u8fdd\u89c4\u8bf4\u660e"):
        return (
            "\u5f53\u524d\u9009\u4e2d\u7684\u7ed3\u679c\u8bb0\u5f55\u6ca1\u6709\u53ef\u7528\u7684\u300c\u8fdd\u89c4\u8bf4\u660e\u300d\uff0c"
            "\u56e0\u6b64\u4e0d\u4f1a\u8981\u6c42\u5b83\u76f4\u63a5\u7b49\u540c\u300c"
            + _business_label(rule)
            + "\u300d\u4e2d\u7684\u89c4\u5219\u63cf\u8ff0\u3002"
            "\u6211\u5df2\u4fdd\u7559\u5f53\u524d\u5df2\u9a8c\u8bc1\u7684\u89c4\u5219\u4f9d\u636e\uff0c\u540e\u7eed\u4f1a\u7528\u5b83\u89e3\u91ca\u8fd9\u6761\u7ed3\u679c\u3002"
        )
    return (
        "\u6211\u5df2\u5c06\u300c"
        + _business_label(result)
        + "\u300d\u4e0e\u300c"
        + _business_label(rule)
        + "\u300d\u7684\u8bf4\u660e\u4f5c\u4e3a\u5b9a\u4f4d\u5177\u4f53\u89c4\u5219\u7684\u4f9d\u636e\u3002"
        "\u5f53\u524d\u94fe\u8def\u5df2\u5b58\u5728\u53ef\u9a8c\u8bc1\u7684\u89c4\u5219\u8bc1\u636e\uff0c\u540e\u7eed\u4f1a\u7528\u5b83\u89e3\u91ca\u8fd9\u6761\u7ed3\u679c\u3002"
    )


def _result_field_is_blank(trace: dict[str, Any], result: _Endpoint, field: str) -> bool:
    """Whether the selected result sample has no usable value for a field."""

    bundles = trace.get("bundles") if isinstance(trace.get("bundles"), list) else []
    bundle = next((item for item in bundles if isinstance(item, dict)), {})
    result_key = _endpoint_key(result.file, result.table)
    sources = bundle.get("sources") if isinstance(bundle.get("sources"), list) else []
    field_seen = False
    for source in sources:
        if not isinstance(source, dict):
            continue
        if _endpoint_key(source.get("path", ""), source.get("table", "")) != result_key:
            continue
        rows = source.get("rows") if isinstance(source.get("rows"), list) else []
        for row in rows:
            values = row.get("values") if isinstance(row, dict) and isinstance(row.get("values"), dict) else {}
            if field in values:
                field_seen = True
                return values[field] is None or not str(values[field]).strip()
    return not field_seen


def _endpoint_field_reference(
    value: str,
    endpoints: tuple[_Endpoint, ...],
) -> tuple[_Endpoint, str] | None:
    text = _normalized(value)
    matches: list[tuple[_Endpoint, str, int, int, int]] = []
    for endpoint in endpoints:
        aliases = sorted(endpoint.aliases, key=len, reverse=True)
        for alias in aliases:
            alias_position = text.find(alias) if alias else -1
            if alias_position < 0:
                continue
            suffix = text[alias_position + len(alias):]
            fields = [
                (field, suffix.find(token))
                for field in endpoint.fields
                if (token := _normalized(field)) and suffix.find(token) >= 0
            ]
            if not fields:
                continue
            field, distance = min(
                fields,
                key=lambda item: (item[1], -len(_normalized(item[0]))),
            )
            matches.append(
                (
                    endpoint,
                    field,
                    len(_normalized(field)),
                    distance,
                    alias_position,
                )
            )
    if not matches:
        return None
    matches.sort(
        key=lambda item: (item[3], -item[2], -item[4], item[0].label.casefold(), item[1].casefold())
    )
    best_endpoint, best_field, best_length, best_distance, best_alias_position = matches[0]
    if any(
        distance == best_distance
        and length == best_length
        and alias_position == best_alias_position
        and (endpoint != best_endpoint or field != best_field)
        for endpoint, field, length, distance, alias_position in matches[1:]
    ):
        return None
    return best_endpoint, best_field


def _composite_source_endpoints(
    message: str,
    endpoints: tuple[_Endpoint, ...],
) -> tuple[_Endpoint, ...]:
    """Find the table named in a free-text composite-key business request."""

    sources: list[_Endpoint] = []
    raw = unicodedata.normalize("NFKC", str(message or ""))
    for fragment in re.split(r"[;\uff1b\n]", raw):
        text = _normalized(fragment)
        marker_positions = [
            text.find(marker)
            for marker in ("\u590d\u5408\u5173\u8054", "\u590d\u5408\u952e", "\u7ec4\u6210\u590d\u5408")
            if text.find(marker) >= 0
        ]
        if not marker_positions:
            continue
        marker_position = min(marker_positions)
        mentions: list[tuple[int, _Endpoint]] = []
        for endpoint in endpoints:
            positions = [
                text.find(alias)
                for alias in endpoint.aliases
                if alias and text.find(alias) >= 0
            ]
            if positions:
                mentions.append((min(positions), endpoint))
        before = [item for item in mentions if item[0] <= marker_position]
        selected = before[0] if len(before) == 1 else None
        if selected is None and len(mentions) == 1:
            selected = mentions[0]
        if selected is not None and selected[1] not in sources:
            sources.append(selected[1])
    return tuple(sources)


def _relation_signature(correction: dict[str, Any]) -> tuple[tuple[str, str], tuple[str, str]]:
    source = _endpoint_key(correction.get("source_file", ""), correction.get("source_table", ""))
    target = _endpoint_key(correction.get("target_file", ""), correction.get("target_table", ""))
    return tuple(sorted((source, target)))  # type: ignore[return-value]


def _combine_correction_plans(
    plans: list[ParsedTraceCorrection],
    *,
    semantic_notes: tuple[str, ...] = (),
) -> ParsedTraceCorrection | None:
    corrections: list[dict[str, Any]] = []
    interpretations: list[str] = []
    for plan in plans:
        items = plan.corrections or ((plan.correction,) if plan.correction is not None else ())
        for correction in items:
            if correction not in corrections:
                corrections.append(correction)
        if plan.interpretation and plan.interpretation not in interpretations:
            interpretations.append(plan.interpretation)
    if not corrections:
        return None
    return ParsedTraceCorrection(
        corrections[0],
        interpretation="\n".join([*interpretations, *semantic_notes]),
        corrections=tuple(corrections),
        semantic_notes=semantic_notes,
    )


def _plan_endpoint_pair(
    message: str,
    source: _Endpoint,
    target: _Endpoint,
) -> ParsedTraceCorrection:
    """Turn one identified endpoint pair into a correction when evidence permits."""

    text = _normalized(message)
    positive_clause = re.split(
        r"(?:\u800c\u4e0d\u662f|\u4e0d\u662f\u7528|\u4e0d\u7528|\u4e0d\u8981\u7528|\u66ff\u4ee3)",
        text,
        maxsplit=1,
    )[0]
    shared = _shared_field_plan(positive_clause, source, target)
    if shared is not None:
        key_pairs, alternative_note = shared
        return _parsed_correction(
            message,
            source,
            target,
            key_pairs,
            alternative_note=alternative_note,
        )

    # Preserve explicit cross-name mappings such as "order number" to
    # "business number".  Those cannot be inferred as shared field names.
    mentioned_fields = _mentioned_fields(positive_clause, (source, target))
    source_fields = [field for endpoint, field in mentioned_fields if endpoint == source]
    target_fields = [field for endpoint, field in mentioned_fields if endpoint == target]
    if source_fields and not target_fields and all(field in target.fields for field in source_fields):
        target_fields = list(source_fields)
    elif target_fields and not source_fields and all(field in source.fields for field in target_fields):
        source_fields = list(target_fields)
    if (
        not source_fields
        or len(source_fields) != len(target_fields)
        or len(source_fields) > 16
    ):
        return ParsedTraceCorrection(None, _pair_field_question(source, target))
    return _parsed_correction(
        message,
        source,
        target,
        [
            {"source_field": source_field, "target_field": target_field}
            for source_field, target_field in zip(source_fields, target_fields, strict=True)
        ],
    )


def _infer_counterpart_correction(
    message: str,
    *,
    trace: dict[str, Any],
    source: _Endpoint,
    endpoints: tuple[_Endpoint, ...],
) -> ParsedTraceCorrection:
    """Infer one counterpart only when trace evidence makes it unique."""

    candidates: list[tuple[_Endpoint, ParsedTraceCorrection]] = []
    for target in endpoints:
        if target == source:
            continue
        plan = _plan_endpoint_pair(message, source, target)
        if plan.correction is not None:
            candidates.append((target, plan))
    if not candidates:
        return ParsedTraceCorrection(None, _counterpart_question(source, ()))

    message_text = _normalized(message)
    requires_composite = any(
        marker in message_text
        for marker in ("\u590d\u5408\u5173\u8054", "\u590d\u5408\u952e", "\u7ec4\u6210\u590d\u5408")
    )
    if requires_composite:
        composite_candidates = [
            item
            for item in candidates
            if len(item[1].correction.get("key_pairs", [])) >= 2  # type: ignore[union-attr]
        ]
        if not composite_candidates:
            return ParsedTraceCorrection(
                None,
                _counterpart_question(source, tuple(item[0] for item in candidates)),
            )
        candidates = composite_candidates

    result_anchors = [item for item in candidates if item[0].role == "result_anchor"]
    if len(result_anchors) == 1:
        return result_anchors[0][1]

    linked = _linked_counterparts(trace, source)
    directly_linked = [
        item for item in candidates
        if _endpoint_key(item[0].file, item[0].table) in linked
    ]
    if len(directly_linked) == 1:
        return directly_linked[0][1]
    if len(directly_linked) > 1:
        return ParsedTraceCorrection(
            None,
            _counterpart_question(source, tuple(item[0] for item in directly_linked)),
        )
    if len(candidates) == 1:
        return candidates[0][1]
    return ParsedTraceCorrection(
        None,
        _counterpart_question(source, tuple(item[0] for item in candidates)),
    )


def _shared_field_plan(
    text: str,
    source: _Endpoint,
    target: _Endpoint,
) -> tuple[list[dict[str, str]], str] | None:
    """Resolve same-named business keys, including an explicit alternative."""

    mentions = _shared_field_mentions(text, source, target)
    if not mentions:
        return None
    groups = _field_groups(text, mentions)
    selected: list[str] = []
    alternative_note = ""
    for group in groups:
        viable = [
            token for token in group
            if _field_for_token(source, token) and _field_for_token(target, token)
        ]
        if not viable:
            continue
        chosen = max(
            viable,
            key=lambda token: (_field_preference(token), -viable.index(token)),
        )
        selected.append(chosen)
        if len(group) > 1 and chosen != group[0]:
            alternative_note = (
                "\u5df2\u4f18\u5148\u4f7f\u7528\u66f4\u7a33\u5b9a\u7684\u300c"
                + _field_for_token(source, chosen)
                + "\u300d\uff0c\u907f\u514d\u540c\u540d\u76ee\u5f55\u5e26\u6765\u7684\u8bef\u5339\u914d\u3002"
            )
    selected = list(dict.fromkeys(selected))
    if not selected or len(selected) > 16:
        return None
    return (
        [
            {
                "source_field": _field_for_token(source, token),
                "target_field": _field_for_token(target, token),
            }
            for token in selected
        ],
        alternative_note,
    )


def _shared_field_mentions(
    text: str,
    source: _Endpoint,
    target: _Endpoint,
) -> list[tuple[int, int, str]]:
    shared = {
        _normalized(field)
        for field in source.fields
        if len(_normalized(field)) >= 2
    } & {
        _normalized(field)
        for field in target.fields
        if len(_normalized(field)) >= 2
    }
    hits = [
        (position, position + len(token), token)
        for token in shared
        if (position := text.find(token)) >= 0
    ]
    hits.sort(key=lambda item: (item[0], -(item[1] - item[0]), item[2]))
    selected: list[tuple[int, int, str]] = []
    for hit in hits:
        if selected and hit[0] < selected[-1][1]:
            continue
        selected.append(hit)
    return selected


def _field_groups(
    text: str,
    mentions: list[tuple[int, int, str]],
) -> list[list[str]]:
    groups: list[list[str]] = []
    current: list[str] = []
    previous_end = -1
    for start, end, token in mentions:
        connector = text[previous_end:start] if previous_end >= 0 else ""
        if current and _alternative_connector(connector):
            current.append(token)
        else:
            if current:
                groups.append(current)
            current = [token]
        previous_end = end
    if current:
        groups.append(current)
    return groups


def _alternative_connector(value: str) -> bool:
    return any(marker in value for marker in ("\u6216\u8005", "\u6216", "or", "/"))


def _field_for_token(endpoint: _Endpoint, token: str) -> str:
    return next(
        (field for field in endpoint.fields if _normalized(field) == token),
        "",
    )


def _field_preference(token: str) -> int:
    """Rank stable identifiers above display labels for a stated alternative."""

    if any(marker in token for marker in ("\u7f16\u7801", "\u4ee3\u7801", "code")):
        return 30
    if "id" in token or "\u53f7" in token:
        return 20
    if "\u540d\u79f0" in token or "name" in token:
        return 10
    return 0


def _parsed_correction(
    message: str,
    source: _Endpoint,
    target: _Endpoint,
    key_pairs: list[dict[str, str]],
    *,
    alternative_note: str = "",
) -> ParsedTraceCorrection:
    fields = " + ".join(pair["source_field"] for pair in key_pairs)
    interpretation = (
        "\u6211\u7406\u89e3\u4e3a\uff1a\u300c"
        + _business_label(source)
        + "\u300d\u4e0e\u300c"
        + _business_label(target)
        + "\u300d\u5e94\u4f7f\u7528\u300c"
        + fields
        + "\u300d\u4f5c\u4e3a\u590d\u5408\u5173\u8054\u3002"
    )
    if alternative_note:
        interpretation += alternative_note
    correction = {
        "source_file": source.file,
        "source_table": source.table,
        "target_file": target.file,
        "target_table": target.table,
        "key_pairs": key_pairs,
        "reason": str(message).strip()[:4000],
    }
    return ParsedTraceCorrection(
        correction,
        interpretation=interpretation,
        corrections=(correction,),
    )


def _linked_counterparts(trace: dict[str, Any], source: _Endpoint) -> set[tuple[str, str]]:
    bundles = trace.get("bundles") if isinstance(trace.get("bundles"), list) else []
    bundle = next((item for item in bundles if isinstance(item, dict)), {})
    links = bundle.get("links") if isinstance(bundle.get("links"), list) else []
    source_key = _endpoint_key(source.file, source.table)
    counterparts: set[tuple[str, str]] = set()
    for link in links:
        if not isinstance(link, dict):
            continue
        left = _endpoint_key(link.get("source_file", ""), link.get("source_table", ""))
        right = _endpoint_key(link.get("target_file", ""), link.get("target_table", ""))
        if left == source_key:
            counterparts.add(right)
        elif right == source_key:
            counterparts.add(left)
    return counterparts


def _endpoint_question(
    mentioned: list[_Endpoint],
    endpoints: tuple[_Endpoint, ...],
) -> str:
    if len(mentioned) == 1:
        return _counterpart_question(mentioned[0], ())
    if len(mentioned) > 2:
        return (
            "\u8fd9\u6b21\u9700\u8981\u4fee\u6b63\u54ea\u4e00\u5bf9\u8868\u7684\u5173\u8054\uff1f\u53ef\u9009\uff1a"
            + _table_choices(mentioned)
            + "\u3002"
        )
    return (
        "\u8bf7\u786e\u8ba4\uff1a\u8fd9\u6b21\u9700\u8981\u91cd\u65b0\u5173\u8054\u54ea\u4e24\u5f20\u8868\uff1f"
        + ("\u53ef\u9009\uff1a" + _table_choices(endpoints) + "\u3002" if endpoints else "")
    )


def _counterpart_question(source: _Endpoint, candidates: tuple[_Endpoint, ...]) -> str:
    prompt = (
        "\u6211\u5df2\u7406\u89e3\u300c"
        + _business_label(source)
        + "\u300d\u9700\u8981\u6539\u4e3a\u590d\u5408\u5173\u8054\u3002\u5b83\u8981\u4e0e\u54ea\u5f20\u8868\u91cd\u65b0\u5173\u8054\uff1f"
    )
    return prompt + ("\u53ef\u9009\uff1a" + _table_choices(candidates) + "\u3002" if candidates else "")


def _pair_field_question(source: _Endpoint, target: _Endpoint) -> str:
    return (
        "\u300c"
        + _business_label(source)
        + "\u300d\u4e0e\u300c"
        + _business_label(target)
        + "\u300d\u8981\u7528\u54ea\u4e9b\u5b57\u6bb5\u7ec4\u6210\u8fd9\u6761\u5173\u8054\uff1f"
    )


def _table_choices(endpoints: tuple[_Endpoint, ...] | list[_Endpoint]) -> str:
    labels = list(dict.fromkeys(_business_label(item) for item in endpoints if item.table))[:3]
    return "\u3001".join(labels)


def _correction_format_hint(endpoints: tuple[_Endpoint, ...] | list[_Endpoint]) -> str:
    return _endpoint_question([], tuple(endpoints))


__all__ = [
    "ParsedTraceCorrection",
    "classify_lineage_chat",
    "is_correction_request",
    "is_role_approval_request",
    "is_role_ready_followup",
    "is_trace_request",
    "plan_trace_correction",
    "parse_trace_correction",
]
