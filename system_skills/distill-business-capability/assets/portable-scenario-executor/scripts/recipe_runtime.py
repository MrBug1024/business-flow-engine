"""Validated, declarative rule recipes for portable scenario executors.

The executor intentionally does not turn arbitrary natural-language policy into
SQL at runtime.  When a rule can be represented by a reviewed recipe, this
module evaluates it deterministically.  Rules outside the supported recipe
catalog stay on the evidence-handoff path instead of producing a guessed
business conclusion.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Sequence


class RecipeError(ValueError):
    """A portable recipe is malformed or incompatible with the bound data."""


SUPPORTED_KINDS = {
    "grouped_cooccurrence",
    "grouped_cooccurrence_from_rule_text",
}
SUPPORTED_OPERATORS = {"contains", "equals"}
RECIPE_VERIFICATION_SCHEMA_VERSION = 1


def canonical_rule_fingerprint(row: dict[str, Any]) -> str:
    """Fingerprint the complete selected rule, not a historical data sample."""
    payload = {
        str(key): "" if value is None else str(value)
        for key, value in sorted(row.items(), key=lambda item: str(item[0]))
    }
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def load_recipes(path: Path) -> list[dict[str, Any]]:
    """Read an optional reviewed recipe catalog from the package references."""
    if not path.is_file():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RecipeError(f"Invalid compiled recipe catalog: {exc}") from exc
    if not isinstance(payload, dict):
        raise RecipeError("Compiled recipe catalog must be a JSON object")
    if payload.get("schema_version") != 1:
        raise RecipeError("Compiled recipe catalog schema_version must be 1")
    recipes = payload.get("recipes", [])
    if not isinstance(recipes, list):
        raise RecipeError("Compiled recipe catalog recipes must be a list")
    return [item for item in recipes if isinstance(item, dict)]


def recipe_catalog_fingerprint(path: Path) -> str:
    """Return the byte-level fingerprint used to bind replay evidence to a catalog.

    A replay approval is meaningful only for the exact catalog that was
    exercised.  Fingerprinting the original bytes (rather than a parsed JSON
    projection) also catches an otherwise invisible change to a selector,
    predicate, or output projection.
    """
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as exc:
        raise RecipeError(f"Could not fingerprint compiled recipe catalog: {exc}") from exc


def _unverified_recipe_catalog_status(
    status: str, reason: str, *, verification_path: Path | None = None,
    verified_recipe_ids: Sequence[str] = (), catalog_fingerprint: str = "",
) -> dict[str, Any]:
    return {
        "status": status,
        "verified": False,
        "certificate_validated": False,
        "reason": reason,
        "verification_path": str(verification_path) if verification_path is not None else "",
        "catalog_fingerprint": catalog_fingerprint,
        "verified_recipe_ids": sorted({str(item) for item in verified_recipe_ids if str(item)}),
    }


def inspect_recipe_verification(
    verification_path: Path | None, catalog_path: Path, recipes: Sequence[dict[str, Any]],
) -> dict[str, Any]:
    """Load a fail-closed replay-verification certificate for this catalog.

    ``compiled-recipes.json`` says what *could* be executed.  It is not proof
    that the generated predicates reproduce an approved business result.
    Deterministic execution therefore needs a separately generated, catalog-
    bound certificate with an explicit per-recipe allow-list.  Old packages
    have no such file and deliberately remain on the evidence-handoff path.

    This validates only package-local integrity and shape.  The distillation
    runner is responsible for producing the certificate after real replay;
    this runtime never upgrades a hand-written replay report by itself.
    """
    if not recipes:
        try:
            catalog_fingerprint = recipe_catalog_fingerprint(catalog_path)
        except RecipeError:
            # ``load_recipes`` intentionally treats a missing optional catalog
            # as an empty one.  Preserve that backwards-compatible path for
            # document-only and evidence-only packages.
            catalog_fingerprint = ""
        return {
            "status": "not_required",
            "verified": True,
            "certificate_validated": True,
            "reason": "No compiled deterministic recipe is declared.",
            "verification_path": str(verification_path) if verification_path is not None else "",
            "catalog_fingerprint": catalog_fingerprint,
            "verified_recipe_ids": [],
        }
    if verification_path is None or not verification_path.is_file():
        return _unverified_recipe_catalog_status(
            "missing_recipe_verification",
            "No catalog-bound real-replay verification certificate is present; compiled recipes may only provide evidence for human judgment.",
            verification_path=verification_path,
        )
    try:
        payload = json.loads(verification_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return _unverified_recipe_catalog_status(
            "invalid_recipe_verification",
            f"Recipe verification certificate cannot be read safely: {exc}",
            verification_path=verification_path,
        )
    if not isinstance(payload, dict):
        return _unverified_recipe_catalog_status(
            "invalid_recipe_verification",
            "Recipe verification certificate must be a JSON object.",
            verification_path=verification_path,
        )
    catalog_fingerprint = recipe_catalog_fingerprint(catalog_path)
    supplied_fingerprint = str(payload.get("recipe_catalog_fingerprint", ""))
    verified_ids_raw = payload.get("verified_recipe_ids", [])
    verified_ids = (
        [str(item) for item in verified_ids_raw if isinstance(item, (str, int, float)) and str(item)]
        if isinstance(verified_ids_raw, list) else []
    )
    if payload.get("schema_version") != RECIPE_VERIFICATION_SCHEMA_VERSION:
        return _unverified_recipe_catalog_status(
            "invalid_recipe_verification",
            f"Recipe verification certificate schema_version must be {RECIPE_VERIFICATION_SCHEMA_VERSION}.",
            verification_path=verification_path,
            verified_recipe_ids=verified_ids,
            catalog_fingerprint=catalog_fingerprint,
        )
    if supplied_fingerprint != catalog_fingerprint:
        return _unverified_recipe_catalog_status(
            "recipe_verification_catalog_mismatch",
            "Recipe verification certificate belongs to a different compiled recipe catalog.",
            verification_path=verification_path,
            verified_recipe_ids=verified_ids,
            catalog_fingerprint=catalog_fingerprint,
        )
    verified_claim = (
        payload.get("status") == "verified"
        and payload.get("verifiable") is True
        and payload.get("publishable") is True
    )
    if not verified_claim:
        return _unverified_recipe_catalog_status(
            "unverified_recipe_catalog",
            "The catalog has not been marked verified by the real replay runner.",
            verification_path=verification_path,
            verified_recipe_ids=verified_ids,
            catalog_fingerprint=catalog_fingerprint,
        )
    declared_ids = {
        str(recipe.get("id", "")) for recipe in recipes
        if isinstance(recipe, dict) and str(recipe.get("id", ""))
    }
    unknown_ids = sorted(set(verified_ids) - declared_ids)
    if unknown_ids:
        return _unverified_recipe_catalog_status(
            "invalid_recipe_verification",
            "Recipe verification certificate names recipe id(s) absent from this catalog: " + ", ".join(unknown_ids),
            verification_path=verification_path,
            verified_recipe_ids=verified_ids,
            catalog_fingerprint=catalog_fingerprint,
        )
    if not verified_ids:
        return _unverified_recipe_catalog_status(
            "unverified_recipe_catalog",
            "The real replay runner did not explicitly verify any compiled recipe id.",
            verification_path=verification_path,
            verified_recipe_ids=verified_ids,
            catalog_fingerprint=catalog_fingerprint,
        )
    return {
        "status": "verified",
        "verified": True,
        "certificate_validated": True,
        "reason": "Catalog-bound real replay verification is present for the listed recipe ids.",
        "verification_path": str(verification_path),
        "catalog_fingerprint": catalog_fingerprint,
        "verified_recipe_ids": sorted(set(verified_ids)),
    }


def recipe_execution_gate(recipe: dict[str, Any], verification: dict[str, Any] | None) -> dict[str, Any]:
    """Decide whether a selected compiled recipe may produce a final fact.

    Fail closed for a missing, stale, malformed, or catalog-level-only
    certificate.  The caller can still use its bounded evidence path; it just
    must not label an un-replayed recipe result as deterministic.
    """
    recipe_id = str(recipe.get("id", ""))
    status = verification if isinstance(verification, dict) else {}
    verified_ids = {
        str(item) for item in status.get("verified_recipe_ids", [])
        if isinstance(item, (str, int, float)) and str(item)
    }
    if (
        status.get("status") == "verified"
        and status.get("verified") is True
        and status.get("certificate_validated") is True
        and recipe_id
        and recipe_id in verified_ids
    ):
        return {
            "status": "verified_recipe",
            "verified": True,
            "recipe_id": recipe_id,
            "catalog_fingerprint": status.get("catalog_fingerprint", ""),
            "verification_path": status.get("verification_path", ""),
            "reason": "This recipe id is explicitly covered by catalog-bound real replay verification.",
        }
    if not recipe_id:
        reason = "Compiled recipe has no stable id and cannot be matched to replay verification evidence."
    elif recipe_id not in verified_ids:
        reason = f"Compiled recipe {recipe_id} is not explicitly covered by real replay verification."
    else:
        reason = str(status.get("reason") or "Compiled recipe verification is unavailable.")
    return {
        "status": "unverified_recipe_evidence_only",
        "verified": False,
        "recipe_id": recipe_id,
        "catalog_fingerprint": status.get("catalog_fingerprint", ""),
        "verification_path": status.get("verification_path", ""),
        "verification_status": status.get("status", "missing_recipe_verification"),
        "reason": reason,
    }


def _selector_matches(selector: dict[str, Any], selected_rule: dict[str, Any]) -> bool:
    row = selected_rule.get("row") if isinstance(selected_rule.get("row"), dict) else {}
    source_id = str(selector.get("source_id", ""))
    if source_id and source_id != str(selected_rule.get("source_id", "")):
        return False
    fingerprint = str(selector.get("rule_fingerprint", ""))
    if fingerprint and fingerprint != canonical_rule_fingerprint(row):
        return False
    equals = selector.get("equals", {})
    if equals and not isinstance(equals, dict):
        raise RecipeError("recipe.rule_selector.equals must be an object")
    return all(str(row.get(str(field), "")) == str(value) for field, value in equals.items())


def _is_rule_family_selector(selector: dict[str, Any]) -> bool:
    """Whether a selector is a reviewed template for one complete rule row.

    Family selectors intentionally contain no historical rule value.  Their
    executable literals are derived only from the complete row selected at
    runtime, after that row has passed the normal unique-rule gate.
    """
    return str(selector.get("mode", "")) == "any_complete_rule"


def select_recipe(recipes: Sequence[dict[str, Any]], selected_rule: dict[str, Any] | None) -> dict[str, Any] | None:
    """Return exactly one matching recipe; ambiguity is a safe execution blocker."""
    if not selected_rule:
        return None
    exact_matches = [
        recipe for recipe in recipes
        if isinstance(recipe.get("rule_selector"), dict)
        and not _is_rule_family_selector(recipe["rule_selector"])
        and _selector_matches(recipe["rule_selector"], selected_rule)
    ]
    if len(exact_matches) > 1:
        raise RecipeError("More than one compiled recipe matches the selected governing rule")
    if exact_matches:
        return exact_matches[0]
    family_matches = [
        recipe for recipe in recipes
        if isinstance(recipe.get("rule_selector"), dict)
        and _is_rule_family_selector(recipe["rule_selector"])
        and _selector_matches(recipe["rule_selector"], selected_rule)
    ]
    if len(family_matches) > 1:
        raise RecipeError("More than one reviewed rule-family template matches the selected governing rule")
    return family_matches[0] if family_matches else None


def required_source_ids(recipe: dict[str, Any]) -> set[str]:
    source_id = str(recipe.get("source_id", ""))
    if not source_id:
        raise RecipeError("recipe.source_id is required")
    values = {source_id}
    values.update(str(item) for item in recipe.get("context_source_ids", []) if str(item))
    return values


def validate_recipe(recipe: dict[str, Any], source_columns: dict[str, list[str]]) -> None:
    kind = str(recipe.get("kind", ""))
    if kind not in SUPPORTED_KINDS:
        raise RecipeError(f"Unsupported compiled recipe kind: {kind or '<empty>'}")
    source_id = str(recipe.get("source_id", ""))
    columns = set(source_columns.get(source_id, []))
    if not columns:
        raise RecipeError(f"Compiled recipe source is not a declared runtime table: {source_id}")
    group_by = [str(item) for item in recipe.get("group_by", []) if str(item)]
    if not group_by:
        raise RecipeError("grouped_cooccurrence recipe requires group_by")
    missing_groups = sorted(set(group_by) - columns)
    if missing_groups:
        raise RecipeError(f"recipe.group_by fields are missing: {', '.join(missing_groups)}")
    if kind == "grouped_cooccurrence_from_rule_text":
        item_field = str(recipe.get("item_field", ""))
        if item_field not in columns:
            raise RecipeError("grouped_cooccurrence_from_rule_text requires item_field in its source")
        rule_text_fields = [str(item) for item in recipe.get("rule_text_fields", []) if str(item)]
        if not rule_text_fields:
            raise RecipeError("grouped_cooccurrence_from_rule_text requires rule_text_fields")
        minimum_terms = int(recipe.get("minimum_terms", 2))
        if minimum_terms < 2 or minimum_terms > 8:
            raise RecipeError("grouped_cooccurrence_from_rule_text minimum_terms must be 2-8")
        annotations_from_rule = recipe.get("result_annotations_from_rule", {})
        if not isinstance(annotations_from_rule, dict) or any(
            not str(output) or not str(source_field)
            for output, source_field in annotations_from_rule.items()
        ):
            raise RecipeError("result_annotations_from_rule must map non-empty output names to rule fields")
        return
    all_of = recipe.get("all_of", [])
    any_of = recipe.get("any_of", [])
    if not isinstance(all_of, list) or not all_of or not isinstance(any_of, list) or not any_of:
        raise RecipeError("grouped_cooccurrence recipe requires non-empty all_of and any_of predicates")
    for predicate in [*all_of, *any_of]:
        if not isinstance(predicate, dict):
            raise RecipeError("recipe predicates must be objects")
        field = str(predicate.get("field", ""))
        operator = str(predicate.get("operator", ""))
        if field not in columns:
            raise RecipeError(f"recipe predicate field is missing: {field or '<empty>'}")
        if operator not in SUPPORTED_OPERATORS:
            raise RecipeError(f"Unsupported recipe predicate operator: {operator or '<empty>'}")
        if not str(predicate.get("value", "")):
            raise RecipeError("recipe predicates require a non-empty literal value")
    measure = str(recipe.get("summary_measure", ""))
    if measure and measure not in columns:
        raise RecipeError(f"recipe.summary_measure field is missing: {measure}")
    result_fields = recipe.get("result_fields", [])
    if result_fields and (not isinstance(result_fields, list) or any(not str(item) for item in result_fields)):
        raise RecipeError("recipe.result_fields must be a list of non-empty source fields")
    missing_result_fields = sorted({str(item) for item in result_fields} - columns)
    if missing_result_fields:
        raise RecipeError(f"recipe.result_fields are missing: {', '.join(missing_result_fields)}")
    annotations = recipe.get("result_annotations", {})
    if not isinstance(annotations, dict) or any(not str(key) for key in annotations):
        raise RecipeError("recipe.result_annotations must be an object with non-empty keys")
    duplicate_annotation_fields = sorted(set(str(key) for key in annotations) & {str(item) for item in result_fields})
    if duplicate_annotation_fields:
        raise RecipeError(
            "recipe.result_annotations cannot overwrite result_fields: "
            + ", ".join(duplicate_annotation_fields)
        )
    emit = str(recipe.get("emit", "any_match_rows"))
    if emit not in {"any_match_rows", "all_group_rows"}:
        raise RecipeError("recipe.emit must be any_match_rows or all_group_rows")


QUOTED_TERM_PATTERN = re.compile(r"[\"\u201c\u201d\u300a\u300b]([^\"\u201c\u201d\u300a\u300b]{2,160})[\"\u201c\u201d\u300a\u300b]")


def _rule_text_terms(recipe: dict[str, Any], selected_rule: dict[str, Any]) -> list[str]:
    """Extract explicit literals from a selected governing record only.

    This is deliberately a parser for quotation delimiters, not a language
    model or a sample-value heuristic.  A family template remains unusable
    until a rule itself provides enough explicit terms; that failure is safer
    than fabricating a predicate from prose.
    """
    row = selected_rule.get("row") if isinstance(selected_rule.get("row"), dict) else {}
    fields = [str(item) for item in recipe.get("rule_text_fields", []) if str(item)]
    values = "\n".join(str(row.get(field, "")) for field in fields)
    terms: list[str] = []
    for raw in QUOTED_TERM_PATTERN.findall(values):
        term = re.sub(r"\s+", " ", raw).strip(" \t\r\n,，。；;：:")
        if len(term) < 2 or term in terms:
            continue
        # A parent phrase makes a shorter quoted phrase redundant.  Keeping
        # only the longer literal avoids over-constraining a runtime group.
        if any(term in existing and term != existing for existing in terms):
            continue
        terms = [existing for existing in terms if existing not in term]
        terms.append(term)
    minimum = int(recipe.get("minimum_terms", 2))
    if len(terms) < minimum:
        raise RecipeError(
            "The selected rule does not expose enough explicit quoted terms for its reviewed rule-family template"
        )
    return terms[:8]


def materialize_rule_family_recipe(recipe: dict[str, Any], selected_rule: dict[str, Any]) -> dict[str, Any]:
    """Bind a reviewed family template to one selected rule without Agent input."""
    if str(recipe.get("kind", "")) != "grouped_cooccurrence_from_rule_text":
        return recipe
    terms = _rule_text_terms(recipe, selected_rule)
    item_field = str(recipe["item_field"])
    annotations = {
        str(output): (selected_rule.get("row", {}) or {}).get(str(rule_field))
        for output, rule_field in (recipe.get("result_annotations_from_rule", {}) or {}).items()
    }
    generated = {
        **recipe,
        "id": f"{str(recipe.get('id', 'rule-family'))}:{canonical_rule_fingerprint(selected_rule.get('row', {}))[:12]}",
        "kind": "grouped_cooccurrence",
        "all_of": [{"field": item_field, "operator": "contains", "value": terms[0]}],
        "any_of": [
            {"field": item_field, "operator": "contains", "value": term}
            for term in terms[1:]
        ],
        "result_annotations": annotations,
        "rule_family": {
            "template_id": str(recipe.get("id", "")),
            "term_source_fields": [str(item) for item in recipe.get("rule_text_fields", []) if str(item)],
            "extracted_terms": terms,
        },
    }
    generated.pop("item_field", None)
    generated.pop("rule_text_fields", None)
    generated.pop("minimum_terms", None)
    generated.pop("result_annotations_from_rule", None)
    return generated


def _predicate_sql(reader: Any, predicate: dict[str, Any]) -> tuple[str, list[str]]:
    field = reader.quote_identifier(str(predicate["field"]))
    operator = str(predicate["operator"])
    value = str(predicate["value"])
    if operator == "contains":
        return f"coalesce(cast({field} as varchar), '') ILIKE ?", [f"%{value}%"]
    if operator == "equals":
        return f"cast({field} as varchar) = ?", [value]
    raise RecipeError(f"Unsupported recipe predicate operator: {operator}")


def execute_recipe(
    recipe: dict[str, Any],
    reader: Any,
    contract: dict[str, Any],
    connection: Any,
    source_columns: dict[str, list[str]],
    max_rows: int,
    selected_rule: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Evaluate a reviewed recipe in one parameterized, bounded SQL transaction."""
    if str(recipe.get("kind", "")) == "grouped_cooccurrence_from_rule_text":
        if not selected_rule:
            raise RecipeError("A selected governing rule is required for a rule-family template")
        recipe = materialize_rule_family_recipe(recipe, selected_rule)
    validate_recipe(recipe, source_columns)
    source_id = str(recipe["source_id"])
    source_by_id = {
        str(item.get("source_id", "")): item
        for item in contract.get("sources", []) if isinstance(item, dict)
    }
    source = source_by_id.get(source_id, {})
    relation_name = str(source.get("view_name", ""))
    if not relation_name:
        raise RecipeError(f"Compiled recipe source has no registered view: {source_id}")
    group_by = [str(item) for item in recipe["group_by"]]
    group_sql = ", ".join(reader.quote_identifier(item) for item in group_by)
    all_of = [item for item in recipe["all_of"] if isinstance(item, dict)]
    any_of = [item for item in recipe["any_of"] if isinstance(item, dict)]
    all_checks: list[str] = []
    any_checks: list[str] = []
    params: list[str] = []
    for index, predicate in enumerate(all_of):
        expression, values = _predicate_sql(reader, predicate)
        alias = f"_recipe_all_{index}"
        all_checks.append(f"max(case when {expression} then 1 else 0 end) over (partition by {group_sql}) as {alias}")
        params.extend(values)
    for index, predicate in enumerate(any_of):
        expression, values = _predicate_sql(reader, predicate)
        alias = f"_recipe_any_{index}"
        any_checks.append(f"max(case when {expression} then 1 else 0 end) over (partition by {group_sql}) as {alias}")
        params.extend(values)
    all_filter = " AND ".join(f"{alias} = 1" for alias in (f"_recipe_all_{index}" for index in range(len(all_of))))
    any_filter = " OR ".join(f"{alias} = 1" for alias in (f"_recipe_any_{index}" for index in range(len(any_of))))
    emit = str(recipe.get("emit", "any_match_rows"))
    emit_filter = ""
    if emit == "any_match_rows":
        predicates: list[str] = []
        for predicate in any_of:
            expression, values = _predicate_sql(reader, predicate)
            predicates.append(expression)
            params.extend(values)
        emit_filter = " AND (" + " OR ".join(predicates) + ")"
    measure = str(recipe.get("summary_measure", ""))
    measure_expression = (
        f"sum(try_cast({reader.quote_identifier(measure)} as double)) over ()"
        if measure else "cast(null as double)"
    )
    relation = reader.quote_identifier(relation_name)
    result_fields = [str(item) for item in recipe.get("result_fields", []) if str(item)]
    projection = ", ".join(reader.quote_identifier(field) for field in result_fields) if result_fields else "*"
    sql = (
        "WITH grouped AS ("
        f"SELECT *, {', '.join([*all_checks, *any_checks])} FROM {relation}"
        "), matched AS ("
        f"SELECT * FROM grouped WHERE {all_filter} AND ({any_filter}){emit_filter}"
        f") SELECT {projection}, count(*) over () AS _recipe_total_rows, "
        f"count(distinct concat_ws(chr(31), {group_sql})) over () AS _recipe_total_groups, "
        f"{measure_expression} AS _recipe_measure_sum FROM matched LIMIT {max(1, int(max_rows)) + 1}"
    )
    cursor = connection.execute(sql, params)
    payload = reader.cursor_payload(cursor, max(1, int(max_rows)))
    columns = [str(item) for item in payload.get("columns", [])]
    raw_rows = payload.get("rows", []) if isinstance(payload.get("rows"), list) else []
    total_rows = int(raw_rows[0][columns.index("_recipe_total_rows")]) if raw_rows and "_recipe_total_rows" in columns else 0
    total_groups = int(raw_rows[0][columns.index("_recipe_total_groups")]) if raw_rows and "_recipe_total_groups" in columns else 0
    measure_total = raw_rows[0][columns.index("_recipe_measure_sum")] if raw_rows and "_recipe_measure_sum" in columns else None
    visible_columns = [column for column in columns if not column.startswith("_recipe_")]
    visible_indices = [columns.index(column) for column in visible_columns]
    rows = [
        {column: row[index] if index < len(row) else None for column, index in zip(visible_columns, visible_indices)}
        for row in raw_rows
    ]
    annotations = recipe.get("result_annotations", {})
    if annotations:
        rows = [{**row, **annotations} for row in rows]
        visible_columns.extend(str(key) for key in annotations)
    result = {
        "recipe_id": str(recipe.get("id", "")),
        "kind": str(recipe["kind"]),
        "source_id": source_id,
        "group_by": group_by,
        "emit": emit,
        "rows": rows,
        "columns": visible_columns,
        "summary": {
            "matched_row_count": total_rows,
            "matched_group_count": total_groups,
            "measure_field": measure,
            "measure_sum": measure_total,
        },
        "coverage": {
            "mode": "deterministic_recipe",
            "complete_for_all_matching_runtime_rows": not bool(payload.get("truncated")),
            "returned_row_count": len(rows),
            "total_matched_row_count": total_rows,
            "truncated": bool(payload.get("truncated")),
        },
    }
    if isinstance(recipe.get("rule_family"), dict):
        result["rule_family"] = recipe["rule_family"]
    return result
