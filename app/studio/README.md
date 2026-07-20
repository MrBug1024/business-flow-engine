# Studio module layout

`app.studio` is split by responsibility so platform infrastructure does not leak
business workflow decisions into the runtime.

- `models.py`: persisted domain and API data models.
- `storage.py`: business workspace persistence, versions, and file metadata.
- `orchestrator.py`: chat task lifecycle, progress messages, continuation, and recovery.
- `settings.py`: model and capability configuration state.
- `file_preview.py`: bounded previews for workspace artifacts.
- `graphs.py`: compatibility views derived from `BusinessContext`.
- `runtime/`: model gateway, LangGraph loop, sandbox, execution ledger, and tool context.
- `capabilities/`: Tool, Skill, and MCP discovery, installation, secrets, and adapters.

Business-specific execution belongs in complete Skills or external MCP capabilities.
The modules here provide only generic workspace, model, capability, and event-stream
infrastructure.
