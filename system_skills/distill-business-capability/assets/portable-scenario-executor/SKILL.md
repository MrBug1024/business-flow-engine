---
name: portable-scenario-executor
description: Execute a distilled business scenario end to end through one bounded, read-only entrypoint. Use it when a third-party agent receives a complete business request and must locate rules, bind runtime data, validate evidence-backed joins, and return a traceable evidence package without manually orchestrating every stage.
---

# Portable scenario executor

Use `scripts/execute_scenario.py` as the primary entrypoint for a complete business request (`produce` is the explicit business-output alias of `execute`). The generated package replaces this file with the scenario-specific contract and keeps the executable self-contained. When `--output` is used, continue from the small sibling `*.agent.json` handoff; keep the full `*.json` package for audit detail.

`--output scenario-evidence.json` always writes the bounded audit/evidence package, not a user-facing final conclusion. Add `--delivery-output result.json`, `result.csv`, or `result.xlsx` only when the same request reaches `completed_deterministically`; the runtime then writes a separately described, content-addressed result file without reopening source data. `deliver --result scenario-evidence.json --output result.json|csv|xlsx` performs that later materialization without repeating the business request. A `ready_for_agent_judgment`, blocked, or unverified-recipe result never creates a final result file.

JSON, dependency-free UTF-8 CSV, and a single-sheet data-only XLSX workbook are the supported portable result-file formats. To materialize a design-time output template, pass `--delivery-template-id <id>` and require that its declared format and every declared column exactly match the verified recipe output. XLSX writes only direct verified values (no formulas, styling, macro content, or inferred columns). Historical DOCX/PDF templates remain schema evidence only until a separately packaged renderer and verified mapping are supplied; do not relabel a CSV/JSON/XLSX projection as one of those formats.

The same `execute` call routes declared tabular, document, and hybrid inputs. For documents it builds a bounded temporary index and returns only matching chunks with `source_digest`, `locator`, `chunk_id`, and `text_digest`; it never silently omits a declared non-tabular source. A sparse PDF returns `blocked_ocr_required`: run the declared OCR capability once, bind its JSON output, then re-run the request. Do not merge document chunks with table rows unless the contract exposes an accepted business key or explicit link.

`completed_deterministically` is reserved for a compiled recipe explicitly covered by the catalog-bound real-replay certificate in `references/recipe-verification.json`. A missing, stale, malformed, or recipe-ID-incomplete certificate returns bounded evidence with `ready_for_agent_judgment` (when source evidence is available), never a guessed zero-result conclusion. Read `recipe_execution` in the handoff and perform exactly one evidence-based business judgment when it says `unverified_recipe_evidence_only`.

Do not make the third-party agent recreate the stage state machine. Use `describe` for capabilities, then `produce` for an end-to-end request. Use `query` only when the returned handoff explicitly requires a bounded follow-up query.
