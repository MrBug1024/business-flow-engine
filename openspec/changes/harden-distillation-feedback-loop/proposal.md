# Proposal: Harden Distillation Feedback Loop

**Change ID:** `harden-distillation-feedback-loop`
**Created:** 2026-07-09
**Status:** Draft

---

## Problem Statement

- Data trace sampling can accept weak value matches from unrelated fields, causing unrelated rows to appear as causal samples.
- Manual relation corrections are not consistently reflected in the visible trace sample and downstream ER/flow artifacts.
- Confirmation questions can still appear as plain AI text instead of structured interaction popups.
- Some generated questions are too technical for business users.
- Scenario descriptions are optional but are used by inference prompts, so missing descriptions reduce inference quality.

## Proposed Solution

- Only use strong evidence for business trace sampling: confirmed/inferred relation keys first, then strict key-like matches. Remove broad all-column value/text fallback for business tables.
- Re-run and persist trace samples immediately after manual relation confirmation, and emit refresh events for both relations and trace.
- Convert any model/tool confirmation text into structured `interaction` events with options, custom text, and per-question selection mode.
- Keep clarification wording business-facing and suppress low-value technical prompts.
- Add a post-upload prompt to collect a short business description when the scenario has none, and persist it through a scenario update API.

## Scope

### In Scope
- Backend trace sampling strictness.
- Relation confirmation persistence and refresh behavior.
- Structured interaction event generation.
- Frontend business-description prompt after upload.
- Focused tests for new behavior.

### Out of Scope
- Replacing the LLM provider or prompt framework.
- Full UI redesign.
- Changing published skill runtime behavior.

## Impact Analysis

| Component | Change Required | Details |
|-----------|-----------------|---------|
| Database | No | File-backed scenario metadata only. |
| API | Yes | Scenario update endpoint; relation/trace behavior unchanged externally. |
| State | Yes | Trace and relation invalidation order must preserve user corrections. |
| UI | Yes | Description dialog and interaction popup handling. |

## Success Criteria

- [ ] Business trace samples are not produced from unrelated all-column value scans.
- [ ] Manual relation confirmation persists as `confirmed=True` and refreshes trace samples.
- [ ] Relation correction over chat causes the trace panel to refresh.
- [ ] Confirmation questions are emitted as structured interactions with custom answer support.
- [ ] Users are prompted for a business description after upload when it is missing.
