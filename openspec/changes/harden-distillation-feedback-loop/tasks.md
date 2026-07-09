# Implementation Tasks: Harden Distillation Feedback Loop

**Change ID:** `harden-distillation-feedback-loop`

---

## Phase 1: Trace Evidence

- [x] 1.1 Remove weak all-column/text fallback from business table tracing.
- [x] 1.2 Preserve strict knowledge-table fallback behavior where useful.
- [x] 1.3 Add tests covering unrelated value matches.

## Phase 2: Corrections And Refresh

- [x] 2.1 Preserve confirmed relations during API and tool correction paths.
- [x] 2.2 Refresh both relation and trace resources after correction.
- [x] 2.3 Add tests for confirmed relation trace refresh.

## Phase 3: Structured Questions

- [x] 3.1 Convert text confirmation prompts into interaction events.
- [x] 3.2 Ensure each question supports options, single/multi selection, and custom text.
- [x] 3.3 Keep technical/low-evidence prompts suppressed.

## Phase 4: Business Context

- [x] 4.1 Add scenario description update API/store action.
- [x] 4.2 Prompt after upload when description is empty.
- [x] 4.3 Use concise business-facing wording.

## Verification

- [x] Python tests pass.
- [x] Frontend build passes.
