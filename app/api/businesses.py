"""AI Business Studio API endpoints."""

from __future__ import annotations

import json
import mimetypes
from collections.abc import Iterator
from pathlib import Path
from time import time
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, StreamingResponse

from app.studio.file_preview import preview_workspace_file
from app.studio.graphs import entity_graph, evidence_graph, flow_graph, lineage_graph
from app.studio.models import (
    BusinessContext,
    BusinessFile,
    BusinessRecord,
    BusinessSummary,
    ChatRequest,
    ChatSession,
    ConfirmationRequest,
    CreateBusinessRequest,
    CreateChatSessionRequest,
    DescriptionMarkdownRequest,
    ResumeChatRequest,
    UpdateBusinessRequest,
    WorkspaceNode,
)
from app.studio.orchestrator import ResumeBlockedError, orchestrator
from app.studio.registry import SYSTEM_SKILLS_ROOT
from app.studio.runtime import clear_runtime_thread
from app.studio.sandbox_runtime import SandboxError, sandbox_manager
from app.studio.storage import new_id, store

router = APIRouter(tags=["business-studio"])

MAX_UPLOAD_SIZE = 500 * 1024 * 1024
UPLOAD_CHUNK_SIZE = 1024 * 1024
PREVIEW_ROW_LIMIT = 20
PROJECT_SANDBOX_SCOPE = "project"


@router.post("/businesses", response_model=BusinessRecord, status_code=201)
def create_business(req: CreateBusinessRequest) -> BusinessRecord:
    name = req.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="业务场景名称不能为空。")
    return store.create(name=name, goal=req.goal, description=req.description)


@router.get("/businesses", response_model=list[BusinessSummary])
def list_businesses() -> list[BusinessSummary]:
    return store.list()


@router.get("/businesses/{business_id}", response_model=BusinessRecord)
def get_business(business_id: str) -> BusinessRecord:
    return _record_or_404(business_id)


@router.get("/businesses/{business_id}/workspace/tree", response_model=WorkspaceNode)
def workspace_tree(business_id: str) -> WorkspaceNode:
    record = _record_or_404(business_id)
    return store.workspace_tree(record)


@router.get("/businesses/{business_id}/workspace/preview")
def preview_workspace_path(business_id: str, path: str) -> dict[str, Any]:
    record = _record_or_404(business_id)
    source, relative = _resolve_workspace_file(business_id, path)
    payload = preview_workspace_file(source)
    registered = next(
        (
            item
            for item in record.files
            if Path(item.storage_path).resolve() == source
        ),
        None,
    )
    encoded_path = quote(relative, safe="")
    payload.update({
        "path": relative,
        "file": registered,
        "raw_url": f"/api/businesses/{business_id}/workspace/raw?path={encoded_path}",
        "download_url": f"/api/businesses/{business_id}/workspace/raw?path={encoded_path}&download=true",
    })
    return payload


@router.get("/businesses/{business_id}/workspace/raw")
def raw_workspace_path(business_id: str, path: str, download: bool = False) -> FileResponse:
    _record_or_404(business_id)
    source, _relative = _resolve_workspace_file(business_id, path)
    return FileResponse(
        source,
        filename=source.name,
        media_type=_guess_mime(source.name),
        content_disposition_type="attachment" if download else "inline",
    )


@router.delete("/businesses/{business_id}/workspace/file")
def delete_workspace_path(business_id: str, path: str) -> dict[str, Any]:
    record = _record_or_404(business_id)
    try:
        deleted = store.delete_workspace_file(record, path)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="无效的工作区文件路径。") from exc
    if deleted is None:
        raise HTTPException(status_code=404, detail="工作区文件不存在。")
    return {
        "ok": True,
        "deleted": deleted,
        "business": record.model_dump(mode="json"),
    }


@router.get("/businesses/{business_id}/description")
@router.get("/businesses/{business_id}/scenario-description", deprecated=True)
def get_description(business_id: str) -> dict[str, Any]:
    record = _record_or_404(business_id)
    return {
        "path": "description.md",
        "filename": "description.md",
        "content": store.read_description_markdown(record),
    }


@router.patch("/businesses/{business_id}/description", response_model=BusinessRecord)
@router.patch("/businesses/{business_id}/scenario-description", response_model=BusinessRecord, deprecated=True)
def update_description(business_id: str, req: DescriptionMarkdownRequest) -> BusinessRecord:
    record = _record_or_404(business_id)
    store.write_description_markdown(record, req.content)
    return store.save(record)


@router.patch("/businesses/{business_id}", response_model=BusinessRecord)
def update_business(business_id: str, req: UpdateBusinessRequest) -> BusinessRecord:
    record = _record_or_404(business_id)
    if req.name is not None:
        record.name = req.name.strip()
    if req.goal is not None:
        record.goal = req.goal.strip()
        record.context.goal = record.goal
    if req.description is not None:
        record.description = req.description.strip()
    if req.status is not None:
        record.status = req.status
    store.create_version(record, "更新业务工作区信息", "update_business", actor="user")
    return store.save(record)


