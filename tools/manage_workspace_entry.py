"""Safe workspace directory, move, and delete operations for the Agent."""

from __future__ import annotations

import shutil
from pathlib import Path, PurePosixPath
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
    target = context.resolve_workspace_path(relative)

    if action == "create_directory":
        if target.exists():
            raise ValueError(f"Workspace entry already exists: {relative}")
        target.mkdir(parents=True)
        _record_change(store, context, f"Created workspace directory {relative}", "create_workspace_directory")
        return _result(action, relative, is_directory=True)

    if action == "move":
        destination_relative = _relative_workspace_path(destination)
        destination_path = context.resolve_workspace_path(destination_relative)
        if not target.exists():
            raise FileNotFoundError(f"Workspace entry does not exist: {relative}")
        if destination_path.exists():
            raise FileExistsError(f"Workspace destination already exists: {destination_relative}")
        if target.is_dir() and target in destination_path.parents:
            raise ValueError("Cannot move a directory inside itself.")
        destination_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(target), str(destination_path))
        _update_registered_paths(context.record, target, destination_path)
        context.record.workspace_deleted_paths = sorted(
            {*context.record.workspace_deleted_paths, relative}
        )
        context.record.workspace_deleted_paths = [
            item for item in context.record.workspace_deleted_paths
            if item.replace("\\", "/").strip("/") != destination_relative
        ]
        _record_change(store, context, f"Moved workspace entry {relative} to {destination_relative}", "move_workspace_entry")
        return _result(action, relative, destination=destination_relative, is_directory=destination_path.is_dir())

    if not target.exists():
        raise FileNotFoundError(f"Workspace entry does not exist: {relative}")
    if target.is_file():
        deleted = store.delete_workspace_file(context.record, relative, actor="agent")
        if deleted is None:
            raise FileNotFoundError(f"Workspace entry does not exist: {relative}")
        return _result(action, relative, is_directory=False)
    if any(target.iterdir()) and not recursive:
        raise ValueError("Directory is not empty. Set recursive=true only when deletion is intentional.")
    affected_files = _registered_under(context.record.files, target)
    affected_packages = _registered_under(context.record.packages, target)
    if recursive:
        shutil.rmtree(target)
    else:
        target.rmdir()
    context.record.files = [item for item in context.record.files if item not in affected_files]
    context.record.packages = [item for item in context.record.packages if item not in affected_packages]
    removed_ids = {item.id for item in affected_files}
    context.record.context.source_files = [
        item for item in context.record.context.source_files if item.get("id") not in removed_ids
    ]
    context.record.workspace_deleted_paths = sorted(
        {*context.record.workspace_deleted_paths, relative}
    )
    _record_change(store, context, f"Deleted workspace directory {relative}", "delete_workspace_directory")
    return _result(action, relative, is_directory=True)


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


def _record_change(store, context, summary: str, trigger: str) -> None:
    store.create_version(context.record, summary, trigger, actor="agent", model="agent")
    context.save()


def _registered_under(items: list, root: Path) -> list:
    result = []
    resolved_root = root.resolve()
    for item in items:
        try:
            path = Path(item.storage_path).resolve()
        except (OSError, ValueError):
            continue
        if path == resolved_root or resolved_root in path.parents:
            result.append(item)
    return result


def _update_registered_paths(record, source: Path, destination: Path) -> None:
    source = source.resolve()
    for item in [*record.files, *record.packages]:
        try:
            current = Path(item.storage_path).resolve()
            relative = current.relative_to(source)
        except (OSError, ValueError):
            continue
        updated = (destination / relative).resolve()
        item.storage_path = str(updated)
        if current == source and hasattr(item, "filename"):
            item.filename = updated.name
        if current == source and hasattr(item, "suffix"):
            item.suffix = updated.suffix.lower()


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
