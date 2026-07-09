# Delta: Distillation Feedback Loop

**Change ID:** `harden-distillation-feedback-loop`
**Affects:** trace sampling, relation correction, AI interaction, upload workflow

---

## ADDED

### Requirement: Business Context Capture

After data upload, when a scenario has no description, the UI shall ask the user to describe the business in plain language.

#### Scenario: Missing Business Description
- GIVEN a scenario with uploaded tables and empty description
- WHEN upload completes
- THEN the UI prompts for a short description of what the business does, what data it has, and what it outputs

### Requirement: Structured Confirmation Interactions

AI confirmation questions shall be delivered as structured interaction events rather than only inline prose.

#### Scenario: Questions Returned By Inference
- GIVEN relation or flow inference returns ambiguous questions
- WHEN the chat stream reaches the user
- THEN the frontend receives an `interaction` payload with selectable options and custom answer support

## MODIFIED

### Requirement: Trace Sampling Evidence

Business table trace samples shall come from explicit relation keys or strict key-like evidence, not broad all-column value scans.

#### Scenario: Unrelated Value Overlap
- GIVEN a result row value appears in an unrelated descriptive column
- WHEN trace sampling runs without a confirmed relation for that column
- THEN the unrelated row is not returned as a business trace sample

### Requirement: Manual Relation Correction

Manual relation corrections shall be persisted, used for trace refresh, and reflected in visible ER/trace state.

#### Scenario: Confirmed Relation
- GIVEN a user confirms a relation
- WHEN the correction is saved
- THEN the relation is marked confirmed and the trace chain is regenerated using that relation

## REMOVED

(None)