@router.delete("/businesses/{business_id}")
def delete_business(business_id: str) -> dict[str, Any]:
    _record_or_404(business_id)
    sandbox_cleanup = _release_project_sandbox_best_effort(business_id)
    deleted = store.delete(business_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="业务场景不存在。")
    return {
        "ok": True,
        "deleted": business_id,
        "sandbox_cleanup": sandbox_cleanup,
    }


@router.get("/businesses/{business_id}/sandbox/status")
def project_sandbox_status(business_id: str) -> dict[str, Any]:
    _record_or_404(business_id)
    return sandbox_manager.status(business_id, PROJECT_SANDBOX_SCOPE)


@router.post("/businesses/{business_id}/sandbox/prepare")
def prepare_project_sandbox(business_id: str) -> dict[str, Any]:
    _record_or_404(business_id)
    try:
        sandbox_manager.backend_for(
            business_id=business_id,
            workspace_root=store.workspace_dir(business_id),
            skills_root=SYSTEM_SKILLS_ROOT,
        )
    except SandboxError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return sandbox_manager.status(business_id, PROJECT_SANDBOX_SCOPE)


@router.delete("/businesses/{business_id}/sandbox")
def release_project_sandbox(business_id: str) -> dict[str, Any]:
    _record_or_404(business_id)
    try:
        released = sandbox_manager.remove(business_id, PROJECT_SANDBOX_SCOPE)
    except SandboxError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {
        "ok": True,
        "released": released,
        "shared_environment_preserved": True,
        "status": sandbox_manager.status(business_id, PROJECT_SANDBOX_SCOPE),
    }


@router.post("/businesses/{business_id}/files", response_model=BusinessRecord)
async def upload_business_files(
    business_id: str,
    files: list[UploadFile] = File(...),
) -> BusinessRecord:
    record = _record_or_404(business_id)
    uploaded: list[BusinessFile] = []
    for upload in files:
        filename = _safe_filename(upload.filename or "upload.bin")
        suffix = Path(filename).suffix.lower()
        file_id = new_id("file")
        dest = store.next_data_file_path(business_id, filename)
        size = 0
        try:
            with dest.open("wb") as fp:
                while chunk := await upload.read(UPLOAD_CHUNK_SIZE):
                    size += len(chunk)
                    if size > MAX_UPLOAD_SIZE:
                        fp.close()
                        dest.unlink(missing_ok=True)
                        raise HTTPException(status_code=400, detail=f"{filename} 超过 500MB 上传限制。")
                    fp.write(chunk)
        finally:
            await upload.close()

        meta = BusinessFile(
            id=file_id,
            business_id=business_id,
            filename=dest.name,
            suffix=suffix,
            size=size,
            mime_type=upload.content_type or _guess_mime(filename),
            storage_path=str(dest),
            uploaded_at=time(),
        )
        record.files.append(meta)
        uploaded_path = f"data/{dest.name}"
        record.workspace_deleted_paths = [
            item for item in record.workspace_deleted_paths if item != uploaded_path
        ]
        uploaded.append(meta)

    record.status = "files_uploaded"
    store.create_version(
        record,
        f"Uploaded {len(uploaded)} workspace file(s)",
        "upload_files",
        actor="user",
        evidence_ids=[file.id for file in uploaded],
    )
    return store.save(record)


@router.get("/businesses/{business_id}/files", response_model=list[BusinessFile])
def list_business_files(business_id: str) -> list[BusinessFile]:
    return _record_or_404(business_id).files


@router.get("/files/{file_id}/preview")
def preview_file(file_id: str) -> dict[str, Any]:
    found = store.find_file(file_id)
    if found is None:
        raise HTTPException(status_code=404, detail="文件不存在。")
    _record, file = found
    return {
        "file": file,
        "text": file.text,
        "columns": file.columns,
        "sample_rows": file.sample_rows[:PREVIEW_ROW_LIMIT],
        "sheets": _preview_sheets(file.sheets),
        "warnings": file.warnings,
        "preview_row_limit": PREVIEW_ROW_LIMIT,
    }


@router.get("/files/{file_id}/raw")
def raw_file(file_id: str) -> FileResponse:
    found = store.find_file(file_id)
    if found is None:
        raise HTTPException(status_code=404, detail="文件不存在。")
    _record, file = found
    return FileResponse(file.storage_path, filename=file.filename, media_type=file.mime_type)


