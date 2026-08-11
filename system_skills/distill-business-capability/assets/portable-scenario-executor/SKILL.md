---
name: portable-scenario-executor
description: Execute a distilled business scenario end to end through one bounded, read-only entrypoint. Use it when a third-party agent receives a complete business request and must locate rules, bind runtime data, validate evidence-backed joins, and return a traceable evidence package without manually orchestrating every stage.
---

# Portable scenario executor

Use `scripts/execute_scenario.py` as the primary entrypoint for a complete business request (`produce` is the explicit business-output alias of `execute`). The generated package replaces this file with the scenario-specific contract and keeps the executable self-contained. When `--output` is used, continue from the small sibling `*.agent.json` handoff; keep the full `*.json` package for audit detail.

Do not make the third-party agent recreate the stage state machine. Use `describe` for capabilities, then `produce` for an end-to-end request. Use `query` only when the returned handoff explicitly requires a bounded follow-up query.
