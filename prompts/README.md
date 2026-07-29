# Agent prompt catalog

All model-facing Studio prompts live in this directory. Python code may assemble
runtime facts, but durable instructions and prompt wording belong in Markdown.

## Layout

- `agent/core-system.md`: stable operating contract and capability-selection gate.
- `agent/skills-system.md`: bounded Skill catalog injected by Skills middleware.
- `runtime/context-summary.md`: LangGraph context-compaction instructions.
- `runtime/resume.md`: continuation after a user answers an Agent question.
- `runtime/auto-continuation.md`: continuation after a real context boundary.

Templates use Python named placeholders such as `{optional_capability_index}`.
`app.studio.prompt_loader` validates the placeholder contract before rendering.
Keep catalogs bounded: large schemas and complete capability packages must remain
discoverable on demand instead of being copied into the system prompt.