@router.delete("/files/{file_id}")
def delete_file(file_id: str) -> dict[str, Any]:
    found = store.find_file(file_id)
    if found is None:
        raise HTTPException(status_code=404, detail="文件不存在。")
    record, file = found
    deleted = store.delete_file(record, file_id)
    if deleted is None:
        raise HTTPException(status_code=404, detail="文件不存在。")
    store.create_version(record, f"删除文件 {file.filename}", "delete_file", actor="user", evidence_ids=[file.id])
    store.save(record)
    return {"ok": True, "deleted": file.model_dump(mode="json"), "context": record.context}


@router.post("/businesses/{business_id}/chat")
def chat(business_id: str, req: ChatRequest) -> dict[str, Any]:
    record = _record_or_404(business_id)
    _chat_session_or_404(record, req.session_id)
    return orchestrator.chat(record, req.message, req.model, req.session_id)


@router.post("/businesses/{business_id}/chat/stream")
def chat_stream(business_id: str, req: ChatRequest) -> StreamingResponse:
    record = _record_or_404(business_id)
    _chat_session_or_404(record, req.session_id)
    return StreamingResponse(
        _sse(orchestrator.stream_chat(record, req.message, req.model, req.session_id)),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/businesses/{business_id}/chat/sessions/{session_id}/resume/stream")
def resume_chat_stream(
    business_id: str,
    session_id: str,
    req: ResumeChatRequest | None = None,
) -> StreamingResponse:
    record = _record_or_404(business_id)
    _chat_session_or_404(record, session_id)
    request = req or ResumeChatRequest()
    try:
        preparation = orchestrator.prepare_resume(
            record,
            session_id,
            request.model,
            request.run_id,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Run not found in this chat session.") from exc
    except ResumeBlockedError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return StreamingResponse(
        _sse(orchestrator.stream_resume(record, preparation)),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/businesses/{business_id}/chat/sessions", response_model=list[ChatSession])
def chat_sessions(business_id: str) -> list[ChatSession]:
    record = _record_or_404(business_id)
    return store.list_chat_sessions(record)


@router.post("/businesses/{business_id}/chat/sessions", response_model=BusinessRecord, status_code=201)
def create_chat_session(
    business_id: str,
    req: CreateChatSessionRequest | None = None,
) -> BusinessRecord:
    record = _record_or_404(business_id)
    store.create_chat_session(record, req.title if req else "")
    return record


@router.delete("/businesses/{business_id}/chat/sessions/{session_id}", response_model=BusinessRecord)
def delete_chat_session(business_id: str, session_id: str) -> BusinessRecord:
    record = _record_or_404(business_id)
    clear_runtime_thread(
        business_id,
        session_id,
        tuple(item.id for item in record.runs if item.session_id == session_id),
    )
    deleted = store.delete_chat_session(record, session_id)
    if deleted is None:
        raise HTTPException(status_code=404, detail="Chat session not found.")
    return record


@router.delete(
    "/businesses/{business_id}/chat/sessions/{session_id}/messages",
    response_model=BusinessRecord,
)
def clear_chat_session(business_id: str, session_id: str) -> BusinessRecord:
    record = _record_or_404(business_id)
    clear_runtime_thread(
        business_id,
        session_id,
        tuple(item.id for item in record.runs if item.session_id == session_id),
    )
    session = store.clear_chat_session(record, session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Chat session not found.")
    return record


@router.get("/businesses/{business_id}/messages")
def messages(business_id: str, session_id: str | None = None) -> list[Any]:
    record = _record_or_404(business_id)
    if not session_id:
        return record.messages
    session = _chat_session_or_404(record, session_id)
    return [item for item in record.messages if item.session_id == session.id]


@router.get("/businesses/{business_id}/runs/{run_id}")
def get_run(business_id: str, run_id: str) -> Any:
    record = _record_or_404(business_id)
    run = next((item for item in record.runs if item.id == run_id), None)
    if run is None:
        raise HTTPException(status_code=404, detail="运行记录不存在。")
    return run


@router.post("/businesses/{business_id}/confirmations")
def confirm(business_id: str, req: ConfirmationRequest) -> dict[str, Any]:
    record = _record_or_404(business_id)
    if req.session_id:
        _chat_session_or_404(record, req.session_id)
    try:
        confirmation = orchestrator.confirm(
            record,
            req.question_id,
            req.answer,
            req.accepted,
            req.session_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    run_id = confirmation.get("run_id")
    linked_questions = [item for item in record.context.questions if item.get("run_id") == run_id]
    return {
        "confirmation": confirmation,
        "context": record.context,
        "resume": {
            "session_id": confirmation.get("session_id"),
            "run_id": run_id,
            "ready": (
                all(item.get("status") == "answered" for item in linked_questions)
                if linked_questions
                else bool(confirmation.get("session_id") and confirmation.get("answer"))
            ),
        },
    }


@router.get("/businesses/{business_id}/context", response_model=BusinessContext)
def get_context(business_id: str) -> BusinessContext:
    return _record_or_404(business_id).context


@router.patch("/businesses/{business_id}/context", response_model=BusinessContext)
def patch_context(business_id: str, payload: dict[str, Any]) -> BusinessContext:
    record = _record_or_404(business_id)
    data = record.context.model_dump(mode="json")
    for key, value in payload.items():
        if key in data and key not in {"business_id", "versions"}:
            data[key] = value
    record.context = BusinessContext.model_validate(data)
    store.create_version(record, "手动编辑 Business Context", "patch_context", actor="user")
    store.save(record)
    return record.context


@router.get("/businesses/{business_id}/context/versions")
def context_versions(business_id: str) -> list[Any]:
    return _record_or_404(business_id).context.versions


@router.post("/businesses/{business_id}/context/rollback")
def rollback_context(business_id: str, payload: dict[str, int]) -> BusinessContext:
    record = _record_or_404(business_id)
    version = payload.get("version")
    if not version:
        raise HTTPException(status_code=400, detail="缺少 version。")
    try:
        store.rollback(record, int(version))
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return record.context


@router.get("/businesses/{business_id}/graphs/entity")
def get_entity_graph(business_id: str) -> dict[str, Any]:
    return entity_graph(_record_or_404(business_id).context)


@router.get("/businesses/{business_id}/graphs/flow")
def get_flow_graph(business_id: str) -> dict[str, Any]:
    return flow_graph(_record_or_404(business_id).context)


@router.get("/businesses/{business_id}/graphs/lineage")
def get_lineage_graph(business_id: str) -> dict[str, Any]:
    return lineage_graph(_record_or_404(business_id).context)


@router.get("/businesses/{business_id}/graphs/evidence")
def get_evidence_graph(business_id: str) -> dict[str, Any]:
    return evidence_graph(_record_or_404(business_id).context)


@router.get("/packages/{package_id}/download")
def download_package(package_id: str) -> FileResponse:
    found = store.find_package(package_id)
    if found is None:
        raise HTTPException(status_code=404, detail="能力包不存在。")
    _record, package = found
    return FileResponse(
        package.storage_path,
        filename=package.filename,
        media_type="application/zip",
    )


def _release_project_sandbox_best_effort(business_id: str) -> dict[str, Any]:
    """Release business-local runtime state without deleting the shared venv."""

    try:
        released = sandbox_manager.remove(business_id, PROJECT_SANDBOX_SCOPE)
    except SandboxError as exc:
        return {
            "attempted": True,
            "released": False,
            "shared_environment_preserved": True,
            "error": str(exc),
        }
    return {
        "attempted": True,
        "released": released,
        "shared_environment_preserved": True,
        "error": None,
    }


def _record_or_404(business_id: str) -> BusinessRecord:
    try:
        return store.require(business_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="业务场景不存在。") from exc


def _chat_session_or_404(record: BusinessRecord, session_id: str | None) -> ChatSession:
    try:
        return store.require_chat_session(record, session_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Chat session not found.") from exc


def _safe_filename(filename: str) -> str:
    cleaned = filename.replace("\\", "_").replace("/", "_").strip()
    if not cleaned:
        return "upload.bin"
    return cleaned[:180]


def _resolve_workspace_file(business_id: str, requested_path: str) -> tuple[Path, str]:
    workspace = store.workspace_dir(business_id).resolve()
    normalized = requested_path.replace("\\", "/").strip("/")
    relative = Path(normalized)
    if not normalized or "\x00" in normalized or relative.is_absolute() or ".." in relative.parts:
        raise HTTPException(status_code=400, detail="无效的工作区文件路径。")
    try:
        source = (workspace / relative).resolve()
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="无效的工作区文件路径。") from exc
    if workspace not in source.parents or not source.is_file():
        raise HTTPException(status_code=404, detail="工作区文件不存在。")
    return source, source.relative_to(workspace).as_posix()


def _preview_sheets(sheets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    preview: list[dict[str, Any]] = []
    for sheet in sheets:
        item = dict(sheet)
        rows = item.get("sample_rows") or []
        item["sample_rows"] = rows[:PREVIEW_ROW_LIMIT]
        item["row_sample_count"] = min(len(rows), PREVIEW_ROW_LIMIT)
        preview.append(item)
    return preview


def _guess_mime(filename: str) -> str:
    return mimetypes.guess_type(filename)[0] or "application/octet-stream"


def _sse(events: Iterator[dict[str, Any]]):
    try:
        for event in events:
            event_type = event.get("type", "message")
            payload = json.dumps(event, ensure_ascii=False, default=str)
            yield f"event: {event_type}\ndata: {payload}\n\n"
    finally:
        close = getattr(events, "close", None)
        if callable(close):
            close()
