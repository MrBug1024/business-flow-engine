"""Safe workspace directory, move, and delete operations for the Agent."""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import Literal

from langchain_core.tools import tool

from app.studio.runtime.tool_context import get_tool_context


WorkspaceAction = Literal["create_directory", "move", "delete"]


@tool(
    description=(
        "Manage files or folders inside the active business workspace. Use create_directory "
        "for a folder, move to rename or relocate an entry, and delete only when the user goal "
        "requires removal. Built-in filesystem tools already handle list, read, search, create "
        "file, and edit file. Set recursive=true only to delete a non-empty directory intentionally."
    )
)
def manage_workspace_entry(
    action: WorkspaceAction,
    path: str,
    destination: str = "",
    recursive: bool = False,
) -> dict:
    """Apply one bounded workspace structure mutation."""

    from app.studio.storage import store  # Loaded after dynamic Tool discovery.

    context = get_tool_context()
    relative = _relative_workspace_path(path)

    if action == "create_directory":
        store.create_workspace_entry(
            context.record,
            relative,
            "folder",
            actor="agent",
        )
        return _result(action, relative, is_directory=True)

    if action == "move":
        destination_relative = _relative_workspace_path(destination)
        moved = store.move_workspace_entry(
            context.record,
            relative,
            destination_relative,
            actor="agent",
        )
        return _result(
            action,
            relative,
            destination=destination_relative,
            is_directory=moved["kind"] == "folder",
        )

    deleted = store.delete_workspace_entry(
        context.record,
        relative,
        recursive=recursive,
        actor="agent",
    )
    if deleted is None:
        raise FileNotFoundError(f"Workspace entry does not exist: {relative}")
    return _result(action, relative, is_directory=deleted.get("kind") == "folder")


manage_workspace_entry.metadata = {
    "studio": {"protocol": "workspace_file", "retry_safe": False}
}


def _relative_workspace_path(value: str) -> str:
    normalized = str(value or "").replace("\\", "/").strip()
    if normalized == "/workspace":
        raise ValueError("The workspace root cannot be mutated.")
    if normalized.startswith("/workspace/"):
        normalized = normalized.removeprefix("/workspace/")
    normalized = normalized.strip("/")
    pure = PurePosixPath(normalized)
    if not normalized or pure.is_absolute() or ".." in pure.parts:
        raise ValueError("Path must stay inside /workspace.")
    return pure.as_posix()


def _result(action: str, path: str, *, destination: str = "", is_directory: bool) -> dict:
    summary = f"{action}: {path}" + (f" -> {destination}" if destination else "")
    return {
        "status": "success",
        "action": action,
        "path": f"/workspace/{path}",
        "destination": f"/workspace/{destination}" if destination else "",
        "is_directory": is_directory,
        "summary": summary,
    }
