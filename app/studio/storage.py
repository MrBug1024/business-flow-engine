"""File-backed persistence for AI Business Studio workspaces."""

from __future__ import annotations

import json
import re
import shutil
import threading
import uuid
from pathlib import Path
from time import time
from typing import Any

from app.core.config import settings
from app.studio.graphs import entity_graph, evidence_graph, flow_graph, lineage_graph
from app.studio.runtime.llm import strip_thinking_markup
from app.studio.models import (
    AIRun,
    BusinessContext,
    BusinessRecord,
    BusinessSummary,
    ChatMessage,
    ChatSession,
    ContextVersion,
    PackageRecord,
    WorkspaceNode,
)
from app.studio.capabilities.registry import installed_skill_names


DESCRIPTION_FILENAME = "description.md"
LEGACY_DESCRIPTION_FILENAME = "scenario.md"


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def now() -> float:
    return time()


class StudioStore:
    """Single-node persistence for Studio business workspaces."""

    def __init__(self, root: Path | None = None) -> None:
        self.root = (root or settings.data_path) / "business_studio"
        self.business_root = self.root / "businesses"
        self.business_root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    def business_dir(self, business_id: str) -> Path:
        return self.business_root / business_id

    def workspace_dir(self, business_id: str) -> Path:
        path = self.business_dir(business_id) / "workspace"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def description_markdown_path(self, business_id: str) -> Path:
        return self.workspace_dir(business_id) / DESCRIPTION_FILENAME

    def scenario_markdown_path(self, business_id: str) -> Path:
        """Return the canonical description path for legacy callers."""

        return self.description_markdown_path(business_id)

    def files_dir(self, business_id: str) -> Path:
        path = self.workspace_dir(business_id) / "data"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def graphs_dir(self, business_id: str) -> Path:
        path = self.workspace_dir(business_id) / "graphs"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def context_dir(self, business_id: str) -> Path:
        path = self.workspace_dir(business_id) / "context"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def output_dir(self, business_id: str) -> Path:
        path = self.workspace_dir(business_id) / "output"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def packages_dir(self, business_id: str) -> Path:
        path = self.output_dir(business_id) / "skill-package"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def settings_dir(self, business_id: str) -> Path:
        path = self.workspace_dir(business_id) / "settings"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def package_work_dir(self, business_id: str, package_id: str) -> Path:
        path = self.packages_dir(business_id) / package_id
        path.mkdir(parents=True, exist_ok=True)
        return path

    def next_data_file_path(self, business_id: str, filename: str) -> Path:
        target = self.files_dir(business_id) / _safe_filename(filename)
        if not target.exists():
            return target
        stem = target.stem
        suffix = target.suffix
        for index in range(2, 1000):
            candidate = target.with_name(f"{stem}-{index}{suffix}")
            if not candidate.exists():
                return candidate
        return target.with_name(f"{stem}-{new_id('copy')}{suffix}")

    def _meta_file(self, business_id: str) -> Path:
        return self.business_dir(business_id) / "business.json"

    def create(self, name: str, goal: str = "", description: str = "") -> BusinessRecord:
        with self._lock:
            business_id = new_id("biz")
            ts = now()
            cleaned_goal = goal.strip()
            cleaned_description = description.strip()
            context = BusinessContext(
                business_id=business_id,
                name=name.strip(),
                goal=cleaned_goal or cleaned_description,
                user_requirements=[
                    {
                        "id": new_id("req"),
                        "text": cleaned_description or cleaned_goal,
                        "source": DESCRIPTION_FILENAME,
                        "created_at": ts,
                    }
                ]
                if (cleaned_description or cleaned_goal)
                else [],
            )
            record = BusinessRecord(
                id=business_id,
                name=name.strip(),
                goal=cleaned_goal,
                description=cleaned_description,
                created_at=ts,
                updated_at=ts,
                context=context,
                chat_sessions=[
                    ChatSession(
                        id=new_id("chat"),
                        business_id=business_id,
                        created_at=ts,
                        updated_at=ts,
                    )
                ],
            )
            self._ensure_workspace(record)
            self.description_markdown_path(business_id).write_text(_description_markdown(record), encoding="utf-8")
            self.create_version(record, "Created business workspace", "create_business")
            self.save(record)
            return record

    def list(self) -> list[BusinessSummary]:
        with self._lock:
            items: list[BusinessSummary] = []
            for meta in self.business_root.glob("*/business.json"):
                try:
                    record = self._read(meta)
                except Exception:  # noqa: BLE001
                    continue
                items.append(self.to_summary(record))
            items.sort(key=lambda item: item.updated_at, reverse=True)
            return items

    def get(self, business_id: str) -> BusinessRecord | None:
        with self._lock:
            meta = self._meta_file(business_id)
            if not meta.exists():
                return None
            record = self._read(meta)
            changed = _sanitize_legacy_runtime_state(record)
            changed = _ensure_chat_sessions(record) or changed
            changed = self._ensure_workspace(record) or changed
            changed = _migrate_description_sources(record) or changed
            self._write_workspace_artifacts(record)
            if changed:
                target = self._meta_file(record.id)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(record.model_dump_json(indent=2), encoding="utf-8")
            return record

    def require(self, business_id: str) -> BusinessRecord:
        record = self.get(business_id)
        if record is None:
            raise KeyError(business_id)
        return record

    def save(self, record: BusinessRecord) -> BusinessRecord:
        with self._lock:
            record.updated_at = now()
            record.context.name = record.name
            record.context.goal = record.goal or record.context.goal
            _ensure_chat_sessions(record)
            self._ensure_workspace(record)
            _migrate_description_sources(record)
            self._write_workspace_artifacts(record)
            target = self._meta_file(record.id)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(record.model_dump_json(indent=2), encoding="utf-8")
            return record

    def delete(self, business_id: str) -> bool:
        with self._lock:
            target = self.business_dir(business_id)
            if not target.exists():
                return False
            shutil.rmtree(target)
            return True

    def workspace_tree(self, record: BusinessRecord) -> WorkspaceNode:
        self._ensure_workspace(record)
        return _tree_node(self.workspace_dir(record.id), self.workspace_dir(record.id), record.name)

    def read_description_markdown(self, record: BusinessRecord) -> str:
        self._ensure_workspace(record)
        return self.description_markdown_path(record.id).read_text(encoding="utf-8")

    def write_description_markdown(self, record: BusinessRecord, content: str) -> BusinessRecord:
        self._ensure_workspace(record)
        path = self.description_markdown_path(record.id)
        path.write_text(content, encoding="utf-8")
        _clear_workspace_tombstone(record, DESCRIPTION_FILENAME)
        record.description = content[:4000]
        _append_requirement_from_description(record, content)
        self.create_version(record, "Updated description.md", "edit_description_markdown", actor="user")
        return self.save(record)

    def create_version(
        self,
        record: BusinessRecord,
        summary: str,
        trigger: str,
        *,
        actor: str = "system",
        model: str = "local-context-builder",
        evidence_ids: list[str] | None = None,
    ) -> ContextVersion:
        record.current_version += 1
        snapshot = record.context.model_dump(mode="json")
        snapshot["versions"] = []
        version = ContextVersion(
            version=record.current_version,
            summary=summary,
            trigger=trigger,
            created_at=now(),
            actor=actor,
            model=model,
            evidence_ids=evidence_ids or [],
            snapshot=snapshot,
        )
        record.context.versions.append(version)
        return version

    def rollback(self, record: BusinessRecord, version: int) -> BusinessRecord:
        match = next((item for item in record.context.versions if item.version == version), None)
        if match is None:
            raise ValueError(f"version {version} not found")
        restored = dict(match.snapshot)
        previous_versions = record.context.versions
        record.context = BusinessContext.model_validate(restored)
        record.context.versions = previous_versions
        self.create_version(record, f"Rolled back to v{version}", "rollback")
        return self.save(record)

    def list_chat_sessions(self, record: BusinessRecord) -> list[ChatSession]:
        _ensure_chat_sessions(record)
        return sorted(record.chat_sessions, key=lambda item: item.updated_at, reverse=True)

    def get_chat_session(self, record: BusinessRecord, session_id: str) -> ChatSession | None:
        _ensure_chat_sessions(record)
        return next((item for item in record.chat_sessions if item.id == session_id), None)

    def require_chat_session(self, record: BusinessRecord, session_id: str | None = None) -> ChatSession:
        _ensure_chat_sessions(record)
        if session_id:
            session = self.get_chat_session(record, session_id)
            if session is None:
                raise KeyError(session_id)
            return session
        return max(record.chat_sessions, key=lambda item: item.updated_at)

    def create_chat_session(self, record: BusinessRecord, title: str = "") -> ChatSession:
        with self._lock:
            ts = now()
            session = ChatSession(
                id=new_id("chat"),
                business_id=record.id,
                title=title.strip()[:120],
                created_at=ts,
                updated_at=ts,
            )
            record.chat_sessions.append(session)
            self.save(record)
            return session

    def clear_chat_session(self, record: BusinessRecord, session_id: str) -> ChatSession | None:
        with self._lock:
            session = self.get_chat_session(record, session_id)
            if session is None:
                return None
            record.messages = [item for item in record.messages if item.session_id != session_id]
            record.runs = [item for item in record.runs if item.session_id != session_id]
            session.updated_at = now()
            self.save(record)
            return session

    def delete_chat_session(self, record: BusinessRecord, session_id: str) -> ChatSession | None:
        with self._lock:
            session = self.get_chat_session(record, session_id)
            if session is None:
                return None
            record.chat_sessions = [item for item in record.chat_sessions if item.id != session_id]
            record.messages = [item for item in record.messages if item.session_id != session_id]
            record.runs = [item for item in record.runs if item.session_id != session_id]
            if not record.chat_sessions:
                ts = now()
                record.chat_sessions.append(
                    ChatSession(
                        id=new_id("chat"),
                        business_id=record.id,
                        created_at=ts,
                        updated_at=ts,
                    )
                )
            self.save(record)
            return session

    def append_message(
        self,
        record: BusinessRecord,
        role: str,
        content: str,
        run_id: str | None = None,
        session_id: str | None = None,
        *,
        task_id: str = "",
        kind: str = "standard",
        progress_action: str = "",
        work_item_id: str = "",
        progress: dict[str, Any] | None = None,
        activity_events: list[dict[str, Any]] | None = None,
    ) -> ChatMessage:
        session = self.require_chat_session(record, session_id)
        message = ChatMessage(
            id=new_id("msg"),
            session_id=session.id,
            role=role,  # type: ignore[arg-type]
            content=content,
            created_at=now(),
            run_id=run_id,
            task_id=task_id,
            kind=kind,  # type: ignore[arg-type]
            progress_action=progress_action,
            work_item_id=work_item_id,
            progress=progress or {},
            activity_events=activity_events or [],
        )
        record.messages.append(message)
        session.updated_at = message.created_at
        if role == "user" and not session.title:
            session.title = _chat_session_title(content)
        return message

    def append_run(self, record: BusinessRecord, run: AIRun) -> AIRun:
        if run.session_id:
            session = self.require_chat_session(record, run.session_id)
            session.updated_at = max(session.updated_at, run.started_at)
        record.runs.append(run)
        return run

    def append_package(self, record: BusinessRecord, package: PackageRecord) -> PackageRecord:
        record.packages.append(package)
        try:
            relative = Path(package.storage_path).resolve().relative_to(
                self.workspace_dir(record.id).resolve()
            ).as_posix()
            _clear_workspace_tombstone(record, relative)
        except (OSError, ValueError):
            pass
        return package

    def delete_workspace_file(
        self,
        record: BusinessRecord,
        requested_path: str,
        *,
        actor: str = "user",
    ) -> dict[str, Any] | None:
        with self._lock:
            workspace = self.workspace_dir(record.id).resolve()
            normalized = requested_path.replace("\\", "/").strip("/")
            relative = Path(normalized)
            if not normalized or "\x00" in normalized or relative.is_absolute() or ".." in relative.parts:
                raise ValueError("Invalid workspace file path.")
            try:
                target = (workspace / relative).resolve()
            except (OSError, ValueError) as exc:
                raise ValueError("Invalid workspace file path.") from exc
            if workspace not in target.parents:
                raise ValueError("Invalid workspace file path.")
            if not target.is_file():
                return None

            relative_path = target.relative_to(workspace).as_posix()
            registered = next(
                (
                    item
                    for item in record.files
                    if _same_resolved_file(Path(item.storage_path), target)
                ),
                None,
            )
            package = next(
                (
                    item
                    for item in record.packages
                    if _same_resolved_file(Path(item.storage_path), target)
                ),
                None,
            )
            target.unlink()
            record.workspace_deleted_paths = sorted(
                {*record.workspace_deleted_paths, relative_path}
            )
            if registered is not None:
                record.files = [item for item in record.files if item.id != registered.id]
                record.context.source_files = [
                    item
                    for item in record.context.source_files
                    if item.get("id") != registered.id
                ]
                record.context.tool_usages = [
                    item
                    for item in record.context.tool_usages
                    if item.get("source_file_id") != registered.id
                ]
            if package is not None:
                record.packages = [item for item in record.packages if item.id != package.id]
            if relative_path == DESCRIPTION_FILENAME:
                record.description = ""
            evidence_ids = [registered.id] if registered is not None else []
            self.create_version(
                record,
                f"Deleted workspace file {relative_path}",
                "delete_workspace_file",
                actor=actor,
                evidence_ids=evidence_ids,
            )
            self.save(record)
            return {
                "path": relative_path,
                "filename": target.name,
                "registered_file_id": registered.id if registered is not None else None,
                "package_id": package.id if package is not None else None,
            }

    def delete_file(self, record: BusinessRecord, file_id: str) -> Any | None:
        match = next((item for item in record.files if item.id == file_id), None)
        if match is None:
            return None
        record.files = [item for item in record.files if item.id != file_id]
        storage_path = Path(match.storage_path)
        try:
            resolved = storage_path.resolve()
            workspace = self.workspace_dir(record.id).resolve()
            if resolved == workspace or workspace not in resolved.parents:
                return match
            if resolved.exists() and resolved.is_file():
                resolved.unlink()
        except OSError:
            pass
        return match

    def find_file(self, file_id: str) -> tuple[BusinessRecord, Any] | None:
        with self._lock:
            for summary in self.list():
                record = self.get(summary.id)
                if record is None:
                    continue
                match = next((item for item in record.files if item.id == file_id), None)
                if match is not None:
                    return record, match
        return None

    def find_package(self, package_id: str) -> tuple[BusinessRecord, PackageRecord] | None:
        with self._lock:
            for summary in self.list():
                record = self.get(summary.id)
                if record is None:
                    continue
                match = next((item for item in record.packages if item.id == package_id), None)
                if match is not None:
                    return record, match
        return None

    def to_summary(self, record: BusinessRecord) -> BusinessSummary:
        open_questions = [
            item
            for item in record.context.questions
            if item.get("status", "open") == "open"
        ]
        return BusinessSummary(
            id=record.id,
            name=record.name,
            goal=record.goal,
            description=record.description,
            status=record.status,
            created_at=record.created_at,
            updated_at=record.updated_at,
            current_version=record.current_version,
            file_count=len(record.files),
            open_question_count=len(open_questions),
            package_count=len(record.packages),
        )

    def _read(self, meta_file: Path) -> BusinessRecord:
        data = json.loads(meta_file.read_text(encoding="utf-8"))
        return BusinessRecord.model_validate(data)

    def _ensure_workspace(self, record: BusinessRecord) -> bool:
        workspace = self.workspace_dir(record.id)
        self.files_dir(record.id)
        self.graphs_dir(record.id)
        self.context_dir(record.id)
        self.packages_dir(record.id)
        self.settings_dir(record.id)
        changed = _migrate_legacy_description_file(workspace)
        description = self.description_markdown_path(record.id)
        if not description.exists() and not _workspace_path_is_deleted(record, DESCRIPTION_FILENAME):
            description.write_text(_description_markdown(record), encoding="utf-8")
            changed = True
        return changed

    def _write_workspace_artifacts(self, record: BusinessRecord) -> None:
        context_payload = record.context.model_dump(mode="json")
        # Rollback snapshots remain in the internal business record. The Agent-facing
        # workspace artifact exposes only current state and lean version metadata.
        for version in context_payload.get("versions", []):
            if isinstance(version, dict):
                version.pop("snapshot", None)
        if not _workspace_path_is_deleted(record, "context/business_context.json"):
            (self.context_dir(record.id) / "business_context.json").write_text(
                json.dumps(context_payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        graph_builders = {
            "entity.mmd": entity_graph,
            "flow.mmd": flow_graph,
            "lineage.mmd": lineage_graph,
            "evidence.mmd": evidence_graph,
        }
        for filename, builder in graph_builders.items():
            relative = f"graphs/{filename}"
            if not _workspace_path_is_deleted(record, relative):
                (self.graphs_dir(record.id) / filename).write_text(
                    builder(record.context)["mermaid"],
                    encoding="utf-8",
                )
        capability_state = {
            "skills": record.context.skill_references,
            "mcp": record.context.mcp_references,
            "tools": record.context.tool_usages,
        }
        if not _workspace_path_is_deleted(record, "settings/capabilities.json"):
            (self.settings_dir(record.id) / "capabilities.json").write_text(
                json.dumps(capability_state, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )


def _description_markdown(record: BusinessRecord) -> str:
    goal = record.goal or "Describe the outcome this business workspace should produce."
    description = record.description or "Write the business scenario, source context, constraints, and expected skill package here."
    return f"""# {record.name}

## Business Goal

{goal}

## Scenario Description

{description}

## Source Notes

- Add uploaded files under `data/`.
- Ask AI to analyze the workspace after the description is updated.

## Acceptance Criteria

- Business Context is traceable.
- Graphs are generated from context.
- Skill package is exported under `output/skill-package`.
"""


def _append_requirement_from_description(record: BusinessRecord, content: str) -> None:
    text = content.strip()
    if not text:
        return
    existing = [item.get("text") for item in record.context.user_requirements]
    if text not in existing:
        record.context.user_requirements.append(
            {
                "id": new_id("req"),
                "text": text,
                "source": DESCRIPTION_FILENAME,
                "created_at": now(),
            }
        )


def _migrate_legacy_description_file(workspace: Path) -> bool:
    canonical = workspace / DESCRIPTION_FILENAME
    legacy = workspace / LEGACY_DESCRIPTION_FILENAME
    if not legacy.is_file():
        return False
    if not canonical.exists():
        legacy.rename(canonical)
        return True
    if canonical.is_file() and canonical.read_bytes() == legacy.read_bytes():
        legacy.unlink()
        return True
    legacy.rename(_next_legacy_description_backup(workspace))
    return True


def _next_legacy_description_backup(workspace: Path) -> Path:
    candidate = workspace / "scenario.legacy.md"
    index = 2
    while candidate.exists():
        candidate = workspace / f"scenario.legacy-{index}.md"
        index += 1
    return candidate


def _migrate_description_sources(record: BusinessRecord) -> bool:
    payload = record.context.model_dump(mode="python")
    if not _replace_legacy_description_sources(payload):
        return False
    record.context = BusinessContext.model_validate(payload)
    return True


def _replace_legacy_description_sources(value: Any) -> bool:
    changed = False
    if isinstance(value, dict):
        for key, item in value.items():
            if key == "source" and item == LEGACY_DESCRIPTION_FILENAME:
                value[key] = DESCRIPTION_FILENAME
                changed = True
            else:
                changed = _replace_legacy_description_sources(item) or changed
    elif isinstance(value, list):
        for item in value:
            changed = _replace_legacy_description_sources(item) or changed
    return changed


def _same_resolved_file(candidate: Path, target: Path) -> bool:
    try:
        return candidate.resolve() == target
    except OSError:
        return False


def _clear_workspace_tombstone(record: BusinessRecord, relative_path: str) -> None:
    normalized = relative_path.replace("\\", "/").strip("/")
    record.workspace_deleted_paths = [
        item for item in record.workspace_deleted_paths if item != normalized
    ]


def _workspace_path_is_deleted(record: BusinessRecord, relative_path: str) -> bool:
    normalized = relative_path.replace("\\", "/").strip("/")
    return any(
        normalized == item.replace("\\", "/").strip("/")
        or normalized.startswith(item.replace("\\", "/").strip("/") + "/")
        for item in record.workspace_deleted_paths
        if item.replace("\\", "/").strip("/")
    )


def _tree_node(path: Path, base: Path, root_name: str | None = None) -> WorkspaceNode:
    relative = "" if path == base else path.relative_to(base).as_posix()
    if path.is_dir():
        children = [
            _tree_node(child, base)
            for child in sorted(path.iterdir(), key=_sort_key)
            if child.name != "_field-evidence"
        ]
        return WorkspaceNode(
            name=root_name or path.name,
            path=relative,
            kind="folder",
            icon=_folder_icon(path.name),
            children=children,
        )
    return WorkspaceNode(
        name=path.name,
        path=relative,
        kind="file",
        icon=_file_icon(path.suffix.lower(), path.name),
        size=path.stat().st_size,
    )


def _sort_key(path: Path) -> tuple[int, str]:
    return (0 if path.is_dir() else 1, path.name.lower())


def _folder_icon(name: str) -> str:
    return {
        "data": "database",
        "graphs": "graph",
        "context": "brain",
        "output": "package",
        "skill-package": "package",
        "settings": "settings",
    }.get(name, "folder")


def _file_icon(suffix: str, name: str) -> str:
    if name == DESCRIPTION_FILENAME:
        return "scenario"
    if suffix in {".md", ".markdown"}:
        return "markdown"
    if suffix in {".csv", ".tsv", ".xlsx", ".xls", ".parquet"}:
        return "table"
    if suffix in {".json", ".jsonl", ".ndjson", ".yaml", ".yml"}:
        return "json"
    if suffix in {".mmd"}:
        return "graph"
    if suffix in {".zip", ".tar", ".tgz", ".gz", ".bz2", ".xz"}:
        return "package"
    if suffix in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg"}:
        return "image"
    if suffix in {".sqlite", ".sqlite3", ".db"}:
        return "database"
    if suffix in {".mp4", ".webm", ".mov", ".m4v"}:
        return "video"
    if suffix in {".mp3", ".wav", ".ogg", ".m4a", ".flac"}:
        return "audio"
    if suffix in {".pdf", ".docx", ".pptx"}:
        return "document"
    return "file"


def _safe_filename(filename: str) -> str:
    cleaned = filename.replace("\\", "_").replace("/", "_").strip()
    cleaned = re.sub(r"[\x00-\x1f]+", "_", cleaned)
    return cleaned[:180] or "upload.bin"


def _ensure_chat_sessions(record: BusinessRecord) -> bool:
    """Migrate legacy chat history without assigning background analysis runs."""

    changed = False
    sessions_by_id = {item.id: item for item in record.chat_sessions}
    referenced_ids = {
        session_id
        for session_id in [
            *(message.session_id for message in record.messages),
            *(run.session_id for run in record.runs),
        ]
        if session_id
    }
    for session_id in referenced_ids - sessions_by_id.keys():
        timestamps = [
            message.created_at
            for message in record.messages
            if message.session_id == session_id
        ] + [
            run.started_at
            for run in record.runs
            if run.session_id == session_id
        ]
        created_at = min(timestamps) if timestamps else record.created_at
        updated_at = max(timestamps) if timestamps else created_at
        session = ChatSession(
            id=session_id,
            business_id=record.id,
            created_at=created_at,
            updated_at=updated_at,
        )
        record.chat_sessions.append(session)
        sessions_by_id[session_id] = session
        changed = True

    if not record.chat_sessions:
        timestamps = [message.created_at for message in record.messages] + [
            run.started_at for run in record.runs if run.id in {message.run_id for message in record.messages}
        ]
        created_at = min(timestamps) if timestamps else record.created_at
        updated_at = max(timestamps) if timestamps else created_at
        session = ChatSession(
            id=new_id("chat"),
            business_id=record.id,
            created_at=created_at,
            updated_at=updated_at,
        )
        record.chat_sessions.append(session)
        sessions_by_id[session.id] = session
        changed = True

    default_session = min(record.chat_sessions, key=lambda item: item.created_at)
    runs_by_id = {run.id: run for run in record.runs}
    for message in record.messages:
        if message.session_id:
            continue
        linked_run = runs_by_id.get(message.run_id or "")
        message.session_id = linked_run.session_id if linked_run and linked_run.session_id else default_session.id
        changed = True

    message_sessions_by_run = {
        message.run_id: message.session_id
        for message in record.messages
        if message.run_id and message.session_id
    }
    for run in record.runs:
        if run.session_id or run.id not in message_sessions_by_run:
            continue
        run.session_id = message_sessions_by_run[run.id]
        changed = True

    for session in record.chat_sessions:
        if session.business_id != record.id:
            session.business_id = record.id
            changed = True
        messages = [item for item in record.messages if item.session_id == session.id]
        runs = [item for item in record.runs if item.session_id == session.id]
        timestamps = [item.created_at for item in messages] + [item.started_at for item in runs]
        if timestamps:
            created_at = min(timestamps)
            updated_at = max(
                [*timestamps, *(run.finished_at for run in runs if run.finished_at is not None)]
            )
            if created_at < session.created_at:
                session.created_at = created_at
                changed = True
            if updated_at > session.updated_at:
                session.updated_at = updated_at
                changed = True
        if not session.title:
            first_user_message = next((item.content for item in messages if item.role == "user"), "")
            title = _chat_session_title(first_user_message)
            if title:
                session.title = title
                changed = True
    return changed


def _chat_session_title(content: str) -> str:
    return re.sub(r"\s+", " ", content).strip()[:48]


def _sanitize_legacy_runtime_state(record: BusinessRecord) -> bool:
    changed = False
    fake_markers = ("本轮已更新 Business Context", "当前建议先确认这些问题")
    clean_messages = [
        message
        for message in record.messages
        if not (message.role == "assistant" and all(marker in message.content for marker in fake_markers))
    ]
    if len(clean_messages) != len(record.messages):
        record.messages = clean_messages
        changed = True
    for message in record.messages:
        if message.role == "assistant":
            cleaned = strip_thinking_markup(message.content)
            if cleaned != message.content:
                message.content = cleaned
                changed = True

    real_skills = installed_skill_names()
    clean_skill_refs = [item for item in record.context.skill_references if item.get("name") in real_skills]
    if len(clean_skill_refs) != len(record.context.skill_references):
        record.context.skill_references = clean_skill_refs
        changed = True
    return changed


store = StudioStore()
