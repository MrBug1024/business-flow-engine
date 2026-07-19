# Dynamic tools

Place trusted Python modules in this directory. Every public LangChain
`BaseTool` instance is discovered and mounted automatically. The usual form is:

```python
from langchain_core.tools import tool


@tool(description="Describe exactly when and why the model should use this tool.")
def example_tool(value: str) -> str:
    return value
```

Modules and directories whose names start with `_` or `.` are not scanned.
Use the configuration refresh action after adding or changing a file. Tool names
must be unique across this directory; duplicate names are reported and not mounted.

Python modules execute during discovery. Only install code you trust.

Tools that need the active workspace can call
`app.studio.tool_context.get_tool_context()`. The returned context exposes the
workspace root, the active record, a safe relative-path resolver, persistence,
and event emission. Optional tool metadata is also discovered by the runtime:

```python
@tool(description="...")
def example() -> dict:
    ...

example.metadata = {"studio": {"retry_safe": True, "protocol": "custom"}}
```

`retry_safe` controls crash recovery in the execution ledger. Protocol values
are reserved for platform interaction adapters such as `user_input` and
`task_progress`; ordinary tools should omit it.

Skills are not installed as Tools. Standard `SKILL.md` packages are discovered by
DeepAgents `SkillsMiddleware`; each complete package is mapped read-only at
`/skills/<name>` by the Studio filesystem sandbox. DeepAgents' filesystem middleware
provides generic read/search/execute capabilities there, while `python` resolves to
Studio's system-level managed venv. No Skill script is imported, registered, or
executed as an individual Tool in the Web process.
