# Studio module layout

`app.studio` is split by responsibility so platform infrastructure does not leak
business workflow decisions into the runtime.

- `models.py`: persisted domain and API data models.
- `storage.py`: business workspace persistence, versions, and file metadata.
- `orchestrator.py`: chat task lifecycle, progress messages, continuation, and recovery.
- `settings.py`: model and capability configuration state.
- `file_preview.py`: bounded previews for workspace artifacts.
- `runtime/`: model gateway, LangGraph loop, sandbox, execution ledger, and tool context.
- `capabilities/`: Tool, Skill, and MCP discovery, installation, secrets, and adapters.

Persistent data follows two roots: `data/accounts/<account>/<business>` contains
only account-owned business projects, while `system/` contains accounts, private
capability settings, runtime databases, sandbox environments, logs, and migration
state. Built-in Skills are shared from `system_skills/`; user-installed Skills and
their runtime views are account-scoped under `system/`.

Business-specific execution belongs in complete Skills or external MCP capabilities.
The modules here provide only generic workspace, model, capability, and event-stream
infrastructure.
