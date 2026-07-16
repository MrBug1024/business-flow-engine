from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from app.studio import capability_runtime
from app.studio.capability_runtime import discover_capabilities, execute_capability
from app.studio.models import BusinessContext, BusinessRecord
from app.studio.tool_registry import DynamicToolRegistry


def _write(path: Path, source: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")


def _tool_source(name: str, description: str = "Return a useful value.") -> str:
    return f'''from langchain_core.tools import tool

@tool({name!r}, description={description!r})
def exported(value: str = "ok") -> str:
    return value
'''


def test_registry_recursively_discovers_and_mounts_public_tools(tmp_path: Path) -> None:
    root = tmp_path / "tools"
    _write(root / "alpha.py", _tool_source("alpha", "Alpha description."))
    _write(root / "nested" / "beta.py", _tool_source("beta", "Beta description."))
    _write(root / "_private.py", _tool_source("private_file"))
    _write(root / "_private" / "hidden.py", _tool_source("private_directory"))
    _write(root / "__pycache__" / "cached.py", _tool_source("cached"))

    registry = DynamicToolRegistry(root)

    assert [tool.name for tool in registry.tools()] == ["alpha", "beta"]
    assert registry.get_tools() == registry.tools()
    assert registry.get("alpha") is not None
    assert registry.get("missing") is None
    records = registry.list()
    assert [(record.name, record.status, record.source) for record in records] == [
        ("alpha", "ready", "alpha.py"),
        ("beta", "ready", "nested/beta.py"),
    ]
    assert records[0].mounted is True
    assert records[0].description == "Alpha description."
    assert records[0].input_schema["properties"]["value"]["type"] == "string"
    assert records[0].model_dump(mode="json")["status"] == "ready"


def test_registry_reports_broken_modules_without_hiding_valid_tools(tmp_path: Path) -> None:
    root = tmp_path / "tools"
    _write(root / "healthy.py", _tool_source("healthy"))
    _write(root / "broken.py", "raise RuntimeError('broken during import')\n")

    registry = DynamicToolRegistry(root)

    assert [tool.name for tool in registry.tools()] == ["healthy"]
    error = next(record for record in registry.list() if record.record_type == "module_error")
    assert error.source == "broken.py"
    assert error.status == "error"
    assert error.mounted is False
    assert "RuntimeError: broken during import" in (error.error or "")


def test_duplicate_names_are_visible_and_neither_tool_is_mounted(tmp_path: Path) -> None:
    root = tmp_path / "tools"
    _write(root / "first.py", _tool_source("collision"))
    _write(root / "nested" / "second.py", _tool_source("collision"))
    _write(root / "valid.py", _tool_source("valid"))

    registry = DynamicToolRegistry(root)

    assert [tool.name for tool in registry.tools()] == ["valid"]
    assert registry.get("collision") is None
    duplicates = [record for record in registry.list() if record.name == "collision"]
    assert len(duplicates) == 2
    assert {record.status for record in duplicates} == {"duplicate"}
    assert all("first.py" in (record.error or "") for record in duplicates)
    assert all("nested/second.py" in (record.error or "") for record in duplicates)


def test_refresh_atomically_replaces_the_catalog(tmp_path: Path) -> None:
    root = tmp_path / "tools"
    module = root / "changing.py"
    _write(module, _tool_source("before"))
    registry = DynamicToolRegistry(root)
    first_generation = registry.generation
    original = registry.get("before")

    _write(module, _tool_source("after"))
    refreshed = registry.refresh()

    assert registry.generation == first_generation + 1
    assert registry.get("before") is None
    assert registry.get("after") is not None
    assert registry.get("after") is not original
    assert [record.name for record in refreshed] == ["after"]


def test_nested_modules_can_use_relative_private_helpers(tmp_path: Path) -> None:
    root = tmp_path / "tools"
    _write(root / "nested" / "_helper.py", "PREFIX = 'nested'\n")
    _write(
        root / "nested" / "relative.py",
        '''from langchain_core.tools import tool
from ._helper import PREFIX

@tool(description="Use a private sibling helper.")
def relative_tool(value: str) -> str:
    return f"{PREFIX}:{value}"
''',
    )

    registry = DynamicToolRegistry(root)

    tool = registry.get("relative_tool")
    assert tool is not None
    assert tool.invoke({"value": "ok"}) == "nested:ok"
    assert [record.name for record in registry.list()] == ["relative_tool"]


def test_discovered_tool_executes_generically_with_workspace_context(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "tools"
    _write(
        root / "contextual.py",
        '''from langchain_core.tools import tool
from app.studio.tool_context import get_tool_context

@tool(description="Inspect the current Studio workspace context.")
def contextual(value: str) -> dict:
    context = get_tool_context()
    return {
        "business_id": context.business_id,
        "workspace": str(context.workspace_path),
        "value": value,
        "_studio": {"summary": "Context inspected"},
    }

contextual.metadata = {"studio": {"retry_safe": True}}
''',
    )
    registry = DynamicToolRegistry(root)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setattr(capability_runtime, "tool_registry", registry)
    monkeypatch.setattr(
        capability_runtime,
        "studio_settings",
        SimpleNamespace(load=lambda: SimpleNamespace(installed_skills=[], mcp_configs=[])),
    )
    monkeypatch.setattr(
        capability_runtime,
        "store",
        SimpleNamespace(workspace_dir=lambda _business_id: workspace, save=lambda record: record),
    )
    record = BusinessRecord(
        id="business-context",
        name="Context tool test",
        created_at=1,
        updated_at=1,
        context=BusinessContext(business_id="business-context", name="Context tool test"),
    )

    capability = discover_capabilities(record)[0]
    result = execute_capability(
        capability,
        record,
        {"value": "ok"},
        run_id="run-context",
        session_id="session-context",
    )

    assert capability.handler is registry.get("contextual")
    assert capability.retry_safe is True
    assert result.summary == "Context inspected"
    assert result.output == {
        "business_id": "business-context",
        "workspace": str(workspace),
        "value": "ok",
    }
    assert record.context.tool_usages[-1]["tool"] == "contextual"

